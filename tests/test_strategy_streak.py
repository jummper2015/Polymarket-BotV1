"""Unit tests for strategy_streak.py — mocked Binance API and DB calls."""

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.strategy_streak import StreakSnapperStrategy, StreakSignal


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_windows(directions: list[str]) -> list[dict]:
    base_ts = 1_785_600_000
    return [
        {"ts": base_ts + i * 300, "open": 62000.0, "close": 62000.0, "direction": d}
        for i, d in enumerate(directions)
    ]


def _make_trend(direction: str, ts: int = 1_785_600_000) -> dict:
    return {
        "ts": ts,
        "open": 63000.0,
        "close": 63500.0 if direction == "UP" else 62500.0,
        "direction": direction,
    }


class FakeState:
    def __init__(self):
        self.ss_fade_base_shares = 5.0
        self.ss_fade_limit_cap = 0.60
        self.ss_fade_streak_min = 4
        self.ss_trend_base_shares = 5.0
        self.ss_trend_limit_cap = 0.52
        self.ss_martingale_mult_factor = 1.5
        self.ss_fade_martingale_mult = 1.0
        self.ss_fade_loss_streak = 0
        self.ss_trend_martingale_mult = 1.0
        self.ss_trend_loss_streak = 0


@pytest.fixture
def strategy():
    state = FakeState()
    with patch(
        "bot.strategy_streak.get_or_create_martingale_state",
        return_value=MagicMock(multiplier=1.0, loss_streak=0),
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


class TestTrendSignal:

    def test_up_trend_signal(self, strategy):
        trend = _make_trend("UP")
        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend):
            sig = strategy.get_trend_signal()
        assert sig is not None
        assert sig.strategy == "ss_trend"
        assert sig.direction == "UP"
        assert sig.shares == 5
        assert sig.limit_cap == 0.52

    def test_down_trend_signal(self, strategy):
        trend = _make_trend("DOWN")
        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend):
            sig = strategy.get_trend_signal()
        assert sig is not None
        assert sig.direction == "DOWN"

    def test_no_data_returns_none(self, strategy):
        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=None):
            sig = strategy.get_trend_signal()
        assert sig is None

    def test_trend_caching(self, strategy):
        trend1 = _make_trend("UP", ts=1_785_600_000)
        trend2 = _make_trend("UP", ts=1_785_600_000)
        trend2["_cached"] = True

        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend1):
            sig1 = strategy.get_trend_signal()
        assert sig1 is not None
        assert sig1.direction == "UP"

        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend2):
            sig2 = strategy.get_trend_signal()
        assert sig2 is not None
        assert sig2.direction == "UP"

    def test_trend_follows_intra_candle_flip(self, strategy):
        """The 4h candle is still forming — if it flips, follow the new direction.

        Regression: the direction used to be latched on first read, so a candle
        that opened UP and turned DOWN kept signalling UP for up to four hours.
        """
        ts = 1_785_600_000
        with patch("bot.strategy_streak.get_4h_trend_cached",
                   return_value=_make_trend("UP", ts=ts)):
            assert strategy.get_trend_signal().direction == "UP"

        # Same candle (same ts), price has since fallen below the open.
        with patch("bot.strategy_streak.get_4h_trend_cached",
                   return_value=_make_trend("DOWN", ts=ts)):
            assert strategy.get_trend_signal().direction == "DOWN"

    def test_trend_changes_direction(self, strategy):
        trend1 = _make_trend("UP", ts=1_785_600_000)
        trend2 = _make_trend("DOWN", ts=1_785_614_400)

        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend1):
            sig1 = strategy.get_trend_signal()
        assert sig1.direction == "UP"

        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend2):
            sig2 = strategy.get_trend_signal()
        assert sig2.direction == "DOWN"

    def test_with_martingale_multiplier(self, strategy):
        """Trend with martingale ×3.375 -> shares = round(5 * 3.375, 2) = 16.88."""
        strategy.state.ss_trend_martingale_mult = 3.375
        strategy.state.ss_trend_loss_streak = 3
        trend = _make_trend("DOWN")
        with patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend):
            sig = strategy.get_trend_signal()
        assert sig is not None
        assert sig.shares == 16.88  # round(5.0 * 3.375, 2) = 16.88
        assert sig.multiplier == 3.375
        assert sig.loss_streak == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Martingale progression
# ═══════════════════════════════════════════════════════════════════════════════


class TestMartingaleProgression:

    def test_on_win_resets_state(self, strategy):
        strategy.state.ss_fade_martingale_mult = 3.375
        strategy.state.ss_fade_loss_streak = 3
        with patch("bot.strategy_streak.reset_martingale_state") as mock_reset:
            strategy.on_win("ss_fade")
        mock_reset.assert_called_once_with("ss_fade")
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
        mock_adv.assert_called_once_with("ss_fade", 1.5)
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
        trend   = _make_trend("DOWN")
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows), \
             patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend):
            fade_sig  = strategy.get_fade_signal()
            trend_sig = strategy.get_trend_signal()
        assert fade_sig is not None
        assert fade_sig.direction == "DOWN"
        assert trend_sig is not None
        assert trend_sig.direction == "DOWN"

    def test_both_signals_opposite_directions(self, strategy):
        windows = _make_windows(["UP", "DOWN", "DOWN", "DOWN", "DOWN"])
        trend   = _make_trend("DOWN")
        with patch("bot.strategy_streak.get_5min_windows", return_value=windows), \
             patch("bot.strategy_streak.get_4h_trend_cached", return_value=trend):
            fade_sig  = strategy.get_fade_signal()
            trend_sig = strategy.get_trend_signal()
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
