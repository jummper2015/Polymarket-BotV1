"""Trading loop: arms a buy on UP or DOWN when the trigger price is reached.

Rules:
  1. Only enter watching mode in the last LAST_MINUTE_SECONDS of each window.
  2. Buy the first side that crosses TRIGGER_PRICE.  One initial trade per window.
  3. Hedge: if the initial side's price rises to HEDGE_THRESHOLD (default 0.96),
     buy the opposite side with the same number of shares, provided:
       - no hedge has been placed yet for this window
       - the opposite side's mid price < 1.00 (market still liquid)
  4. Never sell; hold all positions to settlement.
"""
from __future__ import annotations

import math
import threading
import time
from typing import List, Optional

from . import logger
from .config import Config
from .market import load_market_for_current_window
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
            f"starting bot — mode={cfg.mode} trigger={cfg.trigger_price} "
            f"buy=${cfg.buy_amount} bankroll=${cfg.starting_bankroll:.2f} "
            f"last_minute={cfg.last_minute_seconds}s hedge@{cfg.hedge_threshold}",
            icon="🤖",
        )
        if cfg.is_real and not cfg.has_credentials:
            logger.err("real mode requires PRIVATE_KEY and PROXY_WALLET secrets; refusing to start real trading")
            STATE.set_status("error", "Missing PRIVATE_KEY / PROXY_WALLET for real mode")
            return

        while not self._stop.is_set():
            try:
                self._run_one_window()
            except Exception as exc:
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

            # ---- Phase 1: wait until the last minute of the window ----
            self._wait_for_last_minute(tokens)

            # ---- Phase 2: watch for the trigger ----
            STATE.set_status("watching", f"last minute — watching {tokens.slug}")
            self._monitor_until_trigger_or_window_end(tokens)

            # ---- Phase 3: hold + watch for hedge until window ends ----
            initial_trade = STATE.find_open_trade_for_window(tokens.slug)
            if initial_trade is not None:
                STATE.set_status("holding", f"{tokens.slug} — holding to settlement")
                self._hold_and_hedge_until_window_end(tokens, initial_trade)

            # Give market a moment to settle to ~1.0 / ~0.0
            self._sleep_until(tokens.window_ts + 300 + 2.0)
        finally:
            feed.stop()
            STATE.set_ws_connected(False)

        self._resolve_all_open_trades(tokens)

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

    def _wait_for_first_prices(self, timeout_s: float) -> None:
        start = time.time()
        while time.time() - start < timeout_s:
            if STATE.last_up_price is not None and STATE.last_down_price is not None:
                return
            _sleep_ms(50)
        logger.warn("no initial prices received within timeout; continuing anyway")

    def _wait_for_last_minute(self, tokens) -> None:
        """Block until we are inside the final LAST_MINUTE_SECONDS of the window."""
        cfg = self.cfg
        window_ends = tokens.window_ts + 300
        entry_time = window_ends - cfg.last_minute_seconds

        while not self._stop.is_set():
            now = time.time()
            if now >= entry_time:
                return
            remaining_to_entry = entry_time - now
            window_ttl = window_ends - now
            logger.transient(
                f"{tokens.slug}  ⏳ waiting for last minute  "
                f"entry_in={int(remaining_to_entry)}s  window_ttl={int(window_ttl)}s"
            )
            time.sleep(min(remaining_to_entry, 1.0))

        logger.info(f"{tokens.slug}  entering last-minute watch window", icon="⏱")

    def _monitor_until_trigger_or_window_end(self, tokens) -> None:
        """Watch prices until trigger fires or window closes."""
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
                self._fire_initial_trade(tokens, "UP", tokens.up_token_id, up)
                return
            if down is not None and down >= cfg.trigger_price and not STATE.find_open_trade_for_window(tokens.slug):
                self._fire_initial_trade(tokens, "DOWN", tokens.down_token_id, down)
                return

            _sleep_ms(cfg.poll_interval_ms)

    def _hold_and_hedge_until_window_end(self, tokens, initial_trade: Trade) -> None:
        """Hold position until window ends; place one hedge if price hits threshold."""
        cfg = self.cfg
        end = tokens.window_ts + 300

        while not self._stop.is_set():
            now = time.time()
            if now >= end:
                return

            up = STATE.last_up_price
            down = STATE.last_down_price

            ttl = int(end - now)
            if up is not None and down is not None:
                logger.transient(
                    f"{tokens.slug}  HOLD  UP={up:.4f}  DOWN={down:.4f}  ttl={ttl}s"
                )

            # Hedge check — only if no hedge placed yet for this window
            if STATE.find_hedge_for_window(tokens.slug) is None:
                if initial_trade.side == "UP" and up is not None and up >= cfg.hedge_threshold:
                    # Opposite side = DOWN; check its mid < 1.00
                    if down is not None and down < 1.00:
                        self._fire_hedge(tokens, initial_trade, "DOWN", tokens.down_token_id, down)
                    else:
                        logger.warn(
                            f"hedge skipped: DOWN mid={down} is not < 1.00 (market may be resolving)"
                        )

                elif initial_trade.side == "DOWN" and down is not None and down >= cfg.hedge_threshold:
                    # Opposite side = UP; check its mid < 1.00
                    if up is not None and up < 1.00:
                        self._fire_hedge(tokens, initial_trade, "UP", tokens.up_token_id, up)
                    else:
                        logger.warn(
                            f"hedge skipped: UP mid={up} is not < 1.00 (market may be resolving)"
                        )

            time.sleep(0.5)

    # ----- trade execution -----

    def _fire_initial_trade(self, tokens, side: str, token_id: str, observed_price: float) -> None:
        cfg = self.cfg
        shares = math.floor((cfg.buy_amount / cfg.trigger_price) * 100) / 100.0
        cost = shares * cfg.trigger_price
        logger.ok(
            f"TRIGGER  side={side}  observed={observed_price:.4f}  "
            f"buying {shares:.2f} shares @ {cfg.trigger_price}",
            icon="🚀",
        )

        order_id: Optional[str] = None
        note = ""
        if cfg.is_real and self._client is not None:
            try:
                order_id, note = self._place_real_order(token_id, shares, cfg.trigger_price)
            except Exception as exc:
                logger.err(f"order failed: {exc}")
                STATE.set_status("error", f"order failed: {exc}")
                return
        else:
            note = "paper trade"

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
            is_hedge=False,
        )
        trade = STATE.add_trade(trade)
        STATE.set_status("traded", f"{side} @ {cfg.trigger_price}")

    def _fire_hedge(
        self,
        tokens,
        initial_trade: Trade,
        opp_side: str,
        opp_token_id: str,
        opp_mid: float,
    ) -> None:
        """Buy the opposite side with the same share count as the initial trade."""
        cfg = self.cfg
        shares = initial_trade.shares   # mirror the initial trade size
        cost = shares * opp_mid
        logger.ok(
            f"HEDGE  initial={initial_trade.side}@{initial_trade.price:.4f} "
            f"→ buying {opp_side} {shares:.2f} shares @ ~{opp_mid:.4f}",
            icon="🛡",
        )

        order_id: Optional[str] = None
        note = ""
        if cfg.is_real and self._client is not None:
            try:
                order_id, note = self._place_real_order(opp_token_id, shares, opp_mid)
            except Exception as exc:
                logger.err(f"hedge order failed: {exc}")
                note = f"hedge order failed: {exc}"
        else:
            note = f"paper hedge (opposite of #{initial_trade.id})"

        hedge = Trade(
            id=0,
            window_slug=tokens.slug,
            window_ts=tokens.window_ts,
            side=opp_side,
            token_id=opp_token_id,
            price=opp_mid,
            shares=shares,
            cost=cost,
            mode=cfg.mode,
            opened_at=time.time(),
            order_id=order_id,
            note=note,
            is_hedge=True,
        )
        STATE.add_trade(hedge)
        STATE.set_status("hedged", f"{initial_trade.side}+{opp_side} hedged")

    # ----- settlement -----

    def _resolve_all_open_trades(self, tokens) -> None:
        """Resolve every open trade for this window using final observed prices."""
        open_trades = STATE.find_all_open_trades_for_window(tokens.slug)
        if not open_trades:
            return

        final_up = STATE.last_up_price
        final_down = STATE.last_down_price

        # Determine which side won (binary outcome for the whole window)
        winner: Optional[str] = None
        if final_up is not None and final_down is not None:
            if final_up >= 0.99 and final_down <= 0.05:
                winner = "UP"
            elif final_down >= 0.99 and final_up <= 0.05:
                winner = "DOWN"
            else:
                winner = "UP" if final_up >= final_down else "DOWN"

        for trade in open_trades:
            side_final = final_up if trade.side == "UP" else final_down

            if winner is None:
                STATE.resolve_trade(trade.id, "expired", side_final or 0.0, 0.0, note="no final price")
                logger.warn(f"trade #{trade.id} expired (no final price)  hedge={trade.is_hedge}")
                continue

            label = "HEDGE" if trade.is_hedge else "TRADE"
            if winner == trade.side:
                payout = trade.shares * 1.0
                pnl = payout - trade.cost
                STATE.resolve_trade(trade.id, "won", side_final or 1.0, pnl)
                logger.ok(
                    f"{label} #{trade.id} WON  side={trade.side}  pnl=${pnl:+.2f}",
                    icon="🟢",
                )
            else:
                pnl = -trade.cost
                STATE.resolve_trade(trade.id, "lost", side_final or 0.0, pnl)
                logger.warn(
                    f"{label} #{trade.id} LOST side={trade.side}  pnl=${pnl:+.2f}",
                    icon="🔴",
                )

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

    def _place_real_order(self, token_id: str, shares: float, price: float):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        order = self._client.create_order(
            OrderArgs(price=price, size=shares, side=BUY, token_id=token_id)
        )
        resp = self._client.post_order(order, OrderType.GTC)
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        return str(order_id) if order_id else None, "real order placed"
