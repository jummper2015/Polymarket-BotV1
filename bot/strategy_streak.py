"""Streak Snapper strategy — signal detection + martingale sizing for both forms.

Forma 1 (Fade / Anti-racha):
  - Detect 4+ consecutive same-direction 5-min windows via Binance
  - Signal: bet AGAINST the streak (fade) at limit ≤ ss_fade_limit_cap
  - Martingale ×1.5 on loss, reset on win

Forma 2 (Trend / Seguir tendencia):
  - Measure the last *closed* 4h candle via Binance
  - If it moved at least `ss_trend_min_strength`, lock that side and bet it on
    every 5-min window of the following 4h block
  - The lock outlives the block while the martingale is still recovering: the
    cycle runs until it wins, and only then is the trend re-read
  - Martingale ×`ss_martingale_mult_factor` on loss, reset on win

Both strategies maintain independent martingale states persisted in the DB.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from . import logger
from .binance_api import FOUR_HOURS, get_5min_windows, get_last_closed_4h_candle
from .config import kelly_fraction
from .db import (
    close_cycle,
    get_or_create_martingale_state,
    open_cycle,
    reset_martingale_state,
    advance_martingale_state,
)


# ── Measured accuracy, used only to size a Kelly stake ────────────────────────
# From docs/RUTA.md Fase 8, at the pre-open price plus half the measured 1-cent
# spread: fade wins 53.8% (n=1150, t=+1.32), trend wins 48.2% (n=1725).
#
# Trend's number is below its own price on purpose. Kelly returns 0 for a bet
# with no edge, so selecting `kelly` sizing sizes ss_trend to nothing rather
# than betting a strategy the data says is on the losing side.
MEASURED_WIN_PROB = {
    "ss_fade": 0.538,
    "ss_trend": 0.482,
}

# Never stake more than this share of bankroll on one window, whatever the
# sizing mode computes. A single 5-minute binary is not worth a quarter of the
# account no matter how good the estimate looks.
MAX_BANKROLL_FRACTION = 0.10

# Polymarket rejects dust orders; this is also the floor the original strategy
# used while validating (Revisar Estrategias/RESUMEN_STREAK_SNAPPER.md).
MIN_SHARES = 5.0


# ── Signal dataclass ──────────────────────────────────────────────────────────


@dataclass
class StreakSignal:
    strategy: str       # "ss_fade" | "ss_trend"
    direction: str      # "UP" | "DOWN" — which side to buy
    limit_cap: float    # max entry price
    shares: float       # number of shares to buy (martingale-scaled, fractional)
    multiplier: float   # current martingale multiplier
    loss_streak: int    # consecutive losses before this trade
    signal_reason: str  # human-readable reason (for logging)

    # Set only by the ss_trend signal that would *open* a 4h cycle, and only
    # acted on once the entry is actually on the books (`on_entry`). Committing
    # a side is a claim about a position we hold, so it cannot be written while
    # the signal can still be dropped by the tie-break or refused by the ask,
    # the order or the fill.
    pending_cycle_anchor_ts: Optional[int] = None


# ── Strategy class ────────────────────────────────────────────────────────────


class StreakSnapperStrategy:
    """Signal generator and martingale manager for both Streak Snapper forms."""

    def __init__(self, state) -> None:
        self.state = state
        # Every BotState belongs to exactly one market, so the strategy takes
        # its symbol from there rather than being told twice.
        self.symbol = getattr(state, "symbol", "btc")

        # Set once per extension so the log records the hand-off without
        # repeating it every five minutes for as long as the cycle runs.
        self._extension_logged_for: Optional[int] = None

        # Load persisted martingale states from DB.
        # ss_fade and ss_trend are the only strategies that use martingale;
        # the attributes written here are dynamic (not in BotState.__init__)
        # and are only consumed by this class's own methods.
        self._load_martingale_states()

    def _load_martingale_states(self) -> None:
        """Sync in-memory martingale state from DB (survives restarts).

        Writes ss_fade_* and ss_trend_* attrs dynamically onto BotState.
        These attrs are NOT declared in BotState.__init__ — they are
        private to this class and not exposed on the dashboard.
        """
        try:
            fade_state  = get_or_create_martingale_state("ss_fade",  self.symbol)
            trend_state = get_or_create_martingale_state("ss_trend", self.symbol)
            self.state.ss_fade_martingale_mult    = fade_state.multiplier
            self.state.ss_fade_loss_streak        = fade_state.loss_streak
            self.state.ss_trend_martingale_mult   = trend_state.multiplier
            self.state.ss_trend_loss_streak       = trend_state.loss_streak
            self.state.ss_trend_cycle_side        = trend_state.cycle_side
            self.state.ss_trend_cycle_anchor_ts   = trend_state.cycle_anchor_ts
        except Exception as exc:
            logger.warn(
                f"[SS] no se pudo cargar estado de martingala de la DB: {exc} "
                f"— usando valores por defecto"
            )
            # Safe defaults so the rest of this class never sees missing attrs.
            self.state.ss_fade_martingale_mult    = 1.0
            self.state.ss_fade_loss_streak        = 0
            self.state.ss_trend_martingale_mult   = 1.0
            self.state.ss_trend_loss_streak       = 0
            self.state.ss_trend_cycle_side        = None
            self.state.ss_trend_cycle_anchor_ts   = None

    # ── Sizing ────────────────────────────────────────────────────────────────

    def _size_for(self, strategy: str, limit_cap: float) -> tuple[float, float]:
        """Shares to buy, and the effective multiplier over the base stake.

        Returns (0.0, 0.0) when the entry should be skipped — either because
        Kelly sees no edge, or because the risk ceiling can't accommodate even
        the exchange minimum.

        Sizing happens at `limit_cap` rather than at the fill price: the cap is
        the worst price we'll accept, so a better fill only makes the realised
        stake more conservative than planned. Sizing at a price we haven't been
        quoted yet would be the other way round.
        """
        base = (self.state.ss_fade_base_shares if strategy == "ss_fade"
                else self.state.ss_trend_base_shares)
        mode = getattr(self.state, "ss_sizing", "flat")
        bankroll = self.state.current_bankroll()

        if limit_cap <= 0:
            return 0.0, 0.0

        if mode == "martingale":
            mult = (self.state.ss_fade_martingale_mult if strategy == "ss_fade"
                    else self.state.ss_trend_martingale_mult)
            shares = base * mult
        elif mode == "kelly":
            edge = kelly_fraction(MEASURED_WIN_PROB.get(strategy, 0.0), limit_cap)
            if edge <= 0:
                logger.transient(
                    f"[SS] {strategy}: Kelly no ve ventaja a {limit_cap:.2f} — sin operar"
                )
                return 0.0, 0.0
            shares = bankroll * edge * self.state.ss_kelly_fraction / limit_cap
            mult = shares / base if base else 1.0
        else:
            shares, mult = base, 1.0

        # Risk ceiling, applied after every mode so no path can bypass it.
        max_shares = bankroll * MAX_BANKROLL_FRACTION / limit_cap
        if max_shares < MIN_SHARES:
            logger.warn(
                f"[SS] {strategy}: el mínimo de {MIN_SHARES:.0f} shares a "
                f"{limit_cap:.2f} supera el {MAX_BANKROLL_FRACTION:.0%} del "
                f"bankroll (${bankroll:.2f}) — sin operar"
            )
            return 0.0, 0.0

        capped = max(MIN_SHARES, min(shares, max_shares))
        # Only re-derive the multiplier when the ceiling actually moved the
        # stake — otherwise rounding would report ×3.376 for a ×3.375 cycle.
        if abs(capped - shares) > 1e-9:
            mult = capped / base if base else 1.0
        return round(capped, 2), round(mult, 4)

    # ── Forma 1: Fade signal ──────────────────────────────────────────────────

    def get_fade_signal(self) -> Optional[StreakSignal]:
        """Check if we have 4+ consecutive same-direction windows.
        If so, signal to fade (bet against) the streak.
        """
        windows = get_5min_windows(n=16, symbol=self.symbol)
        if windows is None:
            logger.warn("[SS Fade] sin datos de Binance — omitiendo señal")
            return None

        # Count consecutive same-direction windows from most recent backward
        streak_dir = windows[-1]["direction"]
        streak_len = 0
        for w in reversed(windows):
            if w["direction"] == streak_dir:
                streak_len += 1
            else:
                break

        logger.info(
            f"[SS Fade] streak={streak_len}x {streak_dir}  "
            f"min={self.state.ss_fade_streak_min}"
        )

        if streak_len < self.state.ss_fade_streak_min:
            return None

        fade_dir = "DOWN" if streak_dir == "UP" else "UP"

        cap = self.state.ss_fade_limit_cap
        shares, mult = self._size_for("ss_fade", cap)
        if shares <= 0:
            return None

        return StreakSignal(
            strategy="ss_fade",
            direction=fade_dir,
            limit_cap=cap,
            shares=shares,
            multiplier=mult,
            loss_streak=self.state.ss_fade_loss_streak,
            signal_reason=f"racha {streak_len}x {streak_dir} → fade {fade_dir}",
        )

    # ── Forma 2: Trend signal ─────────────────────────────────────────────────

    def _trend_signal(self, side: str, reason: str) -> Optional[StreakSignal]:
        cap = self.state.ss_trend_limit_cap
        shares, mult = self._size_for("ss_trend", cap)
        if shares <= 0:
            return None
        return StreakSignal(
            strategy="ss_trend",
            direction=side,
            limit_cap=cap,
            shares=shares,
            multiplier=mult,
            loss_streak=self.state.ss_trend_loss_streak,
            signal_reason=reason,
        )

    def _open_trend_cycle(self, side: str, anchor_ts: int) -> None:
        """Persist the locked side, then mirror it in memory."""
        try:
            open_cycle("ss_trend", side, anchor_ts, self.symbol)
        except Exception as exc:
            logger.warn(f"[SS Trend] no se pudo guardar el ciclo en DB: {exc}")
        self.state.ss_trend_cycle_side = side
        self.state.ss_trend_cycle_anchor_ts = anchor_ts
        self._extension_logged_for = None

    def _close_trend_cycle(self) -> None:
        try:
            close_cycle("ss_trend", self.symbol)
        except Exception as exc:
            logger.warn(f"[SS Trend] no se pudo cerrar el ciclo en DB: {exc}")
        self.state.ss_trend_cycle_side = None
        self.state.ss_trend_cycle_anchor_ts = None
        self._extension_logged_for = None

    def get_trend_signal(self) -> Optional[StreakSignal]:
        """Trade one side for the whole 4h block the last closed candle chose.

        The signal comes from the candle that has *finished*, not the one being
        formed: a candle that just opened has close == open and no trend to
        read.  Its direction is then locked for the following four hours, and
        the lock survives past them while the martingale is still recovering —
        the cycle runs until it wins before any new trend is considered.
        """
        candle = get_last_closed_4h_candle(self.symbol)
        if candle is None:
            logger.warn("[SS Trend] sin datos de vela 4h — omitiendo señal")
            return None

        self.state.ss_trend_last_strength = candle["strength"]

        side = self.state.ss_trend_cycle_side
        anchor = self.state.ss_trend_cycle_anchor_ts

        # ── an open cycle decides the side, not the candle ────────────────────
        if side and anchor is not None:
            if time.time() < anchor + 2 * FOUR_HOURS:
                return self._trend_signal(
                    side, f"ciclo 4h {side} (vela {anchor})"
                )

            # Block over. Unrecovered losses keep the side committed: the point
            # of the cycle is to run until it wins, and abandoning it here would
            # book the loss and start a fresh martingale on the opposite side.
            if self.state.ss_trend_martingale_mult > 1.0:
                if self._extension_logged_for != anchor:
                    logger.warn(
                        f"[SS Trend] bloque de la vela {anchor} agotado con "
                        f"×{self.state.ss_trend_martingale_mult:.2f} sin recuperar "
                        f"— se prorroga el ciclo {side} hasta ganar",
                        icon="🔁",
                    )
                    self._extension_logged_for = anchor
                return self._trend_signal(
                    side, f"ciclo {side} prorrogado hasta ganar"
                )

            logger.info(
                f"[SS Trend] bloque de la vela {anchor} completado sin pérdidas "
                f"pendientes — se reevalúa la tendencia",
                icon="✅",
            )
            self._close_trend_cycle()

        # ── no cycle: does the last closed candle call a trend? ───────────────
        strength = candle["strength"]
        min_strength = self.state.ss_trend_min_strength

        if abs(strength) < min_strength:
            logger.transient(
                f"[SS Trend] vela 4h {candle['ts']} sin tendencia clara: "
                f"{strength * 100:+.3f}% < {min_strength * 100:.3f}% — sin operar"
            )
            return None

        trend_dir = candle["direction"]
        logger.info(
            f"[SS Trend] tendencia clara en la vela 4h {candle['ts']}: "
            f"{candle['open']:.2f} → {candle['close']:.2f} "
            f"({strength * 100:+.3f}%) → se opera {trend_dir} durante 4h",
            icon="📈",
        )
        # The cycle is *not* opened here. This signal still has to survive the
        # tie-break against fade, the ask-vs-cap gate, the order and the fill;
        # writing the locked side now would commit ss_trend to a direction for
        # four hours on the strength of a position it may never take.
        signal = self._trend_signal(
            trend_dir, f"tendencia 4h {trend_dir} ({strength * 100:+.2f}%)"
        )
        if signal is not None:
            signal.pending_cycle_anchor_ts = int(candle["ts"])
        return signal

    def on_entry(self, sig: StreakSignal) -> None:
        """Commit what the signal only proposed, now that the entry exists.

        Called by the trader once the position is on the books. Only the
        ss_trend signal that opens a block carries an anchor; every other signal
        — including ss_trend inside an already-open cycle — is a no-op here.
        """
        if sig.pending_cycle_anchor_ts is None:
            return
        self._open_trend_cycle(sig.direction, sig.pending_cycle_anchor_ts)

    # ── Martingale hooks ──────────────────────────────────────────────────────
    # ss_fade and ss_trend are the only strategies that carry martingale state.
    # Box Builder, Coin-Flip Dog, Temporal Arb and Near-Resolution Capture use
    # flat sizing: calling these hooks for them is a no-op except for the
    # unnecessary DB call, so we guard the live state writes with an explicit
    # check.  The DB helper still runs (no harm in a reset on an untracked
    # strategy — it creates a row with multiplier=1 and loss_streak=0).

    _FADE_TREND = frozenset({"ss_fade", "ss_trend"})

    def on_win(self, strategy: str) -> None:
        """Reset martingale multiplier to 1.0 after a winning trade.

        Only ss_fade and ss_trend carry martingale state.  All other active
        strategies (box_builder, coin_flip_dog, temporal_arb, near_res) use
        flat sizing — calling this for them is a no-op beyond the log line.
        """
        if strategy not in self._FADE_TREND:
            # Non-martingale strategy: nothing to update, no log noise.
            return

        try:
            reset_martingale_state(strategy, self.symbol)
        except Exception as exc:
            logger.warn(f"[SS] DB reset martingale ({strategy}) failed: {exc}")

        if strategy == "ss_fade":
            self.state.ss_fade_martingale_mult = 1.0
            self.state.ss_fade_loss_streak = 0
            logger.ok(f"[SS Fade] 🎯 GANÓ → multiplicador reseteado a 1.0", icon="💰")
        else:
            self.state.ss_trend_martingale_mult = 1.0
            self.state.ss_trend_loss_streak = 0
            self.state.ss_trend_cycle_side = None
            self.state.ss_trend_cycle_anchor_ts = None
            self._extension_logged_for = None
            logger.ok(
                f"[SS Trend] 🎯 GANÓ → ciclo cerrado, multiplicador a 1.0",
                icon="💰",
            )

    def on_loss(self, strategy: str) -> None:
        """Multiply the martingale by the configured factor after a loss.

        Only ss_fade and ss_trend carry martingale state; all other strategies
        use flat sizing and are silently ignored here.
        """
        if strategy not in self._FADE_TREND:
            return

        factor = self.state.ss_martingale_mult_factor
        try:
            advance_martingale_state(strategy, factor, self.symbol)
        except Exception as exc:
            logger.warn(f"[SS] DB advance martingale ({strategy}) failed: {exc}")

        if strategy == "ss_fade":
            self.state.ss_fade_martingale_mult = round(
                self.state.ss_fade_martingale_mult * factor, 4
            )
            self.state.ss_fade_loss_streak += 1
            logger.warn(
                f"[SS Fade] ❌ PERDIÓ → nuevo multiplicador: "
                f"×{self.state.ss_fade_martingale_mult:.2f}  "
                f"(racha {self.state.ss_fade_loss_streak} pérdidas)",
                icon="📉",
            )
        else:
            self.state.ss_trend_martingale_mult = round(
                self.state.ss_trend_martingale_mult * factor, 4
            )
            self.state.ss_trend_loss_streak += 1
            logger.warn(
                f"[SS Trend] ❌ PERDIÓ → nuevo multiplicador: "
                f"×{self.state.ss_trend_martingale_mult:.2f}  "
                f"(racha {self.state.ss_trend_loss_streak} pérdidas)",
                icon="📉",
            )
