"""Tests for Temporal Arbitrage strategy — bot/strategies/temporal_arb.py."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from bot.strategies.temporal_arb import (
    find_cheap_side,
    second_leg_worthwhile,
    _get_window,
    _TAWindow,
    _observe,
    DESCRIPTOR,
)
from bot.strategies.base import StrategyContext


# ── find_cheap_side ──────────────────────────────────────────────────────────

class TestFindCheapSide:
    def test_both_above_threshold_returns_none(self):
        side, px = find_cheap_side(0.50, 0.48, 0.35)
        assert side is None
        assert px is None

    def test_up_cheap_only(self):
        side, px = find_cheap_side(0.30, 0.72, 0.35)
        assert side == "UP"
        assert px == 0.30

    def test_down_cheap_only(self):
        side, px = find_cheap_side(0.68, 0.28, 0.35)
        assert side == "DOWN"
        assert px == 0.28

    def test_both_cheap_picks_cheaper(self):
        # UP cheaper → pick UP first
        side, px = find_cheap_side(0.22, 0.31, 0.35)
        assert side == "UP"
        assert px == 0.22

    def test_both_cheap_picks_down_when_cheaper(self):
        side, px = find_cheap_side(0.33, 0.20, 0.35)
        assert side == "DOWN"
        assert px == 0.20

    def test_exactly_at_threshold(self):
        side, px = find_cheap_side(0.35, 0.60, 0.35)
        assert side == "UP"

    def test_none_asks_returns_none(self):
        side, px = find_cheap_side(None, None, 0.35)
        assert side is None

    def test_one_ask_none(self):
        side, px = find_cheap_side(None, 0.28, 0.35)
        assert side == "DOWN"
        assert px == 0.28


# ── second_leg_worthwhile ────────────────────────────────────────────────────

class TestSecondLegWorthwhile:
    def test_pair_fits_within_cap(self):
        assert second_leg_worthwhile(0.27, 0.49, 0.82) is True  # 0.76 ≤ 0.82

    def test_pair_exactly_at_cap(self):
        assert second_leg_worthwhile(0.35, 0.47, 0.82) is True  # 0.82 = 0.82

    def test_pair_exceeds_cap(self):
        assert second_leg_worthwhile(0.35, 0.50, 0.82) is False  # 0.85 > 0.82

    def test_both_cheap_tight_cap(self):
        # 0.25 + 0.54 = 0.79 ≤ 0.80 → True (worthwhile)
        assert second_leg_worthwhile(0.25, 0.54, 0.80) is True
        # 0.25 + 0.56 = 0.81 > 0.80 → False (not worthwhile)
        assert second_leg_worthwhile(0.25, 0.56, 0.80) is False


# ── window auto-reset ────────────────────────────────────────────────────────

class TestGetWindow:
    def test_same_window_ts_returns_same_object(self):
        w1 = _get_window("btc", 1000)
        w2 = _get_window("btc", 1000)
        assert w1 is w2

    def test_new_window_ts_resets(self):
        w1 = _get_window("eth", 1000)
        w1.phase = "complete"
        w2 = _get_window("eth", 1300)
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
    ta_cheap_threshold=0.35,
    ta_complete_cap=0.82,
    ta_shares_per_leg=5.0,
    ta_entry_cutoff_sec=150.0,
    ta_bailout_sec=60.0,
    ta_cancel_all_sec=10.0,
    ask_up=0.50,
    ask_dn=0.50,
    skips=None,
    obs=None,
    mode="paper",
):
    skips = skips if skips is not None else []
    obs   = obs   if obs   is not None else []
    state = SimpleNamespace(
        ta_enabled=ta_enabled,
        ta_cheap_threshold=ta_cheap_threshold,
        ta_complete_cap=ta_complete_cap,
        ta_shares_per_leg=ta_shares_per_leg,
        ta_entry_cutoff_sec=ta_entry_cutoff_sec,
        ta_bailout_sec=ta_bailout_sec,
        ta_cancel_all_sec=ta_cancel_all_sec,
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


class TestObserveIdle:
    def _call(self, state, tokens, trader, secs=200.0):
        with (
            patch("bot.strategies.temporal_arb._get_window") as gw,
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
        ):
            win = _TAWindow(window_ts=tokens.window_ts)
            gw.return_value = win
            ctx = _ctx(state, tokens, trader, secs)
            _observe(ctx)
            return win

    def test_no_cheap_side_stays_idle(self):
        state = _make_state(ask_up=0.50, ask_dn=0.52)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader)
        assert win.phase == "idle"
        trader._place_taker_order.assert_not_called()

    def test_up_cheap_buys_first_leg(self):
        orders, records = [], []
        state = _make_state(ask_up=0.28, ask_dn=0.74)
        tokens = _make_tokens()
        trader = _make_trader(orders=orders, records=records)
        win = self._call(state, tokens, trader)
        assert win.phase == "half_open"
        assert win.first_side == "UP"
        assert win.first_px == 0.28
        assert len(orders) == 1
        assert orders[0][1] == "BUY"
        assert len(records) == 1
        assert records[0][1]["strategy"] == "temporal_arb"

    def test_down_cheap_buys_first_leg(self):
        orders = []
        state = _make_state(ask_up=0.74, ask_dn=0.25)
        tokens = _make_tokens()
        trader = _make_trader(orders=orders)
        win = self._call(state, tokens, trader)
        assert win.phase == "half_open"
        assert win.first_side == "DOWN"
        assert win.first_px == 0.25

    def test_skip_late_when_cutoff_passed(self):
        skips = []
        state = _make_state(ask_up=0.25, ask_dn=0.75, skips=skips)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader, secs=100.0)  # < 150 cutoff
        assert win.phase == "closed"
        assert "TA_SKIP_LATE" in skips
        trader._place_taker_order.assert_not_called()

    def test_no_data_stays_idle(self):
        state = _make_state(ask_up=None, ask_dn=None)
        tokens = _make_tokens()
        trader = _make_trader()
        win = self._call(state, tokens, trader)
        assert win.phase == "idle"

    def test_taker_order_failure_stays_idle(self):
        state = _make_state(ask_up=0.28, ask_dn=0.74)
        tokens = _make_tokens()
        trader = MagicMock()
        trader._place_taker_order.return_value = None  # order rejected
        win = self._call(state, tokens, trader)
        assert win.phase == "idle"


class TestObserveHalfOpen:
    def _call(self, state, tokens, trader, win, secs=200.0):
        with (
            patch("bot.strategies.temporal_arb._get_window", return_value=win),
            patch("bot.logger.info"),
            patch("bot.logger.ok"),
            patch("bot.logger.warn"),
        ):
            ctx = _ctx(state, tokens, trader, secs)
            _observe(ctx)

    def test_second_leg_cheap_completes_pair(self):
        records = []
        state = _make_state(ask_up=0.28, ask_dn=0.45)  # UP bought first at 0.28; DOWN now 0.45: 0.73 ≤ 0.82
        tokens = _make_tokens()
        trader = _make_trader(records=records)
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.28)
        self._call(state, tokens, trader, win)
        assert win.phase == "complete"
        # Second leg recorded
        assert any(r[1].get("strategy") == "temporal_arb" for r in records)

    def test_second_leg_too_expensive_stays_half_open(self):
        state = _make_state(ask_up=0.28, ask_dn=0.60)  # 0.28+0.60=0.88 > 0.82
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.28)
        self._call(state, tokens, trader, win, secs=120.0)  # > bail_sec
        assert win.phase == "half_open"
        trader._place_taker_order.assert_not_called()

    def test_bailout_closes_when_time_runs_out(self):
        skips = []
        state = _make_state(ask_up=0.28, ask_dn=0.62, skips=skips)  # pair too expensive
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.28)
        self._call(state, tokens, trader, win, secs=45.0)  # ≤ bail_sec=60
        assert win.phase == "closed"
        assert "TA_BAILOUT" in skips

    def test_bailout_only_logged_once(self):
        skips = []
        state = _make_state(ask_up=0.28, ask_dn=0.62, skips=skips)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="half_open",
                        first_side="UP", first_px=0.28, logged_bailout=True)
        self._call(state, tokens, trader, win, secs=45.0)
        # logged_bailout already True → no new skip
        assert "TA_BAILOUT" not in skips


class TestObserveTerminal:
    def test_complete_phase_returns_immediately(self):
        state = _make_state(ask_up=0.25, ask_dn=0.74)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="complete")
        with patch("bot.strategies.temporal_arb._get_window", return_value=win):
            _observe(StrategyContext(
                state=state, symbol="btc", tokens=tokens, trader=trader,
                seconds_left=200.0,
            ))
        trader._place_taker_order.assert_not_called()

    def test_closed_phase_returns_immediately(self):
        state = _make_state(ask_up=0.25, ask_dn=0.74)
        tokens = _make_tokens()
        trader = _make_trader()
        win = _TAWindow(window_ts=tokens.window_ts, phase="closed")
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
        for expected in ("ta_enabled", "ta_cheap_threshold", "ta_complete_cap",
                         "ta_shares_per_leg", "ta_entry_cutoff_sec", "ta_bailout_sec"):
            assert expected in names, f"missing param: {expected}"

    def test_enabled_when_matches_is_enabled(self):
        ew = DESCRIPTOR.enabled_when
        assert ew is not None
        field = ew["field"]
        for val in ew["values"]:
            state = SimpleNamespace(**{field: val})
            assert DESCRIPTOR.is_enabled(state) is True
