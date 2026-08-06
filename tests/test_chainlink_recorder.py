"""Tests for the Chainlink tape recorder.

The tape is unrecoverable if lost — Chainlink serves no history — so these
cover the two ways it could be silently corrupted (precision, batching) and the
one way it could take down the VPS (unbounded growth).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from decimal import Decimal

import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

E18 = Decimal(10) ** 18


@pytest.fixture(autouse=True)
def _reset_db_module():
    import bot.db as db_mod

    db_mod._app = None
    db_mod._initialized = False
    yield
    if db_mod._app is not None:
        try:
            with db_mod._app.app_context():
                db_mod.db.session.remove()
        except Exception:
            pass
        db_mod._app = None


@pytest.fixture
def app():
    from bot.db import init_db

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_cl_")
    os.close(fd)

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    init_db(flask_app, database_url=f"sqlite:///{db_path}")

    yield flask_app

    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


# ── persistence ───────────────────────────────────────────────────────────────


class TestRecording:
    def test_flush_writes_buffered_ticks(self, ctx):
        from bot.chainlink_recorder import ChainlinkRecorder
        from bot.db import ChainlinkTickModel, db

        rec = ChainlinkRecorder()
        now_ms = int(time.time() * 1000)
        rec.record("btc/usd", 30, Decimal("65000.5"), now_ms, now_ms + 300)

        assert rec.flush() == 1
        row = db.session.query(ChainlinkTickModel).one()
        assert row.symbol == "btc/usd"
        assert row.window_s == 30
        assert row.observed_at == now_ms // 1000

    def test_stores_e18_without_precision_loss(self, ctx):
        from bot.chainlink_recorder import ChainlinkRecorder
        from bot.db import ChainlinkTickModel, db

        # 18 significant digits — a float round-trip would corrupt this.
        exact = Decimal("65000123456789012345678") / E18
        rec = ChainlinkRecorder()
        now_ms = int(time.time() * 1000)
        rec.record("btc/usd", 30, exact, now_ms, now_ms)
        rec.flush()

        row = db.session.query(ChainlinkTickModel).one()
        assert row.value_e18 == "65000123456789012345678"
        assert Decimal(row.value_e18) / E18 == exact

    def test_batches_instead_of_committing_per_tick(self, ctx):
        from bot.chainlink_recorder import FLUSH_EVERY, ChainlinkRecorder
        from bot.db import ChainlinkTickModel, db

        rec = ChainlinkRecorder()
        now_ms = int(time.time() * 1000)

        for i in range(FLUSH_EVERY - 1):
            rec.record("btc/usd", 30, Decimal(65000 + i), now_ms, now_ms)
        assert db.session.query(ChainlinkTickModel).count() == 0
        assert rec.stats()["pending"] == FLUSH_EVERY - 1

        rec.record("btc/usd", 30, Decimal(66000), now_ms, now_ms)
        assert db.session.query(ChainlinkTickModel).count() == FLUSH_EVERY

    def test_flush_of_empty_buffer_is_a_noop(self, ctx):
        from bot.chainlink_recorder import ChainlinkRecorder

        assert ChainlinkRecorder().flush() == 0

    def test_both_windows_recorded(self, ctx):
        from bot.chainlink_recorder import ChainlinkRecorder
        from bot.db import ChainlinkTickModel, db

        rec = ChainlinkRecorder()
        now_ms = int(time.time() * 1000)
        rec.record("btc/usd", 30, Decimal(65000), now_ms, now_ms)
        rec.record("btc/usd", 60, Decimal(64000), now_ms, now_ms)
        rec.flush()

        assert {r.window_s for r in db.session.query(ChainlinkTickModel).all()} == {30, 60}


# ── retention ─────────────────────────────────────────────────────────────────


class TestPurge:
    def test_removes_only_rows_past_retention(self, ctx):
        from bot.db import ChainlinkTickModel, db, purge_old_ticks

        now = int(time.time())
        for age_days, value in [(40, "old"), (10, "recent")]:
            db.session.add(ChainlinkTickModel(
                symbol="btc/usd", window_s=30, value_e18=value,
                observed_at=now - age_days * 86_400, received_at=now,
            ))
        db.session.commit()

        assert purge_old_ticks(30) == 1
        remaining = db.session.query(ChainlinkTickModel).all()
        assert [r.value_e18 for r in remaining] == ["recent"]

    def test_zero_retention_disables_purge(self, ctx):
        from bot.db import ChainlinkTickModel, db, purge_old_ticks

        db.session.add(ChainlinkTickModel(
            symbol="btc/usd", window_s=30, value_e18="1",
            observed_at=0, received_at=0,
        ))
        db.session.commit()

        assert purge_old_ticks(0) == 0
        assert db.session.query(ChainlinkTickModel).count() == 1


# ── failure handling ──────────────────────────────────────────────────────────


class TestFailureHandling:
    def test_db_error_drops_batch_without_raising(self, ctx, monkeypatch):
        # A tape gap is bad; taking the trader down with it is worse.
        from bot import chainlink_recorder as mod
        from bot.chainlink_recorder import ChainlinkRecorder

        rec = ChainlinkRecorder()
        now_ms = int(time.time() * 1000)
        rec.record("btc/usd", 30, Decimal(65000), now_ms, now_ms)

        def boom(*a, **k):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(mod.db.session, "bulk_save_objects", boom)

        assert rec.flush() == 0
        assert rec.stats()["dropped"] == 1
        assert rec.stats()["written"] == 0
