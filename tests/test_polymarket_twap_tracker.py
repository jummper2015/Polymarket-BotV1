"""Tests for `bot.polymarket_twap_tracker` — per-window TWAP-60s accumulator.

The tracker pulls from the global `coinbase_ticker_feed` buffer (mocked
in tests) and computes the partial TWAP-60s projection for each window.
These tests cover:
  - empty state (insufficient samples → no projected winner)
  - partial state (some ticks, partial required_avg)
  - full state (60 samples → exact required_avg)
  - projected_winner logic (UP vs DOWN based on current_btc vs required_avg)
  - should_hedge logic (only fires when winner ≠ position_side AND margin)
  - pruning of expired windows
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the tracker state before AND after each test."""
    from bot import polymarket_twap_tracker as tr
    tr.clear_all()
    yield
    tr.clear_all()


def _fill_global_feed_with_ticks(ticks):
    """Mock coinbase_ticker_feed's global buffer with the given ticks.

    Each tick: (timestamp_ms, price).
    """
    from bot import coinbase_ticker_feed as feed

    now_ms = int(time.time() * 1000)
    with feed._lock:  # noqa: SLF001
        feed._buffer.clear()
        for ts_ms, price in ticks:
            feed._buffer.append((ts_ms, price))
    feed._last_tick_at = time.time()
    feed._connected = True


def _window_ticks_in_range(start_ts: int, end_ts: int, count: int, base_price: float) -> list:
    """Helper: generate `count` ticks in [start_ts, end_ts] at `base_price`."""
    now_ms = int(time.time() * 1000)
    if count == 1:
        return [(int(end_ts * 1000), base_price)]
    interval = (end_ts - start_ts) * 1000 / (count - 1)
    return [
        (int(start_ts * 1000 + i * interval), base_price)
        for i in range(count)
    ]


# ── get_state: empty / partial / full ───────────────────────────────────────


def test_get_state_empty_when_no_ticks():
    from bot import polymarket_twap_tracker as tr
    from bot import coinbase_ticker_feed as feed

    _fill_global_feed_with_ticks([])  # empty buffer
    feed._connected = False  # is_ready() False → no ticks added

    s = tr.get_state(window_ts=1_789_594_800, current_ts=1_789_595_000, strike=75_000.0)

    assert s.samples_so_far == 0
    assert s.sum_so_far == 0.0
    assert s.avg_so_far == 0.0
    assert s.required_avg is None
    assert s.projected_winner is None
    assert s.margin is None


def test_get_state_partial_with_some_ticks():
    from bot import polymarket_twap_tracker as tr

    # Window: 1789594800 (20:30) → 1789595100 (20:35)
    # TWAP resolution window: [1789595040, 1789595100] (T+240 to T+300)
    # current_ts = 1789595070 (T+270, mid-window of the TWAP window)
    window_ts = 1_789_594_800
    current_ts = window_ts + 270  # 30 ticks into the TWAP resolution window

    # Generate 30 ticks at constant price 76_000.0 in [T+240, T+270]
    ticks = _window_ticks_in_range(window_ts + 240, current_ts, 30, 76_000.0)
    _fill_global_feed_with_ticks(ticks)

    s = tr.get_state(window_ts=window_ts, current_ts=current_ts, strike=75_000.0)

    assert s.samples_so_far == 30
    assert s.sum_so_far == 30 * 76_000.0
    assert s.avg_so_far == pytest.approx(76_000.0)
    assert s.samples_remaining == 30
    # required_avg = (strike * 60 - sum) / remaining
    # = (75_000 * 60 - 30 * 76_000) / 30 = (4_500_000 - 2_280_000) / 30 = 74_000
    assert s.required_avg == pytest.approx(74_000.0)
    # current_btc=76000, required=74000, margin=+2000 → UP winning
    assert s.projected_winner == "UP"
    assert s.margin == pytest.approx(2_000.0)


def test_get_state_full_60_ticks():
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    # Use current_ts = window_ts + 300 so the last tick at the boundary is included.
    current_ts = window_ts + 300

    # 60 ticks at constant 76_000.0 in [T+240, T+300]
    ticks = _window_ticks_in_range(window_ts + 240, window_ts + 300, 60, 76_000.0)
    _fill_global_feed_with_ticks(ticks)

    s = tr.get_state(window_ts=window_ts, current_ts=current_ts, strike=76_000.0)

    assert s.samples_so_far == 60
    assert s.samples_remaining == 0
    # With 0 remaining, required_avg is undefined → None
    assert s.required_avg is None
    # Don't compute projected_winner when samples_remaining is 0
    assert s.projected_winner is None


def test_get_state_projects_DOWN_when_current_below_required():
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    current_ts = window_ts + 270

    # 30 ticks at 75_000 (BELOW strike), current_btc=74_500 (also below)
    ticks = _window_ticks_in_range(window_ts + 240, current_ts, 30, 75_000.0)
    _fill_global_feed_with_ticks(ticks)

    s = tr.get_state(window_ts=window_ts, current_ts=current_ts,
                     strike=75_500.0, current_btc=74_500.0)

    assert s.samples_so_far == 30
    # sum = 30 * 75_000 = 2_250_000
    # required = (75_500 * 60 - 2_250_000) / 30 = (4_530_000 - 2_250_000) / 30 = 76_000
    assert s.required_avg == pytest.approx(76_000.0)
    # current=74500, required=76000, margin=-1500 → DOWN winning
    assert s.projected_winner == "DOWN"
    assert s.margin == pytest.approx(-1_500.0)


# ── current_btc behavior ────────────────────────────────────────────────────


def test_get_state_uses_passed_current_btc_when_provided():
    """current_btc parameter overrides the latest tick in the buffer."""
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    current_ts = window_ts + 270

    # Ticks at 76_000, but current_btc is passed as 77_000
    ticks = _window_ticks_in_range(window_ts + 240, current_ts, 30, 76_000.0)
    _fill_global_feed_with_ticks(ticks)

    s = tr.get_state(window_ts=window_ts, current_ts=current_ts,
                     strike=75_000.0, current_btc=77_000.0)

    assert s.current_btc == 77_000.0
    # margin should be based on current_btc=77000 vs required_avg
    # required = (75_000 * 60 - 30 * 76_000) / 30 = (4_500_000 - 2_280_000) / 30 = 74_000
    assert s.required_avg == pytest.approx(74_000.0)
    assert s.margin == pytest.approx(3_000.0)


def test_get_state_uses_latest_tick_when_current_btc_not_provided():
    """Without current_btc, latest tick in the per-window buffer is used."""
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    current_ts = window_ts + 270

    # Last tick at 76_500, others at 76_000
    base_ticks = _window_ticks_in_range(window_ts + 240, current_ts - 1, 29, 76_000.0)
    last_tick = (int(current_ts * 1000), 76_500.0)
    _fill_global_feed_with_ticks(base_ticks + [last_tick])

    s = tr.get_state(window_ts=window_ts, current_ts=current_ts, strike=75_000.0)

    assert s.current_btc == 76_500.0


# ── should_hedge ─────────────────────────────────────────────────────────────


def test_should_hedge_false_when_no_signal():
    """Empty state → can't project a winner → don't hedge."""
    from bot import polymarket_twap_tracker as tr
    from bot import coinbase_ticker_feed as feed

    _fill_global_feed_with_ticks([])
    feed._connected = False

    assert tr.should_hedge(
        window_ts=1_789_594_800, current_ts=1_789_595_000,
        strike=75_000.0, position_side="UP",
    ) is False


def test_should_hedge_false_when_projection_matches_position():
    """If TWAP projects UP wins and position is UP → no hedge."""
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    current_ts = window_ts + 270
    ticks = _window_ticks_in_range(window_ts + 240, current_ts, 30, 76_000.0)
    _fill_global_feed_with_ticks(ticks)

    # Strike 75000, current_btc 76000 → UP winning
    # Position UP → don't hedge
    assert tr.should_hedge(
        window_ts=window_ts, current_ts=current_ts,
        strike=75_000.0, position_side="UP", current_btc=76_000.0,
    ) is False


def test_should_hedge_true_when_projection_opposes_position():
    """If TWAP projects DOWN wins and position is UP → hedge."""
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    current_ts = window_ts + 270
    ticks = _window_ticks_in_range(window_ts + 240, current_ts, 30, 75_000.0)
    _fill_global_feed_with_ticks(ticks)

    # Strike 75500, current_btc 74500 → DOWN winning
    # Position UP → hedge (DOWN side)
    assert tr.should_hedge(
        window_ts=window_ts, current_ts=current_ts,
        strike=75_500.0, position_side="UP", current_btc=74_500.0,
        margin_threshold=0.0,
    ) is True


def test_should_hedge_respects_margin_threshold():
    """Small opposite projection below threshold → don't hedge."""
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    current_ts = window_ts + 270
    ticks = _window_ticks_in_range(window_ts + 240, current_ts, 30, 75_000.0)
    _fill_global_feed_with_ticks(ticks)

    # Strike 75_100, current_btc 75_000 → DOWN winning by exactly $200
    # (math: required_avg = (75_100*60 - 30*75_000)/30 = 75_200; margin = -200)
    # Position UP, threshold $300 → don't hedge (|margin|=200 < 300)
    assert tr.should_hedge(
        window_ts=window_ts, current_ts=current_ts,
        strike=75_100.0, position_side="UP", current_btc=75_000.0,
        margin_threshold=300.0,
    ) is False

    # Threshold $100 → hedge (|margin|=200 >= 100)
    assert tr.should_hedge(
        window_ts=window_ts, current_ts=current_ts,
        strike=75_100.0, position_side="UP", current_btc=75_000.0,
        margin_threshold=100.0,
    ) is True


# ── Pruning ──────────────────────────────────────────────────────────────────


def test_prune_expired_drops_old_windows():
    """Windows that ended >60s ago should be pruned on next access."""
    from bot import polymarket_twap_tracker as tr

    # Insert a window that ended long ago.
    old_window_ts = 1_700_000_000
    with tr._lock:  # noqa: SLF001
        tr._state[old_window_ts] = __import__("collections").deque()  # noqa: SLF001
        tr._state[old_window_ts].append((1_700_000_000 * 1000, 75_000.0))

    # Insert a recent window.
    recent_window_ts = int(time.time()) - 300
    with tr._lock:  # noqa: SLF001
        tr._state[recent_window_ts] = __import__("collections").deque()  # noqa: SLF001

    # Trigger pruning.
    tr._prune_expired(int(time.time()))  # noqa: SLF001

    with tr._lock:  # noqa: SLF001
        assert old_window_ts not in tr._state  # noqa: SLF001
        assert recent_window_ts in tr._state  # noqa: SLF001


def test_feed_from_ticker_buffer_does_not_duplicate_ticks():
    """Calling feed twice with the same buffer should not double-count ticks."""
    from bot import polymarket_twap_tracker as tr

    window_ts = 1_789_594_800
    current_ts = window_ts + 270
    ticks = _window_ticks_in_range(window_ts + 240, current_ts, 30, 76_000.0)
    _fill_global_feed_with_ticks(ticks)

    added1 = tr.feed_from_ticker_buffer(window_ts, current_ts)
    assert added1 == 30

    added2 = tr.feed_from_ticker_buffer(window_ts, current_ts)
    assert added2 == 0  # nothing new to add

    s = tr.get_state(window_ts=window_ts, current_ts=current_ts, strike=75_000.0)
    assert s.samples_so_far == 30  # not 60


def test_feed_returns_zero_when_global_feed_not_ready():
    """If the global ticker feed isn't ready, no ticks are added."""
    from bot import polymarket_twap_tracker as tr
    from bot import coinbase_ticker_feed as feed

    _fill_global_feed_with_ticks([(int(time.time() * 1000), 76_000.0)])
    feed._connected = False  # not ready

    added = tr.feed_from_ticker_buffer(1_789_594_800, int(time.time()))
    assert added == 0
