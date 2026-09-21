"""Per-window TWAP-60s accumulator for Temporal Arb hedge decisions.

Polymarket's 5-min up/down markets resolve on the average of the LAST 60
seconds of the window (the official Chainlink btc-usd-twap-60s stream at
close time, applied to [T+240, T+300]). The strike set at window OPEN
tells us who would win if the resolution were a spot snapshot, but the
real resolution is the time-weighted average of the closing minute.

This module tracks the partial TWAP-60s as ticks arrive, so the bot can
decide in the last 60 seconds whether the current TWAP projection favors
its open position (hold) or the opposite side (hedge early, before the
price drop becomes catastrophic).

## Math

At time `t` in [T+240, T+300]:
  - `samples_so_far` = number of ticks in [T+240, t]
  - `accumulated_sum` = sum of those tick prices
  - `samples_remaining` = 60 - samples_so_far
  - `required_avg_for_up` = (strike * 60 - accumulated_sum) / samples_remaining
  - if current_btc > required_avg_for_up → UP will win
  - if current_btc < required_avg_for_up → DOWN will win

## Integration

The tracker is fed from the same ticker buffer that powers the local TWAP
strike source (`bot.coinbase_ticker_feed`). The Temporal Arb strategy calls
`get_state(window_ts, current_ts, strike)` from its `_observe` tick when
`secs < 60`, and uses the projected winner to decide whether to hedge.

## Lifetime

State is keyed by `(window_ts, current_ts)` and lives only during the
window's 60-second TWAP accumulation period. Cleanup is automatic: state
older than `window_ts + 300` is pruned on each access.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from . import coinbase_ticker_feed

# The resolution window is the LAST 60 seconds of each 5-min window.
_TWAP_WINDOW_SECONDS = 60
_WINDOW_SECONDS = 300

# State: per-window_ts → deque of (timestamp_ms, price) for ticks in
# the resolution window. Auto-pruned when the window expires.
_state: Dict[int, Deque[Tuple[int, float]]] = {}
_lock = threading.Lock()

# Module-level so the bot can use it without re-importing.
_last_state = None  # type: ignore[var-annotated]


@dataclass
class TWAPState:
    """Snapshot of the partial TWAP-60s for one window at one moment."""
    window_ts:        int
    current_ts:       int
    samples_so_far:   int          # ticks accumulated in [T+240, current_ts]
    sum_so_far:       float        # sum of those ticks
    avg_so_far:       float        # mean of accumulated ticks (None if no samples)
    current_btc:      Optional[float]  # latest BTC price seen
    samples_remaining: int         # 60 - samples_so_far
    required_avg:     Optional[float]  # avg needed for remaining samples so final TWAP-60s ≥ strike
    projected_winner: Optional[str]    # "UP" | "DOWN" | None (insufficient data)
    margin:           Optional[float]  # current_btc - required_avg; positive = UP favored

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"TWAPState(samples={self.samples_so_far}/60, "
            f"avg_so_far={self.avg_so_far}, "
            f"required={self.required_avg}, "
            f"winner={self.projected_winner}, margin={self.margin})"
        )


def _prune_expired(current_ts: int) -> None:
    """Drop state for windows that ended more than 60s ago."""
    with _lock:
        expired = [w for w in _state if w + _WINDOW_SECONDS + 60 < current_ts]
        for w in expired:
            del _state[w]


def _window_ticks(window_ts: int) -> Deque[Tuple[int, float]]:
    """Return (creating if needed) the per-window tick buffer."""
    if window_ts not in _state:
        _state[window_ts] = deque()
    return _state[window_ts]


def feed_from_ticker_buffer(window_ts: int, current_ts: int) -> int:
    """Pull ticks from the global ticker buffer that fall in this window's
    TWAP resolution period [window_ts+240, current_ts].

    Returns the number of ticks added. Called by the bot's observe tick
    OR by a background sync, depending on architecture. Cheap (just a
    filter over the rolling buffer).

    No-op if the global feed isn't ready.
    """
    if not coinbase_ticker_feed.is_ready():
        return 0

    start_ms = (window_ts + _WINDOW_SECONDS - _TWAP_WINDOW_SECONDS) * 1000  # T+240
    end_ms = current_ts * 1000

    target = _window_ticks(window_ts)

    with coinbase_ticker_feed._lock:  # noqa: SLF001 — intentional cross-module access
        source = coinbase_ticker_feed._buffer  # noqa: SLF001
        new_ticks = [(t, p) for t, p in source if start_ms <= t <= end_ms]

    added = 0
    if new_ticks:
        # Avoid duplicates: build a set of existing timestamps in this window's buffer.
        with _lock:
            existing_ts = {t for t, _ in target}
            for t, p in new_ticks:
                if t not in existing_ts:
                    target.append((t, p))
                    existing_ts.add(t)
                    added += 1

    _prune_expired(current_ts)
    return added


def get_state(
    window_ts: int,
    current_ts: int,
    strike: Optional[float],
    current_btc: Optional[float] = None,
) -> TWAPState:
    """Snapshot the partial TWAP-60s state for one window at one moment.

    `current_btc` may be passed in (latest tick from price feed); if None,
    the latest tick in this window's buffer is used.
    """
    feed_from_ticker_buffer(window_ts, current_ts)

    with _lock:
        target = _state.get(window_ts, deque())
        ticks = list(target)
        samples_so_far = len(ticks)

    if samples_so_far == 0:
        return TWAPState(
            window_ts=window_ts,
            current_ts=current_ts,
            samples_so_far=0,
            sum_so_far=0.0,
            avg_so_far=0.0,  # type: ignore[arg-type]
            current_btc=current_btc,
            samples_remaining=_TWAP_WINDOW_SECONDS,
            required_avg=None,
            projected_winner=None,
            margin=None,
        )

    sum_so_far = sum(p for _, p in ticks)
    avg_so_far = sum_so_far / samples_so_far

    # If current_btc wasn't supplied, use the latest tick in this window.
    if current_btc is None:
        current_btc = ticks[-1][1]

    samples_remaining = _TWAP_WINDOW_SECONDS - samples_so_far

    required_avg = None
    projected_winner = None
    margin = None
    if strike is not None and strike > 0 and samples_remaining > 0:
        # Required average so that final TWAP-60s ≥ strike (UP wins).
        # final_twap = (sum_so_far + required_avg * samples_remaining) / 60
        # final_twap ≥ strike  →  required_avg ≥ (strike * 60 - sum_so_far) / samples_remaining
        required_avg = (strike * 60.0 - sum_so_far) / samples_remaining
        margin = current_btc - required_avg
        if margin > 0:
            projected_winner = "UP"
        elif margin < 0:
            projected_winner = "DOWN"
        else:
            projected_winner = None  # exact tie — shouldn't happen with floats

    return TWAPState(
        window_ts=window_ts,
        current_ts=current_ts,
        samples_so_far=samples_so_far,
        sum_so_far=sum_so_far,
        avg_so_far=avg_so_far,
        current_btc=current_btc,
        samples_remaining=samples_remaining,
        required_avg=required_avg,
        projected_winner=projected_winner,
        margin=margin,
    )


def get_rolling_twap(
    current_ts: int,
    lookback_seconds: int = 60,
) -> Optional[float]:
    """TWAP of BTC ticks in `[current_ts − lookback, current_ts]`.

    Unlike `get_state` (which tracks the resolution window [T+240, T+300]),
    this computes a rolling TWAP over **any** recent interval. Use it during
    the entry window (secs < 240) where no resolution TWAP exists yet.

    Smoother than spot (which can whipsaw 30 ticks in 60s) but more
    responsive than the final TWAP-60s. Falls back to None if the feed
    isn't ready or there are fewer than ~10 ticks in the window — in those
    cases the caller should use spot.
    """
    if not coinbase_ticker_feed.is_ready():
        return None

    start_ms = (int(current_ts) - int(lookback_seconds)) * 1000
    end_ms = int(current_ts) * 1000

    with coinbase_ticker_feed._lock:  # noqa: SLF001 — intentional cross-module
        relevant = [(t, p) for t, p in coinbase_ticker_feed._buffer
                   if start_ms <= t <= end_ms]

    # Below ~10 samples the average is too noisy to be useful — return None
    # so the caller can fall back to spot.
    if len(relevant) < 10:
        return None

    prices = [p for _, p in relevant]
    return sum(prices) / len(prices)


def should_hedge(
    window_ts: int,
    current_ts: int,
    strike: Optional[float],
    position_side: str,           # "UP" | "DOWN"
    current_btc: Optional[float] = None,
    margin_threshold: float = 0.0,
) -> bool:
    """True when the projected TWAP-60s winner is opposite to `position_side`
    by at least `margin_threshold` dollars.

    `margin_threshold` is in dollars (not %). The current_btc must be at
    least that many dollars below `required_avg` for the opposite side to
    be the projected winner — gives the bot a hysteresis band so it
    doesn't churn on small TWAP swings.

    Returns False when the projected winner can't be determined yet
    (insufficient samples).
    """
    s = get_state(window_ts, current_ts, strike, current_btc)
    if s.projected_winner is None:
        return False
    return s.projected_winner != position_side and (
        s.margin is not None and abs(s.margin) >= margin_threshold
    )


def clear_all() -> None:
    """Wipe all per-window state. For tests."""
    with _lock:
        _state.clear()
