"""Streak Snapper Trader — main 5-min window cycle for both strategy forms.

Runs as a daemon thread. Each 5-min window:
  1. Fetch Binance data (5m windows + 4h trend)
  2. Resolve open trades from previous windows (Gamma outcomePrices)
  3. Check signals for enabled strategies (fade / trend / both)
  4. Execute limit buy orders via CLOB V2
  5. Hold positions to resolution

Uses the existing bot infrastructure:
  - market.py for token discovery (Gamma API)
  - price_feed.py for real-time bid/ask (WebSocket)
  - db.py for trade persistence
  - logger.py for logging
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal, ROUND_FLOOR
from typing import Optional

import requests

from . import logger
from .binance_api import get_5min_candles, get_window_direction
from .config import Config
from . import regime
from . import strategies
from .db import TradeModel, db as _db, db_context
from .db import MartingaleStateModel
from .market import load_market_for_current_window, fetch_market, MarketTokens
from .price_feed import PriceFeed
from .state import Trade, state_for
from .strategy_streak import StreakSnapperStrategy, StreakSignal


WINDOW_SECONDS = 300      # 5 minutes
PRICE_TICK    = 0.01

# Resolution timing: how long after a window closes we start asking for the
# outcome, how often we re-ask, and how long we're willing to block the cycle
# waiting for it (the martingale for the next window depends on it).
RESOLVE_GRACE_SECONDS = 5
RESOLVE_POLL_SECONDS  = 5.0
RESOLVE_MAX_WAIT      = 45.0

# Gamma publishes outcomePrices roughly three minutes after a window closes —
# longer than the window itself, which is why Binance settles trades first and
# Gamma only confirms them afterwards.
GAMMA_SETTLE_SECONDS = 200
CONFIRM_BATCH_SIZE   = 50

# Fill verification (real mode only). The cap gate guarantees the limit price
# equals the ask, so the order is marketable and either crosses at once or is
# not a position at all. This is only how long we let it prove that before
# cancelling the remainder — kept tight because Fase 8 measured that entering
# late costs about 3 points of ROI, and a resting bid is the very thing this
# check exists to avoid leaving behind.
FILL_WAIT_SECONDS = 5.0
FILL_POLL_SECONDS = 0.5

# How often observer strategies are ticked while a window runs. Only paid when
# such a strategy is enabled — otherwise the window is one blocking wait, as it
# has always been.
OBSERVE_TICK_SECONDS = 4.0

# How many past trades to keep in the in-memory dashboard cache.
MEM_TRADE_CACHE_SIZE = 500


def is_entry_too_late(window_ts: float, now: float, max_age: float) -> bool:
    """Whether a window is too far along to open a position in it.

    Split out of the loop so it can be tested without driving a whole window.
    A negative age (clock skew, or a window that hasn't opened yet) is never
    late — the check exists to refuse stale entries, not early ones.
    """
    return (now - window_ts) > max_age


def is_ask_above_cap(ask: Optional[float], cap: float) -> bool:
    """Whether the book prices this window out of the strategy's band.

    Split out of `_execute_signal` so the decision is testable without a live
    book. Missing or non-positive asks return False on purpose: the WebSocket
    hiccuping is not a reason to refuse the window, the same fail-open stance
    the regime gate takes when Binance has no candles to offer.

    The comparison is exact rather than tick-tolerant — prices live on a
    one-cent grid, so an ask *equal* to the cap is tradeable and only a
    genuinely higher one is not.
    """
    if not ask or ask <= 0:
        return False
    return ask > cap


# The CLOB reports an order as filled, resting or dead. Only the first means we
# hold shares; the other two mean we hold nothing *yet* and the difference
# between them is whether waiting could still change that.
_FILLED_STATUSES = {"matched", "filled", "complete"}
_RESTING_STATUSES = {"live", "delayed", "open", "pending"}
_DEAD_STATUSES = {"canceled", "cancelled", "unmatched", "rejected", "expired"}


def matched_shares(payload: object, requested: float) -> Optional[float]:
    """How many shares an order actually filled, per the CLOB's own answer.

    Reads both shapes the client returns as plain dicts: the POST /order
    response and the GET /order lookup. `size_matched` is the authoritative
    field and is in shares, so it wins whenever it is present; the textual
    status is the fallback for responses that omit it.

    Returns **None** when the payload says nothing intelligible about matching.
    That is deliberately distinct from 0.0: "the order did not fill" and "we
    have no idea whether it filled" call for different handling, and collapsing
    them is how a real position becomes untracked.
    """
    if not isinstance(payload, dict):
        return None

    for key in ("size_matched", "sizeMatched", "matched_size", "size_filled"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            # Clamped: an over-fill is not a thing the exchange should report,
            # and if it ever does we would rather under-record than invent
            # shares that the resolution step would then settle.
            return max(0.0, min(float(raw), requested))
        except (TypeError, ValueError):
            break  # present but unreadable — fall through to the status

    status = str(payload.get("status") or "").strip().lower()
    if status in _FILLED_STATUSES:
        return requested
    if status in _RESTING_STATUSES or status in _DEAD_STATUSES:
        return 0.0
    return None


def _floor_to_tick(price: float) -> float:
    """Floor a price to the CLOB tick size, without binary-float drift.

    `math.floor(0.29 / 0.01)` is 28, not 29, because 0.29/0.01 evaluates to
    28.999999999999996 — that silently shaved a full tick off the limit price
    for 0.29, 0.47, 0.57, 0.58, 0.59 and 0.94.  Decimal keeps it exact.
    """
    tick = Decimal(str(PRICE_TICK))
    floored = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return max(PRICE_TICK, float(floored))


class StreakSnapperTrader(threading.Thread):
    """Thread that drives the Streak Snapper 5-min window cycle."""

    def __init__(self, cfg: Config, symbol: str = "btc") -> None:
        super().__init__(name=f"ss-trader-{symbol}", daemon=True)
        self.cfg    = cfg
        self.symbol = symbol
        self.state  = state_for(symbol)
        self._stop = threading.Event()

        # CLOB V2 client (real mode only)
        self._client = None
        if cfg.is_real and cfg.has_credentials:
            self._client = self._build_client()

        # Strategy engine
        self.strategy = StreakSnapperStrategy(self.state)

        # Track current window for resolution
        self._last_window_slug: Optional[str] = None

        # Orders the CLOB refused to cancel, retried before the window ends.
        self._pending_cancels: list[str] = []

        # Pre-fetched tokens for the upcoming window. The next slug is
        # deterministic (window_ts + 300), so we fetch it during the idle wait
        # and skip the Gamma round-trip at the start of the next cycle.
        self._prefetched: Optional[MarketTokens] = None
        self._prefetch_lock = threading.Lock()

        # Set by _on_price when the first price arrives from the feed. Used to
        # replace the fixed 2s sleep with a smart wait that exits as soon as the
        # WebSocket delivers its first book snapshot (typically < 200 ms).
        self._ws_ready = threading.Event()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.set_context(self.state)
        logger.info(f"[SS {self.symbol.upper()}] Streak Snapper Trader iniciado", icon="🚀")

        # Load past trades from DB into memory (survives restarts → correct PNL)
        self._load_state_from_db()

        while not self._stop.is_set():
            try:
                self._run_one_window()
            except Exception as exc:
                logger.err(f"[SS] window error: {exc}")
                self.state.set_status("error", str(exc))
                if not self._stop.is_set():
                    time.sleep(5.0)

    def _load_state_from_db(self) -> None:
        """Reload past trades and martingale state from DB to in-memory state."""
        try:
            with db_context():
                # Newest N, then back to chronological order — the in-memory
                # cache only feeds the dashboard, so it doesn't need full history.
                trades = (
                    TradeModel.query.filter_by(symbol=self.symbol)
                    .order_by(TradeModel.id.desc())
                    .limit(MEM_TRADE_CACHE_SIZE)
                    .all()
                )
                db_trades = [t.to_dict() for t in reversed(trades)]
                count = self.state.load_trades_from_db(db_trades)
                if count:
                    logger.info(f"[SS] {count} trades cargados de DB", icon="📂")
        except Exception as exc:
            logger.warn(f"[SS] could not load trades from DB: {exc}")

    # ── window cycle ──────────────────────────────────────────────────────────

    def _run_one_window(self) -> None:
        if not self.state.ss_enabled:
            self.state.set_status("idle", "Streak Snapper desactivado")
            logger.transient("[SS] desactivado — esperando 15s")
            time.sleep(15.0)
            return

        # ── wait for next 5-min boundary if needed ─────────────────────────
        now = time.time()
        current_boundary = (int(now) // 300) * 300
        next_boundary = current_boundary + 300
        seconds_into_window = now - current_boundary

        # If window is almost done (< 60s left), wait for next one
        if seconds_into_window > 240:
            wait_s = next_boundary - now + 2  # +2s buffer
            self.state.set_status("waiting",
                f"Esperando próximo evento en {int(wait_s)}s")
            logger.info(
                f"[SS] esperando próximo boundary 5m  "
                f"(actual={seconds_into_window:.0f}s → esperando {wait_s:.0f}s)",
                icon="⏳",
            )
            time.sleep(wait_s)

        # ── load tokens for the current window ────────────────────────────────
        self.state.set_status("loading", "Cargando mercado")
        tokens = self._consume_prefetched()
        if tokens is not None:
            # Pre-fetch landed during the previous window's idle wait — skip
            # the Gamma round-trip entirely (typically saves 1–3 s on the
            # critical path before the order).
            self.state.set_window(tokens.slug, tokens.window_ts)
            self.state.set_tokens(tokens.up_token_id, tokens.down_token_id)
            logger.ok(f"[SS] market listo (pre-cargado)  {tokens.slug}", icon="⚡")
        else:
            tokens = load_market_for_current_window(
                self.cfg.gamma_host,
                symbol=self.symbol,
                retry_seconds=self.cfg.market_load_retry_seconds,
                on_slug_change=lambda slug, ts: self.state.set_window(slug, ts),
            )
            self.state.set_window(tokens.slug, tokens.window_ts)
            self.state.set_tokens(tokens.up_token_id, tokens.down_token_id)
            logger.ok(f"[SS] market listo  {tokens.slug}", icon="📈")

        # ── resolve previous window trades ────────────────────────────────────
        self._resolve_pending_trades()
        self._confirm_binance_resolutions()

        # ── start price feed ──────────────────────────────────────────────────
        feed = PriceFeed(
            ws_url=self.cfg.ws_url,
            up_token_id=tokens.up_token_id,
            down_token_id=tokens.down_token_id,
            on_price=self._on_price,
            on_status=self.state.set_ws_connected,
            on_order_book=self._on_order_book,
        )
        self._ws_ready.clear()
        feed.start()
        # Wait for the first price from the feed rather than sleeping a fixed
        # 2 s. The book snapshot arrives within ~200 ms on a healthy connection;
        # the fallback ceiling is the original 2 s so a slow feed doesn't block
        # longer than before.
        if not self._ws_ready.wait(timeout=2.0):
            logger.warn("[SS] WS sin precios en 2s — continuando")

        try:
            # ── late-entry gate ───────────────────────────────────────────────
            # A window that is already well underway is not the window the
            # strategies were measured on. Worse, the entry is adversely
            # selected by construction: the limit cap prices the favourite out,
            # so the only side that can still fill is the one the market has
            # already written off. Observed in paper — a restart 182 s into a
            # window bought DOWN at $0.06, which is a lottery ticket, not a fade.
            #
            # Before the regime gate because it needs no network at all.
            now = time.time()
            age = now - tokens.window_ts
            if is_entry_too_late(tokens.window_ts, now, self.state.ss_max_entry_age):
                self.state.record_skip("SKIP_LATE")
                detail = (
                    f"ventana abierta hace {age:.0f}s "
                    f"(máximo {self.state.ss_max_entry_age}s)"
                )
                self.state.set_status("watching", detail)
                logger.info(f"[SS] SKIP_LATE: {detail}", icon="⏭")
                self._start_prefetch(tokens.window_ts)
                ttl = tokens.window_ts + WINDOW_SECONDS - time.time()
                if ttl > 0:
                    self._stop.wait(ttl + RESOLVE_GRACE_SECONDS)
                self._last_window_slug = tokens.slug
                return

            # ── regime gate ───────────────────────────────────────────────────
            # Checked before the signals so a filtered window costs one Binance
            # call instead of the whole signal path, and so the skip reason is
            # recorded even when a signal would have fired.
            verdict = self._check_regime()
            if not verdict.allowed:
                self.state.record_skip(verdict.reason)
                self.state.set_status("watching", verdict.detail)
                logger.info(f"[SS] {verdict.reason}: {verdict.detail}", icon="⏭")
                self._start_prefetch(tokens.window_ts)
                ttl = tokens.window_ts + WINDOW_SECONDS - time.time()
                if ttl > 0:
                    self._stop.wait(ttl + RESOLVE_GRACE_SECONDS)
                self._last_window_slug = tokens.slug
                return

            # ── evaluate signals ──────────────────────────────────────────────
            # Which strategies run comes from the registry, not from a chain of
            # `if mode in (...)`: a Fase B strategy is a descriptor away from
            # trading, and it can't be forgotten here.
            ctx = strategies.StrategyContext(
                state=self.state,
                symbol=self.symbol,
                tokens=tokens,
                streak=self.strategy,
                trader=self,
            )

            signals: list[StreakSignal] = []
            for descriptor in strategies.enabled_for(self.state, self.symbol):
                try:
                    signals.extend(descriptor.evaluate(ctx))
                except Exception as exc:
                    # One broken strategy must not take the window down with it.
                    logger.error(
                        f"[SS] {descriptor.id} falló al evaluar: {exc}", icon="💥"
                    )

            # Two strategies can point at opposite sides of the same window.
            # Buying both costs exactly what the pair pays out, so it's a
            # guaranteed wash that also feeds each strategy one fake win and one
            # fake loss. `resolve_conflicts` keeps the highest-priority side.
            #
            # Fade outranks Trend (priority 100 vs 50). This used to be the
            # other way round, on the argument that a locked trend cycle
            # shouldn't stall — but the measurement says the cycle was the
            # losing side: ss_fade returns +3.74% per entry and ss_trend −4.22%
            # at the pre-open price (docs/RUTA.md Fase 8). The old rule dropped
            # 98 of 1.152 Fade entries in favour of the worse signal.
            signals, dropped = strategies.resolve_conflicts(signals)
            if dropped:
                kept_detail = " ".join(f"{s.strategy}→{s.direction}" for s in signals)
                lost_detail = " ".join(f"{s.strategy}→{s.direction}" for s in dropped)
                logger.warn(
                    f"[SS] señales contradictorias ({kept_detail}  vs  "
                    f"{lost_detail}) — se descarta {lost_detail} en {tokens.slug}",
                    icon="⚖",
                )

            if not signals:
                self.state.set_status("watching", "Sin señal — observando")
                logger.transient(
                    f"[SS] {tokens.slug}  sin señal  "
                    f"ttl={int(tokens.window_ts + WINDOW_SECONDS - time.time())}s"
                )

            # ── execute signals ───────────────────────────────────────────────
            for sig in signals:
                self._execute_signal(tokens, sig)

            # ── wait until window end (WS stays alive for live prices) ────────
            # Late-evaluation strategies (e.g. coin_flip_dog, which enters at
            # T-60 to T-5) execute their signals inside _wait_out_window and
            # return them here so the resolution step below knows to settle the
            # window even when the early-signal list was empty.
            late_signals = self._wait_out_window(tokens)
            signals.extend(late_signals)

        finally:
            feed.stop()
            self.state.set_ws_connected(False)
            # Last chance before the slug is abandoned: the bot never revisits a
            # window, so an order still alive here can only fill against one that
            # has already settled. In `finally` so an exception on the way out
            # cannot be what leaves it resting.
            self._sweep_pending_cancels()

        self._last_window_slug = tokens.slug

        # Settle THIS window before the next one opens — otherwise the martingale
        # multiplier used for the next entry is a window out of date.
        if signals and not self._stop.is_set():
            self._resolve_pending_trades(
                wait_for_slug=tokens.slug, max_wait=RESOLVE_MAX_WAIT
            )

    # ── waiting out the window ────────────────────────────────────────────────

    def _wait_out_window(self, tokens) -> list:
        """Sleep until the window closes; tick observers and late evaluators.

        Returns any signals generated by late-evaluation strategies so the
        caller can include them in the resolution step. With no observers and no
        late evaluators this is exactly the single blocking wait it replaced —
        deliberately, because every measured result in `docs/RUTA.md` was
        produced by that path and a tick loop that runs when nobody asked for it
        would perturb it for nothing.

        The WebSocket stays connected for the whole window, so an observer reads
        live prices and the book without spending a single request.
        """
        # Kick off the next window's token fetch right away. The slug is
        # deterministic (window_ts + 300) and the wait is otherwise idle; in
        # practice the Gamma call returns in < 500 ms and is ready long before
        # this window closes, eliminating it from the critical path of the next
        # cycle.
        self._start_prefetch(tokens.window_ts)

        ttl = tokens.window_ts + WINDOW_SECONDS - time.time()

        enabled = strategies.enabled_for(self.state, self.symbol)
        observers    = [d for d in enabled if d.observe        is not None]
        late_evals   = [d for d in enabled if d.evaluate_late  is not None]

        if not observers and not late_evals:
            if ttl > 0:
                logger.transient(f"[SS] esperando cierre de ventana... {int(ttl)}s")
                self._stop.wait(ttl + RESOLVE_GRACE_SECONDS)
            return []

        logger.transient(
            f"[SS] observando la ventana... {int(max(ttl, 0))}s "
            f"({len(observers)} obs, {len(late_evals)} late)"
        )
        deadline = tokens.window_ts + WINDOW_SECONDS + RESOLVE_GRACE_SECONDS

        # Strategies that already fired this window must not fire again.
        late_fired: set[str] = set()
        late_signals: list = []

        while not self._stop.is_set():
            now = time.time()
            if now >= deadline:
                break

            ctx = strategies.StrategyContext(
                state=self.state,
                symbol=self.symbol,
                tokens=tokens,
                streak=self.strategy,
                trader=self,
                seconds_left=tokens.window_ts + WINDOW_SECONDS - now,
            )

            for descriptor in observers:
                try:
                    descriptor.observe(ctx)
                except Exception as exc:
                    # Same rule as `evaluate`: one broken strategy must not take
                    # the window — or the settlement that follows it — down.
                    logger.err(f"[SS] {descriptor.id} falló al observar: {exc}")

            for descriptor in late_evals:
                if descriptor.id in late_fired:
                    continue  # one entry per strategy per window
                try:
                    sigs = descriptor.evaluate_late(ctx)
                    if sigs:
                        # Resolve conflicts against everything already in flight.
                        # A late strategy pointing the wrong way is dropped, not
                        # executed — buying both sides of the same window is a
                        # guaranteed wash (docs/RUTA.md Fase 4.5).
                        combined, dropped = strategies.resolve_conflicts(
                            late_signals + list(sigs)
                        )
                        new_sigs = [s for s in sigs if s in combined]
                        if dropped:
                            lost = " ".join(
                                f"{s.strategy}→{s.direction}" for s in dropped
                                if s in sigs
                            )
                            logger.warn(
                                f"[SS] señal tardía descartada (conflicto): "
                                f"{lost}", icon="⚖",
                            )
                        for sig in new_sigs:
                            self._execute_signal(tokens, sig)
                        late_signals.extend(new_sigs)
                        late_fired.add(descriptor.id)
                except Exception as exc:
                    logger.err(
                        f"[SS] {descriptor.id} falló en evaluate_late: {exc}"
                    )

            self._stop.wait(min(OBSERVE_TICK_SECONDS, max(deadline - time.time(), 0.0)))

        return late_signals

    # ── regime gate ───────────────────────────────────────────────────────────

    def _check_regime(self) -> regime.RegimeVerdict:
        """Ask the regime filters whether this window should be traded at all.

        Candles are only fetched when a percentile-based filter is actually
        configured — with everything at its default the gate costs nothing, and
        an hours-only setup doesn't need price history either.
        """
        state = self.state
        hours = getattr(state, "ss_trading_hours", "") or ""
        vol_min = getattr(state, "ss_vol_min_pct", 0.0)
        vol_max = getattr(state, "ss_vol_max_pct", 100.0)
        range_max = getattr(state, "ss_range_max_pct", 100.0)

        needs_candles = vol_min > 0.0 or vol_max < 100.0 or range_max < 100.0
        candles: list = []
        if needs_candles:
            candles = get_5min_candles(regime.PERCENTILE_LOOKBACK + 50, self.symbol) or []
            if not candles:
                # No history means no basis to refuse. Blocking here would turn
                # a Binance hiccup into a silent trading halt.
                logger.warn("[SS] sin velas para el filtro de régimen — no se filtra")
                return regime.hours_filter(hours)

        return regime.evaluate(
            candles,
            hours_spec=hours,
            vol_min_pct=vol_min,
            vol_max_pct=vol_max,
            range_max_pct=range_max,
        )

    # ── signal execution ──────────────────────────────────────────────────────

    def _execute_signal(self, tokens, sig: StreakSignal) -> None:
        """Execute a limit buy order for a given signal."""
        strategy_label = sig.strategy.upper().replace("_", " ")

        # Determine token_id
        token_id = tokens.up_token_id if sig.direction == "UP" else tokens.down_token_id

        # Get current ASK from WebSocket price state
        ask_up, ask_down = self.state.get_asks()
        current_ask = ask_up if sig.direction == "UP" else ask_down

        # ── ask-above-cap gate ────────────────────────────────────────────────
        # `min(cap, ask)` below turns a priced-out window into a resting bid
        # *under* the ask, which is a different trade from the one that was
        # measured: in paper it was booked as a fill that would never have
        # happened, and in real mode it left a GTC order the bot neither
        # verifies nor cancels — the position lands in `trades` either way.
        #
        # And the fills it does get are the toxic ones: a bid only trades when
        # someone sells into it, i.e. when the side is already going wrong.
        #
        # Skipping instead is also where the edge is. Over the 1.150 fade
        # signals of the Gamma sample the ask beats the 0,52 cap in 36% of
        # windows; dropping exactly those takes the signal from +3,91%/trade
        # (n=1150, t=+1,37) to +8,77% (n=734, 54,9%, t=+2,40), positive in the
        # four ~8,8-day quarters and in both halves. Buying any side at ≤0,52
        # with no signal returns −1,3%, so this is the signal, not cheapness.
        # Reproducible with `python scripts/cap_impact.py`; those ROIs are
        # equal-weighted per operation (what the t needs), so they read a shade
        # above the dollar-weighted +3,74% published in docs/RUTA.md Fase 8.
        if is_ask_above_cap(current_ask, sig.limit_cap):
            self.state.record_skip("SKIP_ASK_ABOVE_CAP")
            detail = (
                f"ask {current_ask:.4f} > cap {sig.limit_cap:.4f} "
                f"({sig.direction})"
            )
            self.state.set_status("watching", detail)
            logger.info(f"[SS {strategy_label}] SKIP_ASK_ABOVE_CAP: {detail}", icon="⏭")
            return

        # Determine limit price: min(cap, current_ask)
        if current_ask and current_ask > 0:
            limit_price = round(min(sig.limit_cap, current_ask), 4)
        else:
            limit_price = sig.limit_cap
            logger.warn(f"[SS {strategy_label}] sin ask del WS — usando cap {limit_price:.4f}")

        limit_price = _floor_to_tick(limit_price)

        # Calculate shares and cost from signal
        shares = sig.shares
        cost   = round(shares * limit_price, 4)

        logger.ok(
            f"[SS {strategy_label}] 🎯 SEÑAL  "
            f"{sig.direction} @ ${limit_price:.4f} ×{shares}  "
            f"cost=${cost:.2f}  mult=×{sig.multiplier:.2f}  "
            f"reason={sig.signal_reason}",
            icon="🎯" if sig.strategy == "box_builder" else "🐕",
        )

        # ── paper or real execution ───────────────────────────────────────────
        is_paper = self.state.mode != "real"
        order_id: Optional[str] = None

        if is_paper:
            logger.info(
                f"[SS {strategy_label}] PAPER  {sig.direction} @ {limit_price:.4f} "
                f"×{shares}  cost=${cost:.4f}",
                icon="📄",
            )
        else:
            if self._client is None:
                logger.err(f"[SS {strategy_label}] real mode: CLOB client no inicializado")
                return

            try:
                order_id, placed = self._place_limit_buy(token_id, limit_price, shares)
            except Exception as exc:
                logger.err(f"[SS {strategy_label}] order failed: {exc}")
                return

            # `_place_limit_buy` swallows its own exceptions and returns None,
            # so without this a rejected order fell through to the persist
            # block and was written as an open real position — note="paper",
            # counted in the P&L, and advancing the martingale on a trade that
            # never existed. Recording a position we don't hold is worse than
            # missing a window.
            if not order_id:
                logger.err(
                    f"[SS {strategy_label}] la orden no se pudo enviar — "
                    f"ventana {tokens.slug} descartada, no se registra posición"
                )
                self.state.record_skip("SKIP_ORDER_FAILED")
                return

            # ── fill verification ─────────────────────────────────────────────
            # Sending an order is not holding a position. Until this existed the
            # trade was written the moment the CLOB accepted the order, so an
            # unfilled bid became a position on the books that the resolution
            # step then settled into a P&L — and the order itself stayed alive
            # past its own window, because nothing ever cancelled it.
            filled = self._settle_order(order_id, placed, shares, strategy_label)

            if filled <= 0:
                logger.warn(
                    f"[SS {strategy_label}] la orden no se llenó en "
                    f"{FILL_WAIT_SECONDS:.0f}s — cancelada, sin posición en "
                    f"{tokens.slug}",
                    icon="⏭",
                )
                self.state.record_skip("SKIP_NO_FILL")
                return

            if filled < shares:
                # A partial fill is a real position at a smaller size, so it is
                # kept and recorded at what actually filled — recording the
                # requested size would overstate the stake and, with martingale
                # sizing, compound that error into the next entry.
                logger.warn(
                    f"[SS {strategy_label}] llenado parcial "
                    f"{filled:g}/{shares:g} shares — resto cancelado",
                    icon="◐",
                )
                shares = filled
                cost = round(shares * limit_price, 4)

        # ── persist trade to DB ───────────────────────────────────────────────
        trade_id: int = 0
        try:
            with db_context():
                trade = TradeModel(
                    strategy=sig.strategy,
                    symbol=self.symbol,
                    direction=sig.direction,
                    token_id=token_id,
                    window_slug=tokens.slug,
                    window_ts=tokens.window_ts,
                    limit_cap=sig.limit_cap,
                    entry_price=limit_price,
                    shares=float(shares),
                    cost=cost,
                    shares_count=float(shares),
                    multiplier=sig.multiplier,
                    loss_streak=sig.loss_streak,
                    mode=self.state.mode,
                    # A real trade always has an order id by this point (the
                    # guard above returns otherwise), so "paper" can only mean
                    # paper. It used to also mean "real order that failed".
                    note=f"order_id={order_id}" if order_id else "paper",
                )
                _db.session.add(trade)
                _db.session.commit()
                trade_id = trade.id
            logger.info(
                f"[SS {strategy_label}] trade #{trade_id} guardado en DB",
                icon="💾",
            )

            # ── sync to in-memory state (for dashboard KPIs) ──────────────
            mem_trade = Trade(
                id=trade_id,
                window_slug=tokens.slug,
                window_ts=tokens.window_ts,
                side=sig.direction,
                token_id=token_id,
                price=limit_price,
                shares=float(shares),
                cost=cost,
                mode=self.state.mode,
                opened_at=time.time(),
                strategy=sig.strategy,
                multiplier=sig.multiplier,
                limit_cap=sig.limit_cap,
            )
            self.state.add_trade(mem_trade)

            # Only now is the position real, so only now may a strategy commit
            # state that claims one. For ss_trend that is the 4h locked side:
            # opening it at signal time meant the tie-break, the cap gate, a
            # rejected order or an unfilled one each left a committed cycle
            # behind with nothing bought against it.
            self.strategy.on_entry(sig)
        except Exception as exc:
            logger.err(f"[SS {strategy_label}] DB save failed: {exc}")

    # ── resolution ────────────────────────────────────────────────────────────

    def _resolve_pending_trades(
        self, wait_for_slug: Optional[str] = None, max_wait: float = 0.0
    ) -> None:
        """Resolve open trades whose window has ended (via Gamma outcomePrices).

        When `wait_for_slug` is given, keep polling Gamma (up to `max_wait`
        seconds) until that window's trades are resolved.  This matters because
        the martingale multiplier for the *next* window is only correct once the
        window that just closed has been settled.
        """
        deadline = time.time() + max_wait

        while True:
            self._resolve_once()

            if wait_for_slug is None or time.time() >= deadline:
                return
            if not self._has_open_trades_for(wait_for_slug):
                return

            logger.transient(
                f"[SS] esperando resolución de {wait_for_slug}... "
                f"{int(deadline - time.time())}s"
            )
            time.sleep(RESOLVE_POLL_SECONDS)

    def _has_open_trades_for(self, slug: str) -> bool:
        try:
            with db_context():
                return (
                    _db.session.query(TradeModel)
                    .filter_by(status="open", window_slug=slug, symbol=self.symbol)
                    .count()
                    > 0
                )
        except Exception:
            return False

    def _resolve_once(self) -> int:
        """One resolution sweep. Returns the number of trades resolved."""
        try:
            with db_context():
                open_trades = (
                    _db.session.query(TradeModel)
                    .filter_by(status="open", symbol=self.symbol)
                    .all()
                )
        except Exception:
            return 0

        if not open_trades:
            return 0

        # First pass: collect outcomes (HTTP calls outside DB context)
        resolutions: list[tuple[TradeModel, str, float, bool, str]] = []
        for trade in open_trades:
            if time.time() < trade.window_ts + WINDOW_SECONDS + RESOLVE_GRACE_SECONDS:
                continue

            # Binance settles the moment the candle closes; Gamma needs ~3 min,
            # which is longer than the window itself.
            source = "binance"
            winner = get_window_direction(trade.window_ts, symbol=self.symbol)
            if winner is None:
                winner = self._get_window_outcome(trade.window_slug)
                source = "gamma"
            if winner is None:
                continue

            won = trade.direction == winner
            pnl = round((trade.shares - trade.cost) if won else -trade.cost, 4)
            resolutions.append((trade, winner, pnl, won, source))

        if not resolutions:
            return 0

        # Second pass: apply resolutions within a single DB context + commit
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)

        try:
            with db_context():
                for trade, winner, pnl, won, source in resolutions:
                    # Re-attach to current session. merge() returns a *different*
                    # instance, so mutate that one — not the detached original.
                    merged = _db.session.merge(trade)
                    merged.status = "won" if won else "lost"
                    merged.outcome = winner
                    merged.won = won
                    merged.pnl = pnl
                    merged.resolved_at = now_utc
                    merged.resolution_source = source
                _db.session.commit()
        except Exception as exc:
            logger.err(f"[SS] DB commit failed: {exc}")
            try:
                with db_context():
                    _db.session.rollback()
            except Exception:
                pass
            return 0

        # Update martingale states (these helpers handle their own DB context)
        for trade, winner, pnl, won, source in resolutions:
            label = trade.strategy.upper().replace("_", " ")
            status = "won" if won else "lost"
            via = "" if source == "gamma" else "  (vía Binance)"

            if won:
                self.strategy.on_win(trade.strategy)
                logger.ok(
                    f"[SS {label}] ✅ trade #{trade.id} GANÓ  "
                    f"side={trade.direction}  pnl=${pnl:+.4f}{via}",
                    icon="🎯",
                )
            else:
                self.strategy.on_loss(trade.strategy)
                logger.warn(
                    f"[SS {label}] ❌ trade #{trade.id} PERDIÓ  "
                    f"side={trade.direction}  pnl=${pnl:+.4f}{via}",
                )

            # Sync resolution to in-memory state (for dashboard KPIs)
            final_price = 1.0 if won else 0.0
            try:
                self.state.resolve_trade(trade.id, status, final_price, pnl,
                                         note=f"resolved: {trade.direction} vs {winner}")
            except Exception as exc:
                logger.warn(f"[SS] in-memory sync failed for trade #{trade.id}: {exc}")

        self.state.set_status("watching", f"{len(resolutions)} trades resueltos")
        return len(resolutions)

    def _confirm_binance_resolutions(self) -> None:
        """Re-check Binance-settled trades against Gamma, the official source.

        Gamma publishes outcomePrices about three minutes after a window closes.
        Disagreements should be rare (they need Binance and Polymarket's feed to
        straddle the open price differently), but when one happens the trade's
        recorded P&L is corrected here.
        """
        try:
            with db_context():
                pending = (
                    _db.session.query(TradeModel)
                    .filter(TradeModel.resolution_source == "binance",
                            TradeModel.symbol == self.symbol)
                    .order_by(TradeModel.id.desc())
                    .limit(CONFIRM_BATCH_SIZE)
                    .all()
                )
        except Exception:
            return

        if not pending:
            return

        corrections: list[tuple[TradeModel, str, float, bool]] = []
        confirmed_ids: list[int] = []

        for trade in pending:
            if time.time() < trade.window_ts + WINDOW_SECONDS + GAMMA_SETTLE_SECONDS:
                continue
            winner = self._get_window_outcome(trade.window_slug)
            if winner is None:
                continue
            if winner == trade.outcome:
                confirmed_ids.append(trade.id)
                continue
            won = trade.direction == winner
            pnl = round((trade.shares - trade.cost) if won else -trade.cost, 4)
            corrections.append((trade, winner, pnl, won))

        if not confirmed_ids and not corrections:
            return

        try:
            with db_context():
                for trade, winner, pnl, won in corrections:
                    merged = _db.session.merge(trade)
                    merged.status = "won" if won else "lost"
                    merged.outcome = winner
                    merged.won = won
                    merged.pnl = pnl
                    merged.resolution_source = "gamma"
                for trade_id in confirmed_ids:
                    obj = _db.session.get(TradeModel, trade_id)
                    if obj is not None:
                        obj.resolution_source = "gamma"
                _db.session.commit()
        except Exception as exc:
            logger.err(f"[SS] confirmación Gamma: commit falló: {exc}")
            try:
                with db_context():
                    _db.session.rollback()
            except Exception:
                pass
            return

        for trade, winner, pnl, won in corrections:
            label = trade.strategy.upper().replace("_", " ")
            logger.err(
                f"[SS {label}] ⚠ trade #{trade.id} CORREGIDO por Gamma: "
                f"Binance dijo {trade.outcome}, Gamma dice {winner} → "
                f"{'GANÓ' if won else 'PERDIÓ'}  pnl=${pnl:+.4f}"
            )
            # The martingale already moved on the Binance result. We can't
            # reconstruct the multiplier it *should* have had (later windows have
            # already used the wrong one), so re-apply the correct transition
            # from here and let the log record the discrepancy.
            if won:
                self.strategy.on_win(trade.strategy)
            else:
                self.strategy.on_loss(trade.strategy)

            try:
                self.state.resolve_trade(
                    trade.id, "won" if won else "lost", 1.0 if won else 0.0, pnl,
                    note=f"corregido por Gamma: {trade.direction} vs {winner}",
                )
            except Exception:
                pass

    def _get_window_outcome(self, slug: str) -> Optional[str]:
        """Get the resolved outcome (UP/DOWN) for a past window via Gamma API."""
        try:
            r = requests.get(
                f"{self.cfg.gamma_host.rstrip('/')}/events",
                params={"slug": slug},
                timeout=10,
            )
            if r.status_code != 200 or not r.json():
                return None
            data = r.json()
            if not isinstance(data, list) or not data:
                return None
            markets = data[0].get("markets") or []
            if not markets:
                return None

            import json
            prices = json.loads(markets[0].get("outcomePrices", "[]") or "[]")
            if len(prices) != 2:
                return None

            up_price = float(prices[0])
            if up_price == 1.0:
                return "UP"
            if up_price == 0.0:
                return "DOWN"
        except Exception:
            pass
        return None

    # ── price callback ────────────────────────────────────────────────────────

    def _on_price(self, side: str, bid: float, ask: float, mid: float) -> None:
        self.state.update_price(side, bid, ask, mid)
        # Signal the smart warmup wait: the feed has live prices.
        self._ws_ready.set()

    def _on_order_book(self, side: str, bids: list, asks: list) -> None:
        self.state.update_order_book(side, bids, asks)

    # ── token pre-fetch ───────────────────────────────────────────────────────

    def _start_prefetch(self, window_ts: int) -> None:
        """Fetch tokens for the next window in a background thread.

        The next slug is deterministic (window_ts + WINDOW_SECONDS), so we fire
        this during the idle wait at the end of a window and have the answer
        ready before the cycle restarts. In practice the Gamma call returns in
        < 500 ms, removing a 1–3 s round-trip from the start of the next cycle.

        Nothing breaks if the fetch fails: `_consume_prefetched` returns None
        and the caller falls back to `load_market_for_current_window`.
        """
        next_ts   = window_ts + WINDOW_SECONDS
        next_slug = f"{self.symbol}-updown-5m-{next_ts}"

        def _fetch() -> None:
            try:
                result = fetch_market(self.cfg.gamma_host, next_slug)
                if result:
                    with self._prefetch_lock:
                        self._prefetched = MarketTokens(
                            slug=next_slug,
                            window_ts=next_ts,
                            up_token_id=result[0],
                            down_token_id=result[1],
                        )
                    logger.info(
                        f"[SS] tokens pre-cargados  {next_slug}", icon="⚡"
                    )
            except Exception as exc:
                logger.warn(f"[SS] pre-carga falló ({next_slug}): {exc}")

        threading.Thread(
            target=_fetch, name=f"ss-prefetch-{self.symbol}", daemon=True
        ).start()

    def _consume_prefetched(self) -> Optional[MarketTokens]:
        """Return and clear the cached tokens if they match the current window.

        Always clears the cache. Stale tokens (from a window that was skipped
        or rolled over) would silently supply the wrong token IDs, which is worse
        than a fresh fetch — so any mismatch is discarded without a fallback.
        """
        from .market import current_slug as _current_slug
        slug, _ = _current_slug(self.symbol)
        with self._prefetch_lock:
            cached = self._prefetched
            self._prefetched = None   # consume regardless
            if cached is not None and cached.slug == slug:
                return cached
            return None

    # ── Maker / cancel helpers (used by box_builder.observe) ─────────────────
    # Paper mode: these methods are no-ops that return a sentinel so the
    # strategy's state machine can still run its logic without real orders.
    # Maker orders require PRIVATE_KEY; the guard matches _place_limit_buy.

    def _place_maker_bid(
        self, token_id: str, price: float, shares: float
    ) -> Optional[str]:
        """POST a post-only GTC bid. Returns order_id or None on failure.

        On a "crosses book" rejection the CLOB returns a 400; we retry 1 c
        lower (same rule as box_builder.py). Paper mode returns a synthetic id
        so the box state machine can track the leg without real CLOB calls.
        """
        is_paper = self.state.mode != "real"
        if is_paper:
            return f"paper-{token_id[:8]}-{int(price*100)}"

        if self._client is None:
            return None

        try:
            from py_clob_client_v2 import OrderArgs, OrderType

            px = round(price, 2)
            for _ in range(3):
                if px < PRICE_TICK:
                    return None
                order_args = OrderArgs(
                    token_id=str(token_id),
                    price=float(px),
                    size=float(shares),
                    side="BUY",
                )
                try:
                    resp = self._client.create_and_post_order(
                        order_args, order_type=OrderType.GTC, post_only=True
                    )
                except Exception as exc:
                    err = str(exc).lower()
                    if "post-only" in err and "cross" in err:
                        px = round(px - PRICE_TICK, 2)
                        continue
                    logger.warn(f"[BB] maker bid falló: {exc}")
                    return None
                if resp and isinstance(resp, dict):
                    oid = resp.get("orderID") or resp.get("id")
                    if oid:
                        return str(oid)
                    if "cross" in str(resp).lower():
                        px = round(px - PRICE_TICK, 2)
                        continue
                return None
        except Exception as exc:
            logger.err(f"[BB] _place_maker_bid error: {exc}")
        return None

    def _place_taker_order(
        self, token_id: str, side: str, price: float, shares: float
    ) -> Optional[str]:
        """Marketable GTC (post_only=False). Returns order_id or None.

        Used by box_builder for the completion lift and the T-90 CUT sell.
        Paper mode returns a sentinel — the strategy just needs to know the
        order was acknowledged, not that it really traded.
        """
        is_paper = self.state.mode != "real"
        if is_paper:
            return f"paper-taker-{token_id[:8]}"

        if self._client is None:
            return None

        try:
            from py_clob_client_v2 import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=str(token_id),
                price=float(round(price, 2)),
                size=float(shares),
                side=side.upper(),
            )
            resp = self._client.create_and_post_order(
                order_args, order_type=OrderType.GTC, post_only=False
            )
            if resp and isinstance(resp, dict):
                return str(resp.get("orderID") or resp.get("id") or "")
        except Exception as exc:
            logger.err(f"[BB] _place_taker_order error: {exc}")
        return None

    def _cancel_token_orders(self, token_id: str) -> bool:
        """Cancel ALL resting orders on a token. Returns True on success.

        Paper mode is a no-op success. Real mode uses cancel_market_orders
        (same endpoint as box_builder.py cancel_token_orders).
        """
        is_paper = self.state.mode != "real"
        if is_paper:
            return True

        if self._client is None:
            return False

        try:
            from py_clob_client_v2.clob_types import OrderMarketCancelParams

            self._client.cancel_market_orders(
                OrderMarketCancelParams(asset_id=str(token_id))
            )
            return True
        except Exception as exc:
            logger.warn(f"[BB] cancel_token_orders falló para {token_id[:12]}: {exc}")
            return False

    def _get_position_size(self, token_id: str) -> float:
        """Shares held for token_id, via the data-api positions endpoint.

        Returns 0.0 on any failure. Paper mode: compare against our
        open-trades table (fills recorded via _record_box_fill are detectable
        there, though it's a lightweight check — the state machine tolerates
        occasional misses on the fill poll).
        """
        is_paper = self.state.mode != "real"
        if is_paper:
            # In paper, box fills are never real; assume 0 so the state
            # machine relies on the order-id sentinel check instead.
            return 0.0

        if not self.cfg.proxy_wallet:
            return 0.0

        try:
            import requests as _req

            r = _req.get(
                "https://data-api.polymarket.com/positions",
                params={
                    "user": self.cfg.proxy_wallet,
                    "limit": 500,
                    "sortBy": "CURRENT",
                    "sortDirection": "DESC",
                },
                timeout=8,
            )
            if r.status_code != 200:
                return 0.0
            for pos in r.json():
                if str(pos.get("asset")) == str(token_id):
                    return float(pos.get("size", 0) or 0)
        except Exception:
            pass
        return 0.0

    def _record_box_fill(
        self,
        tokens,
        direction: str,
        token_id: str,
        fill_price: float,
        shares: float,
    ) -> None:
        """Persist a box leg fill to the DB as a normal open trade.

        Uses the same TradeModel as _execute_signal so the resolution step
        settles it automatically. strategy="box_builder" in both legs so the
        dashboard groups them correctly.
        """
        cost = round(shares * fill_price, 4)
        trade_id: int = 0
        try:
            with db_context():
                trade = TradeModel(
                    strategy="box_builder",
                    symbol=self.symbol,
                    direction=direction,
                    token_id=token_id,
                    window_slug=tokens.slug,
                    window_ts=tokens.window_ts,
                    limit_cap=fill_price,
                    entry_price=fill_price,
                    shares=float(shares),
                    cost=cost,
                    shares_count=float(shares),
                    multiplier=1.0,
                    loss_streak=0,
                    mode=self.state.mode,
                    note="box_leg",
                )
                _db.session.add(trade)
                _db.session.commit()
                trade_id = trade.id
            logger.info(
                f"[BB] trade #{trade_id} guardado  {direction} @ {fill_price:.3f} "
                f"×{shares:.0f}",
                icon="💾",
            )
            from .state import Trade
            mem_trade = Trade(
                id=trade_id,
                window_slug=tokens.slug,
                window_ts=tokens.window_ts,
                side=direction,
                token_id=token_id,
                price=fill_price,
                shares=float(shares),
                cost=cost,
                mode=self.state.mode,
                opened_at=time.time(),
                strategy="box_builder",
                multiplier=1.0,
                limit_cap=fill_price,
            )
            self.state.add_trade(mem_trade)
        except Exception as exc:
            logger.err(f"[BB] _record_box_fill DB save failed: {exc}")

    # ── CLOB V2 order helper ──────────────────────────────────────────────────

    def _place_limit_buy(
        self, token_id: str, price: float, shares: float
    ) -> tuple[Optional[str], Optional[dict]]:
        """Place a GTC limit BUY order. Returns (order_id, raw response).

        The response is handed back, not discarded: for a marketable order the
        CLOB already reports the match in it, so the common case needs no
        follow-up lookup at all.
        """
        try:
            from py_clob_client_v2 import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=str(token_id),
                price=float(price),
                size=float(shares),
                side="BUY",
            )
            resp = self._client.create_and_post_order(
                order_args, order_type=OrderType.GTC, post_only=False
            )
            if resp and isinstance(resp, dict):
                return (resp.get("orderID") or resp.get("id"), resp)
        except Exception as exc:
            logger.err(f"[SS] CLOB order failed: {exc}")
        return (None, None)

    def _fetch_order(self, order_id: str) -> Optional[dict]:
        """Look an order up. Returns None on any failure, never raises.

        A failed lookup is not evidence of anything, which is exactly why it
        returns None rather than an empty dict: `matched_shares` reads None as
        "unknown", and the caller must not mistake a network error for "did not
        fill".
        """
        try:
            resp = self._client.get_order(str(order_id))
            return resp if isinstance(resp, dict) else None
        except Exception as exc:
            logger.warn(f"[SS] no se pudo consultar la orden {order_id}: {exc}")
            return None

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an order. Returns whether the CLOB accepted the cancellation.

        Orders left alive outlive their window: the bot holds to resolution and
        never re-visits a slug, so an uncancelled remainder can fill minutes
        later against a window that has already settled.
        """
        try:
            from py_clob_client_v2 import OrderPayload

            self._client.cancel_order(OrderPayload(orderID=str(order_id)))
            return True
        except Exception as exc:
            logger.err(f"[SS] no se pudo cancelar la orden {order_id}: {exc}")
            return False

    def _settle_order(
        self, order_id: str, placed: Optional[dict], requested: float, label: str
    ) -> float:
        """Shares we actually hold after placing `order_id`, remainder cancelled.

        The order is marketable, so the usual path is a full fill reported in
        the POST response and no lookup at all. Anything short of that is
        polled briefly, then cancelled — a partial fill is a real position and
        is kept; the unfilled remainder is not left resting.
        """
        filled = matched_shares(placed, requested)
        deadline = time.time() + FILL_WAIT_SECONDS

        while (filled is None or filled < requested) and time.time() < deadline:
            if self._stop.wait(FILL_POLL_SECONDS):
                break  # shutdown: stop waiting, still cancel below
            filled = matched_shares(self._fetch_order(order_id), requested)

        if filled is not None and filled >= requested:
            return requested

        cancelled = self._cancel_order(order_id)
        if not cancelled:
            # Sweep it again before the window ends. Retried rather than
            # ignored because the alternative is an order that can fill after
            # its own window has settled.
            self._pending_cancels.append(order_id)

        # After a cancellation the order is final, so this lookup is the
        # authoritative fill count — the poll above may have caught it mid-match.
        final = matched_shares(self._fetch_order(order_id), requested)
        if final is not None:
            return final
        if filled is not None:
            return filled

        # Nothing — the POST response, the polls and the post-cancel lookup all
        # came back unintelligible. Assume the order filled, because the two
        # errors are not symmetric: an over-recorded position shows up in the
        # P&L as a wrong number, while an unrecorded real one is money spent
        # that never resolves and never appears anywhere.
        logger.err(
            f"[SS {label}] estado de llenado desconocido para {order_id} — "
            f"se registra como llenado ({requested}) para no perder la posición"
        )
        return requested

    def _sweep_pending_cancels(self) -> None:
        """Retry cancellations the CLOB refused earlier in the window."""
        if not self._pending_cancels:
            return
        still: list[str] = []
        for order_id in self._pending_cancels:
            if not self._cancel_order(order_id):
                still.append(order_id)
        if still:
            logger.err(
                f"[SS] {len(still)} orden(es) siguen vivas tras reintentar "
                f"la cancelación: {', '.join(still)}"
            )
        self._pending_cancels = still

    # ── CLOB V2 client builder ────────────────────────────────────────────────

    def _build_client(self):
        try:
            from py_clob_client_v2 import ClobClient

            seed = ClobClient(
                host=self.cfg.clob_host,
                chain_id=self.cfg.chain_id,
                key=self.cfg.private_key,
            )
            creds  = seed.create_or_derive_api_key()
            client = ClobClient(
                host=self.cfg.clob_host,
                chain_id=self.cfg.chain_id,
                key=self.cfg.private_key,
                creds=creds,
            )
            logger.ok("[SS] CLOB V2 autenticado", icon="🔑")
            return client
        except Exception as exc:
            logger.err(f"[SS] CLOB V2 auth error: {exc}")
            return None
