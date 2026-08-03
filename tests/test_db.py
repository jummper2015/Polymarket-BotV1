"""Unit tests for db.py — CRUD operations on trades, martingale state, and bot config.

Uses a temporary SQLite file to test real DB interactions via Flask-SQLAlchemy.
"""

import os
import sys
import tempfile
from datetime import datetime, timezone

import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Test setup / teardown ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_db_module():
    """Reset the global state in bot.db before each test."""
    import bot.db as db_mod

    # Reset module globals so each test gets a fresh init
    db_mod._app = None
    db_mod._initialized = False
    yield
    # Cleanup — only if an app was bound
    if db_mod._app is not None:
        try:
            with db_mod._app.app_context():
                db_mod.db.session.remove()
        except Exception:
            pass
        db_mod._app = None


@pytest.fixture
def app():
    """Create a Flask app with a temporary SQLite database."""
    from bot.db import init_db

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_")
    os.close(fd)

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    init_db(flask_app, database_url=f"sqlite:///{db_path}")

    yield flask_app

    # Cleanup
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def ctx(app):
    """Push an app context for the test duration."""
    with app.app_context():
        yield


# ═══════════════════════════════════════════════════════════════════════════════
# TradeModel — CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class TestTradeModelCRUD:

    def test_create_trade(self, app):
        """Create a trade and verify all fields are persisted correctly."""
        from bot.db import TradeModel, db

        with app.app_context():
            trade = TradeModel(
                strategy="ss_fade",
                direction="UP",
                token_id="12345",
                window_slug="btc-updown-5m-1785600000",
                window_ts=1785600000,
                limit_cap=0.60,
                entry_price=0.52,
                shares=5.0,
                cost=2.60,
                shares_count=5.0,
                multiplier=1.0,
                loss_streak=0,
                mode="paper",
            )
            db.session.add(trade)
            db.session.commit()

            # Verify auto-increment
            assert trade.id is not None
            assert trade.id > 0

            # Re-query
            loaded = db.session.get(TradeModel, trade.id)
            assert loaded is not None
            assert loaded.strategy == "ss_fade"
            assert loaded.direction == "UP"
            assert loaded.token_id == "12345"
            assert loaded.window_slug == "btc-updown-5m-1785600000"
            assert loaded.window_ts == 1785600000
            assert loaded.limit_cap == 0.60
            assert loaded.entry_price == 0.52
            assert loaded.shares == 5.0
            assert loaded.cost == 2.60
            assert loaded.shares_count == 5.0
            assert loaded.multiplier == 1.0
            assert loaded.loss_streak == 0
            assert loaded.mode == "paper"
            assert loaded.status == "open"  # default
            assert loaded.outcome is None
            assert loaded.won is None
            assert loaded.pnl is None
            assert loaded.opened_at is not None

    def test_create_trade_trend_strategy(self, app):
        """Create a Trend strategy trade."""
        from bot.db import TradeModel, db

        with app.app_context():
            trade = TradeModel(
                strategy="ss_trend",
                direction="DOWN",
                token_id="67890",
                window_slug="btc-updown-5m-1785600300",
                window_ts=1785600300,
                limit_cap=0.52,
                entry_price=0.50,
                shares=5.0,
                cost=2.50,
                shares_count=5.0,
                multiplier=2.25,
                loss_streak=2,
                mode="real",
            )
            db.session.add(trade)
            db.session.commit()

            loaded = db.session.get(TradeModel, trade.id)
            assert loaded.strategy == "ss_trend"
            assert loaded.direction == "DOWN"
            assert loaded.multiplier == 2.25
            assert loaded.loss_streak == 2
            assert loaded.mode == "real"

    def test_resolve_trade_won(self, app):
        """Resolve a trade as won — check status, outcome, won, pnl."""
        from bot.db import TradeModel, db
        from datetime import datetime, timezone

        with app.app_context():
            trade = TradeModel(
                strategy="ss_fade", direction="UP",
                token_id="x", window_slug="s", window_ts=1,
                limit_cap=0.60, entry_price=0.50, shares=10.0,
                cost=5.0, shares_count=10.0, multiplier=1.0, loss_streak=0, mode="paper",
            )
            db.session.add(trade)
            db.session.commit()

            # Resolve as won
            trade.status = "won"
            trade.outcome = "UP"
            trade.won = True
            trade.pnl = round(10.0 - 5.0, 4)  # shares - cost = 5.0
            trade.resolved_at = datetime.now(timezone.utc)
            db.session.commit()

            loaded = db.session.get(TradeModel, trade.id)
            assert loaded.status == "won"
            assert loaded.outcome == "UP"
            assert loaded.won is True
            assert loaded.pnl == 5.0
            assert loaded.resolved_at is not None

    def test_resolve_trade_lost(self, app):
        """Resolve a trade as lost — pnl should be -cost."""
        from bot.db import TradeModel, db
        from datetime import datetime, timezone

        with app.app_context():
            trade = TradeModel(
                strategy="ss_trend", direction="DOWN",
                token_id="x", window_slug="s", window_ts=1,
                limit_cap=0.52, entry_price=0.50, shares=7.5,
                cost=3.75, shares_count=7.5, multiplier=1.5, loss_streak=1, mode="paper",
            )
            db.session.add(trade)
            db.session.commit()

            trade.status = "lost"
            trade.outcome = "UP"
            trade.won = False
            trade.pnl = -3.75
            trade.resolved_at = datetime.now(timezone.utc)
            db.session.commit()

            loaded = db.session.get(TradeModel, trade.id)
            assert loaded.status == "lost"
            assert loaded.outcome == "UP"
            assert loaded.won is False
            assert loaded.pnl == -3.75

    def test_query_open_trades(self, app):
        """Filter trades by status = open."""
        from bot.db import TradeModel, db

        with app.app_context():
            # Create 2 open trades
            base = dict(token_id="t", window_slug="ws", window_ts=1,
                        limit_cap=0.60, entry_price=0.50, shares=5.0,
                        cost=2.50, shares_count=5.0, multiplier=1.0,
                        loss_streak=0, mode="paper")

            t1 = TradeModel(strategy="ss_fade", direction="UP", **base)
            t2 = TradeModel(strategy="ss_trend", direction="DOWN", **base)
            db.session.add_all([t1, t2])
            db.session.commit()

            # Resolve one
            t1.status = "won"
            t1.outcome = "UP"
            t1.won = True
            t1.pnl = 2.50
            db.session.commit()

            open_trades = db.session.query(TradeModel).filter_by(status="open").all()
            assert len(open_trades) == 1
            assert open_trades[0].id == t2.id

    def test_query_by_strategy(self, app):
        """Filter trades by strategy."""
        from bot.db import TradeModel, db

        with app.app_context():
            base = dict(token_id="t", window_slug="ws", window_ts=1,
                        limit_cap=0.60, entry_price=0.50, shares=5.0,
                        cost=2.50, shares_count=5.0, multiplier=1.0,
                        loss_streak=0, mode="paper")

            t1 = TradeModel(strategy="ss_fade", direction="UP", **base)
            t2 = TradeModel(strategy="ss_trend", direction="DOWN", **base)
            t3 = TradeModel(strategy="ss_fade", direction="DOWN", **base)
            db.session.add_all([t1, t2, t3])
            db.session.commit()

            fade_trades = db.session.query(TradeModel).filter_by(strategy="ss_fade").all()
            assert len(fade_trades) == 2

            trend_trades = db.session.query(TradeModel).filter_by(strategy="ss_trend").all()
            assert len(trend_trades) == 1

    def test_to_dict(self, app):
        """Verify to_dict() returns correct structure."""
        from bot.db import TradeModel, db

        with app.app_context():
            trade = TradeModel(
                strategy="ss_fade", direction="UP", token_id="abc",
                window_slug="btc-updown-5m-1785600000", window_ts=1785600000,
                limit_cap=0.60, entry_price=0.52, shares=5.0, cost=2.60,
                shares_count=5.0, multiplier=1.0, loss_streak=0, mode="paper",
                note="test trade",
            )
            db.session.add(trade)
            db.session.commit()

            d = trade.to_dict()
            assert d["strategy"] == "ss_fade"
            assert d["direction"] == "UP"
            assert d["entry_price"] == 0.52
            assert d["cost"] == 2.60
            assert d["status"] == "open"
            assert d["note"] == "test trade"
            assert d["opened_at"] is not None
            assert isinstance(d["opened_at"], str)

    def test_multiple_trades_ordering(self, app):
        """Trades should be ordered by auto-increment ID."""
        from bot.db import TradeModel, db

        with app.app_context():
            base = dict(token_id="t", window_slug="ws", window_ts=1,
                        limit_cap=0.60, entry_price=0.50, shares=5.0,
                        cost=2.50, shares_count=5.0, multiplier=1.0,
                        loss_streak=0, mode="paper")

            for i in range(10):
                t = TradeModel(strategy="ss_fade", direction="UP", **base)
                db.session.add(t)
            db.session.commit()

            all_trades = db.session.query(TradeModel).order_by(TradeModel.id.asc()).all()
            assert len(all_trades) == 10
            assert all_trades[0].id < all_trades[-1].id

    def test_update_trade_fields(self, app):
        """Update specific fields on an existing trade."""
        from bot.db import TradeModel, db

        with app.app_context():
            trade = TradeModel(
                strategy="ss_fade", direction="UP", token_id="x",
                window_slug="s", window_ts=1,
                limit_cap=0.60, entry_price=0.50, shares=5.0,
                cost=2.50, shares_count=5.0, multiplier=1.0,
                loss_streak=0, mode="paper",
            )
            db.session.add(trade)
            db.session.commit()

            # Update note
            trade.note = "updated note"
            db.session.commit()

            loaded = db.session.get(TradeModel, trade.id)
            assert loaded.note == "updated note"


# ═══════════════════════════════════════════════════════════════════════════════
# MartingaleStateModel — CRUD + helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestMartingaleState:

    def test_create_martingale_state(self, app):
        """Direct creation of martingale state."""
        from bot.db import MartingaleStateModel, db

        with app.app_context():
            ms = MartingaleStateModel(strategy="ss_fade", multiplier=1.0, loss_streak=0)
            db.session.add(ms)
            db.session.commit()

            assert ms.id is not None
            assert ms.strategy == "ss_fade"
            assert ms.multiplier == 1.0
            assert ms.loss_streak == 0
            assert ms.updated_at is not None

    def test_get_or_create_new(self, app):
        """get_or_create_martingale_state creates new if not exists."""
        from bot.db import get_or_create_martingale_state, MartingaleStateModel, db

        with app.app_context():
            get_or_create_martingale_state("ss_fade")
            # Re-query to verify persistence
            loaded = db.session.query(MartingaleStateModel).filter_by(strategy="ss_fade").first()
            assert loaded is not None
            assert loaded.strategy == "ss_fade"
            assert loaded.multiplier == 1.0
            assert loaded.loss_streak == 0

    def test_get_or_create_existing(self, app):
        """get_or_create returns existing state without creating duplicate."""
        from bot.db import get_or_create_martingale_state, MartingaleStateModel, db

        with app.app_context():
            # Pre-create with custom values
            ms = MartingaleStateModel(strategy="ss_trend", multiplier=3.375, loss_streak=3)
            db.session.add(ms)
            db.session.commit()

            # Call helper, then re-query
            get_or_create_martingale_state("ss_trend")
            loaded = db.session.query(MartingaleStateModel).filter_by(strategy="ss_trend").first()
            assert loaded.multiplier == 3.375
            assert loaded.loss_streak == 3

            # No duplicate
            count = db.session.query(MartingaleStateModel).filter_by(strategy="ss_trend").count()
            assert count == 1

    def test_reset_martingale_state(self, app):
        """reset_martingale_state sets multiplier to 1.0 and loss_streak to 0."""
        from bot.db import reset_martingale_state, MartingaleStateModel, db

        with app.app_context():
            ms = MartingaleStateModel(strategy="ss_fade", multiplier=5.0625, loss_streak=4)
            db.session.add(ms)
            db.session.commit()

            reset_martingale_state("ss_fade")
            # Re-query to verify
            loaded = db.session.get(MartingaleStateModel, ms.id)
            assert loaded.multiplier == 1.0
            assert loaded.loss_streak == 0

    def test_reset_creates_if_missing(self, app):
        """reset_martingale_state creates state if it doesn't exist."""
        from bot.db import reset_martingale_state, MartingaleStateModel, db

        with app.app_context():
            reset_martingale_state("ss_trend")
            loaded = db.session.query(MartingaleStateModel).filter_by(strategy="ss_trend").first()
            assert loaded is not None
            assert loaded.strategy == "ss_trend"
            assert loaded.multiplier == 1.0
            assert loaded.loss_streak == 0

    def test_advance_martingale_state(self, app):
        """advance_martingale_state multiplies and increments loss streak."""
        from bot.db import advance_martingale_state, MartingaleStateModel, db

        with app.app_context():
            ms = MartingaleStateModel(strategy="ss_fade", multiplier=1.0, loss_streak=0)
            db.session.add(ms)
            db.session.commit()

            advance_martingale_state("ss_fade", 1.5)
            db.session.expire_all()  # flush cache after external commit
            loaded = db.session.get(MartingaleStateModel, ms.id)
            assert loaded.multiplier == 1.5
            assert loaded.loss_streak == 1

            advance_martingale_state("ss_fade", 1.5)
            db.session.expire_all()
            loaded = db.session.get(MartingaleStateModel, ms.id)
            assert loaded.multiplier == 2.25
            assert loaded.loss_streak == 2

    def test_advance_full_cycle_6_losses(self, app):
        """Simulate 6 consecutive losses — verify martingale progression."""
        from bot.db import advance_martingale_state, MartingaleStateModel, db

        with app.app_context():
            ms = MartingaleStateModel(strategy="ss_trend", multiplier=1.0, loss_streak=0)
            db.session.add(ms)
            db.session.commit()

            expected = [1.5, 2.25, 3.375, 5.0625, 7.5938, 11.3906]
            for i, exp in enumerate(expected):
                advance_martingale_state("ss_trend", 1.5)
                db.session.expire_all()  # flush cache after external commit
                loaded = db.session.get(MartingaleStateModel, ms.id)
                assert loaded.multiplier == pytest.approx(exp, rel=1e-3)
                assert loaded.loss_streak == i + 1

    def test_advance_creates_if_missing(self, app):
        """advance_martingale_state creates state if it doesn't exist."""
        from bot.db import advance_martingale_state, MartingaleStateModel, db

        with app.app_context():
            advance_martingale_state("ss_fade", 2.0)
            loaded = db.session.query(MartingaleStateModel).filter_by(strategy="ss_fade").first()
            assert loaded is not None
            assert loaded.strategy == "ss_fade"
            assert loaded.multiplier == 2.0  # 1.0 * 2.0
            assert loaded.loss_streak == 1

    def test_win_reset_after_losses(self, app):
        """After losses, a win should reset multiplier to 1.0."""
        from bot.db import advance_martingale_state, reset_martingale_state, MartingaleStateModel, db

        with app.app_context():
            ms = MartingaleStateModel(strategy="ss_fade", multiplier=1.0, loss_streak=0)
            db.session.add(ms)
            db.session.commit()

            for _ in range(3):
                advance_martingale_state("ss_fade", 1.5)
            reset_martingale_state("ss_fade")
            db.session.expire_all()
            loaded = db.session.get(MartingaleStateModel, ms.id)
            assert loaded.multiplier == 1.0
            assert loaded.loss_streak == 0

    def test_independent_fade_trend(self, app):
        """Fade and Trend martingale states are independent."""
        from bot.db import (
            advance_martingale_state,
            MartingaleStateModel,
            db,
        )

        with app.app_context():
            ms_fade = MartingaleStateModel(strategy="ss_fade", multiplier=1.0, loss_streak=0)
            ms_trend = MartingaleStateModel(strategy="ss_trend", multiplier=1.0, loss_streak=0)
            db.session.add_all([ms_fade, ms_trend])
            db.session.commit()

            advance_martingale_state("ss_fade", 1.5)
            advance_martingale_state("ss_fade", 1.5)

            db.session.expire_all()
            fade = db.session.get(MartingaleStateModel, ms_fade.id)
            trend = db.session.get(MartingaleStateModel, ms_trend.id)

            assert fade.multiplier == 2.25
            assert fade.loss_streak == 2
            assert trend.multiplier == 1.0
            assert trend.loss_streak == 0


# ═══════════════════════════════════════════════════════════════════════════════
# BotConfigModel — key-value store
# ═══════════════════════════════════════════════════════════════════════════════


class TestBotConfig:

    def test_set_and_get_config(self, app):
        """Set a config value and read it back."""
        from bot.db import set_config, get_config

        with app.app_context():
            set_config("test_key", "test_value")
            assert get_config("test_key") == "test_value"

    def test_get_missing_key_returns_default(self, app):
        """get_config with missing key returns the default."""
        from bot.db import get_config

        with app.app_context():
            assert get_config("nonexistent") is None
            assert get_config("nonexistent", "fallback") == "fallback"

    def test_update_existing_config(self, app):
        """Setting a key twice updates the value, no duplicates."""
        from bot.db import set_config, get_config, BotConfigModel, db

        with app.app_context():
            set_config("key1", "value1")
            set_config("key1", "value2")

            assert get_config("key1") == "value2"

            count = db.session.query(BotConfigModel).filter_by(key="key1").count()
            assert count == 1

    def test_multiple_keys(self, app):
        """Multiple config keys coexist."""
        from bot.db import set_config, get_config

        with app.app_context():
            set_config("a", "1")
            set_config("b", "2")
            set_config("c", "3")

            assert get_config("a") == "1"
            assert get_config("b") == "2"
            assert get_config("c") == "3"

    def test_overwrite_with_empty(self, app):
        """Overwrite a value with empty string."""
        from bot.db import set_config, get_config

        with app.app_context():
            set_config("key", "full")
            set_config("key", "")

            assert get_config("key") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# init_db
# ═══════════════════════════════════════════════════════════════════════════════


class TestInitDB:

    def test_init_creates_tables(self, app):
        """After init_db, all tables should exist."""
        from bot.db import db

        with app.app_context():
            tables = db.inspect(db.engine).get_table_names()
            assert "trades" in tables
            assert "martingale_state" in tables
            assert "bot_config" in tables

    def test_trade_model_table_schema(self, app):
        """Verify TradeModel columns match expected schema."""
        from bot.db import TradeModel

        columns = {c.name: str(c.type) for c in TradeModel.__table__.columns}
        assert columns["id"] == "INTEGER"
        assert columns["strategy"] == "VARCHAR(16)"
        assert columns["direction"] == "VARCHAR(4)"
        assert columns["status"] == "VARCHAR(8)"
        assert columns["pnl"] == "FLOAT"
        assert columns["multiplier"] == "FLOAT"
        assert columns["opened_at"] == "DATETIME"

    def test_db_context(self, app):
        """db_context() pushes app context for daemon threads."""
        from bot.db import db_context, db

        with db_context():
            # Should work without error
            result = db.session.execute(db.text("SELECT 1")).scalar()
            assert result == 1

    def test_cascade_operations(self, app):
        """Create, resolve, and query multiple trades in sequence."""
        from bot.db import TradeModel, db
        from datetime import datetime, timezone

        with app.app_context():
            base = dict(token_id="t", window_slug="ws", window_ts=1,
                        limit_cap=0.60, entry_price=0.50, shares=5.0,
                        cost=2.50, shares_count=5.0, multiplier=1.0,
                        loss_streak=0, mode="paper")

            # Create 5 trades
            trades = []
            for i, strat_dir in enumerate([("ss_fade", "UP"), ("ss_fade", "DOWN"),
                                            ("ss_trend", "UP"), ("ss_trend", "DOWN"),
                                            ("ss_fade", "UP")]):
                t = TradeModel(strategy=strat_dir[0], direction=strat_dir[1], **base)
                db.session.add(t)
                trades.append(t)
            db.session.commit()

            # Resolve 3 as won, 2 as lost
            outcomes = ["UP", "DOWN", "UP", "DOWN", "DOWN"]
            for t, outcome in zip(trades, outcomes):
                t.status = "won" if t.direction == outcome else "lost"
                t.outcome = outcome
                t.won = t.direction == outcome
                t.pnl = round(t.shares - t.cost, 4) if t.won else -t.cost
                t.resolved_at = datetime.now(timezone.utc)
            db.session.commit()

            # directions: UP, DOWN, UP, DOWN, UP
            # outcomes:   UP, DOWN, UP, DOWN, DOWN
            # results:    won, won, won, won, lost (4 won, 1 lost)
            won = db.session.query(TradeModel).filter_by(status="won").all()
            lost = db.session.query(TradeModel).filter_by(status="lost").all()

            assert len(won) == 4
            assert len(lost) == 1

            # Verify P&L: 4 × +2.50 + 1 × -2.50 = +7.50
            total_pnl = sum((t.pnl or 0) for t in trades)
            expected = (2.50 * 4) + (-2.50 * 1)
            assert total_pnl == pytest.approx(expected)


class TestMartingaleSnapshotSurvivesContextExit:
    """The helpers commit, and a commit expires every attribute on the instance.

    Regression: they used to return the live ORM object, so reading
    `.multiplier` after the app context popped raised DetachedInstanceError —
    the bot silently fell back to default multipliers on its first ever run.
    """

    def test_get_or_create_readable_outside_context(self, tmp_path, monkeypatch):
        from bot.db import get_or_create_martingale_state, init_db

        monkeypatch.delenv("DATABASE_URL", raising=False)
        init_db(database_url=f"sqlite:///{tmp_path/'fresh.db'}")

        # Freshly created row — this is the path that used to blow up.
        snap = get_or_create_martingale_state("ss_fade")
        assert snap.multiplier == 1.0
        assert snap.loss_streak == 0
        assert snap.strategy == "ss_fade"

    def test_advance_and_reset_readable_outside_context(self, tmp_path, monkeypatch):
        from bot.db import advance_martingale_state, init_db, reset_martingale_state

        monkeypatch.delenv("DATABASE_URL", raising=False)
        init_db(database_url=f"sqlite:///{tmp_path/'fresh2.db'}")

        after_loss = advance_martingale_state("ss_trend", 1.5)
        assert after_loss.multiplier == 1.5
        assert after_loss.loss_streak == 1

        after_win = reset_martingale_state("ss_trend")
        assert after_win.multiplier == 1.0
        assert after_win.loss_streak == 0
