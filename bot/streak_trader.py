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
from .market import load_market_for_current_window
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

# How many past trades to keep in the in-memory dashboard cache.
MEM_TRADE_CACHE_SIZE = 500


def is_entry_too_late(window_ts: float, now: float, max_age: float) -> bool:
    """Whether a window is too far along to open a position in it.

    Split out of the loop so it can be tested without driving a whole window.
    A negative age (clock skew, or a window that hasn't opened yet) is never
    late — the check exists to refuse stale entries, not early ones.
    """
    return (now - window_ts) > max_age


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
        feed.start()
        time.sleep(2.0)  # brief warmup for first prices

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
            ttl = tokens.window_ts + WINDOW_SECONDS - time.time()
            if ttl > 0:
                logger.transient(f"[SS] esperando cierre de ventana... {int(ttl)}s")
                self._stop.wait(ttl + RESOLVE_GRACE_SECONDS)

        finally:
            feed.stop()
            self.state.set_ws_connected(False)

        self._last_window_slug = tokens.slug

        # Settle THIS window before the next one opens — otherwise the martingale
        # multiplier used for the next entry is a window out of date.
        if signals and not self._stop.is_set():
            self._resolve_pending_trades(
                wait_for_slug=tokens.slug, max_wait=RESOLVE_MAX_WAIT
            )

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
        strategy_label = "FADE" if sig.strategy == "ss_fade" else "TREND"

        # Determine token_id
        token_id = tokens.up_token_id if sig.direction == "UP" else tokens.down_token_id

        # Get current ASK from WebSocket price state
        ask_up, ask_down = self.state.get_asks()
        current_ask = ask_up if sig.direction == "UP" else ask_down

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
            icon="🚀" if sig.strategy == "ss_fade" else "📈",
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
                order_id = self._place_limit_buy(token_id, limit_price, shares)
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
            label = "FADE" if trade.strategy == "ss_fade" else "TREND"
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
            label = "FADE" if trade.strategy == "ss_fade" else "TREND"
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

    def _on_order_book(self, side: str, bids: list, asks: list) -> None:
        self.state.update_order_book(side, bids, asks)

    # ── CLOB V2 order helper ──────────────────────────────────────────────────

    def _place_limit_buy(self, token_id: str, price: float, shares: float) -> Optional[str]:
        """Place a GTC limit BUY order. Returns order_id or None."""
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
                return resp.get("orderID") or resp.get("id")
        except Exception as exc:
            logger.err(f"[SS] CLOB order failed: {exc}")
        return None

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
