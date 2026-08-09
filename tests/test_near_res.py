"""Tests for Near-Resolution Capture strategy — bot/strategies/near_res.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from bot.strategies.near_res import (
    find_frontrunner,
    check_nrc_gates,
    _evaluate_late,
    DESCRIPTOR,
)
from bot.strategies.base import StrategyContext


# ── find_frontrunner ─────────────────────────────────────────────────────────

class TestFindFrontrunner:
    def test_up_is_frontrunner(self):
        d, ask = find_frontrunner(0.980, 0.022, 0.970, 0.995)
        assert d == "UP"
        assert ask == 0.980

    def test_down_is_frontrunner(self):
        d, ask = find_frontrunner(0.018, 0.985, 0.970, 0.995)
        assert d == "DOWN"
        assert ask == 0.985

    def test_neither_qualifies(self):
        d, ask = find_frontrunner(0.55, 0.47, 0.970, 0.995)
        assert d is None
        assert ask is None

    def test_both_qualify_returns_none(self):
        # Pathological: both sides near $1
        d, ask = find_frontrunner(0.975, 0.980, 0.970, 0.995)
        assert d is None

    def test_exactly_at_min_ask(self):
        d, ask = find_frontrunner(0.970, 0.030, 0.970, 0.995)
        assert d == "UP"

    def test_above_max_ask_returns_none(self):
        d, ask = find_frontrunner(0.997, 0.003, 0.970, 0.995)
        assert d is None

    def test_none_asks(self):
        d, ask = find_frontrunner(None, None, 0.970, 0.995)
        assert d is None

    def test_one_none_ask(self):
        d, ask = find_frontrunner(None, 0.982, 0.970, 0.995)
        assert d == "DOWN"


# ── check_nrc_gates ──────────────────────────────────────────────────────────

PASSING = dict(
    ask_up=0.980,
    ask_dn=0.020,
    seconds_left=12.0,
    min_ask=0.970,
    max_ask=0.995,
    min_entry_left=5.0,
    max_entry_left=20.0,
    max_book_sum=1.01,
)


def _ok(**overrides):
    return check_nrc_gates(**{**PASSING, **overrides})


class TestCheckNrcGates:
    def test_all_gates_pass(self):
        passes, reason, d, ask = _ok()
        assert passes is True
        assert reason == "NRC_OK"
        assert d == "UP"
        assert ask == 0.980

    def test_out_of_time_early(self):
        passes, reason, *_ = _ok(seconds_left=25.0)
        assert passes is False
        assert reason == "NRC_OUT_OF_TIME"

    def test_out_of_time_late(self):
        passes, reason, *_ = _ok(seconds_left=3.0)
        assert passes is False
        assert reason == "NRC_OUT_OF_TIME"

    def test_no_frontrunner(self):
        passes, reason, *_ = _ok(ask_up=0.55, ask_dn=0.47)
        assert passes is False
        assert reason == "NRC_NO_FRONTRUNNER"

    def test_premium_book_blocked(self):
        # 0.980 + 0.040 = 1.020 > 1.01
        passes, reason, *_ = _ok(ask_up=0.980, ask_dn=0.040)
        assert passes is False
        assert reason == "NRC_PREMIUM_BOOK"

    def test_premium_book_exactly_at_limit_passes(self):
        # 0.980 + 0.030 = 1.010 = max_book_sum → passes
        passes, reason, *_ = _ok(ask_up=0.980, ask_dn=0.030, max_book_sum=1.01)
        assert passes is True

    def test_down_frontrunner(self):
        passes, reason, d, ask = _ok(ask_up=0.018, ask_dn=0.984)
        assert passes is True
        assert d == "DOWN"
        assert ask == 0.984

    def test_frontrunner_exactly_at_boundaries(self):
        passes, _, d, ask = _ok(ask_up=0.970, ask_dn=0.020, min_ask=0.970, max_ask=0.995)
        assert passes is True
        assert d == "UP"


# ── _evaluate_late ───────────────────────────────────────────────────────────

def _make_ctx(
    *,
    ask_up=0.980,
    ask_dn=0.020,
    seconds_left=12.0,
    nrc_enabled=True,
    nrc_min_ask=0.970,
    nrc_max_ask=0.995,
    nrc_min_entry_left=5.0,
    nrc_max_entry_left=20.0,
    nrc_shares=5.0,
    nrc_max_book_sum=1.01,
    skips=None,
):
    skips = skips if skips is not None else []
    state = SimpleNamespace(
        nrc_enabled=nrc_enabled,
        nrc_min_ask=nrc_min_ask,
        nrc_max_ask=nrc_max_ask,
        nrc_min_entry_left=nrc_min_entry_left,
        nrc_max_entry_left=nrc_max_entry_left,
        nrc_shares=nrc_shares,
        nrc_max_book_sum=nrc_max_book_sum,
    )
    state.get_asks = lambda: (ask_up, ask_dn)
    state.record_skip = lambda r: skips.append(r)
    tokens = SimpleNamespace(window_ts=1_000_000, slug="slug-nrc")
    return StrategyContext(
        state=state, symbol="btc", tokens=tokens, seconds_left=seconds_left
    ), skips


def _call(ctx):
    with (
        patch("bot.logger.info"),
        patch("bot.logger.ok"),
        patch("bot.logger.warn"),
    ):
        return _evaluate_late(ctx)


class TestEvaluateLate:
    def test_signal_fires_on_clear_winner(self):
        ctx, _ = _make_ctx(ask_up=0.982, ask_dn=0.019, seconds_left=12.0)
        sigs = _call(ctx)
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.strategy == "near_res"
        assert sig.direction == "UP"
        assert sig.limit_cap <= 0.995
        assert sig.shares == 5.0
        assert sig.multiplier == 1.0
        assert sig.loss_streak == 0

    def test_no_signal_out_of_time(self):
        ctx, _ = _make_ctx(ask_up=0.982, ask_dn=0.019, seconds_left=25.0)
        sigs = _call(ctx)
        assert sigs == []

    def test_no_signal_no_frontrunner(self):
        ctx, skips = _make_ctx(ask_up=0.50, ask_dn=0.50, seconds_left=12.0)
        sigs = _call(ctx)
        assert sigs == []
        assert "NRC_NO_FRONTRUNNER" in skips

    def test_no_signal_premium_book(self):
        ctx, skips = _make_ctx(ask_up=0.982, ask_dn=0.040, seconds_left=12.0)
        sigs = _call(ctx)
        assert sigs == []
        assert "NRC_PREMIUM_BOOK" in skips

    def test_cap_is_min_of_max_ask_and_fr_ask(self):
        ctx, _ = _make_ctx(ask_up=0.999, ask_dn=0.001, seconds_left=12.0,
                           nrc_max_ask=0.995)
        sigs = _call(ctx)
        # ask 0.999 > max_ask 0.995 → no frontrunner
        assert sigs == []

    def test_down_frontrunner(self):
        ctx, _ = _make_ctx(ask_up=0.018, ask_dn=0.984, seconds_left=12.0)
        sigs = _call(ctx)
        assert len(sigs) == 1
        assert sigs[0].direction == "DOWN"

    def test_no_tokens_returns_empty(self):
        ctx, _ = _make_ctx()
        ctx = StrategyContext(
            state=ctx.state, symbol="btc", tokens=None, seconds_left=12.0
        )
        sigs = _call(ctx)
        assert sigs == []

    def test_out_of_time_does_not_record_skip(self):
        ctx, skips = _make_ctx(ask_up=0.982, ask_dn=0.019, seconds_left=30.0)
        _call(ctx)
        assert "NRC_OUT_OF_TIME" not in skips  # suppressed to avoid log spam


# ── descriptor ───────────────────────────────────────────────────────────────

class TestDescriptor:
    def test_id(self):
        assert DESCRIPTOR.id == "near_res"

    def test_not_enabled_by_default(self):
        state = SimpleNamespace()
        assert DESCRIPTOR.is_enabled(state) is False

    def test_enabled_when_flag_set(self):
        state = SimpleNamespace(nrc_enabled=True)
        assert DESCRIPTOR.is_enabled(state) is True

    def test_has_evaluate_late(self):
        assert DESCRIPTOR.evaluate_late is not None

    def test_observe_is_none(self):
        assert DESCRIPTOR.observe is None

    def test_evaluate_returns_empty(self):
        ctx = StrategyContext(state=SimpleNamespace(), symbol="btc")
        assert DESCRIPTOR.evaluate(ctx) == []

    def test_params_include_required_fields(self):
        names = {p.name for p in DESCRIPTOR.params}
        for expected in ("nrc_enabled", "nrc_min_ask", "nrc_max_ask",
                         "nrc_min_entry_left", "nrc_max_entry_left", "nrc_shares"):
            assert expected in names, f"missing param: {expected}"

    def test_enabled_when_matches_is_enabled(self):
        ew = DESCRIPTOR.enabled_when
        assert ew is not None
        field = ew["field"]
        for val in ew["values"]:
            state = SimpleNamespace(**{field: val})
            assert DESCRIPTOR.is_enabled(state) is True
