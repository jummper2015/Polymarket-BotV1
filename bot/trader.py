"""Trading loop: arms a buy on UP or DOWN when the trigger price is reached.

Rules:
  1. Only enter watching mode in the last LAST_MINUTE_SECONDS of each window.
  2. If the price is ALREADY >= TRIGGER_PRICE when the last minute begins → skip,
     do NOT trade.  We only buy when the price CROSSES the trigger from below
     during the last minute.
  3. Execute the buy at the ACTUAL current market price (observed_price), not
     at the fixed trigger threshold.  TRIGGER_PRICE is purely an entry signal.
  4. Hedge: if the initial side's price rises to HEDGE_THRESHOLD (default 0.96),
     buy the opposite side with the same number of shares, provided:
       - no hedge has been placed yet for this window
       - the opposite side's mid price < 1.00 (market still liquid)
  5. Never sell; hold all positions to settlement.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

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
            logger.err("real mode requires PRIVATE_KEY and PROXY_WALLET; refusing to start")
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

            # ---- Phase 2: check if price already triggered before last minute ----
            #   Rule: only buy if the price CROSSES trigger from below during last
            #   minute.  If it was already above trigger at entry → skip.
            up_at_entry = STATE.last_up_price
            down_at_entry = STATE.last_down_price
            already_above = (
                (up_at_entry is not None and up_at_entry >= cfg.trigger_price) or
                (down_at_entry is not None and down_at_entry >= cfg.trigger_price)
            )
            if already_above:
                logger.warn(
                    f"SKIP — price already above trigger at last-minute entry  "
                    f"UP={up_at_entry}  DOWN={down_at_entry}  trigger={cfg.trigger_price}",
                    icon="🚫",
                )
                STATE.set_status("watching", f"skipped — price above trigger at entry")
                # Just hold until window end (no trade)
                self._sleep_until(tokens.window_ts + 300 + 2.0)
            else:
                # ---- Phase 3: watch for the trigger crossing ----
                STATE.set_status("watching", f"last minute — watching {tokens.slug}")
                self._monitor_until_trigger_or_window_end(tokens)

                # ---- Phase 4: hold + watch for hedge until window ends ----
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
                break
            remaining_to_entry = entry_time - now
            window_ttl = window_ends - now
            logger.transient(
                f"{tokens.slug}  ⏳ waiting for last minute  "
                f"entry_in={int(remaining_to_entry)}s  window_ttl={int(window_ttl)}s"
            )
            time.sleep(min(remaining_to_entry, 1.0))

        logger.info(f"{tokens.slug}  entering last-minute watch window", icon="⏱")

    def _monitor_until_trigger_or_window_end(self, tokens) -> None:
        """Watch prices; buy at the actual market price when it crosses trigger_price.

        TRIGGER_PRICE is only an entry signal — the trade is executed at the
        current observed market price, not at the fixed threshold.
        """
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
                    f"{tokens.slug}  UP={up:.4f}  DOWN={down:.4f}  "
                    f"trigger={cfg.trigger_price:.2f}  ttl={int(window_ends - now)}s"
                )

            # Fire when price crosses trigger from below.
            # Execute at the ACTUAL observed price (not cfg.trigger_price).
            already_traded = STATE.find_open_trade_for_window(tokens.slug) is not None
            if not already_traded:
                if up is not None and up >= cfg.trigger_price:
                    self._fire_initial_trade(tokens, "UP", tokens.up_token_id, up)
                    return
                if down is not None and down >= cfg.trigger_price:
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
                    # Opposite side = DOWN; check its mid < 1.00 (still liquid)
                    if down is not None and down < 1.00:
                        self._fire_hedge(tokens, initial_trade, "DOWN", tokens.down_token_id, down)
                    else:
                        logger.warn(
                            f"hedge skipped: DOWN mid={down} is not < 1.00 (market resolving)"
                        )

                elif initial_trade.side == "DOWN" and down is not None and down >= cfg.hedge_threshold:
                    # Opposite side = UP; check its mid < 1.00 (still liquid)
                    if up is not None and up < 1.00:
                        self._fire_hedge(tokens, initial_trade, "UP", tokens.up_token_id, up)
                    else:
                        logger.warn(
                            f"hedge skipped: UP mid={up} is not < 1.00 (market resolving)"
                        )

            time.sleep(0.5)

    # ----- trade execution -----

    def _fire_initial_trade(self, tokens, side: str, token_id: str, observed_price: float) -> None:
        """Place the initial buy at the ACTUAL observed market price.

        observed_price is the current mid price — this is what we pay, not
        cfg.trigger_price which is merely the signal threshold.
        """
        cfg = self.cfg

        # Round observed price to 4 decimal places (Polymarket tick size)
        exec_price = round(observed_price, 4)
        shares = math.floor((cfg.buy_amount / exec_price) * 100) / 100.0
        cost = round(shares * exec_price, 4)

        logger.ok(
            f"TRIGGER  side={side}  signal={cfg.trigger_price:.2f}  "
            f"exec_price={exec_price:.4f}  shares={shares:.2f}  cost=${cost:.4f}",
            icon="🚀",
        )

        order_id: Optional[str] = None
        note = ""
        if cfg.is_real and self._client is not None:
            try:
                order_id, note = self._place_real_order(token_id, shares, exec_price)
            except Exception as exc:
                logger.err(f"order failed: {exc}")
                STATE.set_status("error", f"order failed: {exc}")
                return
        else:
            note = f"paper trade @ {exec_price:.4f}"

        trade = Trade(
            id=0,
            window_slug=tokens.slug,
            window_ts=tokens.window_ts,
            side=side,
            token_id=token_id,
            price=exec_price,       # actual execution price
            shares=shares,
            cost=cost,
            mode=cfg.mode,
            opened_at=time.time(),
            order_id=order_id,
            note=note,
            is_hedge=False,
        )
        trade = STATE.add_trade(trade)
        STATE.set_status("traded", f"{side} @ {exec_price:.4f} (signal {cfg.trigger_price:.2f})")

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
        exec_price = round(opp_mid, 4)
        shares = initial_trade.shares       # mirror the initial trade size
        cost = round(shares * exec_price, 4)

        logger.ok(
            f"HEDGE  initial={initial_trade.side}@{initial_trade.price:.4f} "
            f"→ buying {opp_side} {shares:.2f} shares @ {exec_price:.4f}",
            icon="🛡",
        )

        order_id: Optional[str] = None
        note = ""
        if cfg.is_real and self._client is not None:
            try:
                order_id, note = self._place_real_order(opp_token_id, shares, exec_price)
            except Exception as exc:
                logger.err(f"hedge order failed: {exc}")
                note = f"hedge order failed: {exc}"
        else:
            note = f"paper hedge of #{initial_trade.id} @ {exec_price:.4f}"

        hedge = Trade(
            id=0,
            window_slug=tokens.slug,
            window_ts=tokens.window_ts,
            side=opp_side,
            token_id=opp_token_id,
            price=exec_price,
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
                # Fallback: whichever side is higher at settlement
                winner = "UP" if final_up >= final_down else "DOWN"

        for trade in open_trades:
            side_final = final_up if trade.side == "UP" else final_down

            if winner is None:
                STATE.resolve_trade(trade.id, "expired", side_final or 0.0, 0.0, note="no final price")
                logger.warn(f"trade #{trade.id} expired (no final price)  is_hedge={trade.is_hedge}")
                continue

            label = "HEDGE" if trade.is_hedge else "TRADE"
            if winner == trade.side:
                # Payout is 1.00 per share for the winning side
                payout = trade.shares * 1.0
                pnl = round(payout - trade.cost, 4)
                STATE.resolve_trade(trade.id, "won", side_final or 1.0, pnl)
                logger.ok(
                    f"{label} #{trade.id} WON  side={trade.side}  "
                    f"shares={trade.shares:.2f}  cost=${trade.cost:.4f}  pnl=${pnl:+.4f}",
                    icon="🟢",
                )
            else:
                pnl = round(-trade.cost, 4)
                STATE.resolve_trade(trade.id, "lost", side_final or 0.0, pnl)
                logger.warn(
                    f"{label} #{trade.id} LOST side={trade.side}  "
                    f"shares={trade.shares:.2f}  cost=${trade.cost:.4f}  pnl=${pnl:+.4f}",
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
