"""Tests for GuardRepository — uses SQLite in-memory via Flask-SQLAlchemy.

Follows the same pattern as tests/test_db.py (Flask app + init_db fixture)
since bot.db uses Flask-SQLAlchemy (db.Model, not declarative Base).
"""
import os
import sys
import time
from decimal import Decimal
import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def session_factory():
    """Create a Flask app with in-memory SQLite, init schema, return sessionmaker."""
    from bot.db import init_db, db as _db

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    init_db(flask_app, database_url="sqlite:///:memory:")

    with flask_app.app_context():
        _db.create_all()
        SessionLocal = _db.session  # the scoped session
        yield lambda: SessionLocal()


class TestLoadAndSave:
    def test_load_returns_fresh_when_no_row(self, session_factory):
        from bot.security_store import GuardRepository
        repo = GuardRepository(session_factory)
        snap = repo.load("btc")
        assert snap.symbol == "btc"
        assert snap.daily_spend_usd == Decimal("0")
        assert snap.consecutive_losses == 0

    def test_save_then_load_roundtrip_preserves_fields(self, session_factory):
        from bot.security_store import GuardRepository, GuardSnapshot
        repo = GuardRepository(session_factory)
        original = GuardSnapshot(
            symbol="btc",
            daily_spend_usd=Decimal("123.45"),
            daily_spend_reset_at=time.time(),
            consecutive_losses=2,
            last_trade_at=time.time(),
            hourly_drawdown_baseline_usd=Decimal("1000"),
            hourly_drawdown_at=time.time(),
            last_reset_at=0.0,
            enabled=True,
            dry_run=False,
        )
        repo.save(original)
        loaded = repo.load("btc")
        assert loaded.daily_spend_usd == Decimal("123.45")
        assert loaded.consecutive_losses == 2
        assert loaded.hourly_drawdown_baseline_usd == Decimal("1000")


class TestReset:
    def test_reset_deletes_row(self, session_factory):
        from bot.security_store import GuardRepository
        repo = GuardRepository(session_factory)
        repo.record_trade("btc", Decimal("10"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=time.time())
        repo.reset("btc")
        snap = repo.load("btc")
        # After reset, should be fresh snapshot
        assert snap.daily_spend_usd == Decimal("0")
        assert snap.last_trade_at == 0.0


class TestRecordTrade:
    def test_increments_daily_spend(self, session_factory):
        from bot.security_store import GuardRepository
        repo = GuardRepository(session_factory)
        repo.record_trade("btc", Decimal("15"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=time.time())
        repo.record_trade("btc", Decimal("20"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=time.time())
        snap = repo.load("btc")
        assert snap.daily_spend_usd == Decimal("35")

    def test_loss_increments_consecutive_losses(self, session_factory):
        from bot.security_store import GuardRepository
        repo = GuardRepository(session_factory)
        now = time.time()
        repo.record_trade("btc", Decimal("10"), is_loss=True,
                          portfolio_value_usd=Decimal("990"), now=now)
        snap = repo.load("btc")
        assert snap.consecutive_losses == 1

    def test_win_resets_consecutive_losses(self, session_factory):
        from bot.security_store import GuardRepository
        repo = GuardRepository(session_factory)
        now = time.time()
        repo.record_trade("btc", Decimal("10"), is_loss=True,
                          portfolio_value_usd=Decimal("990"), now=now)
        repo.record_trade("btc", Decimal("10"), is_loss=True,
                          portfolio_value_usd=Decimal("980"), now=now)
        repo.record_trade("btc", Decimal("20"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=now)
        snap = repo.load("btc")
        assert snap.consecutive_losses == 0

    def test_daily_spend_resets_after_24h(self, session_factory):
        from bot.security_store import GuardRepository
        repo = GuardRepository(session_factory)
        t0 = time.time()
        repo.record_trade("btc", Decimal("100"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=t0)
        # 25 hours later
        repo.record_trade("btc", Decimal("20"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=t0 + 25*3600)
        snap = repo.load("btc")
        assert snap.daily_spend_usd == Decimal("20")  # reset, not 120

    def test_hourly_baseline_initialized_on_first_trade(self, session_factory):
        from bot.security_store import GuardRepository
        repo = GuardRepository(session_factory)
        repo.record_trade("btc", Decimal("10"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=time.time())
        snap = repo.load("btc")
        assert snap.hourly_drawdown_baseline_usd == Decimal("1000")
        assert snap.hourly_drawdown_at > 0
