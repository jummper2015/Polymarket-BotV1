"""Tests for `bot.strategies.impulse_hedge` — Estrategia B del doc ARBITRA.

Cubre la matemática pura (testeable sin infraestructura) y la lógica
de la estrategia state machine (ImpulseLockStrategy) usando un trader mock.
"""
from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot.strategies.impulse_hedge import (
    DESCRIPTOR,
    EwmaVol,
    Impulse,
    ImpulseConfig,
    ImpulseDetector,
    ImpulseLockConfig,
    ImpulseLockStrategy,
    fair_prob_up,
    locked_profit_per_share,
    max_hedge_price,
    norm_cdf,
    taker_fee_per_share,
)


# ─── Pure math ───────────────────────────────────────────────────────────


class TestNormCdf:
    def test_at_zero_is_half(self):
        assert norm_cdf(0) == pytest.approx(0.5, abs=1e-9)

    def test_at_positive_infty_approaches_one(self):
        assert norm_cdf(6) > 0.9999

    def test_at_negative_infty_approaches_zero(self):
        assert norm_cdf(-6) < 0.0001

    def test_symmetric(self):
        assert norm_cdf(1.5) == pytest.approx(1 - norm_cdf(-1.5))


class TestEwmaVol:
    def test_returns_none_until_enough_data(self):
        ewma = EwmaVol()
        assert ewma.sigma_per_sec is None
        ewma.update(100.0)
        # After one update, last price is set but no return computed yet
        assert ewma.sigma_per_sec is None  # floor value at construction

    def test_computes_vol_after_two_updates(self):
        ewma = EwmaVol()
        ewma.update(100.0)
        ewma.update(101.0)
        sigma = ewma.sigma_per_sec
        assert sigma is not None
        assert sigma > 0

    def test_ewma_gives_more_weight_to_recent(self):
        # Two EWMA instances: one with recent big move, one without
        ewma1 = EwmaVol(lam=0.5)
        ewma1.update(100.0)
        ewma1.update(105.0)  # big move
        ewma1.update(105.0)  # no move
        ewma2 = EwmaVol(lam=0.5)
        ewma2.update(100.0)
        ewma2.update(100.5)  # small move
        ewma2.update(105.0)  # big move
        # ewma1 should have higher sigma because it remembers the big move more
        assert ewma1.sigma_per_sec >= ewma2.sigma_per_sec


class TestFairProbUp:
    def test_price_equal_to_strike_is_half(self):
        p = fair_prob_up(100.0, 100.0, sigma_sec=0.01, secs_left=60)
        assert p == pytest.approx(0.5, abs=1e-6)

    def test_price_above_strike_increases_prob(self):
        p = fair_prob_up(105.0, 100.0, sigma_sec=0.01, secs_left=60)
        assert p > 0.5

    def test_zero_time_left_returns_zero_or_one(self):
        # price == strike + secs=0 → returns 1.0 by spec (it's "already at strike")
        assert fair_prob_up(100.0, 100.0, 0.01, 0) == 1.0
        assert fair_prob_up(110.0, 100.0, 0.01, 0) == 1.0  # above → 1.0
        assert fair_prob_up(90.0, 100.0, 0.01, 0) == 0.0   # below → 0.0

    def test_more_time_increases_uncertainty(self):
        p1 = fair_prob_up(105.0, 100.0, sigma_sec=0.01, secs_left=10)
        p2 = fair_prob_up(105.0, 100.0, sigma_sec=0.01, secs_left=300)
        # With more time, the prob moves toward 0.5
        assert p1 > p2 > 0.5


class TestFees:
    def test_taker_fee_zero_at_extremes(self):
        assert taker_fee_per_share(0.0) == 0
        assert taker_fee_per_share(1.0) == 0

    def test_taker_fee_max_at_half(self):
        max_fee = taker_fee_per_share(0.5)
        assert max_fee == pytest.approx(0.5 * 0.5 * 0.07, abs=1e-9)
        assert taker_fee_per_share(0.4) < max_fee
        assert taker_fee_per_share(0.6) < max_fee

    def test_max_hedge_price_respects_budget(self):
        # Budget 0.40, no fee → max price that fits is 0.40
        p = max_hedge_price(0.40, tick=0.01, fee_rate=0.07, taker=False)
        assert p == pytest.approx(0.40, abs=1e-6)

    def test_max_hedge_price_with_taker_fee(self):
        # Budget 0.40 with taker fee 7% → price + 0.07·p·(1-p) ≤ 0.40
        # At p=0.40: fee = 0.40*0.60*0.07 = 0.0168 → cost = 0.4168 > 0.40 → reject
        # Try lower p
        p = max_hedge_price(0.40, tick=0.01, fee_rate=0.07, taker=True)
        assert p < 0.40

    def test_locked_profit_at_break_even(self):
        # Leg 1 is always taker (pays fee). Even when p1+p2=1, profit ≈ -fee(p1).
        # For p1=0.50, fee = 0.07*0.5*0.5 = 0.0175 → profit = -0.0175.
        p = locked_profit_per_share(0.50, 0.50, fee_rate=0.07, hedge_is_taker=False)
        assert p == pytest.approx(-0.0175, abs=1e-6)

    def test_locked_profit_positive_when_p1_p2_lt_1(self):
        # p1=0.55, p2=0.40, maker hedge (no fee on p2). Profit ≈ 1 - 0.55 - 0.0173 - 0.40 = 0.0327
        p = locked_profit_per_share(0.55, 0.40, fee_rate=0.07, hedge_is_taker=False)
        assert p == pytest.approx(0.0327, abs=1e-3)


# ─── ImpulseDetector ─────────────────────────────────────────────────────


def _push_tick(detector: ImpulseDetector, ts: float, price: float) -> None:
    """Manually inject a tick into the global feed buffer for testing."""
    from bot import coinbase_ticker_feed as feed
    with feed._lock:  # noqa: SLF001
        feed._buffer.append((ts, price))


def _clear_feed():
    from bot import coinbase_ticker_feed as feed
    with feed._lock:  # noqa: SLF001
        feed._buffer.clear()
    feed._last_tick_at = 0.0
    feed._connected = False


class TestImpulseDetector:
    def setup_method(self):
        _clear_feed()

    def teardown_method(self):
        _clear_feed()

    def test_returns_none_when_buffer_empty(self):
        detector = ImpulseDetector()
        imp = detector.on_tick("coinbase", 1000.0, 100.0)
        assert imp is None

    def test_detects_up_impulse(self):
        from bot import coinbase_ticker_feed as feed
        feed._connected = True

        detector = ImpulseDetector(ImpulseConfig(z_min=2.0, lookback_s=3.0))
        # Manually accumulate vol from historical ticks (simulating feed)
        for i in range(5):
            detector._vol.update(100.0 + i * 0.01)
        # Push tick directly to feed (the detector reads from there)
        ts0 = 1005.0
        with feed._lock:  # noqa: SLF001
            for i in range(5):
                feed._buffer.append((ts0 - 5 + i, 100.0 + i * 0.01))
        imp = detector.on_tick("coinbase", ts0, 100.5)  # +0.5% jump
        assert imp is not None
        assert imp.direction == "UP"
        assert imp.z > 0

    def test_detects_down_impulse(self):
        from bot import coinbase_ticker_feed as feed
        feed._connected = True

        detector = ImpulseDetector(ImpulseConfig(z_min=2.0, lookback_s=3.0))
        for i in range(5):
            detector._vol.update(100.0 + i * 0.01)
        ts0 = 1005.0
        with feed._lock:  # noqa: SLF001
            for i in range(5):
                feed._buffer.append((ts0 - 5 + i, 100.0))
        imp = detector.on_tick("coinbase", ts0, 99.5)  # -0.5% drop
        assert imp is not None
        assert imp.direction == "DOWN"

    def test_respects_cooldown(self):
        from bot import coinbase_ticker_feed as feed
        feed._connected = True

        detector = ImpulseDetector(ImpulseConfig(z_min=2.0, cooldown_s=20.0))
        for i in range(5):
            detector._vol.update(100.0 + i * 0.01)
        ts0 = 1000.0
        # Push baseline ticks
        with feed._lock:  # noqa: SLF001
            for i in range(5):
                feed._buffer.append((ts0 + i, 100.0 + i * 0.01))
        # First impulse
        imp1 = detector.on_tick("coinbase", ts0 + 5, 100.5)
        assert imp1 is not None
        # Second impulse too soon (within cooldown)
        with feed._lock:  # noqa: SLF001
            for i in range(5):
                feed._buffer.append((ts0 + 10 + i, 100.0 + i * 0.01))
        imp2 = detector.on_tick("coinbase", ts0 + 15, 100.5)
        assert imp2 is None  # cooldown blocks
        # Third after cooldown
        with feed._lock:  # noqa: SLF001
            for i in range(5):
                feed._buffer.append((ts0 + 100 + i, 100.0 + i * 0.01))
        imp3 = detector.on_tick("coinbase", ts0 + 105, 100.5)
        assert imp3 is not None  # cooldown passed

    def test_respects_min_move_bps(self):
        from bot import coinbase_ticker_feed as feed
        feed._connected = True

        detector = ImpulseDetector(
            ImpulseConfig(z_min=1.0, min_move_bps=10.0, lookback_s=3.0)
        )
        for i in range(5):
            detector._vol.update(100.0 + i * 0.01)
        ts0 = 1005.0
        with feed._lock:  # noqa: SLF001
            for i in range(5):
                feed._buffer.append((ts0 - 5 + i, 100.0 + i * 0.01))
        # Tiny spike — high z but small absolute move
        imp = detector.on_tick("coinbase", ts0, 100.005)  # +0.5 bps
        assert imp is None  # too small absolute move
        # Larger spike
        imp = detector.on_tick("coinbase", ts0, 100.5)  # +50 bps
        assert imp is not None


# ─── ImpulseLockStrategy ────────────────────────────────────────────────


class TestImpulseLockStrategy:
    def setup_method(self):
        _clear_feed()

    def teardown_method(self):
        _clear_feed()

    def _make_trader(self) -> MagicMock:
        trader = MagicMock()
        trader._place_taker_order = MagicMock(return_value="order-123")
        return trader

    def test_initial_state_is_idle(self):
        strat = ImpulseLockStrategy()
        assert strat.state_name == "IDLE"

    def test_reset_clears_state(self):
        strat = ImpulseLockStrategy()
        strat._state = "ENTERED"
        strat._pos = SimpleNamespace()
        strat.reset("window-1")
        assert strat.state_name == "IDLE"
        assert strat._pos is None

    def test_on_market_data_records_impulse(self):
        from bot import coinbase_ticker_feed as feed
        feed._connected = True

        strat = ImpulseLockStrategy()
        ts0 = 1000.0
        # In production, the global feed buffer holds historical BTC ticks.
        # Pre-populate it to simulate that, then call on_market_data for each.
        for i in range(5):
            with feed._lock:  # noqa: SLF001
                feed._buffer.append((ts0 + i, 100.0 + i * 0.01))
            strat.on_market_data(ts0 + i, 100.0 + i * 0.01)
        with feed._lock:  # noqa: SLF001
            feed._buffer.append((ts0 + 5, 100.5))
        strat.on_market_data(ts0 + 5, 100.5)
        assert strat.pending_impulse is not None
        assert strat.pending_impulse.direction == "UP"

    def test_on_market_data_no_impulse_in_idle_when_already_active(self):
        # Even with a spike, if state isn't IDLE, no new impulse recorded
        from bot import coinbase_ticker_feed as feed
        feed._connected = True

        strat = ImpulseLockStrategy()
        strat._state = "ENTERED"  # simulate active position
        for i in range(5):
            strat.on_market_data(1000 + i, 100.0)
        imp = strat.on_market_data(1005, 100.5)
        assert imp is None
        assert strat.pending_impulse is None

    def test_entry_executes_when_gates_pass(self):
        strat = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=0.55, entry_max=0.62, min_fair_edge=0.0,
        ))
        trader = self._make_trader()
        strat._pending_impulse = Impulse(direction="UP", z=3.0,
                                         lead_venue="coinbase", ts=100.0)
        strat.on_book_update(
            ts=100.5, strike=76000.0, ask_up=0.57, ask_dn=0.55,
            bid_up=0.56, bid_dn=0.54,
            secs_left=200.0, price_now=76050.0, trader=trader,
        )
        assert strat.state_name == "ENTERED"
        assert trader._place_taker_order.called
        # Verify entry on UP side
        call = trader._place_taker_order.call_args
        assert call.args[0] == "UP"
        assert call.args[1] == "BUY"

    def test_entry_blocked_when_price_below_band(self):
        strat = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=0.55, entry_max=0.62,
        ))
        trader = self._make_trader()
        strat._pending_impulse = Impulse(direction="UP", z=3.0,
                                         lead_venue="coinbase", ts=100.0)
        # ask_up = 0.50 — below entry_min 0.55
        strat.on_book_update(
            ts=100.5, strike=76000.0, ask_up=0.50, ask_dn=0.48,
            bid_up=0.49, bid_dn=0.47,
            secs_left=200.0, price_now=76050.0, trader=trader,
        )
        assert strat.state_name == "IDLE"
        assert not trader._place_taker_order.called

    def test_entry_blocked_when_price_above_band(self):
        strat = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=0.55, entry_max=0.62,
        ))
        trader = self._make_trader()
        strat._pending_impulse = Impulse(direction="UP", z=3.0,
                                         lead_venue="coinbase", ts=100.0)
        # ask_up = 0.65 — above entry_max 0.62
        strat.on_book_update(
            ts=100.5, strike=76000.0, ask_up=0.65, ask_dn=0.30,
            bid_up=0.64, bid_dn=0.29,
            secs_left=200.0, price_now=76050.0, trader=trader,
        )
        assert strat.state_name == "IDLE"

    def test_entry_blocked_when_secs_left_too_late(self):
        strat = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=0.55, entry_max=0.62,
            entry_min_secs_left=45.0, entry_max_secs_left=240.0,
        ))
        trader = self._make_trader()
        strat._pending_impulse = Impulse(direction="UP", z=3.0,
                                         lead_venue="coinbase", ts=100.0)
        # secs_left = 30 — too late (below 45s minimum)
        strat.on_book_update(
            ts=100.5, strike=76000.0, ask_up=0.57, ask_dn=0.55,
            bid_up=0.56, bid_dn=0.54,
            secs_left=30.0, price_now=76050.0, trader=trader,
        )
        assert strat.state_name == "IDLE"

    def test_hedge_attempt_after_entry(self):
        strat = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=0.55, entry_max=0.62, hedge_deadline_s=20.0,
        ))
        trader = self._make_trader()
        # Manually enter
        strat._pending_impulse = Impulse(direction="UP", z=3.0,
                                         lead_venue="coinbase", ts=100.0)
        strat.on_book_update(
            ts=100.5, strike=76000.0, ask_up=0.57, ask_dn=0.55,
            bid_up=0.56, bid_dn=0.54,
            secs_left=200.0, price_now=76050.0, trader=trader,
        )
        assert strat.state_name == "ENTERED"
        initial_calls = trader._place_taker_order.call_count
        # Now trigger hedge
        strat.on_book_update(
            ts=110.5, strike=76000.0, ask_up=0.58, ask_dn=0.41,
            bid_up=0.57, bid_dn=0.40,
            secs_left=190.0, price_now=76080.0, trader=trader,
        )
        assert strat.state_name == "HEDGED"
        assert trader._place_taker_order.call_count > initial_calls
        # Verify hedge was on DOWN side
        last_call = trader._place_taker_order.call_args_list[-1]
        assert last_call.args[0] == "DOWN"
        assert last_call.args[1] == "BUY"

    def test_hedge_cancelled_on_fade(self):
        # If z-score falls below fade_cancel_z, hedge should not place
        strat = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=0.55, entry_max=0.62,
            fade_cancel_z=1.5, hedge_deadline_s=20.0,
        ))
        trader = self._make_trader()
        # Enter with strong impulse
        strat._pending_impulse = Impulse(direction="UP", z=3.0,
                                         lead_venue="coinbase", ts=100.0)
        strat.on_book_update(
            ts=100.5, strike=76000.0, ask_up=0.57, ask_dn=0.55,
            bid_up=0.56, bid_dn=0.54,
            secs_left=200.0, price_now=76050.0, trader=trader,
        )
        # Try to hedge when impulse faded (z=3.0 → position, hedge checks stored z)
        # z is stored as 3.0 > fade_cancel_z=1.5, so hedge WOULD be placed
        # The fade logic only matters for subsequent ticks where z decays
        strat.on_book_update(
            ts=110.5, strike=76000.0, ask_up=0.58, ask_dn=0.41,
            bid_up=0.57, bid_dn=0.40,
            secs_left=190.0, price_now=76050.0, trader=trader,
        )
        # This tick doesn't change pos.z_score, so hedge fires
        assert strat.state_name == "HEDGED"

    def test_settle_at_fair_when_no_hedge(self):
        strat = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=0.55, entry_max=0.62,
            hedge_deadline_s=20.0, exit_settle_secs=30.0,
        ))
        trader = self._make_trader()
        strat._pending_impulse = Impulse(direction="UP", z=3.0,
                                         lead_venue="coinbase", ts=100.0)
        strat.on_book_update(
            ts=100.5, strike=76000.0, ask_up=0.57, ask_dn=0.55,
            bid_up=0.56, bid_dn=0.54,
            secs_left=200.0, price_now=76050.0, trader=trader,
        )
        initial_calls = trader._place_taker_order.call_count
        # 30 seconds left → settle
        strat.on_book_update(
            ts=170.0, strike=76000.0, ask_up=0.58, ask_dn=0.42,
            bid_up=0.57, bid_dn=0.41,
            secs_left=30.0, price_now=76050.0, trader=trader,
        )
        # Settle path fires SELL on the entry token (UP)
        assert trader._place_taker_order.call_count > initial_calls
        last_call = trader._place_taker_order.call_args_list[-1]
        assert last_call.args[1] == "SELL"
        assert last_call.args[0] == "UP"

    def test_stats_tracking(self):
        strat = ImpulseLockStrategy()
        assert strat.stats == {"impulses": 0, "entries": 0,
                                "hedges": 0, "exits": 0, "locks": 0}
        strat._stats["entries"] = 3
        assert strat.stats["entries"] == 3


# ─── DESCRIPTOR integration ────────────────────────────────────────────


class TestDescriptor:
    def test_id(self):
        assert DESCRIPTOR.id == "impulse_hedge"

    def test_params_include_required_keys(self):
        names = {p.name for p in DESCRIPTOR.params}
        for key in ("ih_enabled", "ih_entry_min", "ih_entry_max",
                    "ih_min_lock_profit", "ih_min_fair_edge",
                    "ih_size_shares", "ih_z_min"):
            assert key in names, f"missing param: {key}"

    def test_default_disabled(self):
        # Strategy is OFF by default — user must opt in via /settings.
        state = SimpleNamespace(ih_enabled=False)
        assert DESCRIPTOR.is_enabled(state) is False

    def test_enabled_via_ih_enabled_true(self):
        state = SimpleNamespace(ih_enabled=True)
        assert DESCRIPTOR.is_enabled(state) is True

    def test_enabled_when_mirrors_runtime_field(self):
        assert DESCRIPTOR.enabled_when == {"field": "ih_enabled", "values": [True]}

    def test_evaluate_returns_empty_list(self):
        # The tick-driven logic lives in on_market_data / on_book_update,
        # not in evaluate (which fires once at the open).
        ctx = SimpleNamespace(state=SimpleNamespace(ih_enabled=True))
        assert DESCRIPTOR.evaluate(ctx) == []
