"""Unit tests for strategy_streak.py — mocked Binance API and DB calls."""

import sys
import os
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.binance_api import FOUR_HOURS
from bot.strategy_streak import StreakSnapperStrategy, StreakSignal


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_windows(directions: list[str]) -> list[dict]:
    base_ts = 1_785_600_000
    return [
        {"ts": base_ts + i * 300, "open": 62000.0, "close": 62000.0, "direction": d}
        for i, d in enumerate(directions)
    ]


def _make_candle(direction: str, strength: float = 0.01, ts: int | None = None) -> dict:
    """A closed 4h candle. `ts` defaults to one that licenses the block we're in.

    The cycle compares `time.time()` against `anchor + 8h`, so anchoring at
    `now - 4h` puts us at the start of the block that candle licenses. Anchoring
    further back is how the tests reach an expired block.
    """
    if ts is None:
        ts = int(time.time()) - FOUR_HOURS
    open_px = 63000.0
    move = abs(strength) * open_px
    return {
        "ts": ts,
        "open": open_px,
        "close": open_px + move if direction == "UP" else open_px - move,
        "direction": direction,
        "strength": abs(strength) if direction == "UP" else -abs(strength),
        "block_start": ts + FOUR_HOURS,
        "block_end": ts + 2 * FOUR_HOURS,
    }


class FakeState:
    def __init__(self):
        self.ss_fade_base_shares = 5.0
        self.ss_fade_limit_cap = 0.60
        self.ss_fade_streak_min = 4
        self.ss_trend_base_shares = 5.0
        self.ss_trend_limit_cap = 0.52
        self.ss_trend_min_strength = 0.008
        # Mirrors the production default. Tests that exercise martingale sizing
        # opt in explicitly, so a change to the default can't silently turn them
        # into tests of something else.
        self.ss_sizing = "flat"
        self.ss_kelly_fraction = 0.25
        self.ss_martingale_mult_factor = 1.5
        self.ss_fade_martingale_mult = 1.0
        self.ss_fade_loss_streak = 0
        self.ss_trend_martingale_mult = 1.0
        self.ss_trend_loss_streak = 0
        self.ss_trend_cycle_side = None
        self.ss_trend_cycle_anchor_ts = None
        self.ss_trend_last_strength = None
        self.starting_bankroll = 1000.0

    def current_bankroll(self) -> float:
        return self.starting_bankroll


@pytest.fixture(autouse=True)
def _no_db():
    """Keep cycle persistence out of the unit tests."""
    with patch("bot.strategy_streak.open_cycle"), \
         patch("bot.strategy_streak.close_cycle"):
        yield


@pytest.fixture
def strategy():
    state = FakeState()
    with patch(
        "bot.strategy_streak.get_or_create_martingale_state",
        # cycle_side must be an explicit None: a bare MagicMock attribute is
        # truthy, which would start every test mid-cycle.
        return_value=MagicMock(
            multiplier=1.0, loss_streak=0, cycle_side=None, cycle_anchor_ts=None
        ),
    ):
        s = StreakSnapperStrategy(state)
    return s


# ═══════════════════════════════════════════════════════════════════════════════
# Forma 1 — Fade (anti-racha)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFadeSignal:

    def test_4_up_streak_fires_down(self, strategy):
        windows = _make_windows(["DOWN", "UP", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is not None
        assert sig.strategy == "ss_fade"
        assert sig.direction == "DOWN"
        assert sig.shares == 5
        assert sig.multiplier == 1.0
        assert sig.limit_cap == 0.60

    def test_4_down_streak_fires_up(self, strategy):
        windows = _make_windows(["UP", "DOWN", "DOWN", "DOWN", "DOWN"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is not None
        assert sig.direction == "UP"

    def test_6_down_streak_fires_up(self, strategy):
        windows = _make_windows(["UP", "DOWN", "DOWN", "DOWN", "DOWN", "DOWN", "DOWN"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is not None
        assert sig.direction == "UP"

    def test_2_streak_no_signal(self, strategy):
        windows = _make_windows(["DOWN", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is None

    def test_3_streak_no_signal(self, strategy):
        windows = _make_windows(["DOWN", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is None

    def test_no_data_returns_none(self, strategy):
        with patch("bot.strategy_streak.get_5min_windows", return_value=None):
            sig = strategy.get_fade_signal()
        assert sig is None

    def test_custom_streak_min(self, strategy):
        strategy.state.ss_fade_streak_min = 6
        windows = _make_windows(["DOWN", "UP", "UP", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is None

        windows = _make_windows(["DOWN", "UP", "UP", "UP", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is not None
        assert sig.direction == "DOWN"

    def test_with_martingale_multiplier(self, strategy):
        """Martingale at ×2.25 -> shares = round(5 * 2.25, 2) = 11.25."""
        strategy.state.ss_sizing = "martingale"
        strategy.state.ss_fade_martingale_mult = 2.25
        strategy.state.ss_fade_loss_streak = 2
        windows = _make_windows(["DOWN", "UP", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is not None
        assert sig.shares == 11.25  # round(5.0 * 2.25, 2) = 11.25
        assert sig.multiplier == 2.25
        assert sig.loss_streak == 2

    def test_mixed_streak_only_latest_counts(self, strategy):
        windows = _make_windows(["UP", "DOWN", "UP", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            sig = strategy.get_fade_signal()
        assert sig is not None
        assert sig.direction == "DOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# Forma 2 — Trend (tendencia 4h)
# ═══════════════════════════════════════════════════════════════════════════════


def _signal(strategy, candle):
    with patch("bot.strategy_streak.get_last_closed_4h_candle", return_value=candle):
        return strategy.get_trend_signal()


class TestTrendSignal:

    def test_up_trend_signal(self, strategy):
        sig = _signal(strategy, _make_candle("UP"))
        assert sig is not None
        assert sig.strategy == "ss_trend"
        assert sig.direction == "UP"
        assert sig.shares == 5
        assert sig.limit_cap == 0.52

    def test_down_trend_signal(self, strategy):
        sig = _signal(strategy, _make_candle("DOWN"))
        assert sig is not None
        assert sig.direction == "DOWN"

    def test_no_data_returns_none(self, strategy):
        assert _signal(strategy, None) is None

    def test_below_threshold_no_signal(self, strategy):
        """A candle that barely moved is not a trend, and opens no cycle."""
        sig = _signal(strategy, _make_candle("UP", strength=0.001))
        assert sig is None
        assert strategy.state.ss_trend_cycle_side is None

    def test_threshold_is_on_absolute_move(self, strategy):
        """A big DOWN move clears the threshold as readily as a big UP one."""
        sig = _signal(strategy, _make_candle("DOWN", strength=0.02))
        assert sig is not None
        assert sig.direction == "DOWN"

    def test_exactly_at_threshold_signals(self, strategy):
        sig = _signal(strategy, _make_candle("UP", strength=0.008))
        assert sig is not None

    def test_records_last_strength_even_without_signal(self, strategy):
        _signal(strategy, _make_candle("DOWN", strength=0.001))
        assert strategy.state.ss_trend_last_strength == pytest.approx(-0.001)

    def test_with_martingale_multiplier(self, strategy):
        """Trend with martingale ×3.375 -> shares = round(5 * 3.375, 2) = 16.88."""
        strategy.state.ss_sizing = "martingale"
        strategy.state.ss_trend_martingale_mult = 3.375
        strategy.state.ss_trend_loss_streak = 3
        sig = _signal(strategy, _make_candle("DOWN"))
        assert sig is not None
        assert sig.shares == 16.88  # round(5.0 * 3.375, 2) = 16.88
        assert sig.multiplier == 3.375
        assert sig.loss_streak == 3


class TestTrendCycle:
    """One side for the whole 4h block, and past it until the cycle wins."""

    def test_opening_a_cycle_records_side_and_anchor(self, strategy):
        candle = _make_candle("UP")
        _signal(strategy, candle)
        assert strategy.state.ss_trend_cycle_side == "UP"
        assert strategy.state.ss_trend_cycle_anchor_ts == candle["ts"]

    def test_holds_side_when_a_later_candle_disagrees(self, strategy):
        """The forming candle turning around must not flip the locked side.

        This deliberately reverses the Fase 4.5 behaviour: following the live
        candle is what the 4h cycle exists to stop (docs/revisar.md punto 6).
        """
        anchor = int(time.time()) - FOUR_HOURS
        assert _signal(strategy, _make_candle("UP", ts=anchor)).direction == "UP"

        # A DOWN candle arrives while the block is still running — ignored.
        newer = _make_candle("DOWN", strength=0.03, ts=anchor + FOUR_HOURS)
        assert _signal(strategy, newer).direction == "UP"

    def test_block_expiry_with_losses_extends_the_cycle(self, strategy):
        """Unrecovered losses keep the side committed past the 4h block."""
        expired = int(time.time()) - 3 * FOUR_HOURS
        _signal(strategy, _make_candle("UP", ts=expired))
        strategy.state.ss_trend_martingale_mult = 2.1  # a loss happened

        sig = _signal(strategy, _make_candle("DOWN", strength=0.03, ts=expired))
        assert sig.direction == "UP"
        assert "prorrogado" in sig.signal_reason

    def test_block_expiry_without_losses_reevaluates(self, strategy):
        """A clean block ends the cycle and the next candle decides again."""
        expired = int(time.time()) - 3 * FOUR_HOURS
        _signal(strategy, _make_candle("UP", ts=expired))
        assert strategy.state.ss_trend_martingale_mult == 1.0

        fresh = _make_candle("DOWN", strength=0.03)
        sig = _signal(strategy, fresh)
        assert sig.direction == "DOWN"
        assert strategy.state.ss_trend_cycle_anchor_ts == fresh["ts"]

    def test_expired_clean_block_stops_when_no_clear_trend(self, strategy):
        expired = int(time.time()) - 3 * FOUR_HOURS
        _signal(strategy, _make_candle("UP", ts=expired))

        assert _signal(strategy, _make_candle("UP", strength=0.001)) is None
        assert strategy.state.ss_trend_cycle_side is None

    def test_loss_keeps_the_side(self, strategy):
        _signal(strategy, _make_candle("UP"))
        with patch("bot.strategy_streak.advance_martingale_state"):
            strategy.on_loss("ss_trend")
        assert strategy.state.ss_trend_cycle_side == "UP"

    def test_win_closes_the_cycle(self, strategy):
        _signal(strategy, _make_candle("UP"))
        with patch("bot.strategy_streak.reset_martingale_state"):
            strategy.on_win("ss_trend")
        assert strategy.state.ss_trend_cycle_side is None
        assert strategy.state.ss_trend_cycle_anchor_ts is None

    def test_win_mid_block_reopens_the_same_side_at_base_size(self, strategy):
        """A win inside the block doesn't interrupt the trend, only the stake."""
        candle = _make_candle("UP")
        _signal(strategy, candle)
        strategy.state.ss_trend_martingale_mult = 2.1
        with patch("bot.strategy_streak.reset_martingale_state"):
            strategy.on_win("ss_trend")

        sig = _signal(strategy, candle)
        assert sig.direction == "UP"
        assert sig.multiplier == 1.0
        assert sig.shares == 5

    def test_cycle_survives_a_restart(self, strategy):
        """The locked side is reloaded from the DB, not re-derived."""
        anchor = int(time.time()) - FOUR_HOURS
        with patch(
            "bot.strategy_streak.get_or_create_martingale_state",
            return_value=MagicMock(
                multiplier=2.1, loss_streak=1,
                cycle_side="DOWN", cycle_anchor_ts=anchor,
            ),
        ):
            revived = StreakSnapperStrategy(strategy.state)

        # A brand-new UP candle must not unseat the reloaded DOWN cycle.
        sig = _signal(revived, _make_candle("UP", strength=0.03))
        assert sig.direction == "DOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# Martingale progression
# ═══════════════════════════════════════════════════════════════════════════════


class TestMartingaleProgression:

    def test_on_win_resets_state(self, strategy):
        strategy.state.ss_fade_martingale_mult = 3.375
        strategy.state.ss_fade_loss_streak = 3
        with patch("bot.strategy_streak.reset_martingale_state") as mock_reset:
            strategy.on_win("ss_fade")
        mock_reset.assert_called_once_with("ss_fade", "btc")
        assert strategy.state.ss_fade_martingale_mult == 1.0
        assert strategy.state.ss_fade_loss_streak == 0

    def test_on_win_trend(self, strategy):
        strategy.state.ss_trend_martingale_mult = 2.25
        strategy.state.ss_trend_loss_streak = 2
        with patch("bot.strategy_streak.reset_martingale_state"):
            strategy.on_win("ss_trend")
        assert strategy.state.ss_trend_martingale_mult == 1.0
        assert strategy.state.ss_trend_loss_streak == 0

    def test_on_loss_multiplies(self, strategy):
        with patch("bot.strategy_streak.advance_martingale_state") as mock_adv:
            strategy.on_loss("ss_fade")
        mock_adv.assert_called_once_with("ss_fade", 1.5, "btc")
        assert strategy.state.ss_fade_martingale_mult == 1.5
        assert strategy.state.ss_fade_loss_streak == 1

    def test_on_loss_trend_independent(self, strategy):
        strategy.state.ss_fade_martingale_mult = 1.0
        strategy.state.ss_fade_loss_streak = 0
        with patch("bot.strategy_streak.advance_martingale_state"):
            strategy.on_loss("ss_trend")
        assert strategy.state.ss_fade_martingale_mult == 1.0
        assert strategy.state.ss_fade_loss_streak == 0
        assert strategy.state.ss_trend_martingale_mult == 1.5
        assert strategy.state.ss_trend_loss_streak == 1

    def test_full_martingale_cycle_6_losses(self, strategy):
        expected_mults = [1.5, 2.25, 3.375, 5.0625, 7.5938, 11.3906]
        with patch("bot.strategy_streak.advance_martingale_state"):
            for i, exp in enumerate(expected_mults):
                strategy.on_loss("ss_fade")
                assert strategy.state.ss_fade_martingale_mult == pytest.approx(exp, rel=1e-3)
                assert strategy.state.ss_fade_loss_streak == i + 1

    def test_win_resets_after_losses(self, strategy):
        strategy.state.ss_fade_martingale_mult = 3.375
        strategy.state.ss_fade_loss_streak = 3
        with patch("bot.strategy_streak.reset_martingale_state"):
            strategy.on_win("ss_fade")
        assert strategy.state.ss_fade_martingale_mult == 1.0
        assert strategy.state.ss_fade_loss_streak == 0

    def test_db_failure_graceful(self, strategy):
        with patch("bot.strategy_streak.reset_martingale_state", side_effect=Exception("DB down")):
            strategy.on_win("ss_fade")
        assert strategy.state.ss_fade_martingale_mult == 1.0
        assert strategy.state.ss_fade_loss_streak == 0

    def test_db_advance_failure_graceful(self, strategy):
        with patch("bot.strategy_streak.advance_martingale_state", side_effect=Exception("DB down")):
            strategy.on_loss("ss_trend")
        assert strategy.state.ss_trend_martingale_mult == 1.5
        assert strategy.state.ss_trend_loss_streak == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Combined
# ═══════════════════════════════════════════════════════════════════════════════


class TestCombinedStrategies:

    def test_both_signals_same_window(self, strategy):
        windows = _make_windows(["DOWN", "UP", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            fade_sig = strategy.get_fade_signal()
        trend_sig = _signal(strategy, _make_candle("DOWN"))
        assert fade_sig is not None
        assert fade_sig.direction == "DOWN"
        assert trend_sig is not None
        assert trend_sig.direction == "DOWN"

    def test_both_signals_opposite_directions(self, strategy):
        windows = _make_windows(["UP", "DOWN", "DOWN", "DOWN", "DOWN"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            fade_sig = strategy.get_fade_signal()
        trend_sig = _signal(strategy, _make_candle("DOWN"))
        assert fade_sig.direction == "UP"
        assert trend_sig.direction == "DOWN"

    def test_independent_martingale_states(self, strategy):
        with patch("bot.strategy_streak.advance_martingale_state"):
            strategy.on_loss("ss_fade")
            strategy.on_loss("ss_fade")
        assert strategy.state.ss_fade_martingale_mult == 2.25
        assert strategy.state.ss_fade_loss_streak == 2
        with patch("bot.strategy_streak.reset_martingale_state"):
            strategy.on_win("ss_trend")
        assert strategy.state.ss_trend_martingale_mult == 1.0
        assert strategy.state.ss_trend_loss_streak == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Sizing — flat / kelly / martingale
# ═══════════════════════════════════════════════════════════════════════════════


class TestKellyFraction:
    """The bet-sizing maths, independent of any strategy."""

    def test_no_edge_at_fair_price_sizes_to_zero(self):
        from bot.config import kelly_fraction

        assert kelly_fraction(0.52, 0.52) == pytest.approx(0.0, abs=1e-9)

    def test_measured_fade_edge(self):
        """53.8% at 0.519 is the measured case: 4.0% of bankroll."""
        from bot.config import kelly_fraction

        assert kelly_fraction(0.538, 0.519) == pytest.approx(0.0395, abs=0.002)

    def test_losing_bet_never_goes_negative(self):
        from bot.config import kelly_fraction

        assert kelly_fraction(0.45, 0.52) == 0.0

    def test_degenerate_prices_are_rejected(self):
        from bot.config import kelly_fraction

        assert kelly_fraction(0.60, 0.0) == 0.0
        assert kelly_fraction(0.60, 1.0) == 0.0


class TestSizing:
    def test_flat_ignores_the_martingale_multiplier(self, strategy):
        strategy.state.ss_sizing = "flat"
        strategy.state.ss_fade_martingale_mult = 8.0
        shares, mult = strategy._size_for("ss_fade", 0.52)
        assert shares == 5.0
        assert mult == 1.0

    def test_kelly_scales_with_bankroll(self, strategy):
        strategy.state.ss_sizing = "kelly"
        strategy.state.ss_kelly_fraction = 0.25
        strategy.state.starting_bankroll = 1000.0
        shares, _ = strategy._size_for("ss_fade", 0.52)
        # quarter Kelly on a 3.75% edge at 0.52 -> ~$9.4 -> ~18 shares
        assert 15.0 <= shares <= 22.0

        strategy.state.starting_bankroll = 100.0
        smaller, _ = strategy._size_for("ss_fade", 0.52)
        assert smaller < shares

    def test_kelly_refuses_a_strategy_with_no_measured_edge(self, strategy):
        """ss_trend measured 48.2% — below its own price, so Kelly sizes to 0."""
        strategy.state.ss_sizing = "kelly"
        shares, mult = strategy._size_for("ss_trend", 0.52)
        assert shares == 0.0
        assert mult == 0.0

    def test_risk_ceiling_caps_a_runaway_martingale(self, strategy):
        strategy.state.ss_sizing = "martingale"
        strategy.state.ss_fade_martingale_mult = 500.0
        strategy.state.starting_bankroll = 1000.0
        shares, _ = strategy._size_for("ss_fade", 0.50)
        # 10% of $1.000 at $0.50 is 200 shares, not 2.500.
        assert shares == pytest.approx(200.0)

    def test_skips_when_minimum_exceeds_the_risk_budget(self, strategy):
        strategy.state.ss_sizing = "flat"
        strategy.state.starting_bankroll = 10.0   # 10% = $1, under 5 shares
        shares, mult = strategy._size_for("ss_fade", 0.52)
        assert shares == 0.0
        assert mult == 0.0

    def test_fade_signal_is_dropped_when_sizing_returns_zero(self, strategy):
        strategy.state.ss_sizing = "kelly"
        strategy.state.starting_bankroll = 10.0
        windows = _make_windows(["DOWN", "UP", "UP", "UP", "UP"])
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows):
            assert strategy.get_fade_signal() is None
