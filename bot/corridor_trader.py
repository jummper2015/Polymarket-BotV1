"""Corridor Trader — 15-min BTC window cycle for the Corridor Collector strategy.

Starts/idles based on btc15 BotState.cc_enabled.
Manages two PriceFeeds:
  • feed_15m → updates STATES["btc15"] via state.update_price()
  • feed_5m  → updates local _5m_prices dict (won't interfere with BTC 5m trader)
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from . import logger
from .config import Config
from .market import fetch_market, MarketTokens
from .price_feed import PriceFeed
from .state import STATES, Trade
from .strategy_corridor import (
    CorridorCollectorStrategy,
    get_btc_bar_open,
    get_atr14,
    ACTION_START,
    ACTION_END,
    WIN15,
)


def _sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)


class CorridorTrader(threading.Thread):
    """Thread that drives the 15-min Corridor Collector window cycle."""

    FEED_WARMUP    = 5.0   # seconds to let WebSocket feeds deliver first prices
    CHECK_INTERVAL = 4.0   # seconds between gate evaluations in the action window
    RESOLVE_WAIT   = 6.0   # seconds after window end before resolving trades

    def __init__(self, cfg: Config) -> None:
        super().__init__(name="trader-btc15", daemon=True)
        self.cfg    = cfg
        self.state  = STATES["btc15"]
        self._stop  = threading.Event()

        # 5m leg prices — separate from STATES["btc"] to avoid interference
        self._5m_lock   = threading.Lock()
        self._5m_prices: Dict[str, Dict[str, float]] = {}  # "UP"/"DOWN" → {bid, ask, mid}
        self._5m_tokens: Optional[MarketTokens] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.set_context(self.state)
        logger.info("[BTC15] Corridor Trader iniciado", icon="🌙")
        while not self._stop.is_set():
            try:
                self._run_one_window()
            except Exception as exc:
                logger.err(f"[BTC15] window error: {exc}")
                self.state.set_status("error", str(exc))
                if not self._stop.is_set():
                    time.sleep(5.0)

    # ── window cycle ──────────────────────────────────────────────────────────

    def _run_one_window(self) -> None:
        # ── idle when disabled ────────────────────────────────────────────────
        if not self.state.cc_enabled:
            self.state.set_status("idle", "Corridor desactivado")
            logger.transient("[BTC15] Corridor desactivado — esperando 15s")
            time.sleep(15.0)
            return

        now         = time.time()
        T15         = (int(now) // WIN15) * WIN15
        window_ends = T15 + WIN15
        slug15      = f"btc-updown-15m-{T15}"
        slug5       = f"btc-updown-5m-{T15 + ACTION_START}"

        # Already too late for this window? Jump to next.
        if now >= window_ends - 10:
            next_T15 = T15 + WIN15
            logger.info(
                f"[BTC15] ventana casi cerrada — esperando la próxima T15={next_T15}",
                icon="⏭",
            )
            self._sleep_until(float(next_T15) + 2.0)
            return

        logger.info(f"[BTC15] Nueva ventana 15m  {slug15}", icon="🌙")

        # ── load 15m tokens ───────────────────────────────────────────────────
        self.state.set_status("loading_market", f"Cargando {slug15}")
        tokens15 = self._load_market(slug15, T15, max_attempts=30, retry_s=3.0)
        if tokens15 is None:
            logger.warn(f"[BTC15] No se pudo cargar {slug15} — omitiendo ventana")
            self._sleep_until(float(window_ends) + 2.0)
            return

        self.state.set_window(tokens15.slug, tokens15.window_ts, window_duration=WIN15)
        self.state.set_tokens(tokens15.up_token_id, tokens15.down_token_id)
        logger.ok(f"[BTC15] 15m market listo  {slug15}", icon="📈")

        # ── start 15m price feed ──────────────────────────────────────────────
        feed_15m = PriceFeed(
            ws_url=self.cfg.ws_url,
            up_token_id=tokens15.up_token_id,
            down_token_id=tokens15.down_token_id,
            on_price=self._on_price_15m,
            on_status=self.state.set_ws_connected,
        )
        feed_15m.start()

        # ── fetch P0 (BTC 1-min open at T15) ─────────────────────────────────
        self.state.set_status(
            "watching",
            f"Esperando ventana de acción T+{ACTION_START}s | {slug15}",
        )
        P0 = self._retry_binance(lambda: get_btc_bar_open(T15), label="P0", attempts=5)
        if P0 is None:
            logger.warn("[BTC15] Sin datos P0 de Binance — ventana omitida")
            feed_15m.stop()
            self._sleep_until(float(window_ends) + 2.0)
            return

        logger.info(f"[BTC15] P0 = ${P0:,.2f}", icon="📊")

        # ── wait for action window ────────────────────────────────────────────
        action_opens_at = float(T15 + ACTION_START)
        action_ends_at  = float(T15 + ACTION_END)

        while not self._stop.is_set():
            remaining = action_opens_at - time.time()
            if remaining <= 0:
                break
            logger.transient(
                f"[BTC15] ⏳ action window en {int(remaining)}s  "
                f"window_ttl={int(window_ends - time.time())}s"
            )
            time.sleep(min(remaining, 5.0))

        if self._stop.is_set():
            feed_15m.stop()
            return

        # ── fetch P10 + ATR14 (available once T15+600 1-min bar opens) ───────
        P10   = self._retry_binance(lambda: get_btc_bar_open(T15 + ACTION_START), label="P10",   attempts=8)
        atr14 = self._retry_binance(lambda: get_atr14(T15 + ACTION_START),        label="ATR14", attempts=8)

        if P10 is None or atr14 is None or atr14 <= 0:
            logger.warn(
                f"[BTC15] Sin datos P10/ATR14 — P10={P10} ATR14={atr14} — ventana omitida"
            )
            feed_15m.stop()
            self._sleep_until(float(window_ends) + 2.0)
            return

        logger.info(f"[BTC15] P10 = ${P10:,.2f}  ATR14 = ${atr14:,.2f}", icon="📊")

        # ── load 5m tokens ────────────────────────────────────────────────────
        tokens5 = self._load_market(slug5, T15 + ACTION_START, max_attempts=12, retry_s=2.0)
        if tokens5 is None:
            logger.warn(f"[BTC15] No se pudo cargar {slug5} — ventana omitida")
            feed_15m.stop()
            self._sleep_until(float(window_ends) + 2.0)
            return

        with self._5m_lock:
            self._5m_prices.clear()
            self._5m_tokens = tokens5

        # ── start 5m price feed ───────────────────────────────────────────────
        feed_5m = PriceFeed(
            ws_url=self.cfg.ws_url,
            up_token_id=tokens5.up_token_id,
            down_token_id=tokens5.down_token_id,
            on_price=self._on_price_5m,
            on_status=lambda _: None,
        )
        feed_5m.start()
        time.sleep(self.FEED_WARMUP)  # let both feeds stabilise

        # ── action window: evaluate entry gate ────────────────────────────────
        strat  = CorridorCollectorStrategy(self.cfg, self.state)
        fired  = False
        fill15 = fill5 = None

        while not self._stop.is_set():
            now = time.time()
            if now >= action_ends_at or now >= window_ends:
                break
            if fired:
                break

            # Read current asks
            up_ask_15m = self.state.last_up_ask
            dn_ask_15m = self.state.last_down_ask
            with self._5m_lock:
                p5u = self._5m_prices.get("UP")
                p5d = self._5m_prices.get("DOWN")
            up_ask_5m = p5u["ask"] if p5u else None
            dn_ask_5m = p5d["ask"] if p5d else None

            if None in (up_ask_15m, dn_ask_15m, up_ask_5m, dn_ask_5m):
                logger.transient(
                    f"[BTC15] ⏳ esperando precios  "
                    f"15m UP={up_ask_15m} DN={dn_ask_15m}  "
                    f"5m UP={up_ask_5m} DN={dn_ask_5m}"
                )
                time.sleep(2.0)
                continue

            signal = strat.evaluate_entry(
                T15=T15,
                P0=P0, P10=P10, atr14=atr14,
                up_ask_15m=up_ask_15m, dn_ask_15m=dn_ask_15m,
                up_ask_5m=up_ask_5m,   dn_ask_5m=dn_ask_5m,
            )

            if signal is not None:
                hedged, fill15, fill5 = strat.execute_pair(
                    tokens15=tokens15,
                    tokens5=tokens5,
                    signal=signal,
                    T15=T15,
                    window_slug_15m=slug15,
                )
                if hedged:
                    fired = True
                    self.state.set_status(
                        "holding",
                        f"Corridor par abierto | "
                        f"15m-{signal.s15}@{fill15:.4f} + 5m-{signal.s5}@{fill5:.4f}",
                    )
                    break

            # Check if time remains for another evaluation
            remaining_action = action_ends_at - time.time()
            if remaining_action <= self.CHECK_INTERVAL:
                break
            time.sleep(self.CHECK_INTERVAL)

        if not fired:
            logger.info("[BTC15] Sin entrada en la ventana de acción", icon="⏭")
            self.state.set_status("watching", "Ventana observada — sin entrada")

        # ── hold until window end ─────────────────────────────────────────────
        if not self._stop.is_set():
            ttl = window_ends - time.time()
            if ttl > 0:
                logger.transient(
                    f"[BTC15] {'Holding par 🌙' if fired else 'Esperando cierre ventana'} "
                    f"ttl={int(ttl)}s"
                )
            self._sleep_until(float(window_ends) + self.RESOLVE_WAIT)

        # ── clean up feeds ────────────────────────────────────────────────────
        feed_15m.stop()
        feed_5m.stop()
        self.state.set_ws_connected(False)

        # ── resolve corridor trades ───────────────────────────────────────────
        if fired and not self._stop.is_set():
            self._resolve_corridor_trades(tokens15, tokens5)

    # ── resolution ────────────────────────────────────────────────────────────

    def _resolve_corridor_trades(
        self, tokens15: MarketTokens, tokens5: Optional[MarketTokens]
    ) -> None:
        """Resolve open corridor trades from final WebSocket prices."""
        DEFINITIVE = 0.97   # ≥ this → won;  ≤ 1-this → lost

        # Final 15m prices are in state
        final_up_15m = self.state.last_up_price
        final_dn_15m = self.state.last_down_price

        # Final 5m prices are in local dict
        with self._5m_lock:
            p5u = self._5m_prices.get("UP")
            p5d = self._5m_prices.get("DOWN")
        final_up_5m = p5u["mid"] if p5u else None
        final_dn_5m = p5d["mid"] if p5d else None

        open_trades = self.state.find_all_open_trades_for_window(tokens15.slug)
        if not open_trades:
            return

        def _outcome(price: Optional[float]):
            """True=won, False=lost, None=indeterminate."""
            if price is None:
                return None
            if price >= DEFINITIVE:
                return True
            if price <= 1.0 - DEFINITIVE:
                return False
            return None

        for trade in open_trades:
            if trade.strategy != "corridor":
                continue

            # Determine which final price applies to this trade
            tok5_up  = tokens5.up_token_id   if tokens5 else None
            tok5_dn  = tokens5.down_token_id  if tokens5 else None

            if trade.token_id == tokens15.up_token_id:
                final_p = final_up_15m
            elif trade.token_id == tokens15.down_token_id:
                final_p = final_dn_15m
            elif trade.token_id == tok5_up:
                final_p = final_up_5m
            elif trade.token_id == tok5_dn:
                final_p = final_dn_5m
            else:
                final_p = None

            outcome = _outcome(final_p)

            if outcome is None:
                # Can't determine — mark expired
                logger.warn(
                    f"[CORR] trade #{trade.id} precio final indeterminado "
                    f"(price={final_p}) — expirado"
                )
                self.state.resolve_trade(
                    trade.id, "expired", final_p or 0.0, 0.0,
                    note="corridor: precio final no determinado",
                )
            elif outcome:
                pnl = round(trade.shares * 1.0 - trade.cost, 4)
                self.state.resolve_trade(
                    trade.id, "won", final_p, pnl,
                    note=f"corridor won @ {final_p:.4f}",
                )
                logger.ok(
                    f"[CORR] ✅ #{trade.id} WON  side={trade.side}  "
                    f"pnl=${pnl:+.4f}",
                    icon="🌙",
                )
            else:
                pnl = round(-trade.cost, 4)
                self.state.resolve_trade(
                    trade.id, "lost", final_p or 0.0, pnl,
                    note=f"corridor lost @ {final_p:.4f}",
                )
                logger.warn(
                    f"[CORR] ❌ #{trade.id} LOST  side={trade.side}  "
                    f"pnl=${pnl:+.4f}"
                )

        # Update kill switch: check corridor hit rate
        self._update_kill_switch(tokens15.slug)

    def _update_kill_switch(self, slug: str) -> None:
        """Check trailing-30 corridor hit rate; pause entries if < 20%."""
        corridor_trades = [
            t for t in self.state.trades
            if t.strategy == "corridor" and not t.is_hedge
            and t.status in ("won", "lost", "expired")
        ]
        if len(corridor_trades) < 15:
            return  # need minimum 15 resolved pairs before kill switch activates

        recent = corridor_trades[-30:]
        # A corridor hit = BOTH legs of the pair won (the rare $2 payout).
        # Proxy: count windows where at least one "corridor" pair won both legs.
        # Simplified: count slugs where both the leader trade AND the 5m trade are "won".
        slugs = set(t.window_slug for t in recent)
        hits = 0
        for window_slug in slugs:
            pair = [t for t in self.state.trades
                    if t.strategy == "corridor" and t.window_slug == window_slug]
            if all(t.status == "won" for t in pair):
                hits += 1

        rate = hits / max(len(slugs), 1)
        if rate < 0.20:
            self.state.cc_paused = True
            logger.warn(
                f"[CORR] 🔴 KILL SWITCH activado  "
                f"corridor_rate={rate:.0%} ({hits}/{len(slugs)} ventanas)"
            )
        else:
            self.state.cc_paused = False

    # ── price callbacks ───────────────────────────────────────────────────────

    def _on_price_15m(self, side: str, bid: float, ask: float, mid: float) -> None:
        self.state.update_price(side, bid, ask, mid)

    def _on_price_5m(self, side: str, bid: float, ask: float, mid: float) -> None:
        with self._5m_lock:
            self._5m_prices[side] = {"bid": bid, "ask": ask, "mid": mid}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _load_market(
        self,
        slug: str,
        window_ts: int,
        max_attempts: int = 15,
        retry_s: float = 3.0,
    ) -> Optional[MarketTokens]:
        for _ in range(max_attempts):
            if self._stop.is_set():
                return None
            try:
                result = fetch_market(self.cfg.gamma_host, slug)
                if result:
                    return MarketTokens(
                        slug=slug,
                        window_ts=window_ts,
                        up_token_id=result[0],
                        down_token_id=result[1],
                    )
            except Exception as exc:
                logger.warn(f"[BTC15] market load error for {slug}: {exc}")
            time.sleep(retry_s)
        return None

    def _retry_binance(self, fn, label: str, attempts: int = 5):
        for i in range(attempts):
            if self._stop.is_set():
                return None
            val = fn()
            if val is not None:
                return val
            if i < attempts - 1:
                time.sleep(2.0)
        logger.warn(f"[BTC15] {label} no disponible después de {attempts} intentos")
        return None

    def _sleep_until(self, target_unix: float) -> None:
        while not self._stop.is_set():
            remaining = target_unix - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 2.0))
