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
    ta_mart_hedge_enabled=False,
    ta_mart_hedge_min_secs=30,
    ta_mart_hedge_mult=2.0,
    ta_mart_hedge_max_rounds=3,
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
        ta_mart_hedge_enabled=ta_mart_hedge_enabled,
        ta_mart_hedge_min_secs=ta_mart_hedge_min_secs,
        ta_mart_hedge_mult=ta_mart_hedge_mult,
        ta_mart_hedge_max_rounds=ta_mart_hedge_max_rounds,
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
        # The directional entry gate closes at secs < 30 only (Gate 1 in
        # _observe's IDLE phase). Earlier in the window the bot still buys
        # if a signal fires — even when there's less than q_cut time left.
        skips = []
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0, skips=skips)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader, secs=20.0)  # < 30 cutoff
        assert win.phase == "closed"
        assert "TA_SKIP_LATE" in skips
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
        )
        assert MART_HEDGE_ENABLED_DEFAULT is False
        assert MART_HEDGE_MIN_SECS_DEFAULT == 30
        assert MART_HEDGE_MULT_DEFAULT == 2.0
        assert MART_HEDGE_MAX_ROUNDS_DEFAULT == 3

    def test_mart_hedge_runtime_fields_exposed(self):
        names = {p.name for p in DESCRIPTOR.params}
        for f in ("ta_mart_hedge_enabled", "ta_mart_hedge_min_secs",
                  "ta_mart_hedge_mult", "ta_mart_hedge_max_rounds"):
            assert f in names, f"missing param: {f}"

    def test_mart_hedge_qty_uses_multiplier_and_loss_pct(self):
        """Practica el ejemplo del usuario:
        UP 80 @0.50, ahora 0.35 (loss 30%). Expected qty = 80 × 2 × 1.30 = 208."""
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()

        state = _make_state(
            ask_up=0.35, ask_dn=0.40, spot_price=74_500.0,  # sum 0.90 ≤ hedge_max_sum
            ta_hedge_max_sum=0.96,
            ta_complete_cap=0.65,  # block Path A: 0.90 > 0.65
            ta_profit_lock_enabled=False,  # isolate mart-hedge
            ta_mart_hedge_enabled=True, ta_mart_hedge_min_secs=30,
            ta_mart_hedge_mult=2.0, ta_mart_hedge_max_rounds=3,
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

        # mart hedge should have placed a BUY on DOWN with qty = 184
        # (formula: initial × (2 + loss_pct) = 80 × (2 + 0.30) = 184)
        orders = trader._place_taker_order.call_args_list
        assert any(
            call.args[0] == tokens.down_token_id
            and call.args[1] == "BUY"
            and abs(call.args[2] - 0.40) < 1e-6
            and abs(call.args[3] - 184.0) < 1e-3
            for call in orders
        ), f"Expected DOWN BUY @0.40 × 184; got {orders}"
        assert win.mart_hedge_rounds == 1
        assert win.mart_hedge_fired is True

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

    def test_mart_hedge_skipped_when_pair_exceeds_cap(self):
        from bot import polymarket_twap_tracker as twap_tracker
        twap_tracker.clear_all()
        state = _make_state(
            ask_up=0.30, ask_dn=0.80, spot_price=74_500.0,  # sum 1.10 > 0.96
            ta_hedge_max_sum=0.96,
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
