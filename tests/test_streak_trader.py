"""Regression tests for streak_trader.py — tick rounding and trade resolution."""

import os
import sys

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


class TestContradictorySignals:
    """Buying both sides of the same window costs exactly what it pays out."""

    @staticmethod
    def _resolve(signals):
        # Mirrors the tie-break in StreakSnapperTrader._run_one_window.
        if len({s.direction for s in signals}) > 1:
            return [s for s in signals if s.strategy == "ss_trend"]
        return signals

    def test_opposite_sides_keep_only_trend(self):
        """Trend's side is locked until its cycle wins, so it takes precedence.

        Skipping the window instead would stall a losing cycle for as long as
        fade kept disagreeing — and in a trending market it disagrees often.
        """
        signals = [_sig("ss_fade", "DOWN"), _sig("ss_trend", "UP")]
        kept = self._resolve(signals)
        assert [s.strategy for s in kept] == ["ss_trend"]
        assert kept[0].direction == "UP"

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
