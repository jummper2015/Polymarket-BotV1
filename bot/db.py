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
from sqlalchemy import (
    Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, inspect, text,
)

from . import logger

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
    # Which asset this trade belongs to. Defaults to "btc" so rows written
    # before the column existed keep their real meaning after the backfill.
    symbol      = db.Column(String(8), nullable=False, default="btc", index=True)
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
            "symbol": self.symbol or "btc",
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
    # One cycle per strategy *per asset*: BTC's losing run must not resize ETH's
    # next entry. This replaced a bare UNIQUE on `strategy`, which is why
    # `_migrate_martingale_symbol()` has to rebuild the table on existing DBs.
    __table_args__ = (UniqueConstraint("strategy", "symbol", name="uq_martingale_strategy_symbol"),)

    id              = db.Column(Integer, primary_key=True, autoincrement=True)
    strategy        = db.Column(String(16), nullable=False)  # "ss_fade" | "ss_trend"
    symbol          = db.Column(String(8), nullable=False, default="btc")
    multiplier      = db.Column(Float, nullable=False, default=1.0)
    loss_streak     = db.Column(Integer, nullable=False, default=0)

    # ── Trend cycle (ss_trend only; NULL for ss_fade) ─────────────────────────
    # The side locked in for the current cycle and the 4h candle that chose it.
    # They live on the martingale row on purpose: side and multiplier are
    # written in the same transaction, so a crash can't leave the bot holding a
    # direction that disagrees with the stake it was sized for.
    cycle_side      = db.Column(String(4), nullable=True)    # "UP" | "DOWN"
    cycle_anchor_ts = db.Column(Integer, nullable=True)      # open ts of the signal candle

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
            # Before create_all: the rebuild drops the table so create_all can
            # put it back with the new UNIQUE(strategy, symbol).
            _migrate_martingale_symbol()
            db.create_all()
            _add_missing_columns()
            _backfill_symbol()
            _initialized = True


# Columns added after the first release. `create_all()` only creates missing
# tables, never missing columns, so an existing DB needs them backfilled.
_LATE_COLUMNS: dict[str, dict[str, str]] = {
    "trades": {
        "resolution_source": "VARCHAR(16)",
        "symbol": "VARCHAR(8)",
    },
    "martingale_state": {
        "cycle_side": "VARCHAR(4)",
        "cycle_anchor_ts": "INTEGER",
    },
}


def _migrate_martingale_symbol() -> None:
    """Rebuild `martingale_state` when it predates the `symbol` column.

    The original schema declared `strategy` UNIQUE. That has to become
    UNIQUE(strategy, symbol) so a losing run on BTC can't resize ETH's next
    entry — and SQLite writes an inline UNIQUE into CREATE TABLE with no way to
    drop it. So the table is copied out, dropped, and left for `create_all()` to
    recreate with the right constraint.

    Rebuilding a table is a heavy hammer, but this one holds a single row per
    strategy — two in practice — and losing it would only reset a multiplier to
    1.0. Any failure is swallowed for that reason: a migration problem here must
    not stop the bot from trading.
    """
    try:
        inspector = inspect(db.engine)
        if "martingale_state" not in inspector.get_table_names():
            return
        columns = {c["name"] for c in inspector.get_columns("martingale_state")}
        if "symbol" in columns:
            return

        # `updated_at` is NOT NULL, so leaving it out of the copy made every
        # INSERT fail and silently discarded the multiplier being preserved.
        keep = [c for c in (
            "strategy", "multiplier", "loss_streak",
            "cycle_side", "cycle_anchor_ts", "updated_at",
        ) if c in columns]

        with db.engine.begin() as conn:
            rows = [dict(r._mapping) for r in
                    conn.execute(text(f"SELECT {', '.join(keep)} FROM martingale_state"))]
            conn.execute(text("DROP TABLE martingale_state"))

        db.create_all()

        if rows:
            with db.engine.begin() as conn:
                cols = ", ".join(keep) + ", symbol"
                vals = ", ".join(f":{c}" for c in keep) + ", 'btc'"
                for row in rows:
                    conn.execute(
                        text(f"INSERT INTO martingale_state ({cols}) VALUES ({vals})"),
                        row,
                    )
        logger.info(
            f"[db] martingale_state migrada a (strategy, symbol) — "
            f"{len(rows)} fila(s) conservadas como 'btc'",
            icon="🗄️",
        )
    except Exception as exc:
        logger.warn(f"[db] no se pudo migrar martingale_state: {exc}")


def _backfill_symbol() -> None:
    """Give pre-multi-asset rows the symbol they were actually traded on."""
    try:
        inspector = inspect(db.engine)
        for table in ("trades", "martingale_state"):
            if table not in inspector.get_table_names():
                continue
            if "symbol" not in {c["name"] for c in inspector.get_columns(table)}:
                continue
            with db.engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {table} SET symbol = 'btc' WHERE symbol IS NULL")
                )
    except Exception as exc:
        logger.warn(f"[db] no se pudo rellenar symbol: {exc}")


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
    cycle_side: str | None = None
    cycle_anchor_ts: int | None = None


def _snapshot(state: MartingaleStateModel) -> MartingaleSnapshot:
    return MartingaleSnapshot(
        strategy=state.strategy,
        multiplier=state.multiplier,
        loss_streak=state.loss_streak,
        cycle_side=state.cycle_side,
        cycle_anchor_ts=state.cycle_anchor_ts,
    )


def _get_or_add(strategy: str, symbol: str = "btc") -> MartingaleStateModel:
    """The martingale row for (`strategy`, `symbol`), created on first use.

    Caller commits.
    """
    state = (
        db.session.query(MartingaleStateModel)
        .filter_by(strategy=strategy, symbol=symbol)
        .first()
    )
    if state is None:
        state = MartingaleStateModel(
            strategy=strategy, symbol=symbol, multiplier=1.0, loss_streak=0
        )
        db.session.add(state)
    return state


def get_or_create_martingale_state(
    strategy: str, symbol: str = "btc"
) -> MartingaleSnapshot:
    """Get or create the martingale state for a strategy on one asset."""
    with db_context():
        state = _get_or_add(strategy, symbol)
        db.session.commit()
        return _snapshot(state)


def reset_martingale_state(strategy: str, symbol: str = "btc") -> MartingaleSnapshot:
    """Reset martingale multiplier to 1.0 after a win, and close the cycle.

    A win ends both at once: there are no losses left to recover, so the side
    is no longer committed and the next window is free to re-read the trend.
    """
    with db_context():
        state = _get_or_add(strategy, symbol)
        state.multiplier = 1.0
        state.loss_streak = 0
        state.cycle_side = None
        state.cycle_anchor_ts = None
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return _snapshot(state)


def advance_martingale_state(
    strategy: str, factor: float, symbol: str = "btc"
) -> MartingaleSnapshot:
    """Multiply the martingale state after a loss. Leaves the cycle open."""
    with db_context():
        state = _get_or_add(strategy, symbol)
        state.multiplier = round(state.multiplier * factor, 4)
        state.loss_streak += 1
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return _snapshot(state)


def open_cycle(
    strategy: str, side: str, anchor_ts: int, symbol: str = "btc"
) -> MartingaleSnapshot:
    """Lock `strategy` to `side` for the block licensed by candle `anchor_ts`."""
    with db_context():
        state = _get_or_add(strategy, symbol)
        state.cycle_side = side
        state.cycle_anchor_ts = int(anchor_ts)
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return _snapshot(state)


def close_cycle(strategy: str, symbol: str = "btc") -> MartingaleSnapshot:
    """Release the locked side without touching the multiplier.

    Used when a block expires with nothing left to recover. A win goes through
    `reset_martingale_state()` instead, which does both.
    """
    with db_context():
        state = _get_or_add(strategy, symbol)
        state.cycle_side = None
        state.cycle_anchor_ts = None
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
