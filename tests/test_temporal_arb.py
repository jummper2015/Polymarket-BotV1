"""Tests for Temporal Arbitrage strategy — bot/strategies/temporal_arb.py."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from bot.strategies.temporal_arb import (
    find_leader_side,
    second_leg_worthwhile,
    _get_window,
    _TAWindow,
    _observe,
    DESCRIPTOR,
)
from bot.strategies.base import StrategyContext


# ── isolation ───────────────────────────────────────────────────────────────
# The TWAP-based entry signal (added 2026-09-21) pulls from the global
# `coinbase_ticker_feed._buffer`. If a previous test left ticks there
# (e.g., from test_polymarket_twap_tracker), the rolling TWAP will return
# a non-None value and override the spot used by the test. Reset the feed
# before each test so signal behavior is fully under the test's control.
@pytest.fixture(autouse=True)
def _reset_global_feed():
    from bot import coinbase_ticker_feed as _feed
    with _feed._lock:  # noqa: SLF001
        _feed._buffer.clear()
    _feed._last_tick_at = 0.0
    _feed._connected = False
    yield
    with _feed._lock:  # noqa: SLF001
        _feed._buffer.clear()
    _feed._last_tick_at = 0.0
    _feed._connected = False


# ── find_leader_side ─────────────────────────────────────────────────────────

class TestFindLeaderSide:
    def test_btc_above_strike_leader_is_up(self):
        # BTC moved +0.1% above strike → UP is the leader
        side, px, itm = find_leader_side(
            spot=60060.0, strike=60000.0,
            ask_up=0.48, ask_dn=0.54,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side == "UP"
        assert px == 0.48
        assert itm == pytest.approx(0.1, rel=1e-3)

    def test_btc_below_strike_leader_is_down(self):
        # BTC moved -0.1% below strike → DOWN is the leader
        side, px, itm = find_leader_side(
            spot=59940.0, strike=60000.0,
            ask_up=0.54, ask_dn=0.48,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side == "DOWN"
        assert px == 0.48
        assert itm == pytest.approx(-0.1, rel=1e-3)

    def test_itm_below_threshold_returns_none(self):
        # Only 0.02% movement — coin-flip territory, no signal
        side, px, itm = find_leader_side(
            spot=60012.0, strike=60000.0,
            ask_up=0.48, ask_dn=0.54,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side is None
        assert abs(itm) < 0.05

    def test_leader_ask_too_high_returns_none(self):
        # Market already repriced the leader above 0.55 — no misprice to exploit
        side, px, itm = find_leader_side(
            spot=60060.0, strike=60000.0,
            ask_up=0.62, ask_dn=0.40,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side is None

    def test_leader_ask_too_low_returns_none(self):
        # Leader ask below 0.40 — market over-discounted, no edge
        side, px, itm = find_leader_side(
            spot=60060.0, strike=60000.0,
            ask_up=0.35, ask_dn=0.67,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side is None

    def test_leader_ask_at_band_boundaries(self):
        # Exactly at min_ask (0.40) — should enter
        side, px, _ = find_leader_side(
            spot=60060.0, strike=60000.0,
            ask_up=0.40, ask_dn=0.62,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side == "UP"
        assert px == 0.40

        # Exactly at max_ask (0.55) — should enter
        side, px, _ = find_leader_side(
            spot=60060.0, strike=60000.0,
            ask_up=0.55, ask_dn=0.47,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side == "UP"
        assert px == 0.55

    def test_missing_spot_returns_none(self):
        side, px, itm = find_leader_side(
            spot=None, strike=60000.0,
            ask_up=0.48, ask_dn=0.54,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side is None
        assert itm == 0.0

    def test_missing_strike_returns_none(self):
        side, px, itm = find_leader_side(
            spot=60060.0, strike=None,
            ask_up=0.48, ask_dn=0.54,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side is None

    def test_missing_leader_ask_returns_none(self):
        # UP is the leader but its ask is None
        side, px, _ = find_leader_side(
            spot=60060.0, strike=60000.0,
            ask_up=None, ask_dn=0.54,
            min_itm_pct=0.05, min_ask=0.40, max_ask=0.55,
        )
        assert side is None


# ── second_leg_worthwhile ────────────────────────────────────────────────────

class TestSecondLegWorthwhile:
    def test_pair_fits_within_cap(self):
        assert second_leg_worthwhile(0.27, 0.49, 0.82) is True   # 0.76 ≤ 0.82

    def test_pair_exactly_at_cap(self):
        assert second_leg_worthwhile(0.35, 0.47, 0.82) is True   # 0.82 = 0.82

    def test_pair_exceeds_cap(self):
        assert second_leg_worthwhile(0.35, 0.50, 0.82) is False  # 0.85 > 0.82

    def test_realistic_ta_pair(self):
        # Leader bought at 0.48, reversion brings opposite to 0.30: 0.78 ≤ 0.82
        assert second_leg_worthwhile(0.48, 0.30, 0.82) is True
        # Opposite still at 0.40: 0.88 > 0.82 → skip
        assert second_leg_worthwhile(0.48, 0.40, 0.82) is False


# ── window auto-reset ────────────────────────────────────────────────────────

class TestGetWindow:
    def test_same_window_ts_returns_same_object(self):
        w1 = _get_window("btc", 1000)
        w2 = _get_window("btc", 1000)
        assert w1 is w2

    def test_new_window_ts_resets(self):
        w1 = _get_window("eth_ta_reset", 1000)
        w1.phase = "complete"
        w2 = _get_window("eth_ta_reset", 1300)
        assert w2.phase == "idle"
        assert w2 is not w1

    def test_different_symbols_independent(self):
        wb = _get_window("btc_ta_test", 9999)
        we = _get_window("eth_ta_test", 9999)
        wb.phase = "half_open"
        assert we.phase == "idle"


# ── _observe state machine ───────────────────────────────────────────────────

def _make_tokens(window_ts=1_000_000, up="UP_TOK", dn="DN_TOK", slug="slug-1"):
    return SimpleNamespace(
        window_ts=window_ts,
        up_token_id=up,
        down_token_id=dn,
        slug=slug,
    )


def _make_state(
    *,
    ta_enabled=True,
    ta_min_itm_pct=0.05,
    ta_min_ask=0.40,
    ta_max_ask=0.55,
    ta_complete_cap=0.82,
    ta_shares_per_leg=5.0,
    ta_entry_cutoff_sec=150.0,
    ta_bailout_sec=60.0,
    ta_cancel_all_sec=10.0,
    ta_twap_hedge_enabled=False,   # off by default; tests opt in
    ta_hedge_enabled=True,        # off when path B/C must be isolated
    ta_hedge_max_sum=0.92,
    ta_use_twap_signal=False,   # off in tests by default to keep them isolated
    ta_profit_lock_enabled=False,
    ta_profit_lock_min_secs=30,
    ta_profit_lock_cap=1.0,
    ta_mart_hedge_enabled=False,
    ta_mart_hedge_min_secs=30,
    ta_mart_hedge_mult=2.0,
    ta_mart_hedge_max_rounds=3,
    ta_mart_hedge_loss_fallback_pct=0.50,  # spec 2026-10-06 (Issue #3)
    ta_grace_secs=0.0,   # default to 0 in tests; specific tests opt-in
    ta_mh_monitor_secs=30.0,   # monitor window between rounds (rev 2026-10-05)
    ask_up=0.50,
    ask_dn=0.50,
    spot_price=60060.0,   # +0.1% above strike by default
    logged_bailout=False,  # set True to suppress the normal bailout log
    skips=None,
    obs=None,
    mode="paper",
):
    skips = skips if skips is not None else []
    obs   = obs   if obs   is not None else []
    state = SimpleNamespace(
        ta_enabled=ta_enabled,
        ta_min_itm_pct=ta_min_itm_pct,
        ta_min_ask=ta_min_ask,
        ta_max_ask=ta_max_ask,
        ta_complete_cap=ta_complete_cap,
        ta_shares_per_leg=ta_shares_per_leg,
        ta_entry_cutoff_sec=ta_entry_cutoff_sec,
        ta_bailout_sec=ta_bailout_sec,
        ta_cancel_all_sec=ta_cancel_all_sec,
        ta_twap_hedge_enabled=ta_twap_hedge_enabled,
        ta_hedge_enabled=ta_hedge_enabled,
        ta_hedge_max_sum=ta_hedge_max_sum,
        ta_use_twap_signal=ta_use_twap_signal,
        ta_profit_lock_enabled=ta_profit_lock_enabled,
        ta_profit_lock_min_secs=ta_profit_lock_min_secs,
        ta_profit_lock_cap=ta_profit_lock_cap,
        ta_mart_hedge_enabled=ta_mart_hedge_enabled,
        ta_mart_hedge_min_secs=ta_mart_hedge_min_secs,
        ta_mart_hedge_mult=ta_mart_hedge_mult,
        ta_mart_hedge_max_rounds=ta_mart_hedge_max_rounds,
        ta_mart_hedge_loss_fallback_pct=ta_mart_hedge_loss_fallback_pct,
        ta_grace_secs=ta_grace_secs,
        ta_mh_monitor_secs=ta_mh_monitor_secs,
        ta_logged_bailout=logged_bailout,  # NEW: lets tests suppress the bailout
        spot_price=spot_price,
        mode=mode,
    )
    state.get_asks = lambda: (ask_up, ask_dn)
    state.record_skip = lambda r: skips.append(r)
    state.record_observation = lambda k: obs.append(k)
    return state


def _make_trader(orders=None, fills=None, records=None):
    orders  = orders  if orders  is not None else []
    fills   = fills   if fills   is not None else []
    records = records if records is not None else []
    trader = MagicMock()
    trader._place_taker_order.side_effect = lambda tok, side, px, sh: (
        orders.append((tok, side, px, sh)) or f"order-{tok[:6]}-{int(px*100)}"
    )
    trader._record_box_fill.side_effect = lambda *a, **kw: records.append((a, kw))
    return trader


def _ctx(state, tokens, trader, seconds_left=200.0):
    return StrategyContext(
        state=state, symbol="btc_ta", tokens=tokens, trader=trader,
        seconds_left=seconds_left,
    )


STRIKE = 60000.0   # strike cached in _TAWindow for observe tests


class TestObserveIdle:
    """Tests for the IDLE → HALF_OPEN transition."""

    def _call(self, state, tokens, trader, secs=200.0, strike=STRIKE):
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.polymarket_price.get_strike", return_value=strike),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.indicators.get_volume_ratio", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
        ):
            win = _TAWindow(window_ts=tokens.window_ts)
            gw.return_value = win
            ctx = _ctx(state, tokens, trader, secs)
            _observe(ctx)
            return win

    def test_leader_in_band_buys_first_leg(self):
        # BTC +0.1% above strike, UP ask at 0.48 (in band) → buy UP
        orders, records = [], []
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader(orders=orders, records=records)
        win = self._call(state, tokens, trader)
        assert win.phase == "half_open"
        assert win.first_side == "UP"
        assert win.first_px == 0.48
        assert len(orders) == 1
        assert orders[0][1] == "BUY"
        assert len(records) == 1
        assert records[0][1]["strategy"] == "temporal_arb"

    def test_down_leader_buys_first_leg(self):
        # BTC -0.1% below strike, DOWN ask at 0.47 → buy DOWN
        orders = []
        state = _make_state(ask_up=0.55, ask_dn=0.47, spot_price=59940.0)
        tokens = _make_tokens()
        trader = _make_trader(orders=orders)
        win = self._call(state, tokens, trader)
        assert win.phase == "half_open"
        assert win.first_side == "DOWN"
        assert win.first_px == 0.47

    def test_no_signal_when_itm_below_threshold(self):
        # BTC barely moved (0.02%) — no signal
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60012.0)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader)
        assert win.phase == "idle"
        trader._place_taker_order.assert_not_called()

    def test_no_signal_when_ask_above_band(self):
        # BTC up +0.1% but market already repriced UP to 0.62
        state = _make_state(ask_up=0.62, ask_dn=0.40, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader)
        assert win.phase == "idle"
        trader._place_taker_order.assert_not_called()

    def test_skip_late_when_cutoff_passed(self):
        # Spec 2026-10-06: el gate del IDLE usa `ta_entry_cutoff_sec` (default
        # 120s) en vez del hardcoded 30s. secs=119 < 120 → cierra ventana.
        skips = []
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0, skips=skips)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader, secs=20.0)  # < 120 cutoff
        assert win.phase == "closed"
        assert "TA_SKIP_NO_IMPULSE" in skips
        trader._place_taker_order.assert_not_called()

    def test_no_spot_stays_idle(self):
        # spot_price is None — can't calculate itm_pct
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=None)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader)
        assert win.phase == "idle"

    def test_strike_fetch_failure_stays_idle(self):
        # Polymarket price API returns None — retry next tick
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.polymarket_price.get_strike", return_value=None),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.warn"),
        ):
            tokens = _make_tokens()
            state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0)
            trader = _make_trader()
            win = _TAWindow(window_ts=tokens.window_ts)
            gw.return_value = win
            _observe(_ctx(state, tokens, trader))
        assert win.phase == "idle"
        trader._place_taker_order.assert_not_called()

    def test_strike_cached_after_first_fetch(self):
        """Second tick should NOT call get_strike again."""
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.polymarket_price.get_strike",
                  return_value=None) as mock_fetch,
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.warn"),
        ):
            tokens = _make_tokens()
            state = _make_state(ask_up=0.62, ask_dn=0.40, spot_price=60060.0)
            trader = _make_trader()
            # Pre-load the strike so get_strike shouldn't be called
            win = _TAWindow(window_ts=tokens.window_ts, strike=STRIKE)
            gw.return_value = win
            _observe(_ctx(state, tokens, trader))
        mock_fetch.assert_not_called()

    def test_taker_order_failure_stays_idle(self):
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.warn"),
        ):
            tokens = _make_tokens()
            state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0)
            trader = MagicMock()
            trader._place_taker_order.return_value = None  # order rejected
            win = _TAWindow(window_ts=tokens.window_ts)
            gw.return_value = win
            _observe(_ctx(state, tokens, trader))
        assert win.phase == "idle"


class TestObserveHalfOpen:
    def _call(self, state, tokens, trader, win, secs=200.0):
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
        ):
            ctx = _ctx(state, tokens, trader, secs)
            _observe(ctx)

    def test_second_leg_cheap_completes_pair(self):
        # Leader UP bought at 0.48; BTC reverted; DOWN now 0.30: 0.78 ≤ 0.82
        records = []
        state = _make_state(ask_up=0.72, ask_dn=0.30, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader(records=records)
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.48, strike=STRIKE)
        self._call(state, tokens, trader, win)
        assert win.phase == "complete"
        assert any(r[1].get("strategy") == "temporal_arb" for r in records)

    def test_second_leg_too_expensive_stays_half_open(self):
        # DOWN still at 0.40: 0.48 + 0.40 = 0.88 > 0.82
        state = _make_state(ask_up=0.72, ask_dn=0.40, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.48, strike=STRIKE)
        self._call(state, tokens, trader, win, secs=120.0)
        assert win.phase == "half_open"
        trader._place_taker_order.assert_not_called()

    def test_bailout_closes_when_time_runs_out(self):
        skips = []
        state = _make_state(ask_up=0.72, ask_dn=0.40, skips=skips, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.48, strike=STRIKE)
        self._call(state, tokens, trader, win, secs=45.0)  # ≤ bail_sec=60
        assert win.phase == "closed"
        assert "TA_BAILOUT" in skips

    def test_bailout_only_logged_once(self):
        skips = []
        state = _make_state(ask_up=0.72, ask_dn=0.40, skips=skips, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.48, strike=STRIKE,
                        logged_bailout=True)
        self._call(state, tokens, trader, win, secs=45.0)
        assert "TA_BAILOUT" not in skips

    def test_twap_hedge_fires_when_resolution_oracle_opposes_position(self):
        """Path C: TWAP-aware early hedge in the last 60s.

        Setup: position UP @0.55 entered in T+30s, secs=45 (last minute).
        TWAP-60s is accumulating in [T+240, T+300]; projected winner is DOWN
        with margin -$300 (well above ta_twap_hedge_margin=50).
        ta_twap_hedge_enabled=True (default is False; we opt in for this test).

        Expected: bot buys DOWN as hedge, transitions to "hedged".
        """
        from bot import polymarket_twap_tracker as twap_tracker

        twap_tracker.clear_all()
        records = []

        # Position UP @0.55; DOWN ask is 0.35. Pair sum = 0.90 ≤ 0.92 ✓.
        # spot=$60,000 = right at strike → BTC hasn't moved; but TWAP projection says DOWN.
        state = _make_state(
            ask_up=0.81, ask_dn=0.35, spot_price=60_000.0,
            ta_twap_hedge_enabled=True,   # opt in
        )
        tokens = _make_tokens()
        trader = _make_trader(records=records)
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.55, first_shares_filled=80.0,
            strike=STRIKE,
        )

        # Mock the TWAP tracker to project DOWN as winner with strong margin.
        fake_twap_state = SimpleNamespace(
            projected_winner="DOWN",
            margin=-300.0,
        )

        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=fake_twap_state),
        ):
            ctx = _ctx(state, tokens, trader, seconds_left=45.0)
            _observe(ctx)

        assert win.phase == "hedged"
        assert win.hedge_fired is True
        # A hedge BUY on the DOWN token was placed at 0.35.
        orders = trader._place_taker_order.call_args_list
        assert any(
            call.args[0] == tokens.down_token_id
            and call.args[1] == "BUY"
            and abs(call.args[2] - 0.35) < 1e-6
            for call in orders
        ), f"Expected DOWN hedge order at 0.35; got {orders}"

    def test_twap_hedge_skipped_when_disabled_by_default(self):
        """Default config has ta_twap_hedge_enabled=False — Path C is a no-op."""
        from bot import polymarket_twap_tracker as twap_tracker

        twap_tracker.clear_all()
        state = _make_state(ask_up=0.81, ask_dn=0.35, spot_price=60_000.0)
        # NOTE: ta_twap_hedge_enabled defaults to False — we don't override it.
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.55, first_shares_filled=80.0,
            strike=STRIKE,
            logged_bailout=True,
        )

        # TWAP projection would oppose our position.
        fake_twap_state = SimpleNamespace(projected_winner="DOWN", margin=-300.0)

        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=fake_twap_state),
        ):
            ctx = _ctx(state, tokens, trader, seconds_left=45.0)
            _observe(ctx)

        assert win.hedge_fired is False
        trader._place_taker_order.assert_not_called()

    def test_twap_hedge_skipped_when_projection_matches_position(self):
        """If TWAP projects our side wins → don't hedge (we're on the right side)."""
        from bot import polymarket_twap_tracker as twap_tracker

        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.81, ask_dn=0.35, spot_price=60_000.0,
            ta_twap_hedge_enabled=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.55, first_shares_filled=80.0,
            strike=STRIKE,
            logged_bailout=True,  # suppress bailout for isolation
        )

        fake_twap_state = SimpleNamespace(
            projected_winner="UP",  # matches our position
            margin=200.0,
        )

        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=fake_twap_state),
        ):
            ctx = _ctx(state, tokens, trader, seconds_left=45.0)
            _observe(ctx)

        assert win.hedge_fired is False
        trader._place_taker_order.assert_not_called()

    def test_twap_hedge_skipped_outside_last_60s(self):
        """TWAP hedge only fires in the last 60s — earlier we wait for normal hedge."""
        from bot import polymarket_twap_tracker as twap_tracker

        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.81, ask_dn=0.35, spot_price=60_000.0,
            ta_twap_hedge_enabled=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.55, first_shares_filled=80.0,
            strike=STRIKE,
        )

        fake_twap_state = SimpleNamespace(projected_winner="DOWN", margin=-300.0)

        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=fake_twap_state),
        ):
            # secs=120 — outside the last 60s window
            ctx = _ctx(state, tokens, trader, seconds_left=120.0)
            _observe(ctx)

        assert win.phase == "half_open"
        assert win.hedge_fired is False
        trader._place_taker_order.assert_not_called()

    def test_twap_hedge_skipped_when_margin_below_threshold(self):
        """If $|margin|$ < ta_twap_hedge_margin (default 50), don't hedge."""
        from bot import polymarket_twap_tracker as twap_tracker

        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.81, ask_dn=0.35, spot_price=60_000.0,
            ta_twap_hedge_enabled=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.55, first_shares_filled=80.0,
            strike=STRIKE,
            logged_bailout=True,  # suppress bailout for isolation
        )

        # Margin only $30 — below default threshold of 50
        fake_twap_state = SimpleNamespace(projected_winner="DOWN", margin=-30.0)

        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=fake_twap_state),
        ):
            ctx = _ctx(state, tokens, trader, seconds_left=45.0)
            _observe(ctx)

        assert win.hedge_fired is False
        trader._place_taker_order.assert_not_called()

    def test_min_itm_pct_default_lowered_to_0_025(self):
        """2026-09-17: lowered from 0.05 to 0.025 after strike accuracy improved."""
        from bot.strategies.temporal_arb import MIN_ITM_PCT_DEFAULT
        assert MIN_ITM_PCT_DEFAULT == 0.025

    def test_twap_hedge_default_disabled(self):
        """ta_twap_hedge_enabled defaults to False — activate via /settings."""
        from bot.strategies.temporal_arb import TWAP_HEDGE_ENABLED_DEFAULT
        assert TWAP_HEDGE_ENABLED_DEFAULT is False

    def test_twap_hedge_runtime_fields_exposed(self):
        """The three new TWAP-hedge fields must appear in DESCRIPTOR.params."""
        names = {p.name for p in DESCRIPTOR.params}
        for expected in ("ta_twap_hedge_enabled", "ta_twap_hedge_margin", "ta_twap_hedge_max_sum"):
            assert expected in names, f"missing param: {expected}"

    # ── TWAP-based entry signal ───────────────────────────────────────────

    def test_find_leader_side_with_twap_uses_twap_not_spot(self):
        """When `twap` is passed, the itm_pct reference price is the TWAP,
        not spot. A spot-above-strike scenario with TWAP-below-strike should
        classify as DOWN leader (or no signal if TWAP < threshold)."""
        # spot is above strike (would normally trigger UP leader)
        # twap is below strike (smoothed momentum says DOWN)
        side, px, itm = find_leader_side(
            spot=60_080.0,
            strike=60_000.0,
            ask_up=0.50,
            ask_dn=0.45,
            min_itm_pct=0.05,
            min_ask=0.40,
            max_ask=0.55,
            twap=59_850.0,  # TWAP says DOWN
        )
        assert side == "DOWN"
        assert px == 0.45
        # itm = (59850 - 60000) / 60000 * 100 = -0.25%
        assert itm == pytest.approx(-0.25, abs=1e-6)

    def test_find_leader_side_twap_falls_below_min_itm_when_spot_doesnt(self):
        """If spot is just barely above min_itm_pct but TWAP is well below,
        the TWAP reference correctly skips (no false signal)."""
        # spot = 60030 → itm_spot = +0.05% (above 0.05% threshold → would trigger)
        # twap = 59980 → itm_twap = -0.033% (below 0.05% threshold → skip)
        side, px, itm = find_leader_side(
            spot=60_030.0,
            strike=60_000.0,
            ask_up=0.50,
            ask_dn=0.45,
            min_itm_pct=0.05,
            min_ask=0.40,
            max_ask=0.55,
            twap=59_980.0,
        )
        # TWAP signal dominates; with itm_twap < min_itm_pct, no signal.
        assert side is None
        assert px is None
        assert itm == pytest.approx(-0.0333, abs=1e-3)

    def test_find_leader_side_without_twap_falls_back_to_spot(self):
        """Backward compat: when twap=None, behavior is identical to the
        pre-TWAP implementation (uses spot)."""
        side, px, itm = find_leader_side(
            spot=60_080.0,
            strike=60_000.0,
            ask_up=0.50,
            ask_dn=0.45,
            min_itm_pct=0.05,
            min_ask=0.40,
            max_ask=0.55,
            # twap omitted (default None)
        )
        assert side == "UP"
        assert px == 0.50
        assert itm == pytest.approx(0.1333, abs=1e-3)

    def test_use_twap_signal_default_enabled(self):
        """TWAP-based entry signal defaults to ON — bot uses TWAP by default."""
        from bot.strategies.temporal_arb import (
            USE_TWAP_SIGNAL_DEFAULT, TWAP_LOOKBACK_SEC_DEFAULT,
        )
        assert USE_TWAP_SIGNAL_DEFAULT is True
        assert TWAP_LOOKBACK_SEC_DEFAULT == 60

    def test_twap_entry_signal_runtime_fields_exposed(self):
        """The two new TWAP-signal fields must appear in DESCRIPTOR.params."""
        names = {p.name for p in DESCRIPTOR.params}
        assert "ta_use_twap_signal" in names
        assert "ta_twap_lookback_sec" in names

    # ── Profit-Lock completion (Path D) ────────────────────────────────────────

    def test_profit_lock_default_disabled(self):
        from bot.strategies.temporal_arb import PROFIT_LOCK_ENABLED_DEFAULT
        assert PROFIT_LOCK_ENABLED_DEFAULT is False

    def test_profit_lock_runtime_fields_exposed(self):
        names = {p.name for p in DESCRIPTOR.params}
        assert "ta_profit_lock_enabled" in names
        assert "ta_profit_lock_min_secs" in names

    def test_profit_lock_fires_when_in_profit_after_threshold(self):
        """Path D: first leg up after 30s, pair ≤ cap → force-complete."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()

        records = []
        state = _make_state(
            ask_up=0.70, ask_dn=0.20, spot_price=75_500.0,
            ta_complete_cap=0.88,
            ta_profit_lock_enabled=True, ta_profit_lock_min_secs=30,
        )
        tokens = _make_tokens()
        trader = _make_trader(records=records)
        # First leg UP @0.50, now UP ask=0.70 (in profit +40%),
        # DOWN ask=0.20 → sum 0.70 ≤ 0.88 cap.
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )

        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.indicators.get_volume_ratio", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok") as mock_ok,
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))

        assert win.phase == "complete"
        assert win.profit_lock_fired is True
        # A BUY on DOWN was placed at 0.20 for 80 shares
        orders = trader._place_taker_order.call_args_list
        assert any(
            call.args[0] == tokens.down_token_id
            and call.args[1] == "BUY"
            and abs(call.args[2] - 0.20) < 1e-6
            and abs(call.args[3] - 80.0) < 1e-6
            for call in orders
        ), f"Expected DOWN BUY @0.20 ×80; got {orders}"

    def test_profit_lock_skipped_when_not_in_profit(self):
        """If first leg is in loss, profit-lock does not fire (mart-hedge
        handles the loss case)."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.65, spot_price=75_000.0,
            ta_profit_lock_enabled=True, ta_profit_lock_min_secs=30,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.phase == "half_open"
        assert win.profit_lock_fired is False
        trader._place_taker_order.assert_not_called()

    def test_profit_lock_skipped_when_pair_exceeds_cap(self):
        """If pair > profit_lock_cap, profit-lock does not fire.
        Other paths (A, B, C) are disabled to isolate this test."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.80, ask_dn=0.80, spot_price=75_500.0,  # sum 1.30 > 1.00 pl_cap
            ta_complete_cap=0.65,  # block Path A: 1.30 > 0.65
            ta_hedge_enabled=False,  # block Path B
            ta_twap_hedge_enabled=False,  # block Path C
            ta_profit_lock_enabled=True, ta_profit_lock_min_secs=30,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.phase == "half_open"
        assert win.profit_lock_fired is False

    def test_profit_lock_skipped_before_min_secs(self):
        """Profit-lock only fires after the configured grace period."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.70, ask_dn=0.20, spot_price=75_500.0,
            ta_complete_cap=0.65,  # block Path A
            ta_hedge_enabled=False, ta_twap_hedge_enabled=False,
            ta_profit_lock_enabled=True, ta_profit_lock_min_secs=30,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # first leg filled only 5s ago — too soon
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 5,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.phase == "half_open"
        assert win.profit_lock_fired is False

    def test_profit_lock_skipped_when_disabled(self):
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.70, ask_dn=0.20, spot_price=75_500.0,
            ta_complete_cap=0.65,  # block Path A: 0.90 > 0.65
            ta_hedge_enabled=False,  # block Path B
            ta_twap_hedge_enabled=False,  # block Path C
            ta_profit_lock_enabled=False,  # disabled
            ta_profit_lock_min_secs=30,
            logged_bailout=True,  # suppress bailout
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.phase == "half_open"
        assert win.profit_lock_fired is False

    # ── Martingale hedge (Path E) ────────────────────────────────────────────

    def test_mart_hedge_defaults(self):
        from bot.strategies.temporal_arb import (
            MART_HEDGE_ENABLED_DEFAULT,
            MART_HEDGE_MIN_SECS_DEFAULT,
            MART_HEDGE_MULT_DEFAULT,
            MART_HEDGE_MAX_ROUNDS_DEFAULT,
            MART_HEDGE_MONITOR_SECS_DEFAULT,
            MART_HEDGE_LOSS_FALLBACK_DEFAULT,
        )
        assert MART_HEDGE_ENABLED_DEFAULT is False
        # 2026-10-05: round 1 fires quickly when the first leg goes in loss.
        # 30s is too long — the buffer is just an anti-noise gate. Default 5s.
        assert MART_HEDGE_MIN_SECS_DEFAULT == 5
        # 2026-10-06: spec usuario — cada ronda = base × 2.5 constante
        assert MART_HEDGE_MULT_DEFAULT == 2.5
        # 2026-09-28: max_rounds default lowered 3 → 2 to match the round-2
        # reversal-gated design (round 1 + round 2 only on BTC reversal).
        # 2026-10-05: keeps 2 (round 1 + round 2 condicional a cambio de lado).
        assert MART_HEDGE_MAX_ROUNDS_DEFAULT == 2
        # 2026-10-05: new monitor window (30s) replaces the old
        # round2_cooldown + reversal_threshold pair.
        assert MART_HEDGE_MONITOR_SECS_DEFAULT == 30.0
        # 2026-10-06: loss fallback threshold (Issue #3)
        assert MART_HEDGE_LOSS_FALLBACK_DEFAULT == 0.50

    def test_mart_hedge_runtime_fields_exposed(self):
        names = {p.name for p in DESCRIPTOR.params}
        for f in ("ta_mart_hedge_enabled", "ta_mart_hedge_min_secs",
                  "ta_mart_hedge_mult", "ta_mart_hedge_max_rounds"):
            assert f in names, f"missing param: {f}"

    def test_mart_hedge_qty_uses_multiplier_constant(self):
        """Spec 2026-10-06: cada ronda = base × mult constante.
        UP 80 @0.50, ahora 0.35 (loss 30%). Expected qty = 80 × 2.5 = 200.
        BTC must be below STRIKE so DOWN is the current winner — required
        by the side-change filter (revised 2026-10-05 13:30 UTC)."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()

        state = _make_state(
            ask_up=0.35, ask_dn=0.40,
            spot_price=59_500.0,  # BTC below STRIKE (60_000) → DOWN is winner (side change!)
            ta_hedge_max_sum=0.96,
            ta_complete_cap=0.65,  # block Path A: 0.90 > 0.65
            ta_profit_lock_enabled=False,  # isolate mart-hedge
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=30,
            ta_mart_hedge_mult=2.5, ta_mart_hedge_max_rounds=3,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))

        # Spec 2026-10-06: qty = first_shares × mult = 80 × 2.5 = 200 (sin +loss_pct)
        orders = trader._place_taker_order.call_args_list
        assert any(
            call.args[0] == tokens.down_token_id
            and call.args[1] == "BUY"
            and abs(call.args[2] - 0.40) < 1e-6
            and abs(call.args[3] - 200.0) < 1e-3
            for call in orders
        ), f"Expected DOWN BUY @0.40 × 200; got {orders}"
        assert win.mart_hedge_rounds == 1
        # mart_hedge_fired flag was removed: rounds can repeat per oscillation
        # (multi-round martingale). Assert the round counter instead.

    def test_mart_hedge_skipped_when_in_profit(self):
        """If first leg is in profit, mart-hedge does NOT fire. We use a low
        `ta_complete_cap` to block Path A and profit-lock from completing
        the pair, isolating mart-hedge behavior."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.70, ask_dn=0.20, spot_price=75_500.0,
            ta_complete_cap=0.65,  # block Path A (sum 0.90 > 0.65)
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=30,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.mart_hedge_rounds == 0
        trader._place_taker_order.assert_not_called()

    def test_mart_hedge_fires_even_when_pair_exceeds_cap(self):
        """Mart-hedge no longer has a price cap — it must fire whenever the
        first leg is in loss past the min_secs threshold, even if the pair
        sum is above hedge_max_sum (or even above $1.0). When BTC has moved
        hard against the first leg, the opposite ask is near $0.99 — the
        previous cap would have suppressed exactly the trades we need.
        """
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.80,
            spot_price=59_500.0,  # BTC below STRIKE → DOWN is winner (side change!)
            ta_hedge_max_sum=0.96,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=30,
            ta_mart_hedge_mult=2.5,  # spec 2026-10-06
            ta_profit_lock_enabled=False,  # isolate mart-hedge
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Mart-hedge fires once with constant multiplier, even though
        # the pair sum is above hedge_max_sum and above $1.
        assert win.mart_hedge_rounds == 1
        assert trader._place_taker_order.call_count == 1
        # Spec 2026-10-06: qty = first_shares × mult = 80 × 2.5 = 200 (sin +loss_pct)
        called_args = trader._place_taker_order.call_args
        # (token_id, side, price, qty)
        assert called_args.args[1] == "BUY"
        assert called_args.args[3] == round(80.0 * 2.5, 4)

    def test_mart_hedge_stops_at_max_rounds(self):
        """Once mart_hedge_rounds reaches max_rounds, mart-hedge stops firing."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.60, spot_price=74_500.0,
            ta_hedge_max_sum=0.96,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=30,
            ta_mart_hedge_max_rounds=3,
            ta_profit_lock_enabled=False,  # isolate mart-hedge
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # Round counter already at max → no more mart-hedges
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
            mart_hedge_rounds=3,  # at cap
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.mart_hedge_rounds == 3  # unchanged
        trader._place_taker_order.assert_not_called()

    def test_mart_hedge_skipped_before_min_secs(self):
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.60, spot_price=74_500.0,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=30,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 5,  # too soon
            logged_bailout=True,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.mart_hedge_rounds == 0
        trader._place_taker_order.assert_not_called()

    def test_mart_hedge_skipped_when_disabled(self):
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.60, spot_price=74_500.0,
            ta_complete_cap=0.65,  # block Path A
            ta_mart_hedge_enabled=False,  # disabled
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=3,
            ta_profit_lock_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.mart_hedge_rounds == 0
        trader._place_taker_order.assert_not_called()

    def test_mart_hedge_state_attributes_added(self):
        """`_TAWindow` carries the fields profit-lock and mart-hedge need."""
        win = _TAWindow(window_ts=0, phase="half_open")
        assert hasattr(win, "first_leg_filled_at")
        assert hasattr(win, "profit_lock_fired")
        assert hasattr(win, "mart_hedge_rounds")
        assert hasattr(win, "mart_hedge_fired")
        assert win.mart_hedge_rounds == 0
        assert win.mart_hedge_fired is False

    # ── 2026-10-06: regression tests for the 5 user-reported issues ──────────

    def test_hold_winner_no_name_error(self):
        """Issue #5 regression: la línea 650 usaba `ts` no definido, lo que
        rompía el bloque hold-winner con NameError. Tras el fix, el bloque
        debe poder ejecutarse cuando first_leg ask ≥ 0.90 por ≥ 60s y
        registrar TA_HOLD_WINNER."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.92, ask_dn=0.10,  # first_side=UP en winner zone
            spot_price=60_200.0,
            ta_complete_cap=0.50,    # bloquea Path A: 0.92+0.10 > 0.50
            ta_profit_lock_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # winner_zone_entered_at hace >60s → in_winner_zone_long = True
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE,
            first_leg_filled_at=time.time() - 90,
            reached_winner_zone=True,
            winner_zone_entered_at=time.time() - 90,  # > 60s ago
            absolute_peak=0.92,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Sin NameError; debe registrar TA_HOLD_WINNER y loggearlo
        assert win.logged_hold_winner is True
        assert win.phase == "half_open"  # HOLD: no avanza de fase

    def test_idle_gates_at_entry_cutoff_120s(self):
        """Issue #4: gate IDLE usa ta_entry_cutoff_sec (default 120s).
        Con secs=119 → closed + TA_SKIP_NO_IMPULSE.
        Con secs=121 (recién entrado) → sigue en IDLE."""
        skips = []
        # Caso A: secs=119 < 120 → cierra
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0, skips=skips)
        tokens = _make_tokens()
        trader = _make_trader()
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.indicators.get_volume_ratio", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
        ):
            win = _TAWindow(window_ts=tokens.window_ts)
            gw.return_value = win
            _observe(_ctx(state, tokens, trader, seconds_left=119.0))
        assert win.phase == "closed"
        assert "TA_SKIP_NO_IMPULSE" in skips
        trader._place_taker_order.assert_not_called()

    def test_path_a_skipped_after_mart_hedge(self):
        """Issue #2: tras Mart-Hedge (mart_hedge_rounds >= 1), Path A
        (par completo normal) NO debe dispararse aunque second_ask esté
        barato (sum < cap)."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.45, ask_dn=0.40,  # first_leg UP, second_ask 0.40, sum 0.90
            spot_price=60_000.0,
            ta_complete_cap=0.95,      # permite Path A: 0.50+0.40=0.90 < 0.95
            ta_hedge_enabled=False,    # aísla: Path B no dispara primero
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # mart_hedge_rounds=1 → Path A gateado
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE,
            first_leg_filled_at=time.time() - 60,
            mart_hedge_rounds=1,  # Mart-Hedge ya disparó
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Path A NO disparó: phase sigue half_open
        assert win.phase == "half_open"
        assert win.logged_complete is False
        trader._place_taker_order.assert_not_called()

    def test_path_d_skipped_after_mart_hedge(self):
        """Issue #2: tras Mart-Hedge, Path D (profit-lock) NO debe dispararse
        aunque first_leg esté en profit."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.70,  # current_first_ask=0.70 > first_px=0.50 (profit)
            spot_price=74_000.0,
            ta_complete_cap=0.50,        # bloquea Path A
            ta_profit_lock_enabled=True,
            ta_profit_lock_min_secs=10,
            ta_profit_lock_cap=1.0,
            ta_mart_hedge_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE,
            first_leg_filled_at=time.time() - 30,
            mart_hedge_rounds=1,  # Mart-Hedge ya disparó
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Path D NO disparó
        assert win.phase == "half_open"
        assert win.profit_lock_fired is False
        trader._place_taker_order.assert_not_called()

    def test_mart_hedge_fires_on_high_loss_without_side_change(self):
        """Issue #3: Mart-Hedge dispara con pérdida ≥ ta_mart_hedge_loss_fallback_pct
        aunque BTC NO haya cruzado el strike (side_changed_e1 = False)."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        # BTC arriba del strike (mismo lado que first_side) → side_changed=False.
        # Pérdida = (0.50-0.20)/0.50 = 60% ≥ 50% fallback.
        state = _make_state(
            ask_up=0.20, ask_dn=0.85,
            spot_price=75_500.0,           # BTC arriba del strike (UP es ganador)
            ta_complete_cap=0.50,          # bloquea Path A
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=True,
            ta_mart_hedge_min_secs=5,
            ta_mart_hedge_mult=2.5,
            ta_mart_hedge_loss_fallback_pct=0.50,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE,
            first_leg_filled_at=time.time() - 30,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Mart-Hedge disparó por loss-fallback (no side-change)
        assert win.mart_hedge_rounds == 1
        # qty = 80 × 2.5 = 200
        orders = trader._place_taker_order.call_args_list
        assert any(
            call.args[0] == tokens.down_token_id
            and call.args[1] == "BUY"
            and abs(call.args[3] - 200.0) < 1e-3
            for call in orders
        ), f"Expected DOWN BUY × 200; got {orders}"

    # ── 2026-09-28: post-trade grace + mart-hedge round 2 reversal-gated ──────

    def test_path_b_hedge_blocked_by_grace(self):
        """Path B (hedge recovery) must NOT fire before grace_secs even when
        the first leg has dropped hard. The 60s grace lets Path A keep trying
        the cheap-completion route without being preempted by the defensive
        hedge."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        # Block Path A and mart-hedge to isolate Path B
        state = _make_state(
            ask_up=0.20, ask_dn=0.40,  # sum 0.60 ≤ 0.92 hedge_max_sum
            spot_price=74_000.0,
            ta_complete_cap=0.50,  # block Path A
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=False,
            ta_grace_secs=60.0,    # enforce grace
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 30,  # before grace
            logged_bailout=True,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Path B blocked by grace, even though price conditions are met
        assert win.phase == "half_open"
        assert win.hedge_fired is False
        trader._place_taker_order.assert_not_called()

    def test_path_b_hedge_fires_after_grace(self):
        """Path B fires when grace has elapsed AND the price has dropped."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.20, ask_dn=0.40,  # sum 0.60 ≤ 0.92 hedge_max_sum
            spot_price=74_000.0,
            ta_complete_cap=0.50,  # block Path A
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=False,
            ta_grace_secs=60.0,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 65,  # past grace
            logged_bailout=True,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.phase == "hedged"
        assert win.hedge_fired is True
        trader._place_taker_order.assert_called_once()

    def test_mart_hedge_round2_blocked_by_monitor_window(self):
        """Round 2 must NOT fire within the monitor window (ta_mh_monitor_secs)
        even if the side has already changed. Without the monitor gate, the
        bot would fire round 2 on the very next observe tick (~4s later)
        instead of waiting the full window to confirm the side flip is real.
        """
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.10, ask_dn=0.95,   # BTC crashed hard; UP ask way down
            spot_price=70_000.0,         # BTC well below strike → DOWN winner
            ta_complete_cap=0.50,
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=5,
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=2,
            ta_grace_secs=60.0,
            ta_mh_monitor_secs=30.0,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # Round 1 fired 5s ago (within the 30s monitor window). The side has
        # not changed (BTC still below strike, mh_side=DOWN still winning)
        # — but the monitor gate alone is enough to block round 2 here.
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 65,
            mart_hedge_rounds=1,
            mart_hedge_side="DOWN", mart_hedge_px=0.70, mart_hedge_qty=160.0,
            mh_first_ask_at_r1=0.30,
            last_mh_round_at=time.time() - 5,  # within 30s monitor
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=100.0))
        # Round 2 must NOT fire — still within the monitor window.
        assert win.mart_hedge_rounds == 1
        assert trader._place_taker_order.call_count == 0

    def test_mart_hedge_round2_blocked_when_side_unchanged(self):
        """Round 2 must NOT fire when the round-1 leg is still the current
        winner (i.e., the side hasn't changed). This is the side-change
        filter from the user spec (2026-10-05): the bot cannot open
        simultaneous positions on the same side while the operation on that
        side is still winning. Without this filter, the bot would average
        into a winning leg for no reason."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        # Round 1 bought DOWN. BTC is still below strike — DOWN is still
        # the winner. The round-1 leg (DOWN) is still in profit
        # (mh_px=0.70, ask_dn=0.80). Monitor window elapsed.
        state = _make_state(
            ask_up=0.25, ask_dn=0.80,  # DOWN still in profit
            spot_price=59_500.0,         # BTC still below STRIKE → DOWN still winner
            ta_complete_cap=0.50,
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=5,
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=2,
            ta_grace_secs=60.0,
            ta_mh_monitor_secs=30.0,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # Round 1 fired 35s ago (monitor elapsed). mh_side=DOWN is STILL
        # the current winner (BTC still below strike).
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 90,
            mart_hedge_rounds=1,
            mart_hedge_side="DOWN", mart_hedge_px=0.70, mart_hedge_qty=160.0,
            mh_first_ask_at_r1=0.25,
            last_mh_round_at=time.time() - 35,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=100.0))
        # Round 2 must NOT fire — mh_side is still the winner (no side change).
        assert win.mart_hedge_rounds == 1
        assert trader._place_taker_order.call_count == 0

    def test_mart_hedge_round2_fires_on_reversal_with_cooldown(self):
        """Round 2 fires when BOTH the monitor window elapsed AND the side
        changed (BTC re-crossed the strike to the opposite side of round 1).
        Per user spec (2026-10-05), round 2 buys the CURRENT WINNER (the side
        opposite to mh_side), not the same side as round 1. Spec 2026-10-06
        (revised): qty = first_shares × mart_hedge_mult (CONSTANT — no
        compounding on round1_qty)."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        # Round 1 bought DOWN (first_leg=UP, mh_side=DOWN).
        # BTC has crossed BACK above strike: spot=75_500 > strike=75_000,
        # so the current winner is UP (= first_side, opposite to mh_side=DOWN).
        # Round-1 leg DOWN is in loss: mh_px=0.70, current ask_dn=0.50.
        state = _make_state(
            ask_up=0.45, ask_dn=0.50,  # round-1 leg DOWN now at 0.50
            spot_price=75_500.0,         # BTC re-crossed above strike
            ta_complete_cap=0.50,
            ta_profit_lock_enabled=False,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=5,
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=2,
            ta_grace_secs=60.0,
            ta_mh_monitor_secs=30.0,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # Round 1 fired 35s ago (monitor window elapsed).
        # Spec 2026-10-06 (CONSTANT): qty round 2 = first_shares × mult
        # = 80 × 2.0 = 160 (mismo size que round 1, NO compounding).
        # Round 2 buys UP (new winner, opposite to mh_side=DOWN) at ask_up=0.45.
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 90,
            mart_hedge_rounds=1,
            mart_hedge_side="DOWN", mart_hedge_px=0.70, mart_hedge_qty=160.0,
            mh_first_ask_at_r1=0.30,
            last_mh_round_at=time.time() - 35,  # past 30s monitor window
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=100.0))
        assert win.mart_hedge_rounds == 2
        # Round 2 buys the NEW WINNER (UP, opposite to mh_side=DOWN) at ask_up=0.45.
        # Spec 2026-10-06 CONSTANT: qty = first_shares × mult = 80 × 2.0 = 160
        # (mismo size que round 1, sin compounding)
        orders = trader._place_taker_order.call_args_list
        assert any(
            call.args[0] == tokens.up_token_id
            and call.args[1] == "BUY"
            and abs(call.args[2] - 0.45) < 1e-6
            and abs(call.args[3] - 160.0) < 1e-3
            for call in orders
        ), f"Expected UP BUY @0.45 × 365.7143 (new winner); got {orders}"
        # After round 2, the new tracked side is UP.
        assert win.mart_hedge_side == "UP"
        assert abs(win.mart_hedge_px - 0.45) < 1e-6

    def test_grace_and_round2_fields_exposed_in_descriptor(self):
        """The 2 runtime fields (`ta_grace_secs`, `ta_mh_monitor_secs`) must be
        exposed in DESCRIPTOR.params so /settings can render them. The old
        `ta_mh_round2_cooldown_secs` and `ta_mh_reversal_threshold` were
        removed in 2026-10-05 — the side-change filter + monitor window
        replaces them."""
        names = {p.name for p in DESCRIPTOR.params}
        for f in ("ta_grace_secs", "ta_mh_monitor_secs"):
            assert f in names, f"missing param: {f}"

    def test_round1_fires_quickly_without_grace_secs(self):
        """Round 1 must fire as soon as the first leg is in loss — the
        mart-hedge path is NOT gated by `ta_grace_secs` (60s default).
        The 60s grace is only for Path B (hedge recovery) — Path E
        (mart-hedge) needs to respond quickly to losses per user spec.
        """
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.60,
            spot_price=59_500.0,  # BTC below STRIKE → DOWN is winner (side change)
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=5,
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=2,
            ta_grace_secs=60.0,           # grace is HIGH — but shouldn't block
            ta_mh_monitor_secs=30.0,
            ta_profit_lock_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # First leg filled 8s ago (well within min_secs=5, well before grace=60)
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 8,
            logged_bailout=True,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Round 1 must have fired despite grace_secs=60 (the grace only gates
        # Path B, not Path E).
        assert win.mart_hedge_rounds == 1
        assert win.mart_hedge_side == "DOWN"

    def test_round1_blocked_before_min_secs(self):
        """Round 1 must NOT fire before `ta_mart_hedge_min_secs` even if the
        first leg is in loss. The min_secs is a small anti-noise buffer."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.60,
            spot_price=74_000.0,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=30,
            ta_grace_secs=0.0,
            ta_mh_monitor_secs=30.0,
            ta_profit_lock_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 5,  # too soon
            logged_bailout=True,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        assert win.mart_hedge_rounds == 0
        trader._place_taker_order.assert_not_called()

    def test_round1_not_refired_after_first_round(self):
        """Round 1 must NOT fire a second time on subsequent ticks.
        Once `mart_hedge_rounds >= 1`, the round-1 branch is skipped
        and we go straight to the round-2 evaluation."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.60,
            spot_price=74_000.0,
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=5,
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=2,
            ta_grace_secs=0.0,
            ta_mh_monitor_secs=30.0,
            ta_profit_lock_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        # Round 1 already fired; first_leg is STILL in loss (ask_up=0.30 < 0.50).
        # Without the rounds==0 guard, the round-1 branch would re-fire here.
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 10,
            mart_hedge_rounds=1,
            mart_hedge_side="DOWN", mart_hedge_px=0.70, mart_hedge_qty=160.0,
            last_mh_round_at=time.time() - 2,  # within monitor window
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # No new taker order should have been placed.
        trader._place_taker_order.assert_not_called()
        # Round counter stays at 1.
        assert win.mart_hedge_rounds == 1

    def test_round1_blocked_when_side_has_not_changed(self):
        """REGRESSION TEST (bugfix 2026-10-05 13:30 UTC).
        The 12:11:22 trade on the VPS opened DOWN@0.67 then fired mart-hedge
        round 1 buying UP@0.39 even though BTC was STILL below the strike
        (DOWN was still the winner). The trigger `current_first_ask < first_px`
        was firing on ask-price noise instead of an actual side change.

        After the fix, round 1 must NOT fire when:
          (a) the first leg ask has dropped (loss condition met), BUT
          (b) BTC has not crossed the strike against the first leg.

        This test reproduces that exact scenario: first_side=UP, ask_dn
        drops (so the old trigger would fire), but BTC stays above STRIKE
        (so UP is still the winner). Round 1 must NOT fire.
        """
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.50, ask_dn=0.30,  # ask_dn dropped — old trigger would fire
            spot_price=60_500.0,         # BTC STILL above STRIKE → UP still winner
            ta_complete_cap=0.40,        # block Path A: 0.50+0.30=0.80 > 0.40
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=5,
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=2,
            ta_grace_secs=0.0,
            ta_mh_monitor_secs=30.0,
            ta_profit_lock_enabled=False,
            logged_bailout=True,
        )
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(
            window_ts=tokens.window_ts, phase="half_open",
            first_side="UP", first_px=0.50, first_shares_filled=80.0,
            strike=STRIKE, first_leg_filled_at=time.time() - 10,
            logged_bailout=True,
        )
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.polymarket_price.get_strike", return_value=STRIKE),
            patch("bot.indicators.get_atr", return_value=None),
            patch("bot.indicators.get_rsi", return_value=None),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
            patch.object(twap_tracker, "get_state", return_value=SimpleNamespace()),
        ):
            _observe(_ctx(state, tokens, trader, seconds_left=200.0))
        # Round 1 must NOT fire — BTC has not crossed strike, UP is still winner.
        assert win.mart_hedge_rounds == 0
        trader._place_taker_order.assert_not_called()


class TestCurrentWinningSide:
    """Helper for the side-change filter (revised 2026-10-05)."""

    def test_returns_up_when_btc_above_strike(self):
        from bot.strategies.temporal_arb import current_winning_side
        assert current_winning_side(spot=75_500.0, strike=75_000.0) == "UP"

    def test_returns_down_when_btc_below_strike(self):
        from bot.strategies.temporal_arb import current_winning_side
        assert current_winning_side(spot=74_500.0, strike=75_000.0) == "DOWN"

    def test_returns_none_when_neutral(self):
        """BTC EXACTLY at strike (diff_pct = 0) → no clear winner.
        Bugfix 2026-10-05 13:30 UTC: the neutral band was lowered from 0.1%
        to 0% per user spec ("el precio cambie de lado" = any non-zero
        crossing counts). A 0.02% move like the 12:11:22 VPS trade is a
        real side change and must not be filtered out."""
        from bot.strategies.temporal_arb import current_winning_side
        # Exactly at strike (0 diff) → None
        assert current_winning_side(spot=75_000.0, strike=75_000.0) is None
        # Small but non-zero move (0.02%) → real side change, returns the side
        assert current_winning_side(spot=75_015.0, strike=75_000.0) == "UP"
        assert current_winning_side(spot=74_985.0, strike=75_000.0) == "DOWN"
        # Same 0.067% case the old test had — now counts as UP, not None
        assert current_winning_side(spot=75_050.0, strike=75_000.0) == "UP"

    def test_returns_none_when_inputs_invalid(self):
        from bot.strategies.temporal_arb import current_winning_side
        assert current_winning_side(spot=None, strike=75_000.0) is None
        assert current_winning_side(spot=75_500.0, strike=None) is None
        assert current_winning_side(spot=75_500.0, strike=0.0) is None


class TestObserveTerminal:
    def test_complete_phase_returns_immediately(self):
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="complete", strike=STRIKE)
        with patch("bot.strategies.temporal_arb._get_window", return_value=win):
            _observe(StrategyContext(
                state=state, symbol="btc", tokens=tokens, trader=trader,
                seconds_left=200.0,
            ))
        trader._place_taker_order.assert_not_called()

    def test_closed_phase_returns_immediately(self):
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="closed", strike=STRIKE)
        with patch("bot.strategies.temporal_arb._get_window", return_value=win):
            _observe(StrategyContext(
                state=state, symbol="btc", tokens=tokens, trader=trader,
                seconds_left=200.0,
            ))
        trader._place_taker_order.assert_not_called()


# ── descriptor ───────────────────────────────────────────────────────────────

class TestDescriptor:
    def test_id(self):
        assert DESCRIPTOR.id == "temporal_arb"

    def test_not_enabled_by_default(self):
        state = SimpleNamespace()
        assert DESCRIPTOR.is_enabled(state) is False

    def test_enabled_when_flag_set(self):
        state = SimpleNamespace(ta_enabled=True)
        assert DESCRIPTOR.is_enabled(state) is True

    def test_has_observe_hook(self):
        assert DESCRIPTOR.observe is not None

    def test_evaluate_returns_empty(self):
        ctx = StrategyContext(state=SimpleNamespace(), symbol="btc")
        assert DESCRIPTOR.evaluate(ctx) == []

    def test_evaluate_late_is_none(self):
        assert DESCRIPTOR.evaluate_late is None

    def test_params_include_required_fields(self):
        names = {p.name for p in DESCRIPTOR.params}
        for expected in ("ta_enabled", "ta_min_itm_pct", "ta_min_ask", "ta_max_ask",
                         "ta_complete_cap", "ta_shares_per_leg",
                         "ta_entry_cutoff_sec", "ta_bailout_sec"):
            assert expected in names, f"missing param: {expected}"

    def test_old_cheap_threshold_not_in_params(self):
        names = {p.name for p in DESCRIPTOR.params}
        assert "ta_cheap_threshold" not in names, \
            "ta_cheap_threshold was removed; new signal uses ta_min_itm_pct"

    def test_enabled_when_matches_is_enabled(self):
        ew = DESCRIPTOR.enabled_when
        assert ew is not None
        field = ew["field"]
        for val in ew["values"]:
            state = SimpleNamespace(**{field: val})
            assert DESCRIPTOR.is_enabled(state) is True
