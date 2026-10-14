"""Tests for market_hours_filter module and its descriptor."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.strategies.market_hours_filter import (
    is_hour_whitelisted, current_utc_hour, DESCRIPTOR,
    _filter_evaluate,
)


class TestIsHourWhitelisted:
    def test_empty_csv_no_filter(self):
        assert is_hour_whitelisted(0, "") is True
        assert is_hour_whitelisted(23, None) is True
        assert is_hour_whitelisted(12, "  ") is True

    def test_explicit_hour_match(self):
        assert is_hour_whitelisted(14, "0,7,14,21") is True
        assert is_hour_whitelisted(7, "0,7,14,21") is True

    def test_explicit_hour_miss(self):
        assert is_hour_whitelisted(8, "0,7,14,21") is False
        assert is_hour_whitelisted(22, "0,7,14,21") is False

    def test_whitespace_tolerated(self):
        assert is_hour_whitelisted(7, "  0 , 7 , 14 , 21  ") is True

    def test_invalid_entries_ignored(self):
        # 24 is invalid (out of range), "abc" is invalid
        assert is_hour_whitelisted(7, "7,24,abc") is True
        assert is_hour_whitelisted(14, "24,abc") is False  # only invalid -> whitelist empty -> blocks all


class TestCurrentUtcHour:
    def test_returns_int_in_range(self):
        h = current_utc_hour()
        assert isinstance(h, int)
        assert 0 <= h <= 23


class TestFilterEvaluate:
    def test_returns_empty_list_when_whitelisted(self):
        # Spec 2026-10-14: returns [] (not None) so the calling
        # `signals.extend(evaluate(ctx))` works in streak_trader.
        state = MagicMock()
        state.whitelist_hours_btc = f"{current_utc_hour()}"  # always allow
        ctx = MagicMock()
        ctx.state = state
        ctx.symbol = "btc"
        assert _filter_evaluate(ctx) == []

    def test_returns_empty_list_when_blocked(self):
        state = MagicMock()
        not_now = (current_utc_hour() + 1) % 24
        state.whitelist_hours_btc = f"{not_now}"  # current NOT in list
        ctx = MagicMock()
        ctx.state = state
        ctx.symbol = "btc"
        assert _filter_evaluate(ctx) == []

    def test_empty_whitelist_passes(self):
        state = MagicMock()
        state.whitelist_hours_btc = ""
        ctx = MagicMock()
        ctx.state = state
        ctx.symbol = "btc"
        assert _filter_evaluate(ctx) == []


class TestDescriptor:
    def test_registered_with_priority_90(self):
        assert DESCRIPTOR.priority == 90

    def test_has_runtime_fields(self):
        fields = DESCRIPTOR.params
        names = {f.name for f in fields}
        assert "whitelist_hours_btc" in names
        assert "whitelist_hours_eth" in names
        assert "whitelist_hours_sol" in names
