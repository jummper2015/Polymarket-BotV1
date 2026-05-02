"""Trading loop: arms a buy on UP or DOWN when the trigger price is reached.

Rules:
  1. Only enter watching mode in the last last_minute_seconds of each window.
  2. New positions are only opened during the FIRST 45 seconds of that last
     minute.  After second 45 no new initial trades are fired.
  3. If price is ALREADY >= trigger_price when the last minute begins → skip.
     We only buy when the price CROSSES the trigger from below during last minute.
  4. Execute the buy at the ACTUAL current market price (observed_price), not at
     the fixed trigger threshold.  trigger_price is purely an entry signal.
  5. Hedge: if the initial side's price rises to hedge_threshold, buy the opposite
     side with the same number of shares (provided opposite mid < 1.00).
  6. Emergency hedge: if an open position has NOT been hedged and 10 seconds or
     fewer remain in the window, buy the opposite side to guarantee coverage.
  7. max_trades_per_window controls how many initial (non-hedge) trades per window.

All runtime-mutable params (trigger_price, buy_amount, max_trades_per_window,
hedge_threshold, last_minute_seconds, mode) are read from self.state so dashboard
changes take effect on the next window without restarting.
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
from .state import STATES, Trade


def _sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)


class Trader:
    def __init__(self, cfg: Config, symbol: str = "btc") -> None:
        self.cfg = cfg
        self.symbol = symbol
        self.state = STATES[symbol]
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
        self._thread = threading.Thread(
            target=self._run,
            name=f"trader-{self.symbol}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ----- core loop -----

    def _run(self) -> None:
        sym = self.symbol.upper()
        logger.info(
            f"[{sym}] starting bot — mode={self.state.mode} trigger={self.state.trigger_price} "
            f"buy=${self.state.buy_amount} bankroll=${self.state.starting_bankroll:.2f} "
            f"last_minute={self.state.last_minute_seconds}s hedge@{self.state.hedge_threshold}",
            icon="🤖",
        )
        if self.state.mode == "real" and not self.cfg.has_credentials:
            logger.err(f"[{sym}] real mode requires PRIVATE_KEY and PROXY_WALLET; refusing to start")
            self.state.set_status("error", "Missing PRIVATE_KEY / PROXY_WALLET for real mode")
            return

        while not self._stop.is_set():
            try:
                self._run_one_window()
            except Exception as exc:
                logger.err(f"[{sym}] window error: {exc}")
                self.state.set_status("error", str(exc))
                time.sleep(2.0)

    def _run_one_window(self) -> None:
        sym = self.symbol.upper()
        self.state.set_status("loading_market", "Loading market for current window")
        tokens = load_market_for_current_window(
            self.cfg.gamma_host,
            symbol=self.symbol,
            retry_seconds=self.cfg.market_load_retry_seconds,
            on_slug_change=lambda slug, ts: self.state.set_window(slug, ts),
        )
        self.state.set_window(tokens.slug, tokens.window_ts)
        self.state.set_tokens(tokens.up_token_id, tokens.down_token_id)
        logger.ok(f"[{sym}] market ready  slug={tokens.slug}", icon="📈")

        feed = PriceFeed(
            ws_url=self.cfg.ws_url,
            up_token_id=tokens.up_token_id,
            down_token_id=tokens.down_token_id,
            on_price=self._on_price,
            on_status=self.state.set_ws_connected,
        )
        feed.start()

        try:
            self._wait_for_first_prices(timeout_s=self.cfg.first_price_timeout_seconds)

            trigger_active = self.state.active_strategy in ("trigger", "both")
            mm_active = self.state.active_strategy in ("market_making", "both")

            mm_thread = None
            if mm_active:
                from .strategy_mm import MarketMakerStrategy
                mm_strat = MarketMakerStrategy(self.cfg, self.state)
                mm_thread = threading.Thread(
                    target=mm_strat.run_for_window,
                    args=(tokens,),
                    name=f"mm-{self.symbol}",
                    daemon=True,
                )
                mm_thread.start()

            if trigger_active:
                # Phase 1 — wait until last-minute entry point
                self._wait_for_last_minute(tokens)

                # Phase 2 — check if price already above trigger at entry
                up_at_entry = self.state.last_up_price
                down_at_entry = self.state.last_down_price
                trigger = self.state.trigger_price
                already_above = (
                    (up_at_entry is not None and up_at_entry >= trigger) or
                    (down_at_entry is not None and down_at_entry >= trigger)
                )
                if already_above:
                    logger.warn(
                        f"[{sym}] SKIP — price already above trigger at last-minute entry  "
                        f"UP={up_at_entry}  DOWN={down_at_entry}  trigger={trigger}",
                        icon="🚫",
                    )
                    self.state.set_status("watching", "skipped — price above trigger at entry")
                    self._sleep_until(tokens.window_ts + 300 + 2.0)
                else:
                    # Phase 3 — watch for the trigger crossing
                    self.state.set_status("watching", f"last minute — watching {tokens.slug}")
                    self._monitor_until_trigger_or_window_end(tokens)

                    # Phase 4 — hold + hedge until window ends
                    if self.state.count_initial_trades_for_window(tokens.slug) > 0:
                        self.state.set_status("holding", f"{tokens.slug} — holding to settlement")
                        initial_trade = self.state.find_open_trade_for_window(tokens.slug)
                        if initial_trade is not None:
                            self._hold_and_hedge_until_window_end(tokens, initial_trade)

                    self._sleep_until(tokens.window_ts + 300 + 2.0)
            else:
                # No trigger strategy — wait for the window to close
                self._sleep_until(tokens.window_ts + 300 + 2.0)

            if mm_thread is not None and mm_thread.is_alive():
                mm_thread.join(timeout=10.0)

        finally:
            feed.stop()
            self.state.set_ws_connected(False)

        self._resolve_all_open_trades(tokens)

        next_ts = tokens.window_ts + 300
        logger.info(f"[{sym}] advancing to next window {self.symbol}-updown-5m-{next_ts}", icon="⏭")

    # ----- helpers -----

    def _on_price(self, side: str, mid: float) -> None:
        self.state.update_price(side, mid)

    def _sleep_until(self, target_unix: float) -> None:
        while not self._stop.is_set():
            remaining = target_unix - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    def _wait_for_first_prices(self, timeout_s: float) -> None:
        start = time.time()
        while time.time() - start < timeout_s:
            if self.state.last_up_price is not None and self.state.last_down_price is not None:
                return
            _sleep_ms(50)
        logger.warn("no initial prices received within timeout; continuing anyway")

    def _wait_for_last_minute(self, tokens) -> None:
        window_ends = tokens.window_ts + 300
        entry_time = window_ends - self.state.last_minute_seconds

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
        cfg = self.cfg
        window_ends = tokens.window_ts + 300
        entry_cutoff = window_ends - self.state.last_minute_seconds + 45

        while not self._stop.is_set():
            now = time.time()
            if now >= window_ends:
                return

            if now >= entry_cutoff:
                logger.info(
                    f"{tokens.slug}  entry window closed — past first 45s of last minute  "
                    f"ttl={int(window_ends - now)}s",
                    icon="🔒",
                )
                return

            up = self.state.last_up_price
            down = self.state.last_down_price
            trigger = self.state.trigger_price
            max_trades = self.state.max_trades_per_window
            current_count = self.state.count_initial_trades_for_window(tokens.slug)

            if up is not None and down is not None:
                logger.transient(
                    f"{tokens.slug}  UP={up:.4f}  DOWN={down:.4f}  "
                    f"trigger={trigger:.2f}  trades={current_count}/{max_trades}  "
                    f"entry_closes={int(entry_cutoff - now)}s  ttl={int(window_ends - now)}s"
                )

            if current_count < max_trades:
                if up is not None and up >= trigger:
                    if not self.state.has_initial_trade_for_side(tokens.slug, "UP"):
                        self._fire_initial_trade(tokens, "UP", tokens.up_token_id, up)
                        current_count += 1
                        if current_count >= max_trades:
                            return

                if down is not None and down >= trigger:
                    if not self.state.has_initial_trade_for_side(tokens.slug, "DOWN"):
                        self._fire_initial_trade(tokens, "DOWN", tokens.down_token_id, down)
                        current_count += 1
                        if current_count >= max_trades:
                            return
            else:
                return

            _sleep_ms(cfg.poll_interval_ms)

    def _hold_and_hedge_until_window_end(self, tokens, initial_trade: Trade) -> None:
        end = tokens.window_ts + 300
        _EMERGENCY_SECONDS = 10

        while not self._stop.is_set():
            now = time.time()
            if now >= end:
                return

            up = self.state.last_up_price
            down = self.state.last_down_price
            hedge_threshold = self.state.hedge_threshold
            ttl = end - now

            if up is not None and down is not None:
                logger.transient(
                    f"{tokens.slug}  HOLD  UP={up:.4f}  DOWN={down:.4f}  ttl={int(ttl)}s"
                )

            hedge_placed = self.state.find_hedge_for_window(tokens.slug) is not None

            if not hedge_placed and ttl <= _EMERGENCY_SECONDS:
                logger.warn(
                    f"{tokens.slug}  ⚡ last {int(ttl)}s — no hedge reached — buying opposite side",
                    icon="🚨",
                )
                self._fire_emergency_hedge(tokens, initial_trade)
                return

            if not hedge_placed:
                if initial_trade.side == "UP" and up is not None and up >= hedge_threshold:
                    if down is not None and down < 1.00:
                        self._fire_hedge(tokens, initial_trade, "DOWN", tokens.down_token_id, down)
                    else:
                        logger.warn(f"hedge skipped: DOWN={down} not < 1.00")

                elif initial_trade.side == "DOWN" and down is not None and down >= hedge_threshold:
                    if up is not None and up < 1.00:
                        self._fire_hedge(tokens, initial_trade, "UP", tokens.up_token_id, up)
                    else:
                        logger.warn(f"hedge skipped: UP={up} not < 1.00")

            time.sleep(0.5)

    # ----- trade execution -----

    def _fire_emergency_sell(self, tokens) -> None:
        up = self.state.last_up_price
        down = self.state.last_down_price
        open_trades = self.state.find_all_open_trades_for_window(tokens.slug)

        if not open_trades:
            return

        is_real_now = self.state.mode == "real"

        for trade in open_trades:
            sell_price_raw = up if trade.side == "UP" else down
            if sell_price_raw is None:
                logger.warn(f"emergency sell #{trade.id}: no price for {trade.side} — skipping")
                continue

            sell_price = round(sell_price_raw, 4)
            pnl = round(trade.shares * sell_price - trade.cost, 4)
            label = "HEDGE" if trade.is_hedge else "TRADE"

            logger.ok(
                f"EMERGENCY SELL  {label} #{trade.id}  side={trade.side}  "
                f"buy={trade.price:.4f}  sell={sell_price:.4f}  "
                f"shares={trade.shares:.2f}  pnl=${pnl:+.4f}",
                icon="🚨",
            )

            note = ""
            if is_real_now and self._client is not None:
                try:
                    _, note = self._place_real_sell_order(trade.token_id, trade.shares, sell_price)
                except Exception as exc:
                    logger.err(f"emergency sell order failed #{trade.id}: {exc}")
                    note = f"emergency sell failed: {exc}"
            elif is_real_now and self._client is None:
                note = "real mode: CLOB client not initialized — sell skipped"
                logger.err(note)
            else:
                note = f"paper emergency sell @ {sell_price:.4f}"

            self.state.resolve_trade(trade.id, "sold", sell_price, pnl, note=note)

        self.state.set_status("sold", f"emergency exit @ {round((up or 0), 4)}/{round((down or 0), 4)}")

    def _fire_emergency_hedge(self, tokens, initial_trade: Trade) -> None:
        """Buy the opposite side with the same shares as the initial trade."""
        opp_side     = "DOWN" if initial_trade.side == "UP" else "UP"
        opp_token_id = tokens.down_token_id if initial_trade.side == "UP" else tokens.up_token_id
        opp_price_raw = (
            self.state.last_down_price if initial_trade.side == "UP" else self.state.last_up_price
        )

        if opp_price_raw is None:
            logger.warn(f"emergency hedge: sin precio para {opp_side} — omitiendo")
            return

        if opp_price_raw >= 1.00:
            logger.warn(
                f"emergency hedge: {opp_side} precio {opp_price_raw:.4f} >= 1.00 — omitiendo"
            )
            return

        exec_price = round(opp_price_raw, 4)
        shares     = initial_trade.shares
        cost       = round(shares * exec_price, 4)

        logger.ok(
            f"EMERGENCY HEDGE  {initial_trade.side}@{initial_trade.price:.4f} "
            f"→ {opp_side}  {shares:.2f} shares @ {exec_price:.4f}  cost=${cost:.4f}",
            icon="🚨",
        )

        order_id: Optional[str] = None
        note = ""
        is_real_now = self.state.mode == "real"

        if is_real_now and self._client is not None:
            try:
                order_id, note = self._place_real_order(opp_token_id, shares, exec_price)
            except Exception as exc:
                logger.err(f"emergency hedge order failed: {exc}")
                note = f"emergency hedge failed: {exc}"
        elif is_real_now and self._client is None:
            note = "real mode: CLOB client not initialized — emergency hedge skipped"
            logger.err(note)
        else:
            note = f"paper emergency hedge @ {exec_price:.4f}"

        hedge = Trade(
            id=0,
            window_slug=tokens.slug,
            window_ts=tokens.window_ts,
            side=opp_side,
            token_id=opp_token_id,
            price=exec_price,
            shares=shares,
            cost=cost,
            mode=self.state.mode,
            opened_at=time.time(),
            order_id=order_id,
            note=note,
            is_hedge=True,
        )
        self.state.add_trade(hedge)
        self.state.set_status("hedged", f"emergency hedge {initial_trade.side}+{opp_side}")

    def _fire_initial_trade(self, tokens, side: str, token_id: str, observed_price: float) -> None:
        trigger = self.state.trigger_price
        buy_amount = self.state.buy_amount
        exec_price = round(observed_price, 4)
        shares = math.floor((buy_amount / exec_price) * 100) / 100.0
        cost = round(shares * exec_price, 4)

        logger.ok(
            f"TRIGGER  side={side}  signal={trigger:.2f}  "
            f"exec_price={exec_price:.4f}  shares={shares:.2f}  cost=${cost:.4f}",
            icon="🚀",
        )

        order_id: Optional[str] = None
        note = ""
        is_real_now = self.state.mode == "real"
        if is_real_now and self._client is not None:
            try:
                order_id, note = self._place_real_order(token_id, shares, exec_price)
            except Exception as exc:
                logger.err(f"order failed: {exc}")
                self.state.set_status("error", f"order failed: {exc}")
                return
        elif is_real_now and self._client is None:
            logger.err("real mode active but CLOB client not initialized — restart required")
            self.state.set_status("error", "real mode: restart bot with credentials")
            return
        else:
            note = f"paper trade @ {exec_price:.4f}"

        trade = Trade(
            id=0,
            window_slug=tokens.slug,
            window_ts=tokens.window_ts,
            side=side,
            token_id=token_id,
            price=exec_price,
            shares=shares,
            cost=cost,
            mode=self.state.mode,
            opened_at=time.time(),
            order_id=order_id,
            note=note,
            is_hedge=False,
        )
        trade = self.state.add_trade(trade)
        self.state.set_status("traded", f"{side} @ {exec_price:.4f} (signal {trigger:.2f})")

    def _fire_hedge(self, tokens, initial_trade: Trade, opp_side: str, opp_token_id: str, opp_mid: float) -> None:
        exec_price = round(opp_mid, 4)
        shares = initial_trade.shares
        cost = round(shares * exec_price, 4)

        logger.ok(
            f"HEDGE  {initial_trade.side}@{initial_trade.price:.4f} "
            f"→ {opp_side} {shares:.2f} shares @ {exec_price:.4f}",
            icon="🛡",
        )

        order_id: Optional[str] = None
        note = ""
        is_real_now = self.state.mode == "real"
        if is_real_now and self._client is not None:
            try:
                order_id, note = self._place_real_order(opp_token_id, shares, exec_price)
            except Exception as exc:
                logger.err(f"hedge order failed: {exc}")
                note = f"hedge order failed: {exc}"
        elif is_real_now and self._client is None:
            note = "real mode: client not initialized (restart)"
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
            mode=self.state.mode,
            opened_at=time.time(),
            order_id=order_id,
            note=note,
            is_hedge=True,
        )
        self.state.add_trade(hedge)
        self.state.set_status("hedged", f"{initial_trade.side}+{opp_side} hedged")

    # ----- settlement -----

    def _resolve_all_open_trades(self, tokens) -> None:
        open_trades = self.state.find_all_open_trades_for_window(tokens.slug)
        if not open_trades:
            return

        final_up = self.state.last_up_price
        final_down = self.state.last_down_price

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
            label = "HEDGE" if trade.is_hedge else "TRADE"

            if winner is None:
                self.state.resolve_trade(trade.id, "expired", side_final or 0.0, 0.0, note="no final price")
                logger.warn(f"{label} #{trade.id} expired (no final price)")
                continue

            if winner == trade.side:
                payout = trade.shares * 1.0
                pnl = round(payout - trade.cost, 4)
                self.state.resolve_trade(trade.id, "won", side_final or 1.0, pnl)
                logger.ok(f"{label} #{trade.id} WON  side={trade.side}  pnl=${pnl:+.4f}", icon="🟢")
            else:
                pnl = round(-trade.cost, 4)
                self.state.resolve_trade(trade.id, "lost", side_final or 0.0, pnl)
                logger.warn(f"{label} #{trade.id} LOST side={trade.side}  pnl=${pnl:+.4f}", icon="🔴")

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

    def _place_real_sell_order(self, token_id: str, shares: float, price: float):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        order = self._client.create_order(
            OrderArgs(price=price, size=shares, side=SELL, token_id=token_id)
        )
        resp = self._client.post_order(order, OrderType.GTC)
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        return str(order_id) if order_id else None, "real sell order placed"
