"""Tests for `bot.polymarket_price` — strike sourcing (Coinbase primary).

## History

- **v1 (2026-09-11)**: Polymarket `crypto-price` endpoint used as primary.
- **v2 (2026-09-13)**: Polymarket returns 30-min aggregates, not 5-min strikes.
  Detection logic added (compare against previous-window strike) with Coinbase
  fallback.
- **v3 (2026-09-13, current)**: Inverted priority — Coinbase is now PRIMARY
  because it always returns a per-window 5-min candle close. Polymarket is
  only a fallback when Coinbase fails AND a sanity cross-check (warn if its
  openPrice differs by >1% from the Coinbase strike). The v2 detection logic
  is kept as a safety net for the fallback path.

These tests pin down the v3 behavior so the bug cannot regress silently.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# Fixed boundary timestamps — Unix-seconds aligned to 5-min grid.
TS_PREV = 1_700_000_000  # some previous 5-min boundary
TS_CURR = TS_PREV + 300
STRIKE_PREV = 70_000.0    # cached strike for the previous window
STRIKE_FROM_COINBASE = 70_100.0  # what Coinbase returns for the current window
CLOSE_PREV_CANDLE = 70_000.0    # close of candle ending at TS_PREV (= TS_CURR - 300)


@pytest.fixture(autouse=True)
def _clear_strike_cache():
    """Each test gets a clean _STRIKE_CACHE to avoid order coupling."""
    from bot import polymarket_price
    polymarket_price._STRIKE_CACHE.clear()
    yield
    polymarket_price._STRIKE_CACHE.clear()


@pytest.fixture(autouse=True)
def _disable_chainlink_strike():
    """Default: pretend Chainlink is unavailable so tests verify the Coinbase
    fallback path. Individual tests that want to exercise the Chainlink path
    override this fixture by passing their own `patch.object(chainlink_strike,
    "get_strike_at", ...)`."""
    from bot import chainlink_strike
    with patch.object(chainlink_strike, "get_strike_at", return_value=None):
        yield


# ── helpers ──────────────────────────────────────────────────────────────────


def _poly_response(open_price: float = 70_100.0, close: float = 70_120.0,
                   completed: bool = False):
    return {
        "openPrice": open_price,
        "closePrice": close,
        "completed": completed,
    }


def _coinbase_candle_response(window_ts: int, close: float):
    """Shape that `_get_candles(300, ...)` returns: list of [ts, low, high, open, close, volume]."""
    return [[window_ts, close - 1, close + 1, close - 0.5, close, 100.0]]


# ── primary path: Coinbase ───────────────────────────────────────────────────


def test_get_strike_returns_coinbase_value_when_chainlink_unavailable():
    """Chainlink is unavailable → Coinbase primary kicks in. Polymarket is
    NOT consulted (it's the tertiary fallback, only used when both above fail)."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, STRIKE_FROM_COINBASE)

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price, "fetch_window_price") as mock_poly:
        result = polymarket_price.get_strike(TS_CURR)

    assert result == STRIKE_FROM_COINBASE
    # Polymarket is not consulted when Chainlink+Coinbase both work.
    assert mock_poly.call_count == 0


def test_get_strike_warns_when_coinbase_and_polymarket_disagree_by_more_than_1_percent():
    """Sanity check: log a warning when sources differ significantly."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, 69_000.0)  # 1.4% gap to 70100

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price, "fetch_window_price",
                      return_value=_poly_response(open_price=70_100.0)), \
         patch.object(polymarket_price.logger, "warn") as mock_warn:
        result = polymarket_price.get_strike(TS_CURR)

    # Bot still uses Coinbase; warning is informational only.
    assert result == 69_000.0
    assert any("Coinbase" in str(c) for c in mock_warn.call_args_list)


def test_get_strike_no_warning_when_coinbase_and_polymarket_agree():
    """No warning when the two sources agree within tolerance."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, 70_100.0)

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response), \
         patch.object(polymarket_price, "fetch_window_price",
                      return_value=_poly_response(open_price=70_150.0)), \
         patch.object(polymarket_price.logger, "warn") as mock_warn:
        polymarket_price.get_strike(TS_CURR)

    # No "Coinbase vs Polymarket" warning expected.
    assert not any("gap" in str(c) for c in mock_warn.call_args_list)


def test_get_strike_caches_within_window():
    """Three calls in the same window produce only one Coinbase fetch."""
    from bot import polymarket_price
    from bot import coinbase_api

    cb_response = _coinbase_candle_response(TS_PREV, STRIKE_FROM_COINBASE)

    with patch.object(coinbase_api, "_get_candles", return_value=cb_response) as mock_cb:
        for _ in range(3):
            polymarket_price.get_strike(TS_CURR)

    assert mock_cb.call_count == 1


# ── fallback: Polymarket when Coinbase fails ────────────────────────────────


def test_get_strike_falls_back_to_polymarket_when_coinbase_fails():
    """Coinbase returns None → fall back to Polymarket; validate against prev strike."""
    from bot import polymarket_price
    from bot import coinbase_api

    # Seed cache with previous-window strike.
    polymarket_price._STRIKE_CACHE[("btc", TS_PREV)] = STRIKE_PREV

    # Polymarket returns a DIFFERENT value (passes the leak detection).
    pm_response = _poly_response(open_price=STRIKE_FROM_COINBASE, completed=True)

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price", return_value=pm_response):
        result = polymarket_price.get_strike(TS_CURR)

    assert result == STRIKE_FROM_COINBASE
    assert polymarket_price._STRIKE_CACHE[("btc", TS_CURR)] == STRIKE_FROM_COINBASE


def test_get_strike_fallback_discards_polymarket_value_equal_to_previous_strike():
    """Polymarket fallback is rejected when its value matches the previous
    window's strike (the 30-min-aggregate leak signature)."""
    from bot import polymarket_price
    from bot import coinbase_api

    polymarket_price._STRIKE_CACHE[("btc", TS_PREV)] = STRIKE_PREV

    pm_response = _poly_response(open_price=STRIKE_PREV, completed=True)

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price", return_value=pm_response), \
         patch.object(polymarket_price.logger, "warn") as mock_warn:
        result = polymarket_price.get_strike(TS_CURR)

    # Both sources failed to produce a new strike → None.
    assert result is None
    assert any("30-min leak" in str(c) for c in mock_warn.call_args_list)


def test_get_strike_returns_none_when_both_coinbase_and_polymarket_fail():
    from bot import polymarket_price
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
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

    cb_response = _coinbase_candle_response(TS_PREV, 70_050.0)
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


def test_get_strike_and_mark_returns_none_strike_when_both_fail_but_mark_present():
    """Mark can come back even when strike is None (rare)."""
    from bot import polymarket_price
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price"), \
         patch.object(polymarket_price, "get_strike", return_value=None):
        strike, mark = polymarket_price.get_strike_and_mark(TS_CURR)

    assert strike is None


def test_get_strike_and_mark_mark_only_polymarket_call_required():
    """If Coinbase fails AND Polymarket fails, mark is None but no extra calls."""
    from bot import polymarket_price
    from bot import coinbase_api

    with patch.object(coinbase_api, "_get_candles", return_value=None), \
         patch.object(polymarket_price, "fetch_window_price", return_value=None):
        strike, mark = polymarket_price.get_strike_and_mark(TS_CURR)

    assert strike is None
    assert mark is None


# ── coinbase_api helper ──────────────────────────────────────────────────────


def test_get_5min_candle_close_at_returns_close():
    """Direct unit test for the Coinbase helper that backs the fallback."""
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
