"""Trading loop: arms a buy on UP or DOWN when the trigger price is reached."""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

import requests

from . import logger
from .config import Config
from .market import current_slug, load_market_for_current_window
from .price_feed import PriceFeed
from .state import STATE, Trade


def _sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)


class Trader:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client = None
        if cfg.is_real and cfg.has_credentials:
            self._client = self._build_client()

    # ----- lifecycle -----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ----- core loop -----

    def _run(self) -> None:
        cfg = self.cfg
        logger.info(
            f"starting bot — mode={cfg.mode} trigger={cfg.trigger_price} buy=${cfg.buy_amount}",
            icon="🤖",
        )
        if cfg.is_real and not cfg.has_credentials:
            logger.err("real mode requires PRIVATE_KEY and PROXY_WALLET secrets; refusing to start real trading")
            STATE.set_status("error", "Missing PRIVATE_KEY / PROXY_WALLET for real mode")
            return

        while not self._stop.is_set():
            try:
                self._run_one_window()
            except Exception as exc:  # pragma: no cover - defensive
                logger.err(f"window error: {exc}")
                STATE.set_status("error", str(exc))
                time.sleep(2.0)

    def _run_one_window(self) -> None:
        cfg = self.cfg

        STATE.set_status("loading_market", "Loading market for current window")
        tokens = load_market_for_current_window(
            cfg.gamma_host,
            retry_seconds=cfg.market_load_retry_seconds,
            on_slug_change=lambda slug, ts: STATE.set_window(slug, ts),
        )
        STATE.set_window(tokens.slug, tokens.window_ts)
        STATE.set_tokens(tokens.up_token_id, tokens.down_token_id)
        logger.ok(f"market ready  slug={tokens.slug}", icon="📈")

        feed = PriceFeed(
            ws_url=cfg.ws_url,
            up_token_id=tokens.up_token_id,
            down_token_id=tokens.down_token_id,
            on_price=self._on_price,
            on_status=STATE.set_ws_connected,
        )
        feed.start()

        try:
            self._wait_for_first_prices(timeout_s=cfg.first_price_timeout_seconds)
            STATE.set_status("watching", f"watching {tokens.slug}")
            self._monitor_until_trigger_or_window_end(tokens)
            # If we bought, hold the position until the window actually closes
            # so we can read a real settlement price. We never sell.
            if STATE.find_open_trade_for_window(tokens.slug):
                STATE.set_status("holding", f"{tokens.slug} — holding to settlement")
                self._hold_until_window_end(tokens)
            # Give the market a moment to settle to ~1.0 / ~0.0
            self._sleep_until(tokens.window_ts + 300 + 2.0)
        finally:
            feed.stop()
            STATE.set_ws_connected(False)

        self._resolve_open_trade(tokens)

        # Loop will pick up the next window via current_slug() on its own.
        next_ts = tokens.window_ts + 300
        logger.info(f"advancing to next window btc-updown-5m-{next_ts}", icon="⏭")

    # ----- helpers -----

    def _on_price(self, side: str, mid: float) -> None:
        STATE.update_price(side, mid)

    def _sleep_until(self, target_unix: float) -> None:
        while not self._stop.is_set():
            remaining = target_unix - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    def _hold_until_window_end(self, tokens) -> None:
        end = tokens.window_ts + 300
        while not self._stop.is_set():
            now = time.time()
            if now >= end:
                return
            up = STATE.last_up_price
            down = STATE.last_down_price
            if up is not None and down is not None:
                logger.transient(
                    f"{tokens.slug}  HOLD  UP={up:.4f}  DOWN={down:.4f}  ttl={int(end - now)}s"
                )
            time.sleep(0.5)

    def _wait_for_first_prices(self, timeout_s: float) -> None:
        start = time.time()
        while time.time() - start < timeout_s:
            if STATE.last_up_price is not None and STATE.last_down_price is not None:
                return
            _sleep_ms(50)
        logger.warn("no initial prices received within timeout; continuing anyway")

    def _monitor_until_trigger_or_window_end(self, tokens) -> None:
        cfg = self.cfg
        while not self._stop.is_set():
            now = time.time()
            window_ends = tokens.window_ts + 300
            if now >= window_ends:
                return

            up = STATE.last_up_price
            down = STATE.last_down_price
            if up is not None and down is not None:
                logger.transient(
                    f"{tokens.slug}  UP={up:.4f}  DOWN={down:.4f}  ttl={int(window_ends - now)}s"
                )

            if up is not None and up >= cfg.trigger_price and not STATE.find_open_trade_for_window(tokens.slug):
                self._fire_trade(tokens, "UP", tokens.up_token_id, up)
                return
            if down is not None and down >= cfg.trigger_price and not STATE.find_open_trade_for_window(tokens.slug):
                self._fire_trade(tokens, "DOWN", tokens.down_token_id, down)
                return

            _sleep_ms(cfg.poll_interval_ms)

    def _fire_trade(self, tokens, side: str, token_id: str, observed_price: float) -> None:
        cfg = self.cfg
        shares = math.floor((cfg.buy_amount / cfg.trigger_price) * 100) / 100.0
        cost = shares * cfg.trigger_price
        logger.ok(
            f"TRIGGER  side={side}  observed={observed_price:.4f}  buying {shares:.2f} shares @ {cfg.trigger_price}",
            icon="🚀",
        )

        order_id: Optional[str] = None
        note = ""
        if cfg.is_real and self._client is not None:
            try:
                order_id, note = self._place_real_order(token_id, shares)
            except Exception as exc:
                logger.err(f"order failed: {exc}")
                note = f"order failed: {exc}"
                STATE.set_status("error", note)
                return
        else:
            note = "paper trade (no real order placed)"

        trade = Trade(
            id=0,
            window_slug=tokens.slug,
            window_ts=tokens.window_ts,
            side=side,
            token_id=token_id,
            price=cfg.trigger_price,
            shares=shares,
            cost=cost,
            mode=cfg.mode,
            opened_at=time.time(),
            order_id=order_id,
            note=note,
        )
        trade = STATE.add_trade(trade)
        STATE.set_status("traded", f"{side} @ {cfg.trigger_price}")

    def _resolve_open_trade(self, tokens) -> None:
        """Close out the trade for this window using a final mid (last seen)."""
        trade = STATE.find_open_trade_for_window(tokens.slug)
        if trade is None:
            return

        # Last observed prices for each side.
        final_up = STATE.last_up_price
        final_down = STATE.last_down_price
        side_final = final_up if trade.side == "UP" else final_down
        opp_final = final_down if trade.side == "UP" else final_up

        # Determine winner. Binary markets settle to ~1.0 / ~0.0 at expiry.
        winner: Optional[str] = None
        if side_final is not None and opp_final is not None:
            if side_final >= 0.99 and opp_final <= 0.05:
                winner = trade.side
            elif opp_final >= 0.99 and side_final <= 0.05:
                winner = "DOWN" if trade.side == "UP" else "UP"
            else:
                # Fallback: whichever side is higher.
                winner = trade.side if side_final >= opp_final else ("DOWN" if trade.side == "UP" else "UP")

        if winner is None:
            STATE.resolve_trade(trade.id, "expired", side_final or 0.0, 0.0, note="no final price observed")
            logger.warn(f"trade #{trade.id} expired (no final price)")
            return

        if winner == trade.side:
            payout = trade.shares * 1.0
            pnl = payout - trade.cost
            STATE.resolve_trade(trade.id, "won", side_final or 1.0, pnl)
            logger.ok(f"trade #{trade.id} WON  side={trade.side}  pnl=${pnl:+.2f}", icon="🟢")
        else:
            pnl = -trade.cost
            STATE.resolve_trade(trade.id, "lost", side_final or 0.0, pnl)
            logger.warn(f"trade #{trade.id} LOST side={trade.side}  pnl=${pnl:+.2f}", icon="🔴")

    # ----- real-order plumbing -----

    def _build_client(self):
        try:
            from py_clob_client.client import ClobClient
        except Exception as exc:
            logger.err(f"py_clob_client not available: {exc}")
            return None
        try:
            client = ClobClient(
                self.cfg.clob_host,
                key=self.cfg.private_key,
                chain_id=self.cfg.chain_id,
                signature_type=self.cfg.signature_type,
                funder=self.cfg.proxy_wallet,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            logger.ok("CLOB client authenticated", icon="🔑")
            return client
        except Exception as exc:
            logger.err(f"CLOB authentication failed: {exc}")
            return None

    def _place_real_order(self, token_id: str, shares: float):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        order = self._client.create_order(
            OrderArgs(price=self.cfg.trigger_price, size=shares, side=BUY, token_id=token_id)
        )
        resp = self._client.post_order(order, OrderType.GTC)
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        return str(order_id) if order_id else None, "real order placed"
