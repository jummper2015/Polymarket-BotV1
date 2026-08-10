"""Tests for Temporal Arbitrage strategy — bot/strategies/temporal_arb.py."""

from __future__ import annotations

import threading
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
    ask_up=0.50,
    ask_dn=0.50,
    spot_price=60060.0,   # +0.1% above strike by default
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
            patch("bot.binance_api.get_current_window_open",
                  return_value=strike),
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
        skips = []
        state = _make_state(ask_up=0.48, ask_dn=0.54, spot_price=60060.0, skips=skips)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader, secs=100.0)  # < 150 cutoff
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
        # Binance returns None for window open — retry next tick
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.binance_api.get_current_window_open",
                  return_value=None),
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
        """Second tick should NOT call get_current_window_open again."""
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.binance_api.get_current_window_open",
                  return_value=None) as mock_fetch,
            patch("bot.logger.info"),
            patch("bot.logger.warn"),
        ):
            tokens = _make_tokens()
            state = _make_state(ask_up=0.62, ask_dn=0.40, spot_price=60060.0)
            trader = _make_trader()
            # Pre-load the strike so get_current_window_open shouldn't be called
            win = _TAWindow(window_ts=tokens.window_ts, strike=STRIKE)
            gw.return_value = win
            _observe(_ctx(state, tokens, trader))
        mock_fetch.assert_not_called()

    def test_taker_order_failure_stays_idle(self):
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.binance_api.get_current_window_open",
                  return_value=STRIKE),
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
            patch("bot.binance_api.get_current_window_open",
                  return_value=STRIKE),
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
