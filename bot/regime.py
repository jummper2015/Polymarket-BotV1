"""Regime filters — when NOT to trade.

The fade signal is measured at +3.74% per entry over 35 days, but that average
hides a wide spread by regime (docs/RUTA.md, Fase 8):

    franja US 13-21h UTC   +9.22%   (positivo en los 4 tramos de 8,8 días)
    franja Europa 08-13h   -5.00%
    volatilidad media      +6.44%
    volatilidad MUY baja   -0.46%   ← la intuición "operar en calma" falla aquí
    rango de 2h estrecho   +6.06%

The lowest-volatility quartile being the *worst* is the counterintuitive part
and the reason these filters are expressed as bands rather than ceilings: in a
dead-flat market a streak of four windows is noise, not an overextension.

None of these reach statistical significance — roughly 20 filters were tested,
so the best of them looks good by construction. They all default to OFF, and the
dashboard records the skip reason so "with filter" can be compared against
"without" on live data rather than on the sample they were chosen from.

Everything here is a pure function over candle dicts
(`{ts, open, high, low, close}`) so it can be tested without network access.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional, Sequence

# ATR over 12 five-minute candles = 1 hour, matching the original strategy's
# definition (Revisar Estrategias/RESUMEN_STREAK_SNAPPER.md).
ATR_WINDOWS = 12
RANGE_WINDOWS = 24          # 2 hours

# How much history the percentile is computed over. Two days: long enough to
# describe the current regime, short enough to follow it when it shifts. Fixed
# thresholds were rejected because the volatility regime moves and a constant
# calibrated today silently stops meaning the same thing.
PERCENTILE_LOOKBACK = 576   # 2 days of 5-min candles


class RegimeVerdict:
    """Whether to trade this window, and why not when the answer is no."""

    __slots__ = ("allowed", "reason", "detail")

    def __init__(self, allowed: bool, reason: str = "", detail: str = "") -> None:
        self.allowed = allowed
        self.reason = reason      # short machine-readable tag, e.g. "SKIP_HOURS"
        self.detail = detail      # human-readable, goes to the log

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:
        return f"RegimeVerdict(allowed={self.allowed}, reason={self.reason!r})"


ALLOWED = RegimeVerdict(True)


# ── parsing ───────────────────────────────────────────────────────────────────


def parse_hours(spec: str) -> list[tuple[int, int]]:
    """Parse "13-21,22-24" into [(13, 21), (22, 24)]. Empty spec = no restriction.

    Ranges are half-open [start, end) in UTC hours. A range that wraps midnight
    ("22-2") is split into two so the membership test stays a simple comparison.
    Invalid fragments are dropped rather than raising: this value arrives from a
    text box, and a typo should narrow nothing rather than crash the trader.
    """
    ranges: list[tuple[int, int]] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            continue
        lo_s, _, hi_s = chunk.partition("-")
        try:
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            continue
        if not (0 <= lo <= 24 and 0 <= hi <= 24) or lo == hi:
            continue
        if lo < hi:
            ranges.append((lo, hi))
        else:
            # Wraps midnight: 22-2 becomes [22,24) and [0,2).
            ranges.append((lo, 24))
            ranges.append((0, hi))
    return ranges


def is_valid_hours_spec(spec: str) -> bool:
    """True when every non-empty fragment parsed. Used by config validation."""
    fragments = [c for c in (spec or "").split(",") if c.strip()]
    return len(parse_hours(spec)) >= len(fragments)


# ── measurements ──────────────────────────────────────────────────────────────


def _rel_move(candle: dict) -> float:
    open_px = candle.get("open") or 0.0
    if open_px <= 0:
        return 0.0
    return abs((candle["close"] - open_px) / open_px)


def atr(candles: Sequence[dict], windows: int = ATR_WINDOWS) -> Optional[float]:
    """Mean absolute relative move over the last `windows` candles."""
    seg = list(candles)[-windows:]
    if len(seg) < windows:
        return None
    return sum(_rel_move(c) for c in seg) / len(seg)


def price_range(candles: Sequence[dict], windows: int = RANGE_WINDOWS) -> Optional[float]:
    """(high - low) / last close over the last `windows` candles."""
    seg = list(candles)[-windows:]
    if len(seg) < windows:
        return None
    close = seg[-1].get("close") or 0.0
    if close <= 0:
        return None
    highs = [c.get("high", c["close"]) for c in seg]
    lows = [c.get("low", c["close"]) for c in seg]
    return (max(highs) - min(lows)) / close


def _rolling(candles: Sequence[dict], windows: int, fn, lookback: int) -> list[float]:
    """`fn` evaluated at every position in the last `lookback` candles."""
    out: list[float] = []
    n = len(candles)
    start = max(windows, n - lookback)
    for end in range(start, n + 1):
        value = fn(candles[end - windows:end], windows)
        if value is not None:
            out.append(value)
    return out


def percentile_of(values: Iterable[float], target: float) -> float:
    """Percentile rank (0-100) of `target` within `values`."""
    vals = sorted(values)
    if not vals:
        return 50.0
    below = sum(1 for v in vals if v < target)
    return 100.0 * below / len(vals)


# ── filters ───────────────────────────────────────────────────────────────────


def hours_filter(spec: str, now: Optional[float] = None) -> RegimeVerdict:
    """Allow only windows opening inside one of the configured UTC ranges."""
    ranges = parse_hours(spec)
    if not ranges:
        return ALLOWED

    hour = time.gmtime(now if now is not None else time.time()).tm_hour
    if any(lo <= hour < hi for lo, hi in ranges):
        return ALLOWED
    return RegimeVerdict(
        False, "SKIP_HOURS",
        f"hora {hour:02d}h UTC fuera de las franjas permitidas ({spec})",
    )


def volatility_filter(
    candles: Sequence[dict], min_pct: float = 0.0, max_pct: float = 100.0
) -> RegimeVerdict:
    """Allow only when 1h ATR sits between two percentiles of its own history.

    A band, not a ceiling: the bottom quartile measured worse than no filter at
    all, so "as calm as possible" is the wrong target.
    """
    if min_pct <= 0.0 and max_pct >= 100.0:
        return ALLOWED

    current = atr(candles)
    if current is None:
        return ALLOWED   # not enough history to judge; don't block on ignorance

    history = _rolling(candles, ATR_WINDOWS, atr, PERCENTILE_LOOKBACK)
    if len(history) < ATR_WINDOWS * 2:
        return ALLOWED

    pct = percentile_of(history, current)
    if min_pct <= pct <= max_pct:
        return ALLOWED
    return RegimeVerdict(
        False, "SKIP_VOL",
        f"volatilidad en el percentil {pct:.0f} (banda {min_pct:.0f}-{max_pct:.0f})",
    )


def range_filter(candles: Sequence[dict], max_pct: float = 100.0) -> RegimeVerdict:
    """Allow only when the 2h range is below a percentile of its own history."""
    if max_pct >= 100.0:
        return ALLOWED

    current = price_range(candles)
    if current is None:
        return ALLOWED

    history = _rolling(candles, RANGE_WINDOWS, price_range, PERCENTILE_LOOKBACK)
    if len(history) < RANGE_WINDOWS * 2:
        return ALLOWED

    pct = percentile_of(history, current)
    if pct <= max_pct:
        return ALLOWED
    return RegimeVerdict(
        False, "SKIP_RANGE",
        f"rango de 2h en el percentil {pct:.0f} (máximo {max_pct:.0f})",
    )


def evaluate(
    candles: Sequence[dict],
    *,
    hours_spec: str = "",
    vol_min_pct: float = 0.0,
    vol_max_pct: float = 100.0,
    range_max_pct: float = 100.0,
    now: Optional[float] = None,
) -> RegimeVerdict:
    """Run every configured filter. First rejection wins.

    Hours is checked first because it needs no candles — a window outside the
    permitted session is skipped even if Binance is unreachable.
    """
    verdict = hours_filter(hours_spec, now=now)
    if not verdict.allowed:
        return verdict

    verdict = volatility_filter(candles, vol_min_pct, vol_max_pct)
    if not verdict.allowed:
        return verdict

    return range_filter(candles, range_max_pct)
