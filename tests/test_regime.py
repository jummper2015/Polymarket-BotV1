"""Unit tests for regime.py — pure functions, no network."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import regime


def _candles(moves, base=60000.0, spread=0.0):
    """Build candles from a list of relative moves."""
    out, px = [], base
    for i, m in enumerate(moves):
        close = px * (1 + m)
        hi = max(px, close) * (1 + spread)
        lo = min(px, close) * (1 - spread)
        out.append({"ts": 1_785_600_000 + i * 300, "open": px,
                    "high": hi, "low": lo, "close": close})
        px = close
    return out


class TestParseHours:
    def test_empty_means_no_restriction(self):
        assert regime.parse_hours("") == []

    def test_single_range(self):
        assert regime.parse_hours("13-21") == [(13, 21)]

    def test_multiple_ranges(self):
        assert regime.parse_hours("8-13,14-21") == [(8, 13), (14, 21)]

    def test_wrapping_midnight_splits(self):
        assert regime.parse_hours("22-2") == [(22, 24), (0, 2)]

    def test_garbage_is_dropped_not_raised(self):
        assert regime.parse_hours("abc") == []
        assert regime.parse_hours("99-100") == []
        assert regime.parse_hours("5-5") == []

    def test_validity_check(self):
        assert regime.is_valid_hours_spec("")
        assert regime.is_valid_hours_spec("13-21")
        assert not regime.is_valid_hours_spec("13-21,garbage")


class TestHoursFilter:
    def _at(self, hour):
        return time.mktime(time.struct_time((2026, 8, 5, hour, 30, 0, 2, 217, 0))) - time.timezone

    def test_no_spec_allows_everything(self):
        assert regime.hours_filter("", now=self._at(3)).allowed

    def test_inside_range_allowed(self):
        assert regime.hours_filter("13-21", now=self._at(15)).allowed

    def test_outside_range_rejected(self):
        v = regime.hours_filter("13-21", now=self._at(9))
        assert not v.allowed
        assert v.reason == "SKIP_HOURS"

    def test_boundaries_are_half_open(self):
        assert regime.hours_filter("13-21", now=self._at(13)).allowed
        assert not regime.hours_filter("13-21", now=self._at(21)).allowed


class TestMeasurements:
    def test_atr_needs_full_window(self):
        assert regime.atr(_candles([0.001] * 5)) is None

    def test_atr_averages_absolute_moves(self):
        c = _candles([0.001] * regime.ATR_WINDOWS)
        assert regime.atr(c) == pytest.approx(0.001, rel=0.01)

    def test_atr_ignores_direction(self):
        up = regime.atr(_candles([0.002] * regime.ATR_WINDOWS))
        down = regime.atr(_candles([-0.002] * regime.ATR_WINDOWS))
        assert up == pytest.approx(down, rel=0.02)

    def test_range_uses_highs_and_lows(self):
        c = _candles([0.0] * regime.RANGE_WINDOWS, spread=0.01)
        assert regime.price_range(c) == pytest.approx(0.02, abs=0.005)

    def test_percentile_of(self):
        vals = list(range(100))
        assert regime.percentile_of(vals, 50) == pytest.approx(50.0)
        assert regime.percentile_of(vals, 0) == 0.0
        assert regime.percentile_of([], 1.0) == 50.0


class TestVolatilityFilter:
    def test_defaults_allow_everything(self):
        assert regime.volatility_filter(_candles([0.001] * 100)).allowed

    def test_short_history_does_not_block(self):
        assert regime.volatility_filter(_candles([0.001] * 5), 25, 75).allowed

    def test_calm_tail_rejected_by_lower_bound(self):
        """A burst of volatility then dead calm -> current ATR in a low percentile."""
        c = _candles([0.004, -0.004] * 200 + [0.00001] * 20)
        v = regime.volatility_filter(c, 25.0, 100.0)
        assert not v.allowed
        assert v.reason == "SKIP_VOL"

    def test_spike_rejected_by_upper_bound(self):
        c = _candles([0.0005, -0.0005] * 200 + [0.02, -0.02] * 10)
        v = regime.volatility_filter(c, 0.0, 75.0)
        assert not v.allowed
        assert v.reason == "SKIP_VOL"


class TestRangeFilter:
    def test_default_allows_everything(self):
        assert regime.range_filter(_candles([0.001] * 100)).allowed

    def test_wide_range_rejected(self):
        c = _candles([0.0002] * 400 + [0.01] * 30)
        v = regime.range_filter(c, 50.0)
        assert not v.allowed
        assert v.reason == "SKIP_RANGE"


class TestEvaluate:
    def test_all_off_allows(self):
        assert regime.evaluate(_candles([0.001] * 100)).allowed

    def test_hours_checked_before_candles(self):
        """An out-of-session window is skipped even with no candle data."""
        at_9 = time.mktime(time.struct_time((2026, 8, 5, 9, 30, 0, 2, 217, 0))) - time.timezone
        v = regime.evaluate([], hours_spec="13-21", now=at_9)
        assert not v.allowed
        assert v.reason == "SKIP_HOURS"

    def test_verdict_is_truthy(self):
        assert bool(regime.ALLOWED)
        assert not bool(regime.RegimeVerdict(False, "X"))
