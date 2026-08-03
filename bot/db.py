"""SQLAlchemy models and DB initialization for Streak Snapper.

Uses flask_sqlalchemy (already in requirements.txt) with PostgreSQL via psycopg2-binary.
Shares DATABASE_URL with the TypeScript side (lib/db/).

All DB operations automatically push Flask app context so they work from
daemon threads (trader, strategy) without the caller needing to worry.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, NamedTuple

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Float, Integer, String, Boolean, Text, DateTime, inspect, text

db = SQLAlchemy()

# Global Flask app reference — stored by init_db(), used to push context
_app: Flask | None = None


def get_app() -> Flask:
    """Return the global Flask app. Raises RuntimeError if not initialized."""
    if _app is None:
        raise RuntimeError("DB not initialized — call init_db() first")
    return _app


@contextmanager
def db_context() -> Generator[None, None, None]:
    """Push Flask app context for DB operations. Safe for daemon threads."""
    app = get_app()
    with app.app_context():
        yield


# ── Models ────────────────────────────────────────────────────────────────────


class TradeModel(db.Model):
    """Persisted trade record for both Streak Snapper strategies."""

    __tablename__ = "trades"

    id          = db.Column(Integer, primary_key=True, autoincrement=True)
    strategy    = db.Column(String(16), nullable=False, index=True)    # "ss_fade" | "ss_trend"
    direction   = db.Column(String(4), nullable=False)                 # "UP" | "DOWN"
    token_id    = db.Column(String(64), nullable=False)
    window_slug = db.Column(String(128), nullable=False, index=True)
    window_ts   = db.Column(Integer, nullable=False)

    # Execution
    limit_cap   = db.Column(Float, nullable=False)                     # max entry price
    entry_price = db.Column(Float, nullable=False)
    shares      = db.Column(Float, nullable=False)
    cost        = db.Column(Float, nullable=False)                     # shares × entry_price
    shares_count = db.Column(Float, nullable=False)                   # number of shares bought (martingale-scaled, fractional)

    # Martingale state at entry
    multiplier  = db.Column(Float, nullable=False)                     # 1.0 = base bet
    loss_streak = db.Column(Integer, nullable=False, default=0)        # consecutive losses before this trade

    # Mode
    mode        = db.Column(String(8), nullable=False, default="paper")  # "paper" | "real"

    # Outcome (filled after resolution)
    status      = db.Column(String(8), nullable=False, default="open")  # "open" | "won" | "lost"
    outcome     = db.Column(String(4), nullable=True)                   # "UP" | "DOWN" (actual result)
    won         = db.Column(Boolean, nullable=True)                    # True if our direction == outcome
    pnl         = db.Column(Float, nullable=True)                       # profit/loss in USD

    # Who decided the outcome. Binance settles instantly at window close so the
    # martingale is correct for the next window; Gamma confirms ~3 min later.
    # 16, not 8: "chainlink" is 9 characters and would be truncated or rejected.
    resolution_source = db.Column(String(16), nullable=True)   # "binance" | "gamma" | "chainlink"

    # Timestamps
    opened_at   = db.Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(DateTime, nullable=True)

    note        = db.Column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "direction": self.direction,
            "token_id": self.token_id,
            "window_slug": self.window_slug,
            "window_ts": self.window_ts,
            "limit_cap": self.limit_cap,
            "entry_price": self.entry_price,
            "shares": self.shares,
            "cost": self.cost,
            "shares_count": self.shares_count,
            "multiplier": self.multiplier,
            "loss_streak": self.loss_streak,
            "mode": self.mode,
            "status": self.status,
            "outcome": self.outcome,
            "won": self.won,
            "pnl": self.pnl,
            "resolution_source": self.resolution_source,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "note": self.note,
        }


class MartingaleStateModel(db.Model):
    """Persisted martingale multiplier state — survives bot restarts."""

    __tablename__ = "martingale_state"

    id              = db.Column(Integer, primary_key=True, autoincrement=True)
    strategy        = db.Column(String(16), nullable=False, unique=True)  # "ss_fade" | "ss_trend"
    multiplier      = db.Column(Float, nullable=False, default=1.0)
    loss_streak     = db.Column(Integer, nullable=False, default=0)
    updated_at      = db.Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class BotConfigModel(db.Model):
    """Key-value store for runtime bot configuration."""

    __tablename__ = "bot_config"

    key   = db.Column(String(64), primary_key=True)
    value = db.Column(Text, nullable=False)


class ChainlinkTickModel(db.Model):
    """Recorded Chainlink TWAP ticks — our own tape.

    Chainlink serves no history for the report stream: subscriptions start with
    the next update and there is no replay after a disconnect. Whatever we don't
    record is gone for good, which is why this table exists at all
    (docs/CHAINLINK_TWAP.md §6, §7.4).
    """

    __tablename__ = "chainlink_ticks"

    id          = db.Column(Integer, primary_key=True, autoincrement=True)
    symbol      = db.Column(String(16), nullable=False, index=True)   # "btc/usd"
    window_s    = db.Column(Integer, nullable=False)                  # 30 | 60
    # Stored as text, not Float: the payload's full_accuracy_value is an exact
    # E18 integer and float64 can't hold it without losing digits.
    value_e18   = db.Column(String(40), nullable=False)
    # Chainlink's observation time. Indexed — the backtest queries by range.
    observed_at = db.Column(Integer, nullable=False, index=True)
    # When the relay delivered it. The gap between the two is the freshness
    # history, which is how CL_TWAP_STALE_SECONDS gets calibrated from data.
    received_at = db.Column(Integer, nullable=False)


def purge_old_ticks(retention_days: int) -> int:
    """Delete ticks older than `retention_days`. Returns rows removed.

    At ~172.800 rows/day this table outgrows a SQLite file in weeks, so the
    recorder calls this periodically rather than leaving it to be discovered
    when the VPS disk fills.
    """
    if retention_days <= 0:
        return 0

    cutoff = int(datetime.now(timezone.utc).timestamp()) - retention_days * 86_400
    deleted = (
        db.session.query(ChainlinkTickModel)
        .filter(ChainlinkTickModel.observed_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.session.commit()
    return int(deleted or 0)


# ── Init helpers ──────────────────────────────────────────────────────────────


_initialized: bool = False


def init_db(app: Flask | None = None, database_url: str | None = None) -> None:
    """Initialize SQLAlchemy with the Flask app and create all tables."""
    global _app, _initialized

    url = database_url or os.getenv("DATABASE_URL", "")

    if not url:
        # Fallback: SQLite for local dev if no DATABASE_URL set
        # Use absolute path to avoid working-directory issues
        db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "streak_snapper.db")
        url = f"sqlite:///{db_path}"

    if app is None:
        app = Flask(__name__)

    _app = app

    app.config["SQLALCHEMY_DATABASE_URI"] = url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # Always re-init the db with the current app — Flask-SQLAlchemy
    # supports being bound to multiple apps.  This matters when main.py
    # creates a temp app for table creation and dashboard.py later
    # provides the real Flask app.
    db.init_app(app)

    with app.app_context():
        if not _initialized:
            db.create_all()
            _add_missing_columns()
            _initialized = True


# Columns added after the first release. `create_all()` only creates missing
# tables, never missing columns, so an existing DB needs them backfilled.
_LATE_COLUMNS: dict[str, dict[str, str]] = {
    "trades": {"resolution_source": "VARCHAR(16)"},
}


# Columns whose type outgrew its original declaration. Adding a column is
# enough for a new field, but a field that got *wider* needs the existing
# column altered — `resolution_source` was VARCHAR(8), which truncates the
# 9-character "chainlink" that §7.3 introduces.
_WIDEN_COLUMNS: dict[str, dict[str, tuple[str, int]]] = {
    "trades": {"resolution_source": ("VARCHAR(16)", 16)},
}


def _add_missing_columns() -> None:
    """Add columns introduced after a table was first created."""
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    for table, columns in _LATE_COLUMNS.items():
        if table not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl_type in columns.items():
            if name in present:
                continue
            with db.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))

    _widen_columns(inspector, existing_tables)


def _widen_columns(inspector, existing_tables: set[str]) -> None:
    """Grow columns whose declared length is now too small.

    SQLite ignores VARCHAR lengths entirely, so this is a no-op there and only
    matters on PostgreSQL, where an over-long value raises instead of truncating.
    """
    if db.engine.dialect.name != "postgresql":
        return

    for table, columns in _WIDEN_COLUMNS.items():
        if table not in existing_tables:
            continue
        current = {c["name"]: c for c in inspector.get_columns(table)}
        for name, (ddl_type, want_len) in columns.items():
            col = current.get(name)
            if col is None:
                continue
            have_len = getattr(col["type"], "length", None)
            if have_len is not None and have_len >= want_len:
                continue
            with db.engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE {table} ALTER COLUMN {name} TYPE {ddl_type}")
                )


class MartingaleSnapshot(NamedTuple):
    """Plain copy of a martingale row.

    These helpers commit before returning, and a commit expires every attribute
    on the instance — so the ORM object is unusable once the app context pops
    and its session is gone. Callers get values, not a live instance.
    """

    strategy: str
    multiplier: float
    loss_streak: int


def _snapshot(state: MartingaleStateModel) -> MartingaleSnapshot:
    return MartingaleSnapshot(
        strategy=state.strategy,
        multiplier=state.multiplier,
        loss_streak=state.loss_streak,
    )


def get_or_create_martingale_state(strategy: str) -> MartingaleSnapshot:
    """Get or create the martingale state for a given strategy."""
    with db_context():
        state = db.session.query(MartingaleStateModel).filter_by(strategy=strategy).first()
        if state is None:
            state = MartingaleStateModel(strategy=strategy, multiplier=1.0, loss_streak=0)
            db.session.add(state)
            db.session.commit()
        return _snapshot(state)


def reset_martingale_state(strategy: str) -> MartingaleSnapshot:
    """Reset martingale multiplier to 1.0 after a win."""
    with db_context():
        state = db.session.query(MartingaleStateModel).filter_by(strategy=strategy).first()
        if state is None:
            state = MartingaleStateModel(strategy=strategy, multiplier=1.0, loss_streak=0)
            db.session.add(state)
        state.multiplier = 1.0
        state.loss_streak = 0
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return _snapshot(state)


def advance_martingale_state(strategy: str, factor: float) -> MartingaleSnapshot:
    """Multiply the martingale state after a loss."""
    with db_context():
        state = db.session.query(MartingaleStateModel).filter_by(strategy=strategy).first()
        if state is None:
            state = MartingaleStateModel(strategy=strategy, multiplier=1.0, loss_streak=0)
            db.session.add(state)
        state.multiplier = round(state.multiplier * factor, 4)
        state.loss_streak += 1
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return _snapshot(state)


def get_config(key: str, default: str | None = None) -> str | None:
    """Read a config value from the DB."""
    with db_context():
        row = db.session.query(BotConfigModel).filter_by(key=key).first()
        return row.value if row else default


def set_config(key: str, value: str) -> None:
    """Write a config value to the DB."""
    with db_context():
        row = db.session.query(BotConfigModel).filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(BotConfigModel(key=key, value=value))
        db.session.commit()


def get_all_config() -> dict[str, str]:
    """Every persisted config override, as raw strings."""
    with db_context():
        return {r.key: r.value for r in db.session.query(BotConfigModel).all()}


def set_many_config(values: dict[str, str]) -> None:
    """Upsert several config overrides in one transaction."""
    if not values:
        return
    with db_context():
        existing = {
            r.key: r
            for r in db.session.query(BotConfigModel)
            .filter(BotConfigModel.key.in_(list(values)))
            .all()
        }
        for key, value in values.items():
            row = existing.get(key)
            if row:
                row.value = value
            else:
                db.session.add(BotConfigModel(key=key, value=value))
        db.session.commit()


def clear_config(keys: list[str] | None = None) -> int:
    """Delete persisted overrides. Returns how many rows were removed.

    With `keys` omitted, drops every override so the bot falls back to the
    environment on the next restart.
    """
    with db_context():
        query = db.session.query(BotConfigModel)
        if keys is not None:
            if not keys:
                return 0
            query = query.filter(BotConfigModel.key.in_(keys))
        removed = query.delete(synchronize_session=False)
        db.session.commit()
        return removed
