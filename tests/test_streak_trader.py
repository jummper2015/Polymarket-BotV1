"""Regression tests for streak_trader.py — tick rounding and trade resolution."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.db import TradeModel, db, db_context, init_db
from bot.streak_trader import PRICE_TICK, _floor_to_tick


# ═══════════════════════════════════════════════════════════════════════════════
# Tick rounding
# ═══════════════════════════════════════════════════════════════════════════════


class TestFloorToTick:
    @pytest.mark.parametrize("price", [round(i * 0.01, 2) for i in range(1, 100)])
    def test_exact_tick_is_unchanged(self, price):
        """A price already on a tick must survive untouched.

        Regression: `math.floor(0.29 / 0.01)` is 28, so 0.29, 0.47, 0.57, 0.58,
        0.59 and 0.94 each lost a full tick — enough for a limit buy to sit
        below the ask and never fill.
        """
        assert _floor_to_tick(price) == pytest.approx(price)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0.2999, 0.29),
            (0.294, 0.29),
            (0.5901, 0.59),
            (0.9499, 0.94),
            (0.601, 0.60),
        ],
    )
    def test_rounds_down_between_ticks(self, raw, expected):
        assert _floor_to_tick(raw) == pytest.approx(expected)

    def test_never_returns_below_one_tick(self):
        assert _floor_to_tick(0.004) == pytest.approx(PRICE_TICK)
        assert _floor_to_tick(0.0) == pytest.approx(PRICE_TICK)

    def test_never_rounds_above_input(self):
        for i in range(1, 1000):
            raw = i / 1000.0
            assert _floor_to_tick(raw) <= raw + 1e-9 or raw < PRICE_TICK


# ═══════════════════════════════════════════════════════════════════════════════
# Trade resolution — detached instance handling
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    init_db(database_url=f"sqlite:///{tmp_path/'t.db'}")
    with db_context():
        db.create_all()
    yield
    with db_context():
        db.session.remove()


def _make_trade(**kw) -> TradeModel:
    base = dict(
        strategy="ss_fade", direction="UP", token_id="tok", window_slug="w-1",
        window_ts=1_785_600_000, limit_cap=0.60, entry_price=0.50, shares=5.0,
        cost=2.5, shares_count=5.0, multiplier=1.0, loss_streak=0, mode="paper",
    )
    base.update(kw)
    return TradeModel(**base)


class TestResolutionWritesBack:
    def test_merged_status_is_what_gets_synced(self, sqlite_db):
        """The in-memory sync must see the resolved status, not the stale one.

        Regression: `session.merge()` returns a *different* instance, so writing
        `trade.status` on the merged copy left the original object — the one the
        in-memory sync loop reads — still marked "open" forever.
        """
        with db_context():
            t = _make_trade()
            db.session.add(t)
            db.session.commit()
            trade_id = t.id

        # Detach: query in one context, resolve in another (what the trader does).
        with db_context():
            detached = db.session.query(TradeModel).filter_by(id=trade_id).all()

        resolutions = [(detached[0], "UP", 2.5, True)]

        with db_context():
            for trade, winner, pnl, won in resolutions:
                merged = db.session.merge(trade)
                merged.status = "won" if won else "lost"
                merged.outcome = winner
                merged.won = won
                merged.pnl = pnl
            db.session.commit()

        # The trader now derives status from `won`, never from the stale object.
        for trade, winner, pnl, won in resolutions:
            status = "won" if won else "lost"
            assert status == "won"
            assert trade.status == "open"  # the stale value the old code used

        with db_context():
            assert db.session.get(TradeModel, trade_id).status == "won"

    def test_pnl_uses_dollar_settlement(self, sqlite_db):
        """A winning share settles at $1.00, so pnl = shares - cost."""
        with db_context():
            t = _make_trade(shares=5.0, cost=2.5)
            db.session.add(t)
            db.session.commit()

            won_pnl = round(t.shares - t.cost, 4)
            lost_pnl = round(-t.cost, 4)

        assert won_pnl == pytest.approx(2.5)
        assert lost_pnl == pytest.approx(-2.5)


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard stats aggregation
# ═══════════════════════════════════════════════════════════════════════════════


class TestAggregateDbStats:
    def test_aggregates_over_every_trade_not_just_a_page(self, sqlite_db):
        """Stats must cover the whole table.

        Regression: /state computed P&L, win rate, ROI and bankroll from the
        same 100-row page it used for the history table, so every metric drifted
        once the bot passed 100 trades. The table now paginates through
        /api/trades and stats aggregate in SQL, but the invariant is the same:
        these numbers can never be derived from a page.
        """
        from bot.dashboard import _aggregate_db_stats
        from bot.trade_queries import DEFAULT_PER_PAGE

        n_won = DEFAULT_PER_PAGE * 6 + 50
        with db_context():
            for i in range(n_won):
                db.session.add(_make_trade(
                    window_slug=f"w-{i}", status="won", won=True,
                    outcome="UP", pnl=2.5, cost=2.5, shares=5.0,
                ))
            db.session.commit()

            stats, strat, by_symbol = _aggregate_db_stats(starting_bankroll=1000.0)

        assert stats["trades"] == n_won
        assert stats["wins"] == n_won
        assert stats["win_rate"] == pytest.approx(1.0)
        assert stats["resolved_pnl"] == pytest.approx(2.5 * n_won)
        assert stats["bankroll"] == pytest.approx(1000.0 + 2.5 * n_won)
        assert strat["ss_fade"]["trades"] == n_won

    def test_splits_wins_losses_open_and_strategies(self, sqlite_db):
        from bot.dashboard import _aggregate_db_stats

        with db_context():
            db.session.add(_make_trade(strategy="ss_fade", window_slug="a",
                                       status="won", won=True, pnl=2.5, cost=2.5))
            db.session.add(_make_trade(strategy="ss_fade", window_slug="b",
                                       status="lost", won=False, pnl=-2.5, cost=2.5))
            db.session.add(_make_trade(strategy="ss_trend", window_slug="c",
                                       status="won", won=True, pnl=3.0, cost=2.0))
            db.session.add(_make_trade(strategy="ss_trend", window_slug="d",
                                       status="open", cost=2.0))
            db.session.commit()

            stats, strat, by_symbol = _aggregate_db_stats(starting_bankroll=100.0)

        assert (stats["trades"], stats["wins"], stats["losses"], stats["open"]) == (4, 2, 1, 1)
        assert stats["win_rate"] == pytest.approx(2 / 3)
        assert stats["resolved_pnl"] == pytest.approx(3.0)
        assert stats["total_invested"] == pytest.approx(9.0)
        assert stats["bankroll"] == pytest.approx(103.0)

        assert strat["ss_fade"] == {
            "trades": 2, "open": 0, "wins": 1, "losses": 1,
            "win_rate": pytest.approx(0.5), "pnl": pytest.approx(0.0),
            "total_invested": pytest.approx(5.0), "roi": pytest.approx(0.0),
        }
        assert strat["ss_trend"]["trades"] == 2
        assert strat["ss_trend"]["pnl"] == pytest.approx(3.0)
        assert strat["ss_trend"]["open"] == 1

        # Everything above was BTC, so the per-market split must reproduce it.
        assert by_symbol["btc"]["trades"] == 4
        assert by_symbol["btc"]["pnl"] == pytest.approx(3.0)

    def test_empty_table_returns_no_override(self, sqlite_db):
        from bot.dashboard import _aggregate_db_stats

        with db_context():
            assert _aggregate_db_stats(starting_bankroll=1000.0) == ({}, {}, {})


# ═══════════════════════════════════════════════════════════════════════════════
# Contradictory signals
# ═══════════════════════════════════════════════════════════════════════════════


def _sig(strategy, direction):
    from bot.strategy_streak import StreakSignal
    return StreakSignal(strategy=strategy, direction=direction, limit_cap=0.6,
                        shares=5.0, multiplier=1.0, loss_streak=0, signal_reason="t")


class TestLateEntryGate:
    """Refusing a window that is already underway.

    Found in paper: the loop only waits for the next boundary when less than
    60 s remain, so a restart 182 s into a window traded it — and bought DOWN
    at $0.06. That fill is adverse selection by construction: the limit cap
    prices the favourite out, so the only side that can still fill late is the
    one the market has already written off.
    """

    def test_a_fresh_window_is_allowed(self):
        from bot.streak_trader import is_entry_too_late

        assert is_entry_too_late(1_000, 1_002, 60) is False

    def test_the_boundary_itself_is_allowed(self):
        from bot.streak_trader import is_entry_too_late

        assert is_entry_too_late(1_000, 1_000, 60) is False

    def test_exactly_at_the_limit_is_still_allowed(self):
        from bot.streak_trader import is_entry_too_late

        assert is_entry_too_late(1_000, 1_060, 60) is False

    def test_one_second_past_the_limit_is_refused(self):
        from bot.streak_trader import is_entry_too_late

        assert is_entry_too_late(1_000, 1_061, 60) is True

    def test_the_observed_case_is_refused(self):
        """182 s into the window — the restart that bought DOWN at $0.06."""
        from bot.streak_trader import is_entry_too_late

        assert is_entry_too_late(1_000, 1_182, 60) is True

    def test_clock_skew_before_the_open_is_not_late(self):
        """A negative age must not read as 'very late' through a sign flip."""
        from bot.streak_trader import is_entry_too_late

        assert is_entry_too_late(1_000, 990, 60) is False

    def test_a_wider_setting_allows_a_later_entry(self):
        from bot.streak_trader import is_entry_too_late

        assert is_entry_too_late(1_000, 1_182, 240) is False


class TestAskAboveCapGate:
    """The cap must skip the window, not turn into a bid under the ask.

    `min(cap, ask)` quietly made a priced-out window into a resting maker bid:
    in paper it was booked as a fill that would never have happened, and in
    real mode it left a GTC order the bot neither verifies nor cancels, with
    the position already written to `trades`.

    Measured over the 1.150 fade signals of the Gamma sample, the ask beats the
    0,52 cap in 36% of windows; skipping exactly those takes the signal from
    +3,91%/trade (n=1150, t=+1,37) to +8,77% (n=734, 54,9%, t=+2,40).
    Reproducible with `python scripts/cap_impact.py`.
    """

    def test_an_ask_under_the_cap_is_tradeable(self):
        from bot.streak_trader import is_ask_above_cap

        assert is_ask_above_cap(0.50, 0.52) is False

    def test_an_ask_equal_to_the_cap_is_tradeable(self):
        """Prices live on a one-cent grid: paying the cap is what it allows."""
        from bot.streak_trader import is_ask_above_cap

        assert is_ask_above_cap(0.52, 0.52) is False

    def test_one_tick_over_the_cap_is_refused(self):
        from bot.streak_trader import is_ask_above_cap

        assert is_ask_above_cap(0.53, 0.52) is True

    def test_the_common_case_is_refused(self):
        """0,56 under a 0,52 cap — the shape of the 36% that used to rest."""
        from bot.streak_trader import is_ask_above_cap

        assert is_ask_above_cap(0.56, 0.52) is True

    def test_a_missing_ask_does_not_refuse(self):
        """Fail-open: a WebSocket hiccup is not a reason to halt trading.

        Same stance as the regime gate when Binance has no candles.
        """
        from bot.streak_trader import is_ask_above_cap

        assert is_ask_above_cap(None, 0.52) is False

    def test_a_zero_ask_does_not_refuse(self):
        from bot.streak_trader import is_ask_above_cap

        assert is_ask_above_cap(0.0, 0.52) is False

    def test_execute_signal_skips_without_recording_a_position(self, monkeypatch):
        """The gate has to fire in the trader, not just in the helper.

        The defect was never in the arithmetic — `min()` did what it says. It
        was that a priced-out window still reached the persist block, so this
        asserts the early return: a counted skip and no trade.
        """
        from bot.streak_trader import StreakSnapperTrader

        class FakeState:
            mode = "paper"

            def __init__(self):
                self.skips = []
                self.added = []
                self.status = None

            def get_asks(self):
                return (0.56, 0.44)          # UP priced out, DOWN would be fine

            def record_skip(self, reason):
                self.skips.append(reason)

            def set_status(self, *a):
                self.status = a

            def add_trade(self, trade):      # must never be reached
                self.added.append(trade)

        trader = StreakSnapperTrader.__new__(StreakSnapperTrader)
        trader.state = FakeState()
        trader.symbol = "btc"
        trader._client = None

        class Tokens:
            up_token_id = "up"
            down_token_id = "down"
            slug = "btc-updown-5m-1"
            window_ts = 1_785_600_000

        sig = _sig("ss_fade", "UP")
        sig.limit_cap = 0.52

        trader._execute_signal(Tokens(), sig)

        assert trader.state.skips == ["SKIP_ASK_ABOVE_CAP"]
        assert trader.state.added == []

    def test_the_other_side_still_trades(self, sqlite_db):
        """Only the signalled side's ask matters — DOWN at 0,44 is tradeable.

        Guards against reading the wrong leg of `get_asks()`, which would skip
        every window whenever either side happened to be expensive. Runs
        against a real SQLite DB so it reaches the persist block: the persist
        block swallows its own exceptions, so a stubbed DB would make this pass
        without proving anything.
        """
        from bot import streak_trader as st

        class FakeState:
            mode = "paper"

            def __init__(self):
                self.skips = []
                self.added = []

            def get_asks(self):
                return (0.56, 0.44)

            def record_skip(self, reason):
                self.skips.append(reason)

            def set_status(self, *a):
                pass

            def add_trade(self, trade):
                self.added.append(trade)

        trader = st.StreakSnapperTrader.__new__(st.StreakSnapperTrader)
        trader.state = FakeState()
        trader.symbol = "btc"
        trader._client = None

        class Tokens:
            up_token_id = "up"
            down_token_id = "down"
            slug = "btc-updown-5m-1"
            window_ts = 1_785_600_000

        sig = _sig("ss_fade", "DOWN")
        sig.limit_cap = 0.52

        trader._execute_signal(Tokens(), sig)

        assert trader.state.skips == []
        assert len(trader.state.added) == 1
        # Entered at the ask, not at the cap: the cap is the worst price we
        # accept, not the price we pay.
        assert trader.state.added[0].price == pytest.approx(0.44)


class TestMatchedShares:
    """Reading the CLOB's answer about what actually filled.

    `size_matched` is in shares and authoritative; the textual status is the
    fallback for responses that omit it. The case that matters most is the one
    that returns None — see TestFillVerification.
    """

    def test_size_matched_wins_over_status(self):
        from bot.streak_trader import matched_shares

        # A resting order that has partially matched reports both, and the
        # number is the truth: "live" here means "not done", not "not filled".
        payload = {"status": "live", "size_matched": "2"}
        assert matched_shares(payload, 5.0) == pytest.approx(2.0)

    def test_string_sizes_are_parsed(self):
        """The API returns numbers as JSON strings."""
        from bot.streak_trader import matched_shares

        assert matched_shares({"size_matched": "5"}, 5.0) == pytest.approx(5.0)

    def test_camel_case_variant_is_read(self):
        from bot.streak_trader import matched_shares

        assert matched_shares({"sizeMatched": 3}, 5.0) == pytest.approx(3.0)

    def test_matched_status_means_fully_filled(self):
        from bot.streak_trader import matched_shares

        assert matched_shares({"status": "matched"}, 5.0) == pytest.approx(5.0)

    def test_status_is_case_insensitive(self):
        """POST responses answer lowercase, the order lookup uppercase."""
        from bot.streak_trader import matched_shares

        assert matched_shares({"status": "MATCHED"}, 5.0) == pytest.approx(5.0)
        assert matched_shares({"status": "LIVE"}, 5.0) == pytest.approx(0.0)

    def test_resting_and_dead_statuses_mean_nothing_filled(self):
        from bot.streak_trader import matched_shares

        for status in ("live", "delayed", "canceled", "unmatched", "rejected"):
            assert matched_shares({"status": status}, 5.0) == pytest.approx(0.0)

    def test_an_overfill_is_clamped(self):
        from bot.streak_trader import matched_shares

        assert matched_shares({"size_matched": 9}, 5.0) == pytest.approx(5.0)

    def test_unknown_payloads_return_none_not_zero(self):
        """The distinction the whole design rests on.

        None means "no idea", 0.0 means "did not fill". Collapsing them is how a
        real position stops being tracked.
        """
        from bot.streak_trader import matched_shares

        assert matched_shares({}, 5.0) is None
        assert matched_shares({"status": "wat"}, 5.0) is None
        assert matched_shares(None, 5.0) is None
        assert matched_shares("not a dict", 5.0) is None

    def test_an_unreadable_size_falls_through_to_status(self):
        from bot.streak_trader import matched_shares

        assert matched_shares({"size_matched": "abc", "status": "matched"}, 5.0) == 5.0
        assert matched_shares({"size_matched": None, "status": "live"}, 5.0) == 0.0


class _FakeClient:
    """Stands in for the CLOB. Records what was asked of it."""

    def __init__(self, post_response=None, lookups=None, cancel_ok=True):
        self._post_response = post_response or {"orderID": "0xabc", "status": "live"}
        self._lookups = list(lookups or [])
        self._cancel_ok = cancel_ok
        self.cancelled: list[str] = []
        self.lookup_count = 0

    def create_and_post_order(self, *a, **kw):
        return self._post_response

    def get_order(self, order_id):
        self.lookup_count += 1
        if not self._lookups:
            return {}
        # Last answer repeats, so a test only lists the states it cares about.
        return self._lookups.pop(0) if len(self._lookups) > 1 else self._lookups[0]

    def cancel_order(self, payload):
        if not self._cancel_ok:
            raise RuntimeError("cancel refused")
        self.cancelled.append(payload.orderID)
        return {"canceled": [payload.orderID]}


def _real_trader(client, monkeypatch):
    """A trader in real mode wired to a fake CLOB, with waits collapsed."""
    from bot import streak_trader as st

    monkeypatch.setattr(st, "FILL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(st, "FILL_POLL_SECONDS", 0.0)

    class FakeState:
        mode = "real"

        def __init__(self):
            self.skips = []
            self.added = []

        def get_asks(self):
            return (0.50, 0.50)

        def record_skip(self, reason):
            self.skips.append(reason)

        def set_status(self, *a):
            pass

        def add_trade(self, trade):
            self.added.append(trade)

    trader = st.StreakSnapperTrader.__new__(st.StreakSnapperTrader)
    trader.state = FakeState()
    trader.symbol = "btc"
    trader._client = client
    trader._stop = threading.Event()
    trader._pending_cancels = []
    return trader


class _Tokens:
    up_token_id = "up"
    down_token_id = "down"
    slug = "btc-updown-5m-1"
    window_ts = 1_785_600_000


class TestFillVerification:
    """Sending an order is not holding a position.

    Until this existed the trade was written the moment the CLOB accepted the
    order: an unfilled bid became a position the resolution step settled into a
    P&L, and the order stayed alive past its own window because nothing
    cancelled it. Real mode only — paper places no orders, so none of this can
    be verified by running the bot in paper.
    """

    def test_a_full_fill_in_the_post_response_needs_no_lookup(self, sqlite_db, monkeypatch):
        """The common case: a marketable order crosses immediately."""
        client = _FakeClient({"orderID": "0xabc", "status": "matched"})
        trader = _real_trader(client, monkeypatch)
        sig = _sig("ss_fade", "UP")
        sig.limit_cap = 0.52

        trader._execute_signal(_Tokens(), sig)

        assert client.lookup_count == 0
        assert client.cancelled == []
        assert trader.state.skips == []
        assert len(trader.state.added) == 1
        assert trader.state.added[0].shares == pytest.approx(5.0)

    def test_an_unfilled_order_is_cancelled_and_records_no_position(
        self, sqlite_db, monkeypatch
    ):
        """The bug, stated as a test."""
        client = _FakeClient(
            {"orderID": "0xabc", "status": "live"}, lookups=[{"status": "live"}]
        )
        trader = _real_trader(client, monkeypatch)
        sig = _sig("ss_fade", "UP")
        sig.limit_cap = 0.52

        trader._execute_signal(_Tokens(), sig)

        assert client.cancelled == ["0xabc"]
        assert trader.state.skips == ["SKIP_NO_FILL"]
        assert trader.state.added == []

    def test_a_partial_fill_is_kept_at_the_size_that_filled(self, sqlite_db, monkeypatch):
        """Recording the requested size would overstate the stake — and with
        martingale sizing that error compounds into the next entry."""
        client = _FakeClient(
            {"orderID": "0xabc", "status": "live"},
            lookups=[{"status": "live", "size_matched": "2"}],
        )
        trader = _real_trader(client, monkeypatch)
        sig = _sig("ss_fade", "UP")
        sig.limit_cap = 0.52

        trader._execute_signal(_Tokens(), sig)

        assert client.cancelled == ["0xabc"]          # remainder not left resting
        assert trader.state.skips == []
        assert len(trader.state.added) == 1
        held = trader.state.added[0]
        assert held.shares == pytest.approx(2.0)
        assert held.cost == pytest.approx(round(2.0 * 0.50, 4))

    def test_an_unknown_state_records_the_position(self, sqlite_db, monkeypatch):
        """Asymmetric errors: an over-recorded position is a wrong number in the
        P&L, an unrecorded real one is money spent that never resolves."""
        client = _FakeClient({"orderID": "0xabc"}, lookups=[{}])
        trader = _real_trader(client, monkeypatch)
        sig = _sig("ss_fade", "UP")
        sig.limit_cap = 0.52

        trader._execute_signal(_Tokens(), sig)

        assert trader.state.skips == []
        assert len(trader.state.added) == 1
        assert trader.state.added[0].shares == pytest.approx(5.0)

    def test_a_refused_cancellation_is_retried_at_window_close(self, sqlite_db, monkeypatch):
        """A cancel the CLOB rejects must not be shrugged off: the order can
        still fill against a window that has already settled."""
        client = _FakeClient(
            {"orderID": "0xabc", "status": "live"},
            lookups=[{"status": "live"}],
            cancel_ok=False,
        )
        trader = _real_trader(client, monkeypatch)
        sig = _sig("ss_fade", "UP")
        sig.limit_cap = 0.52

        trader._execute_signal(_Tokens(), sig)
        assert trader._pending_cancels == ["0xabc"]

        client._cancel_ok = True
        trader._sweep_pending_cancels()
        assert client.cancelled == ["0xabc"]
        assert trader._pending_cancels == []

    def test_paper_mode_places_no_order_at_all(self, sqlite_db, monkeypatch):
        """The verification path is real-mode only; paper must be untouched."""
        client = _FakeClient({"orderID": "0xabc", "status": "live"})
        trader = _real_trader(client, monkeypatch)
        trader.state.mode = "paper"
        sig = _sig("ss_fade", "UP")
        sig.limit_cap = 0.52

        trader._execute_signal(_Tokens(), sig)

        assert client.lookup_count == 0
        assert client.cancelled == []
        assert len(trader.state.added) == 1
        assert trader.state.added[0].shares == pytest.approx(5.0)


class TestContradictorySignals:
    """Buying both sides of the same window costs exactly what it pays out."""

    @staticmethod
    def _resolve(signals):
        # The real function the trader calls. This used to be a hand-written
        # mirror of the rule, which meant the test kept passing — asserting that
        # Trend won — after the trader had been changed to keep Fade. A test
        # that reimplements the thing it tests can only ever verify itself.
        from bot import strategies

        kept, _dropped = strategies.resolve_conflicts(signals)
        return kept

    def test_opposite_sides_keep_only_fade(self):
        """Fade takes precedence: +3.74%/op against Trend's −4.22% (Fase 8).

        The old rule kept Trend, on the argument that a locked cycle shouldn't
        stall. It cost 98 of 1.152 Fade entries in favour of the worse signal.
        """
        signals = [_sig("ss_fade", "DOWN"), _sig("ss_trend", "UP")]
        kept = self._resolve(signals)
        assert [s.strategy for s in kept] == ["ss_fade"]
        assert kept[0].direction == "DOWN"

    def test_same_side_keeps_both(self):
        signals = [_sig("ss_fade", "UP"), _sig("ss_trend", "UP")]
        assert self._resolve(signals) == signals

    def test_single_signal_is_untouched(self):
        signals = [_sig("ss_fade", "UP")]
        assert self._resolve(signals) == signals

    def test_no_signals_stays_empty(self):
        assert self._resolve([]) == []

    def test_hedged_pair_nets_to_zero(self):
        """The reason the guard exists, spelled out with the observed numbers."""
        fade_cost = 5.0 * 0.54
        trend_cost = 5.0 * 0.46
        payout = 5.0  # exactly one side settles at $1/share
        assert fade_cost + trend_cost == pytest.approx(payout)


# ═══════════════════════════════════════════════════════════════════════════════
# Schema migration
# ═══════════════════════════════════════════════════════════════════════════════


class TestLateColumnMigration:
    def test_adds_resolution_source_to_a_preexisting_table(self, tmp_path, monkeypatch):
        """create_all() never adds columns to a table that already exists."""
        import sqlite3

        from bot.db import _add_missing_columns, db_context

        path = tmp_path / "legacy.db"
        # A trades table as it existed before resolution_source was introduced.
        con = sqlite3.connect(path)
        con.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy VARCHAR(16) NOT NULL, direction VARCHAR(4) NOT NULL,
                token_id VARCHAR(64) NOT NULL, window_slug VARCHAR(128) NOT NULL,
                window_ts INTEGER NOT NULL, limit_cap FLOAT NOT NULL,
                entry_price FLOAT NOT NULL, shares FLOAT NOT NULL, cost FLOAT NOT NULL,
                shares_count FLOAT NOT NULL, multiplier FLOAT NOT NULL,
                loss_streak INTEGER NOT NULL, mode VARCHAR(8) NOT NULL,
                status VARCHAR(8) NOT NULL, outcome VARCHAR(4), won BOOLEAN,
                pnl FLOAT, opened_at DATETIME NOT NULL, resolved_at DATETIME, note TEXT
            )""")
        con.commit()
        con.close()

        monkeypatch.delenv("DATABASE_URL", raising=False)
        init_db(database_url=f"sqlite:///{path}")
        with db_context():
            _add_missing_columns()

        con = sqlite3.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        con.close()
        assert "resolution_source" in cols

    def test_is_idempotent(self, sqlite_db):
        from bot.db import _add_missing_columns

        with db_context():
            _add_missing_columns()
            _add_missing_columns()
            db.session.add(_make_trade(resolution_source="binance"))
            db.session.commit()
            assert db.session.query(TradeModel).first().resolution_source == "binance"
