"""Tests for bot/strategies/coin_flip_dog.py.

The pure functions (compute_coa, find_underdog, check_gates, size_shares) are
covered exhaustively — they run offline, no mocks needed. The evaluate_late
function is covered with minimal stubs for the three external calls it makes
(get_atr4, get_strike_and_mark, state.get_asks).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bot.strategies import coin_flip_dog as cfd
from bot.strategies.coin_flip_dog import (
    COA_MAX_DEFAULT,
    ASK_MIN_DEFAULT,
    ASK_MAX_DEFAULT,
    ENTRY_MIN_LEFT,
    ENTRY_MAX_LEFT,
    MIN_SHARES,
    DESCRIPTOR,
    check_gates,
    compute_coa,
    find_underdog,
    size_shares,
)
from bot.strategies.base import StrategyContext


# ── compute_coa ───────────────────────────────────────────────────────────────

class TestComputeCoa:
    def test_basic(self):
        assert compute_coa(0.51, 0.50, 0.10) == pytest.approx(0.10)

    def test_symmetry(self):
        assert compute_coa(0.49, 0.50, 0.10) == pytest.approx(0.10)

    def test_exact_zero_cushion(self):
        assert compute_coa(0.50, 0.50, 0.05) == pytest.approx(0.0)

    def test_zero_atr_returns_none(self):
        assert compute_coa(0.51, 0.50, 0.0) is None

    def test_negative_atr_returns_none(self):
        assert compute_coa(0.51, 0.50, -0.01) is None

    def test_none_atr_returns_none(self):
        assert compute_coa(0.51, 0.50, None) is None  # type: ignore[arg-type]


# ── find_underdog ─────────────────────────────────────────────────────────────

class TestFindUnderdog:
    def test_up_is_cheaper(self):
        direction, ask = find_underdog(0.35, 0.65)
        assert direction == "UP"
        assert ask == pytest.approx(0.35)

    def test_down_is_cheaper(self):
        direction, ask = find_underdog(0.70, 0.30)
        assert direction == "DOWN"
        assert ask == pytest.approx(0.30)

    def test_equal_asks_up_wins_tiebreak(self):
        direction, ask = find_underdog(0.50, 0.50)
        assert direction == "UP"
        assert ask == pytest.approx(0.50)

    def test_none_ask_up_returns_none(self):
        assert find_underdog(None, 0.40) is None  # type: ignore[arg-type]

    def test_none_ask_down_returns_none(self):
        assert find_underdog(0.40, None) is None  # type: ignore[arg-type]

    def test_zero_ask_returns_none(self):
        assert find_underdog(0.0, 0.40) is None

    def test_both_zero_returns_none(self):
        assert find_underdog(0.0, 0.0) is None


# ── check_gates ───────────────────────────────────────────────────────────────

class TestCheckGates:
    """All three gates: timing, coa, and ask band."""

    # Helpers
    PASSING = dict(
        coa=0.15,
        dog_ask=0.35,
        seconds_left=60.0,
        max_coa=COA_MAX_DEFAULT,
        min_ask=ASK_MIN_DEFAULT,
        max_ask=ASK_MAX_DEFAULT,
        entry_min_left=ENTRY_MIN_LEFT,
        entry_max_left=ENTRY_MAX_LEFT,
    )

    def _ok(self, **overrides):
        kw = dict(self.PASSING)
        kw.update(overrides)
        return check_gates(**kw)

    def test_all_pass(self):
        ok, reason = self._ok()
        assert ok is True
        assert reason == ""

    # Timing gate
    def test_too_early(self):
        ok, reason = self._ok(seconds_left=91.0)
        assert ok is False
        assert reason == "CFD_OUT_OF_TIME"

    def test_too_late(self):
        ok, reason = self._ok(seconds_left=29.0)
        assert ok is False
        assert reason == "CFD_OUT_OF_TIME"

    def test_boundary_min_left(self):
        ok, _ = self._ok(seconds_left=ENTRY_MIN_LEFT)
        assert ok is True

    def test_boundary_max_left(self):
        ok, _ = self._ok(seconds_left=ENTRY_MAX_LEFT)
        assert ok is True

    # COA gate
    def test_no_data(self):
        ok, reason = self._ok(coa=None)
        assert ok is False
        assert reason == "CFD_NO_DATA"

    def test_coa_too_high(self):
        ok, reason = self._ok(coa=0.21)
        assert ok is False
        assert reason == "CFD_COA_TOO_HIGH"

    def test_coa_at_boundary_passes(self):
        ok, _ = self._ok(coa=COA_MAX_DEFAULT)
        assert ok is True

    # Ask band gate
    def test_no_book(self):
        ok, reason = self._ok(dog_ask=None)
        assert ok is False
        assert reason == "CFD_NO_BOOK"

    def test_ask_too_low(self):
        ok, reason = self._ok(dog_ask=0.21)
        assert ok is False
        assert reason == "CFD_ASK_TOO_LOW"

    def test_ask_at_min_boundary_passes(self):
        ok, _ = self._ok(dog_ask=ASK_MIN_DEFAULT)
        assert ok is True

    def test_ask_too_high(self):
        ok, reason = self._ok(dog_ask=0.48)
        assert ok is False
        assert reason == "CFD_ASK_TOO_HIGH"

    def test_ask_at_max_boundary_passes(self):
        ok, _ = self._ok(dog_ask=ASK_MAX_DEFAULT)
        assert ok is True


# ── size_shares ───────────────────────────────────────────────────────────────

class TestSizeShares:
    def test_basic(self):
        # $5 at $0.50 = 10 shares
        assert size_shares(5.0, 0.50) == 10.0

    def test_floors_to_min_shares(self):
        # $5 at $0.99 = ~5 shares (round(5/0.99) = 5)
        assert size_shares(5.0, 0.99) >= MIN_SHARES

    def test_floor_on_tiny_bet(self):
        assert size_shares(0.01, 0.50) == MIN_SHARES

    def test_zero_ask_returns_min(self):
        assert size_shares(5.0, 0.0) == MIN_SHARES

    def test_larger_bet(self):
        # $50 at $0.40 = 125 shares
        assert size_shares(50.0, 0.40) == pytest.approx(125.0)


# ── descriptor ────────────────────────────────────────────────────────────────

class TestDescriptor:
    def test_id(self):
        assert DESCRIPTOR.id == "coin_flip_dog"

    def test_evaluate_returns_empty_at_window_open(self):
        """evaluate() is a no-op — signals only come from evaluate_late."""
        ctx = StrategyContext(state=SimpleNamespace(), symbol="btc")
        assert DESCRIPTOR.evaluate(ctx) == []

    def test_evaluate_late_is_callable(self):
        assert callable(DESCRIPTOR.evaluate_late)

    def test_disabled_by_default(self):
        state = SimpleNamespace(cfd_enabled=False)
        assert DESCRIPTOR.enabled_for(state) is False

    def test_enabled_when_toggle_on(self):
        state = SimpleNamespace(cfd_enabled=True)
        assert DESCRIPTOR.enabled_for(state) is True

    def test_enabled_when_matches_is_enabled(self):
        """The declarative enabled_when and the callable is_enabled must agree."""
        field = DESCRIPTOR.enabled_when["field"]
        for val in DESCRIPTOR.enabled_when["values"]:
            state = SimpleNamespace(**{field: val})
            assert DESCRIPTOR.enabled_for(state) is True

    def test_priority_between_fade_and_box_builder(self):
        from bot.strategies import get as s_get
        # Order: ss_fade (100) > box_builder (90) > coin_flip_dog (80)
        assert s_get("ss_fade").priority > s_get("box_builder").priority > DESCRIPTOR.priority

    def test_params_names(self):
        names = [p.name for p in DESCRIPTOR.params]
        assert "cfd_enabled" in names
        assert "cfd_base_bet" in names
        assert "cfd_max_coa" in names
        assert "cfd_min_ask" in names
        assert "cfd_max_ask" in names
        assert "cfd_entry_min_left" in names
        assert "cfd_entry_max_left" in names


# ── evaluate_late (integration) ───────────────────────────────────────────────

def _make_ctx(
    seconds_left: float = 60.0,
    ask_up: float = 0.65,
    ask_down: float = 0.35,
    mark: float = 0.51,   # coa = |0.51-0.50|/0.10 = 0.10 — safely inside 0.20
    strike: float = 0.50,
    atr4: float = 0.10,
    cfd_enabled: bool = True,
    **state_kwargs,
):
    state = SimpleNamespace(
        cfd_enabled=cfd_enabled,
        cfd_base_bet=5.0,
        cfd_min_ask=ASK_MIN_DEFAULT,
        cfd_max_ask=ASK_MAX_DEFAULT,
        cfd_max_coa=COA_MAX_DEFAULT,
        cfd_entry_min_left=ENTRY_MIN_LEFT,
        cfd_entry_max_left=ENTRY_MAX_LEFT,
        skips={},
        **state_kwargs,
    )
    state.get_asks = lambda: (ask_up, ask_down)
    state.record_skip = lambda reason: state.skips.update(
        {reason: state.skips.get(reason, 0) + 1}
    )
    tokens = SimpleNamespace(window_ts=1_000_000)
    return StrategyContext(
        state=state,
        symbol="btc",
        tokens=tokens,
        seconds_left=seconds_left,
    ), mark, strike, atr4


class TestEvaluateLate:
    def _call(self, ctx, mark, strike, atr4):
        # The imports are lazy (inside the function body) to avoid circular
        # imports at module level, so we patch the source modules directly.
        with (
            patch("bot.binance_api.get_atr4", return_value=atr4),
            patch(
                "bot.polymarket_price.get_strike_and_mark",
                return_value=(strike, mark),
            ),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
        ):
            return cfd._evaluate_late(ctx)

    def test_signal_fires_when_all_gates_pass(self):
        ctx, mark, strike, atr4 = _make_ctx()
        sigs = self._call(ctx, mark, strike, atr4)
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.strategy == "coin_flip_dog"
        assert sig.direction == "DOWN"   # ask_down=0.35 < ask_up=0.65
        assert sig.multiplier == pytest.approx(1.0)
        assert sig.loss_streak == 0
        assert sig.limit_cap == pytest.approx(min(ASK_MAX_DEFAULT, 0.35))

    def test_no_signal_outside_time_band(self):
        ctx, mark, strike, atr4 = _make_ctx(seconds_left=200.0)
        sigs = self._call(ctx, mark, strike, atr4)
        assert sigs == []

    def test_no_signal_when_coa_too_high(self):
        # large cushion relative to ATR
        ctx, _, strike, atr4 = _make_ctx(mark=0.90, strike=0.50, atr4=0.10)
        sigs = self._call(ctx, 0.90, strike, atr4)
        assert sigs == []

    def test_no_signal_when_ask_above_cap(self):
        ctx, mark, strike, atr4 = _make_ctx(ask_down=0.50)
        sigs = self._call(ctx, mark, strike, atr4)
        assert sigs == []

    def test_no_signal_when_ask_below_floor(self):
        ctx, mark, strike, atr4 = _make_ctx(ask_down=0.15)
        sigs = self._call(ctx, mark, strike, atr4)
        assert sigs == []

    def test_no_signal_when_no_atr(self):
        ctx, mark, strike, _ = _make_ctx()
        sigs = self._call(ctx, mark, strike, None)
        assert sigs == []

    def test_no_signal_when_no_strike(self):
        ctx, mark, _, atr4 = _make_ctx()
        sigs = self._call(ctx, mark, None, atr4)
        assert sigs == []

    def test_no_signal_when_no_tokens(self):
        ctx, mark, strike, atr4 = _make_ctx()
        ctx.tokens = None
        sigs = self._call(ctx, mark, strike, atr4)
        assert sigs == []

    def test_shares_proportional_to_base_bet(self):
        ctx, mark, strike, atr4 = _make_ctx(ask_down=0.40)
        # cfd_base_bet=5, ask=0.40 → round(5/0.40)=12 shares
        sigs = self._call(ctx, mark, strike, atr4)
        assert len(sigs) == 1
        assert sigs[0].shares == pytest.approx(12.0)

    def test_up_is_underdog_when_cheaper(self):
        ctx, mark, strike, atr4 = _make_ctx(ask_up=0.30, ask_down=0.70)
        sigs = self._call(ctx, mark, strike, atr4)
        assert len(sigs) == 1
        assert sigs[0].direction == "UP"

    def test_skip_recorded_when_coa_too_high(self):
        ctx, _, strike, atr4 = _make_ctx(mark=0.90, strike=0.50, atr4=0.10)
        self._call(ctx, 0.90, strike, atr4)
        assert "CFD_COA_TOO_HIGH" in ctx.state.skips
