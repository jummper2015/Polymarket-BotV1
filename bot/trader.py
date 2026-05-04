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
  6. Emergency hedge (modified): if an open position has NOT been hedged and 10
     seconds or fewer remain, WAIT until 5 seconds remain and buy 50% of the
     initial shares on the opposite side to mitigate losses.
  7. max_trades_per_window controls how many initial (non-hedge) trades per window.
  8. Early-entry strategy: at window_ts + 40s buy 25% of mm_shares on the dominant
     side, then apply Kelly criterion for a 1–3% coverage hedge immediately.

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


def _kelly_coverage_fraction(p_dominant: float, p_other: float) -> float:
    """Compute Kelly hedge fraction clamped to [1%, 3%].

    Uses price spread as an edge signal:
    - Tight spread (uncertain) → hedge more (up to 3%)
    - Wide spread (confident) → hedge less (down to 1%)
    This ensures we always have some coverage while respecting the 1-3% cap.
    """
    if p_other <= 0 or p_dominant <= 0 or p_dominant >= 1.0:
        return 0.01
    spread = abs(p_dominant - p_other)
    # More certainty (high spread) → smaller fraction needed
    fraction = 0.03 - spread * 0.02
    return max(0.01, min(0.03, fraction))


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
        logger.set_context(self.state)  # route all logs in this thread to the correct market
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

        # Check if this market is enabled; if not, idle-wait and check again
        if not self.state.market_enabled:
            self.state.set_status("idle", "Mercado desactivado")
            logger.info(f"[{sym}] mercado desactivado — esperando 10s", icon="⏸")
            time.sleep(10.0)
            return

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

            trigger_active     = self.state.active_strategy in ("trigger", "both")
            mm_active          = self.state.active_strategy in ("market_making", "both")
            early_entry_active = self.state.early_entry_enabled

            # --- Early-entry strategy thread (fires at window_ts + 40s) ---
            early_thread = None
            if early_entry_active:
                early_thread = threading.Thread(
                    target=self._run_early_entry_strategy,
                    args=(tokens,),
                    name=f"early-{self.symbol}",
                    daemon=True,
                )
                early_thread.start()

            # --- Market-making thread ---
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

                # Phase 2 — check if price already above trigger at entry.
                # Only skip when we have received prices for BOTH sides and at
                # least one of them is already above the trigger.  If prices for
                # one side are still missing (illiquid / feed lag), proceed to
                # the watch loop so we don't silently drop the whole window.
                up_at_entry   = self.state.last_up_price
                down_at_entry = self.state.last_down_price
                trigger       = self.state.trigger_price

                have_both    = up_at_entry is not None and down_at_entry is not None
                already_above = have_both and (
                    up_at_entry >= trigger or down_at_entry >= trigger
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
            if early_thread is not None and early_thread.is_alive():
                early_thread.join(timeout=10.0)

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
        _CHECK_SECONDS = 10   # at this TTL, start monitoring for the half-hedge moment
        _HEDGE_SECONDS = 5    # at this TTL, fire the 50%-shares opposite-side hedge

        half_hedge_armed = False  # True once we've passed the 10s check
        half_hedge_placed = False

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

            # --- Normal hedge: price crosses hedge_threshold ---
            if not hedge_placed and not half_hedge_placed and ttl > _CHECK_SECONDS:
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

            # --- Modified emergency half-hedge at last 10s → fires at 5s ---
            if not hedge_placed and not half_hedge_placed:
                if ttl <= _CHECK_SECONDS and not half_hedge_armed:
                    half_hedge_armed = True
                    logger.warn(
                        f"{tokens.slug}  ⚡ {int(ttl)}s restantes — sin hedge — esperando 5s para cubrir 50%",
                        icon="⏳",
                    )

                if half_hedge_armed and ttl <= _HEDGE_SECONDS:
                    logger.warn(
                        f"{tokens.slug}  🚨 {int(ttl)}s — ejecutando semi-cobertura 50% shares lado contrario",
                        icon="🚨",
                    )
                    self._fire_half_hedge(tokens, initial_trade)
                    half_hedge_placed = True
                    return

            time.sleep(0.25)

    # ----- early-entry strategy -----

    def _run_early_entry_strategy(self, tokens) -> None:
        """Early Entry strategy:
        1. At window_ts + ee_entry_seconds open ONE position (ee_shares) on the dominant side.
        2. Monitor in a single loop:
           a. If price rises ≥ ee_tp_pct% from entry → sell (take-profit), exit — NO hedge.
           b. If UP+DOWN sum ≤ 0.97 AND entry still open AND no hedge yet →
              open hedge with same shares on the opposite side.
        """
        logger.set_context(self.state)  # route all logs in this thread to the correct market
        window_ends = tokens.window_ts + 300
        entry_time  = float(tokens.window_ts) + float(self.state.ee_entry_seconds)

        # ── Phase 1: wait for the 40-second mark ──────────────────────────
        while not self._stop.is_set():
            now = time.time()
            if now >= window_ends:
                return
            if now >= entry_time:
                break
            logger.transient(
                f"EE  {tokens.slug}  ⏳ early-entry en {int(entry_time - now)}s"
            )
            time.sleep(min(entry_time - now, 1.0))

        # Only one EE entry per window
        if self.state.count_early_entry_trades_for_window(tokens.slug) > 0:
            return

        up   = self.state.last_up_price
        down = self.state.last_down_price
        if up is None or down is None:
            logger.warn(f"EE  {tokens.slug}  sin precios — omitiendo early-entry")
            return

        # Dominant side = the side with the higher probability (higher price)
        if up >= down:
            dom_side, dom_token, dom_price = "UP",   tokens.up_token_id,   up
            opp_side, opp_token            = "DOWN", tokens.down_token_id
        else:
            dom_side, dom_token, dom_price = "DOWN", tokens.down_token_id, down
            opp_side, opp_token            = "UP",   tokens.up_token_id

        # Use the dedicated EE shares setting (independent from MM shares)
        initial_shares = self.state.ee_shares
        if initial_shares <= 0:
            logger.warn(f"EE  {tokens.slug}  shares demasiado pequeños — omitiendo")
            return

        exec_price = round(dom_price, 4)
        cost       = round(initial_shares * exec_price, 4)

        logger.ok(
            f"EARLY-ENTRY  side={dom_side}  shares={initial_shares:.2f}  "
            f"price={exec_price:.4f}  cost=${cost:.4f}",
            icon="🎯",
        )

        # ── Phase 2: open the entry position ─────────────────────────────
        is_real_now = self.state.mode == "real"
        order_id: Optional[str] = None
        note = ""

        if is_real_now and self._client is not None:
            try:
                order_id, note = self._place_real_order(dom_token, initial_shares, exec_price)
            except Exception as exc:
                logger.err(f"EE order failed: {exc}")
                return
        elif is_real_now and self._client is None:
            logger.err("EE real mode: CLOB client not initialized")
            return
        else:
            note = f"paper early-entry @ {exec_price:.4f}"

        entry_trade = Trade(
            id=0,
            window_slug=tokens.slug,
            window_ts=tokens.window_ts,
            side=dom_side,
            token_id=dom_token,
            price=exec_price,
            shares=initial_shares,
            cost=cost,
            mode=self.state.mode,
            opened_at=time.time(),
            order_id=order_id,
            note=note,
            is_hedge=False,
            strategy="early_entry",
        )
        entry_trade = self.state.add_trade(entry_trade)
        self.state.set_status("ee_traded", f"EE {dom_side} @ {exec_price:.4f}")

        tp_threshold = self.state.ee_tp_pct / 100.0
        tp_price = round(exec_price * (1.0 + tp_threshold), 4)
        logger.info(
            f"EE MONITOR  {dom_side}  entry={exec_price:.4f}  "
            f"tp={tp_price:.4f} (+{self.state.ee_tp_pct:.1f}%)  hedge cuando UP+DOWN≤0.97",
            icon="👁",
        )

        # ── Phase 3: combined take-profit + hedge monitor ─────────────────
        hedge_placed = False

        while not self._stop.is_set():
            now = time.time()
            if now >= window_ends:
                return

            current = self.state.find_trade_by_id(entry_trade.id)
            if current is None or current.status != "open":
                return  # position resolved externally

            cur_up   = self.state.last_up_price
            cur_down = self.state.last_down_price

            cur_dom = cur_up if dom_side == "UP" else cur_down
            cur_opp = cur_down if dom_side == "UP" else cur_up

            if cur_dom is not None:
                gain = (cur_dom - exec_price) / exec_price

                logger.transient(
                    f"EE  {dom_side}  cur={cur_dom:.4f}  gain={gain:+.2%}  "
                    f"tp={tp_price:.4f}  sum={((cur_up or 0)+(cur_down or 0)):.4f}  "
                    f"hedge={'✓' if hedge_placed else '—'}"
                )

                # (a) Take-profit: sell if ≥ ee_tp_pct% gain — hedge will NOT be placed
                if gain >= tp_threshold:
                    sell_price = round(cur_dom, 4)
                    pnl        = round(initial_shares * sell_price - cost, 4)
                    logger.ok(
                        f"EE TAKE-PROFIT  {dom_side}  buy={exec_price:.4f}  "
                        f"sell={sell_price:.4f}  gain={gain:+.2%}  pnl=${pnl:+.4f}",
                        icon="💰",
                    )
                    tp_note = ""
                    if is_real_now and self._client is not None:
                        try:
                            _, tp_note = self._place_real_sell_order(
                                dom_token, initial_shares, sell_price
                            )
                        except Exception as exc:
                            logger.err(f"EE TP sell failed: {exc}")
                            tp_note = f"EE TP sell failed: {exc}"
                    elif is_real_now:
                        tp_note = "real mode: client not initialized — TP sell skipped"
                        logger.err(tp_note)
                    else:
                        tp_note = f"paper EE take-profit @ {sell_price:.4f}"
                    self.state.resolve_trade(
                        entry_trade.id, "sold", sell_price, pnl, note=tp_note
                    )
                    self.state.set_status(
                        "ee_tp", f"EE TP {dom_side} +{gain:.2%} @ {sell_price:.4f}"
                    )
                    return  # done — no hedge after take-profit

            # (b) Hedge: open opposite side when bid+ask sum ≤ 0.97
            #     Only if entry is still open, no hedge placed yet, and opp price valid
            if (
                not hedge_placed
                and cur_up is not None
                and cur_down is not None
                and cur_opp is not None
                and 0 < cur_opp < 1.0
                and cur_up + cur_down <= 0.97
            ):
                hedge_price = round(cur_opp, 4)
                hedge_cost  = round(initial_shares * hedge_price, 4)
                logger.ok(
                    f"EE HEDGE  sum={cur_up+cur_down:.4f}≤0.97  "
                    f"side={opp_side}  shares={initial_shares:.2f}  "
                    f"price={hedge_price:.4f}  cost=${hedge_cost:.4f}",
                    icon="🛡",
                )
                h_order_id: Optional[str] = None
                h_note = ""
                if is_real_now and self._client is not None:
                    try:
                        h_order_id, h_note = self._place_real_order(
                            opp_token, initial_shares, hedge_price
                        )
                    except Exception as exc:
                        logger.err(f"EE hedge order failed: {exc}")
                        h_note = f"EE hedge failed: {exc}"
                elif is_real_now:
                    h_note = "real mode: client not initialized"
                else:
                    h_note = f"paper EE hedge @ {hedge_price:.4f}"

                ee_hedge = Trade(
                    id=0,
                    window_slug=tokens.slug,
                    window_ts=tokens.window_ts,
                    side=opp_side,
                    token_id=opp_token,
                    price=hedge_price,
                    shares=initial_shares,
                    cost=hedge_cost,
                    mode=self.state.mode,
                    opened_at=time.time(),
                    order_id=h_order_id,
                    note=h_note,
                    is_hedge=True,
                    strategy="early_entry",
                )
                self.state.add_trade(ee_hedge)
                self.state.set_status(
                    "ee_hedged",
                    f"EE {dom_side}+{opp_side} sum={cur_up+cur_down:.4f}",
                )
                hedge_placed = True

            time.sleep(0.25)

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

    def _fire_half_hedge(self, tokens, initial_trade: Trade) -> None:
        """Buy 50% of initial shares on the opposite side to mitigate losses."""
        opp_side     = "DOWN" if initial_trade.side == "UP" else "UP"
        opp_token_id = tokens.down_token_id if initial_trade.side == "UP" else tokens.up_token_id
        opp_price_raw = (
            self.state.last_down_price if initial_trade.side == "UP" else self.state.last_up_price
        )

        if opp_price_raw is None:
            logger.warn(f"half hedge: sin precio para {opp_side} — omitiendo")
            return

        if opp_price_raw >= 1.00:
            logger.warn(
                f"half hedge: {opp_side} precio {opp_price_raw:.4f} >= 1.00 — omitiendo"
            )
            return

        exec_price = round(opp_price_raw, 4)
        shares = math.floor(initial_trade.shares * 0.50 * 100) / 100.0
        if shares <= 0:
            shares = initial_trade.shares
        cost = round(shares * exec_price, 4)

        logger.ok(
            f"SEMI-HEDGE 50%  {initial_trade.side}@{initial_trade.price:.4f} "
            f"→ {opp_side}  {shares:.2f} shares @ {exec_price:.4f}  cost=${cost:.4f}",
            icon="🛡",
        )

        order_id: Optional[str] = None
        note = ""
        is_real_now = self.state.mode == "real"

        if is_real_now and self._client is not None:
            try:
                order_id, note = self._place_real_order(opp_token_id, shares, exec_price)
            except Exception as exc:
                logger.err(f"half hedge order failed: {exc}")
                note = f"half hedge failed: {exc}"
        elif is_real_now and self._client is None:
            note = "real mode: CLOB client not initialized — half hedge skipped"
            logger.err(note)
        else:
            note = f"paper semi-hedge 50% @ {exec_price:.4f}"

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
            strategy="trigger",
        )
        self.state.add_trade(hedge)
        self.state.set_status("hedged", f"semi-hedge 50% {initial_trade.side}+{opp_side}")

    def _fire_emergency_hedge(self, tokens, initial_trade: Trade) -> None:
        """Buy the opposite side with the same shares as the initial trade (legacy, kept for reference)."""
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
            strategy="trigger",
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
            strategy="trigger",
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
            strategy="trigger",
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
