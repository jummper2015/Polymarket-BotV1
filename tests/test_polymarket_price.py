"""Tests for `bot.polymarket_price` — strike sourcing (Coinbase primary).

## History

- **v1 (2026-09-11)**: Polymarket `crypto-price` endpoint used as primary.
- **v2 (2026-09-13)**: Polymarket returns 30-min aggregates, not 5-min strikes.
  Detection logic added (compare against previous-window strike) with Coinbase
  fallback.
- **v3 (2026-09-13)**: Inverted priority — Coinbase is now PRIMARY
  because it always returns a per-window 5-min candle close. Polymarket is
  only a fallback when Coinbase fails AND a sanity cross-check (warn if its
  openPrice differs by >1% from the Coinbase strike). The v2 detection logic
  is kept as a safety net for the fallback path.
- **v4 (2026-09-16)**: Switched Coinbase helper from `get_5min_candle_close_at`
  (close of the prior candle) to `get_5min_candle_open_at` (open of the candle
  that *starts* at window_ts). The previous close-based approach drifted ~5s
  off the boundary; open is the exact boundary tick that matches Polymarket's
  "price at the beginning of the range" rule. Cross-check is now Coinbase-open
  vs Chainlink-latest (>0.3% gap logs a warning).

These tests pin down the v4 behavior so the bug cannot regress silently.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# Fixed boundary timestamps — Unix-seconds aligned to 5-min grid.
TS_PREV = 1_700_000_000  # some previous 5-min boundary
TS_CURR = TS_PREV + 300
STRIKE_PREV = 70_000.0    # cached strike for the previous window
STRIKE_FROM_COINBASE = 70_100.0  # what Coinbase returns for the current window
# Chainlink's latest round for cross-check scenarios.
CHAINLINK_AGREEING = STRIKE_FROM_COINBASE  # matches Coinbase → no warning
CHAINLINK_OFF_BY_A_LOT = 69_000.0  # 1.57% gap → warning


@pytest.fixture(autouse=True)
def _clear_strike_cache():
    """Each test gets a clean _STRIKE_CACHE to avoid order coupling."""
    from bot import polymarket_price
    polymarket_price._STRIKE_CACHE.clear()
    yield
    polymarket_price._STRIKE_CACHE.clear()


@pytest.fixture(autouse=True)
def _default_chainlink_strike():
    """Default: Chainlink returns a value agreeing with Coinbase. Tests that
    exercise the cross-check warning override this with their own patch."""
    from bot import chainlink_strike
    with patch.object(chainlink_strike, "get_strike_at",
                      return_value=CHAINLINK_AGREEING):
        yield


# ── helpers ──────────────────────────────────────────────────────────────────


def _poly_response(open_price: float = 70_100.0, close: float = 70_120.0,
                   completed: bool = False):
    return {
        "openPrice": open_price,
        "closePrice": close,
        "completed": completed,
    }


def _coinbase_candle_response(window_ts: int, open_price: float):
    """Shape that `_get_candles(300, ...)` returns: list of [ts, low, high, open, close, volume].

    For test simplicity open == close (so the same fixture works for both
    the close-based helper and the open-based helper). Tests that care
    about the distinction use distinct fixtures.
    """
    return [[window_ts, open_price - 1, open_price + 1, open_price, open_price, 100.0]]


# ── primary path: Coinbase ───────────────────────────────────────────────────


def test_get_strike_returns_coinbase_open_when_chainlink_returns_same_value():
    """Coinbase open is the primary strike. Chainlink is consulted only as a
    cross-check (and agrees, so no warning)."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, STRIKE_FROM_COINBASE)

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price, "fetch_window_price") as mock_poly:
        result = polymarket_price.get_strike(TS_CURR)

    assert result == STRIKE_FROM_COINBASE
    # Polymarket is not consulted when Coinbase+Chainlink both work.
    assert mock_poly.call_count == 0


def test_get_strike_coinbase_wins_even_when_chainlink_disagrees():
    """Coinbase open is authoritative; Chainlink is consulted for diagnostics
    only and never overrides Coinbase."""
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_CURR, STRIKE_FROM_COINBASE)

    with patch.object(chainlink_strike, "get_strike_at",
                      return_value=CHAINLINK_OFF_BY_A_LOT), \
         patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price, "fetch_window_price") as mock_poly:
        result = polymarket_price.get_strike(TS_CURR)

    # Coinbase wins; Chainlink disagreement is logged but not used.
    assert result == STRIKE_FROM_COINBASE
    assert mock_poly.call_count == 0


def test_get_strike_warns_when_coinbase_and_chainlink_disagree_by_more_than_0_3_percent():
    """Sanity check: log a warning when Coinbase open and Chainlink latest
    differ by more than 0.3%."""
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_CURR, STRIKE_FROM_COINBASE)

    with patch.object(chainlink_strike, "get_strike_at",
                      return_value=CHAINLINK_OFF_BY_A_LOT), \
         patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price.logger, "info") as mock_info:
        polymarket_price.get_strike(TS_CURR)

    # Bot still uses Coinbase; the warning is informational only.
    assert any("Coinbase open" in str(c) and "Chainlink" in str(c)
               for c in mock_info.call_args_list)


def test_get_strike_no_warning_when_coinbase_and_chainlink_agree():
    """No warning when the two sources agree within tolerance."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, STRIKE_FROM_COINBASE)

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price.logger, "info") as mock_info:
        polymarket_price.get_strike(TS_CURR)

    # No "Coinbase open vs Chainlink" warning expected.
    assert not any("gap" in str(c) for c in mock_info.call_args_list)


def test_get_strike_caches_within_window():
    """Three calls in the same window produce only one Coinbase fetch."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, STRIKE_FROM_COINBASE)

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response) as mock_cb:
        for _ in range(3):
            polymarket_price.get_strike(TS_CURR)

    assert mock_cb.call_count == 1


# ── fallback chain: Coinbase → Chainlink → Polymarket ───────────────────────


def test_get_strike_falls_back_to_chainlink_when_coinbase_open_fails():
    """Coinbase returns None → fall back to Chainlink."""
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    chainlink_value = 70_333.0

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(chainlink_strike, "get_strike_at", return_value=chainlink_value), \
         patch.object(polymarket_price, "fetch_window_price") as mock_poly:
        result = polymarket_price.get_strike(TS_CURR)

    assert result == chainlink_value
    assert polymarket_price._STRIKE_CACHE[("btc", TS_CURR)] == chainlink_value
    # Polymarket is not consulted when Chainlink works.
    assert mock_poly.call_count == 0


def test_get_strike_falls_back_to_polymarket_when_coinbase_and_chainlink_fail():
    """Coinbase AND Chainlink fail → Polymarket last resort; validate against
    previous-window strike."""
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    # Seed cache with previous-window strike.
    polymarket_price._STRIKE_CACHE[("btc", TS_PREV)] = STRIKE_PREV

    pm_response = _poly_response(open_price=STRIKE_FROM_COINBASE, completed=True)

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(chainlink_strike, "get_strike_at", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price", return_value=pm_response):
        result = polymarket_price.get_strike(TS_CURR)

    assert result == STRIKE_FROM_COINBASE
    assert polymarket_price._STRIKE_CACHE[("btc", TS_CURR)] == STRIKE_FROM_COINBASE


def test_get_strike_fallback_discards_polymarket_value_equal_to_previous_strike():
    """Polymarket fallback is rejected when its value matches the previous
    window's strike (the 30-min-aggregate leak signature)."""
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    polymarket_price._STRIKE_CACHE[("btc", TS_PREV)] = STRIKE_PREV

    pm_response = _poly_response(open_price=STRIKE_PREV, completed=True)

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(chainlink_strike, "get_strike_at", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price", return_value=pm_response), \
         patch.object(polymarket_price.logger, "warn") as mock_warn:
        result = polymarket_price.get_strike(TS_CURR)

    # All paths failed to produce a new strike → None.
    assert result is None
    assert any("30-min leak" in str(c) for c in mock_warn.call_args_list)


def test_get_strike_returns_none_when_all_three_sources_fail():
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(chainlink_strike, "get_strike_at", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price", return_value=None):
        result = polymarket_price.get_strike(TS_CURR)

    assert result is None


# ── cache validation ─────────────────────────────────────────────────────────


def test_get_strike_overwrites_stale_cache_hit_that_matches_previous_window():
    """If a previous-version bot cached a 30-min aggregate value before the
    fix (cache hit equals previous window's strike), the next call should
    discard it and refetch from Coinbase."""
    from bot import polymarket_price
    from bot import coinbase_api

    # Seed cache with a value that equals the previous window's strike
    # (the leak signature, as if it was cached pre-fix).
    polymarket_price._STRIKE_CACHE[("btc", TS_PREV)] = STRIKE_PREV
    polymarket_price._STRIKE_CACHE[("btc", TS_CURR)] = STRIKE_PREV  # stale!

    cb_response = _coinbase_candle_response(TS_CURR, 70_050.0)
    with patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price, "fetch_window_price") as mock_poly:
        result = polymarket_price.get_strike(TS_CURR)

    assert result == 70_050.0
    assert polymarket_price._STRIKE_CACHE[("btc", TS_CURR)] == 70_050.0


def test_get_strike_uses_cache_hit_that_differs_from_previous_window():
    """If the cached strike differs from the previous window's strike (i.e.
    it's NOT a 30-min aggregate leak), use it directly."""
    from bot import polymarket_price
    from bot import coinbase_api

    # Seed cache with a strike that differs from the previous window.
    polymarket_price._STRIKE_CACHE[("btc", TS_PREV)] = STRIKE_PREV
    polymarket_price._STRIKE_CACHE[("btc", TS_CURR)] = 70_555.0  # different!

    with patch.object(coinbase_api, "_get_candles") as mock_cb:
        result = polymarket_price.get_strike(TS_CURR)

    assert result == 70_555.0
    assert mock_cb.call_count == 0


# ── get_strike_and_mark ──────────────────────────────────────────────────────


def test_get_strike_and_mark_returns_coinbase_strike_and_polymarket_mark():
    """Strike comes from Coinbase, mark (live) comes from Polymarket."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, 70_100.0)

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(
             polymarket_price, "fetch_window_price",
             return_value=_poly_response(open_price=70_100.0, close=70_140.0,
                                          completed=False),
         ):
        strike, mark = polymarket_price.get_strike_and_mark(TS_CURR)

    assert strike == 70_100.0
    assert mark == 70_140.0


def test_get_strike_and_mark_returns_none_strike_when_all_fail_but_mark_present():
    """Mark can come back even when strike is None (rare)."""
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(chainlink_strike, "get_strike_at", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price"), \
         patch.object(polymarket_price, "get_strike", return_value=None):
        strike, mark = polymarket_price.get_strike_and_mark(TS_CURR)

    assert strike is None


def test_get_strike_and_mark_mark_only_polymarket_call_required():
    """If Coinbase fails AND Polymarket fails, mark is None but no extra calls."""
    from bot import chainlink_strike, polymarket_price
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(chainlink_strike, "get_strike_at", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price", return_value=None):
        strike, mark = polymarket_price.get_strike_and_mark(TS_CURR)

    assert strike is None
    assert mark is None


# ── coinbase_api helpers ─────────────────────────────────────────────────────


def test_get_5min_candle_open_at_returns_open_at_window_ts():
    """Direct unit test for the Coinbase OPEN helper that backs the primary
    strike path. Queries without start/end so the forming candle (current
    window) is included; returns the open of the candle whose ts matches
    window_ts."""
    from bot import coinbase_api

    candle = _coinbase_candle_response(TS_CURR, 70_777.0)
    with patch.object(coinbase_api, "_get_candles", return_value=candle) as mock_gc:
        result = coinbase_api.get_5min_candle_open_at(TS_CURR, "btc")

    # _get_candles was called without start/end so the forming candle is
    # included — querying with start/end filters it out (production bug
    # surfaced 2026-09-16).
    args, kwargs = mock_gc.call_args
    assert kwargs.get("start") is None
    assert kwargs.get("end") is None
    assert result == 70_777.0


def test_get_5min_candle_open_at_returns_none_when_window_ts_not_in_response():
    """If the requested window_ts is not in the candle list AND the last
    candle has a non-positive close, return None (no valid strike available
    from Coinbase)."""
    from bot import coinbase_api

    # Candle from the previous window — but with a non-positive close, so
    # even the fallback (last close) is unusable.
    bad = [[TS_PREV, 0, 0, 0, -1, 0]]
    with patch.object(coinbase_api, "_get_candles", return_value=bad):
        result = coinbase_api.get_5min_candle_open_at(TS_CURR)

    assert result is None


def test_get_5min_candle_open_at_falls_back_to_previous_candle_close():
    """When the boundary candle hasn't been published yet (Coinbase REST has
    a ~5-30s delay), the previous candle's CLOSE is the best estimate —
    that's the price at the exact boundary tick, which equals the current
    candle's open once Coinbase publishes it."""
    from bot import coinbase_api

    # Previous candle (no boundary candle in response yet).
    candle = [[TS_PREV, 76057.23, 76148.80, 76081.88, 76087.98, 100.0]]
    with patch.object(coinbase_api, "_get_candles", return_value=candle):
        result = coinbase_api.get_5min_candle_open_at(TS_CURR)

    # Returns the close (76087.98), NOT the open (76081.88).
    assert result == 76087.98


def test_get_5min_candle_open_at_returns_none_when_no_data():
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None):
        assert coinbase_api.get_5min_candle_open_at(TS_CURR) is None


def test_get_5min_candle_open_at_skips_non_positive_opens():
    from bot import coinbase_api

    bad = [[TS_CURR, 0, 0, -1, -1, 0]]
    with patch.object(coinbase_api, "_get_candles", return_value=bad):
        assert coinbase_api.get_5min_candle_open_at(TS_CURR) is None


def test_get_5min_candle_close_at_returns_close():
    """Direct unit test for the Coinbase CLOSE helper (used by cross-check)."""
    from bot import coinbase_api

    candle = _coinbase_candle_response(TS_PREV, 70_222.0)
    with patch.object(coinbase_api, "_get_candles", return_value=candle) as mock_gc:
        result = coinbase_api.get_5min_candle_close_at(TS_CURR, "btc")

    # _get_candles was called with the right window (start=TS_CURR-300, end=TS_CURR+1).
    args, kwargs = mock_gc.call_args
    assert kwargs.get("start") == TS_CURR - 300
    assert kwargs.get("end") == TS_CURR + 1
    assert result == 70_222.0


def test_get_5min_candle_close_at_returns_none_when_no_data():
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None):
        assert coinbase_api.get_5min_candle_close_at(TS_CURR) is None


def test_get_5min_candle_close_at_skips_non_positive_closes():
    from bot import coinbase_api

    bad = [[TS_PREV, 0, 0, 0, -1, 0]]
    with patch.object(coinbase_api, "_get_candles", return_value=bad):
        assert coinbase_api.get_5min_candle_close_at(TS_CURR) is None
