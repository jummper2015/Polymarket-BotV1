"""Streak Snapper strategy — signal detection + martingale sizing for both forms.

Forma 1 (Fade / Anti-racha):
  - Detect 4+ consecutive same-direction 5-min windows via Binance
  - Signal: bet AGAINST the streak (fade) at limit ≤ ss_fade_limit_cap
  - Martingale ×1.5 on loss, reset on win

Forma 2 (Trend / Seguir tendencia):
  - Detect 4h candle direction via Binance (close > open → UP, else DOWN)
  - Signal: bet WITH the 4h trend, EVERY 5-min window
  - Martingale ×1.5 on loss, reset on win

Both strategies maintain independent martingale states persisted in the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import logger
from .binance_api import get_5min_windows, get_4h_trend_cached
from .db import (
    get_or_create_martingale_state,
    reset_martingale_state,
    advance_martingale_state,
)


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


# ── Strategy class ────────────────────────────────────────────────────────────


class StreakSnapperStrategy:
    """Signal generator and martingale manager for both Streak Snapper forms."""

    def __init__(self, state) -> None:
        self.state = state

        # Cached 4h trend (refetched when the candle timestamp changes)
        self._last_4h_ts: Optional[int] = None
        self._cached_trend_dir: Optional[str] = None

        # Load persisted martingale states from DB
        self._load_martingale_states()

    def _load_martingale_states(self) -> None:
        """Sync in-memory martingale state from DB (survives restarts)."""
        try:
            fade_state = get_or_create_martingale_state("ss_fade")
            trend_state = get_or_create_martingale_state("ss_trend")
            self.state.ss_fade_martingale_mult = fade_state.multiplier
            self.state.ss_fade_loss_streak = fade_state.loss_streak
            self.state.ss_trend_martingale_mult = trend_state.multiplier
            self.state.ss_trend_loss_streak = trend_state.loss_streak
        except Exception as exc:
            logger.warn(f"[SS] could not load martingale states from DB: {exc} — using defaults")

    # ── Forma 1: Fade signal ──────────────────────────────────────────────────

    def get_fade_signal(self) -> Optional[StreakSignal]:
        """Check if we have 4+ consecutive same-direction windows.
        If so, signal to fade (bet against) the streak.
        """
        windows = get_5min_windows(n=16)
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

        mult = self.state.ss_fade_martingale_mult
        shares = max(5.0, round(self.state.ss_fade_base_shares * mult, 2))

        return StreakSignal(
            strategy="ss_fade",
            direction=fade_dir,
            limit_cap=self.state.ss_fade_limit_cap,
            shares=shares,
            multiplier=mult,
            loss_streak=self.state.ss_fade_loss_streak,
            signal_reason=f"racha {streak_len}x {streak_dir} → fade {fade_dir}",
        )

    # ── Forma 2: Trend signal ─────────────────────────────────────────────────

    def get_trend_signal(self) -> Optional[StreakSignal]:
        """Get the current 4h trend direction. Signal on EVERY 5-min window."""
        trend = get_4h_trend_cached(self._last_4h_ts)
        if trend is None:
            logger.warn("[SS Trend] sin datos de vela 4h — omitiendo señal")
            return None

        # The 4h candle is still forming, so its direction can flip mid-candle.
        # Always trade the CURRENT direction — latching the first reading meant
        # following a stale trend for up to four hours.
        trend_dir = trend["direction"]

        if trend["ts"] != self._last_4h_ts:
            logger.info(
                f"[SS Trend] nueva vela 4h  ts={trend['ts']}  "
                f"open={trend['open']:.2f}  close={trend['close']:.2f}  "
                f"→ {trend_dir}"
            )
        elif trend_dir != self._cached_trend_dir:
            logger.info(
                f"[SS Trend] la vela 4h en curso giró  "
                f"{self._cached_trend_dir} → {trend_dir}  "
                f"(open={trend['open']:.2f}  close={trend['close']:.2f})"
            )

        self._last_4h_ts = trend["ts"]
        self._cached_trend_dir = trend_dir

        mult = self.state.ss_trend_martingale_mult
        shares = max(5.0, round(self.state.ss_trend_base_shares * mult, 2))

        return StreakSignal(
            strategy="ss_trend",
            direction=trend_dir,
            limit_cap=self.state.ss_trend_limit_cap,
            shares=shares,
            multiplier=mult,
            loss_streak=self.state.ss_trend_loss_streak,
            signal_reason=f"tendencia 4h {trend_dir}",
        )

    # ── Martingale hooks ──────────────────────────────────────────────────────

    def on_win(self, strategy: str) -> None:
        """Reset martingale multiplier to 1.0 after a winning trade."""
        try:
            reset_martingale_state(strategy)
        except Exception as exc:
            logger.warn(f"[SS] DB reset martingale ({strategy}) failed: {exc}")

        if strategy == "ss_fade":
            self.state.ss_fade_martingale_mult = 1.0
            self.state.ss_fade_loss_streak = 0
            logger.ok(f"[SS Fade] 🎯 GANÓ → multiplicador reseteado a 1.0", icon="💰")
        else:
            self.state.ss_trend_martingale_mult = 1.0
            self.state.ss_trend_loss_streak = 0
            logger.ok(f"[SS Trend] 🎯 GANÓ → multiplicador reseteado a 1.0", icon="💰")

    def on_loss(self, strategy: str) -> None:
        """Multiply the martingale by the configured factor after a loss."""
        factor = self.state.ss_martingale_mult_factor
        try:
            advance_martingale_state(strategy, factor)
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
