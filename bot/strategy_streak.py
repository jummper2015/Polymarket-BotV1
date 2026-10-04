"""ME$IRVE strategy — signal detection (fade + trend).

Forma 1 (Fade / Anti-racha):
  - Detect 4+ consecutive same-direction 5-min windows via Coinbase
  - Signal: bet AGAINST the streak (fade) at limit ≤ ss_fade_limit_cap

Forma 2 (Trend / Seguir tendencia):
  - Measure the last *closed* 4h candle via Coinbase
  - If it moved at least `ss_trend_min_strength`, signal that side for the
    current window

Sizing modes: `flat` (default), `kelly`. Martingale was removed 2026-09-15 —
the trade table still carries `multiplier` and `loss_streak` columns from
the legacy schema but they always read as 1.0 / 0 now. The 4h block cycle
that ss_trend used to lock the same side for the whole block was retired
at the same time: every signal is fresh per-window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from . import logger
from .coinbase_api import get_5min_windows, get_last_closed_4h_candle
from .config import kelly_fraction


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
    shares: float       # number of shares to buy
    multiplier: float = 1.0   # always 1.0 post-martingale removal; column kept for schema compat
    loss_streak: int = 0      # always 0 post-martingale removal; column kept for schema compat
    signal_reason: str = ""   # human-readable reason (for logging)


# ── Strategy class ────────────────────────────────────────────────────────────


class StreakSnapperStrategy:
    """Signal generator for the ME$IRVE fade + trend signal paths.

    ss_fade and ss_trend are kept here as back-compat signal paths even
    though the live bot no longer trades them (Fase 8 retirement; only
    `temporal_arb` is active in production). The trader still calls this
    class to build StreakSignals when other strategies are disabled.
    """

    def __init__(self, state) -> None:
        self.state = state
        # Every BotState belongs to exactly one market, so the strategy takes
        # its symbol from there rather than being told twice.
        self.symbol = getattr(state, "symbol", "btc")

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

        if mode == "kelly":
            edge = kelly_fraction(MEASURED_WIN_PROB.get(strategy, 0.0), limit_cap)
            if edge <= 0:
                logger.transient(
                    f"[M$] {strategy}: Kelly no ve ventaja a {limit_cap:.2f} — sin operar"
                )
                return 0.0, 0.0
            shares = bankroll * edge * self.state.ss_kelly_fraction / limit_cap
            mult = shares / base if base else 1.0
        else:
            # flat (or any unknown mode): stake = base, multiplier = 1.0
            shares = base
            mult = 1.0

        # Risk ceiling, applied after every mode so no path can bypass it.
        max_shares = bankroll * MAX_BANKROLL_FRACTION / limit_cap
        if max_shares < MIN_SHARES:
            logger.warn(
                f"[M$] {strategy}: el mínimo de {MIN_SHARES:.0f} shares a "
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
            logger.warn("[M$ Fade] sin datos de Coinbase — omitiendo señal")
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
            f"[M$ Fade] streak={streak_len}x {streak_dir}  "
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
            loss_streak=0,
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
            loss_streak=0,
            signal_reason=reason,
        )

    def get_trend_signal(self) -> Optional[StreakSignal]:
        """Trade the direction of the last closed 4h candle for one window.

        The signal comes from the candle that has *finished*, not the one being
        formed: a candle that just opened has close == open and no trend to
        read. The 4h block cycle was removed together with the martingale
        cleanup (2026-09-15) — every signal is fresh per-window now.
        """
        candle = get_last_closed_4h_candle(self.symbol)
        if candle is None:
            logger.warn("[M$ Trend] sin datos de vela 4h — omitiendo señal")
            return None

        self.state.ss_trend_last_strength = candle["strength"]
        strength = candle["strength"]
        min_strength = self.state.ss_trend_min_strength

        if abs(strength) < min_strength:
            logger.transient(
                f"[M$ Trend] vela 4h {candle['ts']} sin tendencia clara: "
                f"{strength * 100:+.3f}% < {min_strength * 100:.3f}% — sin operar"
            )
            return None

        trend_dir = candle["direction"]
        logger.info(
            f"[M$ Trend] tendencia clara en la vela 4h {candle['ts']}: "
            f"{candle['open']:.2f} → {candle['close']:.2f} "
            f"({strength * 100:+.3f}%) → señal {trend_dir}",
            icon="📈",
        )
        return self._trend_signal(
            trend_dir, f"tendencia 4h {trend_dir} ({strength * 100:+.2f}%)"
        )

    # ── No martingale / cycle hooks remain. ss_fade and ss_trend are
    # legacy signal paths kept for backtest compatibility; the trader no
    # longer calls on_win / on_loss / on_entry on this class.
