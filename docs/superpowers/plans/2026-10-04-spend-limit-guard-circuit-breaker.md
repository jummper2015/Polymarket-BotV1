# Spend Limit Guard + Trading Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global security layer to ME$IRVE that enforces spend caps and circuit breakers before any real-money trade, blocking runaway losses from loops, latency spikes, or trend persistence.

**Architecture:** Three new modules (`bot/security.py` for pure logic, `bot/security_store.py` for DB persistence, `bot/security_runtime.py` as the facade) plus a new `bot_guards` table, integrated as a 4-line hook in `_execute_signal` and a 3-line hook in `_resolve_pending_trades`. UI exposes status via a new dashboard tile and a new settings panel.

**Tech Stack:** Python 3.12, SQLAlchemy (existing `bot/db.py`), Decimal for USD arithmetic, pytest, dataclasses + `frozen=True` for guard config.

**Spec:** `docs/superpowers/specs/2026-10-04-spend-limit-guard-circuit-breaker-design.md`

## Global Constraints

- **No regressions**: existing test suite must stay at 599 passed + 3 pre-existing failures (`test_spread_harvest.py`). Total new tests: ≥27.
- **Decimal only**: all USD arithmetic uses `decimal.Decimal`, never float.
- **English code comments, Spanish log messages** (per project convention).
- **Conservative defaults**: $50/tx, $200/day, 3 consecutive losses, 8% hourly drawdown.
- **`security_enabled=True` by default**; `security_dry_run=False` by default.
- **No breaking changes** to existing public APIs of `bot/state.py`, `bot/streak_trader.py`, `bot/main.py`.
- **All new runtime fields** go to `BotState` (config layer) and `bot_guards` table (persistence layer).
- **Survives VPS reboot**: all counter state persists in `bot_guards` table, not in process memory.

---

### Task 1: Foundation — Config dataclass, exceptions, env vars

**Files:**
- Create: `bot/security.py`
- Modify: `bot/config.py:331-379` (add env vars after existing config block)
- Modify: `.env.example` (if exists, document new vars)

**Interfaces:**
- Consumes: nothing (no other tasks exist yet)
- Produces:
  - `class GuardConfig` — frozen dataclass
  - `class SpendLimitError(Exception)` — `.kind`, `.attempted_usd`, `.cap_usd`, `.spent_usd`
  - `class CircuitBreakerError(Exception)` — `.kind`, `.current`, `.cap`

- [ ] **Step 1: Create `bot/security.py` with dataclass + exceptions**

```python
"""Security guards for trade-level spend caps and portfolio circuit breakers.

Layered defense, adapted from ECC's `llm-trading-agent-security`. All USD
arithmetic uses Decimal; never float. Stateless — receives snapshots and
config, returns decisions or raises typed exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GuardConfig:
    """Immutable config passed to every guard check.

    `dry_run=True` lets the guards LOG what they would block but never
    raise — used in paper mode to validate behavior without halting trades.
    """
    enabled: bool
    dry_run: bool
    max_single_tx_usd: Decimal
    max_daily_spend_usd: Decimal
    max_consecutive_losses: int
    max_hourly_drawdown_pct: Decimal


class SpendLimitError(Exception):
    """Single-tx or daily-cap exceeded. `kind` is 'single_tx' or 'daily'."""
    def __init__(self, kind: str, attempted_usd: Decimal, cap_usd: Decimal,
                 spent_usd: Decimal, msg: str):
        super().__init__(msg)
        self.kind = kind
        self.attempted_usd = attempted_usd
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd


class CircuitBreakerError(Exception):
    """Consecutive losses or hourly drawdown exceeded.

    `kind` is 'consecutive_losses' or 'hourly_drawdown'.
    `current` and `cap` let UI render the breach magnitude.
    """
    def __init__(self, kind: str, msg: str, current, cap):
        super().__init__(msg)
        self.kind = kind
        self.current = current
        self.cap = cap
```

- [ ] **Step 2: Create `tests/test_security.py` with smoke tests**

```python
"""Tests for SpendLimitGuard, TradingCircuitBreaker, and config dataclasses.

Pure-logic module — no DB, no mocks. Tests should run in <100ms total.
"""
from decimal import Decimal
import pytest

from bot.security import GuardConfig, SpendLimitError, CircuitBreakerError


class TestGuardConfig:
    def test_can_instantiate_with_defaults(self):
        cfg = GuardConfig(
            enabled=True,
            dry_run=False,
            max_single_tx_usd=Decimal("50"),
            max_daily_spend_usd=Decimal("200"),
            max_consecutive_losses=3,
            max_hourly_drawdown_pct=Decimal("0.08"),
        )
        assert cfg.enabled is True
        assert cfg.max_single_tx_usd == Decimal("50")

    def test_is_frozen(self):
        cfg = GuardConfig(True, False, Decimal("1"), Decimal("1"), 1, Decimal("0.01"))
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            cfg.enabled = False


class TestExceptions:
    def test_spend_limit_error_carries_context(self):
        e = SpendLimitError(
            kind="single_tx",
            attempted_usd=Decimal("100"),
            cap_usd=Decimal("50"),
            spent_usd=Decimal("0"),
            msg="too big",
        )
        assert e.kind == "single_tx"
        assert e.attempted_usd == Decimal("100")
        assert str(e) == "too big"

    def test_circuit_breaker_error_carries_context(self):
        e = CircuitBreakerError(
            kind="consecutive_losses",
            msg="halted",
            current=3,
            cap=3,
        )
        assert e.kind == "consecutive_losses"
        assert e.current == 3
        assert e.cap == 3
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `python -m pytest tests/test_security.py -v`
Expected: 4 tests PASS

- [ ] **Step 4: Add env var defaults to `bot/config.py`**

Find the line in `bot/config.py` where `TRADING_MODE` is read (around line 332):

```python
    mode = (os.getenv("TRADING_MODE") or "paper").strip().lower()
```

Right after this block (still inside `load_config`), add 6 new lines using existing helpers `_env_bool`, `_env_float`, `_env_int`:

```python
    security_enabled: bool = _env_bool("SECURITY_ENABLED", True)
    security_dry_run: bool = _env_bool("SECURITY_DRY_RUN", False)
    security_max_single_tx_usd: float = _env_float("SECURITY_MAX_SINGLE_TX_USD", 50.0)
    security_max_daily_spend_usd: float = _env_float("SECURITY_MAX_DAILY_SPEND_USD", 200.0)
    security_max_consecutive_losses: int = _env_int("SECURITY_MAX_CONSECUTIVE_LOSSES", 3)
    security_max_hourly_drawdown_pct: float = _env_float("SECURITY_MAX_HOURLY_DRAWDOWN_PCT", 0.08)
```

Then find where the `Config` dataclass is defined and add these 6 fields (matching names) with the same defaults.

- [ ] **Step 5: Verify config loads correctly**

Run: `cd /workspaces/Polymarket-BotV1 && python -c "from bot.config import load_config; c = load_config(); print(c.security_enabled, c.security_max_single_tx_usd)"`
Expected: prints `True 50.0`

- [ ] **Step 6: Commit**

```bash
git add bot/security.py tests/test_security.py bot/config.py
git commit -m "feat(security): add GuardConfig dataclass, exceptions, env vars

Foundation for spend-limit guard + circuit breaker. No logic yet — just
types and config wiring. Tests cover instantiation and frozen dataclass
behavior. Env vars: SECURITY_ENABLED, SECURITY_DRY_RUN, SECURITY_MAX_*."
```

---

### Task 2: DB layer — `bot_guards` table + `GuardSnapshot` + `GuardRepository`

**Files:**
- Modify: `bot/db.py:126-138` (add `BotGuardState` class after `BotConfigModel`)
- Create: `bot/security_store.py`
- Create: `tests/test_security_store.py`

**Interfaces:**
- Consumes: nothing yet (snapshot is opaque to repository for now)
- Produces:
  - `class BotGuardState(db.Model)` — table `bot_guards`, 10 columns
  - `class GuardSnapshot` — frozen dataclass, mirror of row
  - `class GuardRepository` — `load(symbol) -> GuardSnapshot`, `save(snap) -> None`, `reset(symbol) -> None`, `record_trade(symbol, usd, is_loss, portfolio_value_usd, now) -> GuardSnapshot`

- [ ] **Step 1: Write failing tests in `tests/test_security_store.py`**

```python
"""Tests for GuardRepository — uses SQLite in-memory, no external deps."""
import time
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bot.db import Base, BotGuardState
from bot.security_store import GuardRepository, GuardSnapshot


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class TestLoadAndSave:
    def test_load_returns_fresh_when_no_row(self, session_factory):
        repo = GuardRepository(session_factory)
        snap = repo.load("btc")
        assert snap.symbol == "btc"
        assert snap.daily_spend_usd == Decimal("0")
        assert snap.consecutive_losses == 0

    def test_save_then_load_roundtrip_preserves_fields(self, session_factory):
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
        repo = GuardRepository(session_factory)
        repo.record_trade("btc", Decimal("15"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=time.time())
        repo.record_trade("btc", Decimal("20"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=time.time())
        snap = repo.load("btc")
        assert snap.daily_spend_usd == Decimal("35")

    def test_loss_increments_consecutive_losses(self, session_factory):
        repo = GuardRepository(session_factory)
        now = time.time()
        repo.record_trade("btc", Decimal("10"), is_loss=True,
                          portfolio_value_usd=Decimal("990"), now=now)
        snap = repo.load("btc")
        assert snap.consecutive_losses == 1

    def test_win_resets_consecutive_losses(self, session_factory):
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
        repo = GuardRepository(session_factory)
        repo.record_trade("btc", Decimal("10"), is_loss=False,
                          portfolio_value_usd=Decimal("1000"), now=time.time())
        snap = repo.load("btc")
        assert snap.hourly_drawdown_baseline_usd == Decimal("1000")
        assert snap.hourly_drawdown_at > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_security_store.py -v`
Expected: ImportError on `bot.security_store`

- [ ] **Step 3: Add `BotGuardState` to `bot/db.py`**

After the `BotConfigModel` class (around line 134), add:

```python
class BotGuardState(Base):
    """Per-symbol spend cap + circuit breaker counters. Survives restart."""
    __tablename__ = "bot_guards"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    daily_spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    daily_spend_reset_at: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    last_trade_at: Mapped[float] = mapped_column(Float, default=0.0)
    hourly_drawdown_baseline_usd: Mapped[float] = mapped_column(Float, default=0.0)
    hourly_drawdown_at: Mapped[float] = mapped_column(Float, default=0.0)
    last_reset_at: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_snapshot(self):
        # Imported here to avoid circular import; GuardSnapshot lives in security_store
        from .security_store import GuardSnapshot
        return GuardSnapshot(
            symbol=self.symbol,
            daily_spend_usd=Decimal(str(self.daily_spend_usd)),
            daily_spend_reset_at=self.daily_spend_reset_at,
            consecutive_losses=self.consecutive_losses,
            last_trade_at=self.last_trade_at,
            hourly_drawdown_baseline_usd=Decimal(str(self.hourly_drawdown_baseline_usd)),
            hourly_drawdown_at=self.hourly_drawdown_at,
            last_reset_at=self.last_reset_at,
            enabled=self.enabled,
            dry_run=self.dry_run,
        )

    @classmethod
    def from_snapshot(cls, snap):
        return cls(
            symbol=snap.symbol,
            daily_spend_usd=float(snap.daily_spend_usd),
            daily_spend_reset_at=snap.daily_spend_reset_at,
            consecutive_losses=snap.consecutive_losses,
            last_trade_at=snap.last_trade_at,
            hourly_drawdown_baseline_usd=float(snap.hourly_drawdown_baseline_usd),
            hourly_drawdown_at=snap.hourly_drawdown_at,
            last_reset_at=snap.last_reset_at,
            enabled=snap.enabled,
            dry_run=snap.dry_run,
        )
```

Add `from decimal import Decimal` at top of `bot/db.py` if not present.

- [ ] **Step 4: Create `bot/security_store.py`**

```python
"""DB persistence for security guard counters. Pure CRUD + bookkeeping."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class GuardSnapshot:
    """Immutable view of guard state for one symbol."""
    symbol:                       str
    daily_spend_usd:              Decimal
    daily_spend_reset_at:         float
    consecutive_losses:           int
    last_trade_at:                float
    hourly_drawdown_baseline_usd: Decimal
    hourly_drawdown_at:           float
    last_reset_at:                float
    enabled:                      bool
    dry_run:                      bool


class GuardRepository:
    """Per-symbol snapshot persistence. One row in `bot_guards` per symbol."""

    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    def load(self, symbol: str) -> GuardSnapshot:
        from .db import BotGuardState  # avoid circular at module load
        with self._session_factory() as s:
            row = s.get(BotGuardState, symbol)
            if row is None:
                return self._fresh(symbol)
            return row.to_snapshot()

    def save(self, snap: GuardSnapshot) -> None:
        from .db import BotGuardState
        with self._session_factory() as s:
            row = BotGuardState.from_snapshot(snap)
            s.merge(row)
            s.commit()

    def reset(self, symbol: str) -> None:
        from .db import BotGuardState
        with self._session_factory() as s:
            row = s.get(BotGuardState, symbol)
            if row:
                s.delete(row)
                s.commit()

    def record_trade(
        self,
        symbol: str,
        usd: Decimal,
        is_loss: bool,
        portfolio_value_usd: Decimal,
        now: float,
    ) -> GuardSnapshot:
        snap = self.load(symbol)

        # 1. Daily cap rolling reset (>24h since last reset)
        if snap.daily_spend_reset_at > 0 and (now - snap.daily_spend_reset_at) > 86400:
            snap = replace(snap, daily_spend_usd=Decimal("0"),
                           daily_spend_reset_at=now)
        if snap.daily_spend_reset_at == 0:
            snap = replace(snap, daily_spend_reset_at=now)

        # 2. Increment daily spend
        snap = replace(snap, daily_spend_usd=snap.daily_spend_usd + usd)

        # 3. Consecutive losses: increment on loss, reset on win
        new_consec = snap.consecutive_losses + 1 if is_loss else 0
        snap = replace(snap, consecutive_losses=new_consec)

        # 4. Hourly baseline: init if first trade, roll if >1h
        HOUR = 3600.0
        if snap.hourly_drawdown_at == 0:
            snap = replace(snap,
                           hourly_drawdown_baseline_usd=portfolio_value_usd,
                           hourly_drawdown_at=now)
        elif (now - snap.hourly_drawdown_at) > HOUR:
            snap = replace(snap,
                           hourly_drawdown_baseline_usd=portfolio_value_usd,
                           hourly_drawdown_at=now)

        snap = replace(snap, last_trade_at=now)
        self.save(snap)
        return snap

    def _fresh(self, symbol: str) -> GuardSnapshot:
        return GuardSnapshot(
            symbol=symbol,
            daily_spend_usd=Decimal("0"),
            daily_spend_reset_at=0.0,
            consecutive_losses=0,
            last_trade_at=0.0,
            hourly_drawdown_baseline_usd=Decimal("0"),
            hourly_drawdown_at=0.0,
            last_reset_at=time.time(),
            enabled=True,
            dry_run=False,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_security_store.py -v`
Expected: 9 tests PASS

- [ ] **Step 6: Verify migration is idempotent (table created on existing DB)**

Run: `python -c "from bot.db import init_db; init_db()"`
Expected: no errors; table `bot_guards` now exists in `data/streak_snapper.db`

- [ ] **Step 7: Commit**

```bash
git add bot/db.py bot/security_store.py tests/test_security_store.py
git commit -m "feat(security): bot_guards table + GuardRepository

DB layer for spend-cap and circuit-breaker counters. One row per symbol
in `bot_guards` (10 columns). Survives VPS reboot. record_trade does
rolling 24h daily reset, consecutive-losses counter, and hourly baseline
tracking. 9 tests cover load/save/reset/record paths and edge cases."
```

---

### Task 3: SpendLimitGuard logic

**Files:**
- Modify: `bot/security.py` (append `SpendLimitGuard` class)
- Modify: `tests/test_security.py` (add `TestSpendLimitGuard` class)

**Interfaces:**
- Consumes: `GuardConfig`, `GuardSnapshot`, `Decimal`
- Produces: `SpendLimitGuard.check_and_record(proposed_usd, snap, config) -> GuardSnapshot` (raises `SpendLimitError`)

- [ ] **Step 1: Add failing tests to `tests/test_security.py`**

Append:

```python
from bot.security import SpendLimitGuard


def _make_config(**overrides) -> GuardConfig:
    defaults = dict(
        enabled=True,
        dry_run=False,
        max_single_tx_usd=Decimal("50"),
        max_daily_spend_usd=Decimal("200"),
        max_consecutive_losses=3,
        max_hourly_drawdown_pct=Decimal("0.08"),
    )
    defaults.update(overrides)
    return GuardConfig(**defaults)


def _make_snap(daily=Decimal("0"), consec=0) -> GuardSnapshot:
    from bot.security_store import GuardSnapshot
    return GuardSnapshot(
        symbol="btc",
        daily_spend_usd=daily,
        daily_spend_reset_at=time.time(),
        consecutive_losses=consec,
        last_trade_at=0.0,
        hourly_drawdown_baseline_usd=Decimal("1000"),
        hourly_drawdown_at=time.time(),
        last_reset_at=0.0,
        enabled=True,
        dry_run=False,
    )


class TestSpendLimitGuard:
    def test_single_tx_under_cap_passes(self):
        guard = SpendLimitGuard()
        snap = _make_snap()
        result = guard.check_and_record(Decimal("40"), snap, _make_config())
        assert result.daily_spend_usd == Decimal("40")

    def test_single_tx_at_cap_passes(self):
        guard = SpendLimitGuard()
        snap = _make_snap()
        result = guard.check_and_record(Decimal("50"), snap, _make_config())
        assert result.daily_spend_usd == Decimal("50")

    def test_single_tx_over_cap_raises(self):
        guard = SpendLimitGuard()
        snap = _make_snap()
        with pytest.raises(SpendLimitError) as exc:
            guard.check_and_record(Decimal("60"), snap, _make_config())
        assert exc.value.kind == "single_tx"

    def test_daily_under_cap_passes(self):
        guard = SpendLimitGuard()
        snap = _make_snap(daily=Decimal("100"))
        result = guard.check_and_record(Decimal("80"), snap, _make_config())
        assert result.daily_spend_usd == Decimal("180")

    def test_daily_at_cap_passes(self):
        guard = SpendLimitGuard()
        snap = _make_snap(daily=Decimal("150"))
        result = guard.check_and_record(Decimal("50"), snap, _make_config())
        assert result.daily_spend_usd == Decimal("200")

    def test_daily_over_cap_raises(self):
        guard = SpendLimitGuard()
        snap = _make_snap(daily=Decimal("180"))
        with pytest.raises(SpendLimitError) as exc:
            guard.check_and_record(Decimal("30"), snap, _make_config())
        assert exc.value.kind == "daily"

    def test_returned_snapshot_carries_updated_daily(self):
        guard = SpendLimitGuard()
        snap = _make_snap(daily=Decimal("10"))
        result = guard.check_and_record(Decimal("25"), snap, _make_config())
        assert result.daily_spend_usd == Decimal("35")
        # original unchanged
        assert snap.daily_spend_usd == Decimal("10")
```

Add at top of test file: `import time` and `from bot.security_store import GuardSnapshot`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_security.py::TestSpendLimitGuard -v`
Expected: ImportError or AttributeError on `SpendLimitGuard`

- [ ] **Step 3: Implement `SpendLimitGuard` in `bot/security.py`**

Append after `CircuitBreakerError`:

```python
class SpendLimitGuard:
    """Stateless. Verifies a proposed spend against single-tx and daily caps."""

    def check_and_record(
        self,
        proposed_usd: Decimal,
        snapshot: "GuardSnapshot",
        config: "GuardConfig",
    ) -> "GuardSnapshot":
        # 1. Single-tx cap
        if proposed_usd > config.max_single_tx_usd:
            raise SpendLimitError(
                kind="single_tx",
                attempted_usd=proposed_usd,
                cap_usd=config.max_single_tx_usd,
                spent_usd=snapshot.daily_spend_usd,
                msg=f"single tx ${proposed_usd} > cap ${config.max_single_tx_usd}",
            )
        # 2. Daily cap (rolling 24h is handled by repo before snap reaches us)
        new_daily = snapshot.daily_spend_usd + proposed_usd
        if new_daily > config.max_daily_spend_usd:
            raise SpendLimitError(
                kind="daily",
                attempted_usd=proposed_usd,
                cap_usd=config.max_daily_spend_usd,
                spent_usd=snapshot.daily_spend_usd,
                msg=f"daily ${snapshot.daily_spend_usd} + ${proposed_usd} > ${config.max_daily_spend_usd}",
            )
        # 3. Return new snapshot with incremented spend (immutable update)
        from dataclasses import replace
        return replace(snapshot, daily_spend_usd=new_daily)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_security.py::TestSpendLimitGuard -v`
Expected: 7 tests PASS

- [ ] **Step 5: Run full security tests**

Run: `python -m pytest tests/test_security.py -v`
Expected: 4 + 7 = 11 tests PASS

- [ ] **Step 6: Commit**

```bash
git add bot/security.py tests/test_security.py
git commit -m "feat(security): SpendLimitGuard with single-tx + daily caps

Stateless guard. Raises SpendLimitError if proposed_usd exceeds
max_single_tx_usd OR if (daily_spend + proposed) exceeds
max_daily_spend_usd. Returns updated snapshot otherwise.

Daily reset (>24h rolling) is handled upstream in GuardRepository.record_trade
so the guard only deals with the already-reset counter."
```

---

### Task 4: TradingCircuitBreaker logic

**Files:**
- Modify: `bot/security.py` (append `TradingCircuitBreaker` class)
- Modify: `tests/test_security.py` (add `TestTradingCircuitBreaker` class)

**Interfaces:**
- Consumes: `GuardConfig`, `GuardSnapshot`, `Decimal` (portfolio value)
- Produces: `TradingCircuitBreaker.check(snap, portfolio_value_usd, config) -> None` (raises `CircuitBreakerError`)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_security.py`:

```python
from bot.security import TradingCircuitBreaker


class TestTradingCircuitBreaker:
    def test_under_consecutive_losses_passes(self):
        cb = TradingCircuitBreaker()
        snap = _make_snap(consec=2)
        cb.check(snap, Decimal("1000"), _make_config())  # no raise

    def test_at_consecutive_losses_raises(self):
        cb = TradingCircuitBreaker()
        snap = _make_snap(consec=3)
        with pytest.raises(CircuitBreakerError) as exc:
            cb.check(snap, Decimal("1000"), _make_config())
        assert exc.value.kind == "consecutive_losses"

    def test_over_consecutive_losses_raises(self):
        cb = TradingCircuitBreaker()
        snap = _make_snap(consec=5)
        with pytest.raises(CircuitBreakerError) as exc:
            cb.check(snap, Decimal("1000"), _make_config())
        assert exc.value.kind == "consecutive_losses"

    def test_no_hourly_baseline_does_not_trigger(self):
        cb = TradingCircuitBreaker()
        snap = GuardSnapshot(
            symbol="btc",
            daily_spend_usd=Decimal("0"),
            daily_spend_reset_at=0.0,
            consecutive_losses=0,
            last_trade_at=0.0,
            hourly_drawdown_baseline_usd=Decimal("0"),  # no baseline yet
            hourly_drawdown_at=0.0,
            last_reset_at=0.0,
            enabled=True,
            dry_run=False,
        )
        cb.check(snap, Decimal("100"), _make_config())  # no raise on first trade

    def test_hourly_drawdown_under_threshold_passes(self):
        cb = TradingCircuitBreaker()
        snap = _make_snap()
        # baseline=1000, portfolio=950 → -5% > -8% threshold
        cb.check(snap, Decimal("950"), _make_config())

    def test_hourly_drawdown_at_threshold_passes(self):
        cb = TradingCircuitBreaker()
        snap = _make_snap()
        # baseline=1000, portfolio=920 → -8% = threshold (boundary allowed)
        cb.check(snap, Decimal("920"), _make_config())

    def test_hourly_drawdown_over_threshold_raises(self):
        cb = TradingCircuitBreaker()
        snap = _make_snap()
        with pytest.raises(CircuitBreakerError) as exc:
            cb.check(snap, Decimal("900"), _make_config())  # -10%
        assert exc.value.kind == "hourly_drawdown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_security.py::TestTradingCircuitBreaker -v`
Expected: ImportError on `TradingCircuitBreaker`

- [ ] **Step 3: Implement `TradingCircuitBreaker` in `bot/security.py`**

Append:

```python
class TradingCircuitBreaker:
    """Stateless. Verifies consecutive losses and hourly drawdown against caps."""

    def check(
        self,
        snapshot: "GuardSnapshot",
        portfolio_value_usd: Decimal,
        config: "GuardConfig",
    ) -> None:
        # 1. Consecutive losses
        if snapshot.consecutive_losses >= config.max_consecutive_losses:
            raise CircuitBreakerError(
                kind="consecutive_losses",
                msg=f"{snapshot.consecutive_losses} consecutive losses ≥ "
                    f"{config.max_consecutive_losses}",
                current=snapshot.consecutive_losses,
                cap=config.max_consecutive_losses,
            )
        # 2. Hourly drawdown (only if baseline exists)
        if snapshot.hourly_drawdown_baseline_usd > 0:
            drawdown = (
                (portfolio_value_usd - snapshot.hourly_drawdown_baseline_usd)
                / snapshot.hourly_drawdown_baseline_usd
            )
            if drawdown < -config.max_hourly_drawdown_pct:
                raise CircuitBreakerError(
                    kind="hourly_drawdown",
                    msg=f"hourly drawdown {drawdown:.1%} < "
                        f"-{config.max_hourly_drawdown_pct:.0%}",
                    current=drawdown,
                    cap=-config.max_hourly_drawdown_pct,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_security.py -v`
Expected: 11 + 7 = 18 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/security.py tests/test_security.py
git commit -m "feat(security): TradingCircuitBreaker (consecutive losses + drawdown)

Stateless. Raises CircuitBreakerError when consecutive_losses >=
max_consecutive_losses OR hourly drawdown exceeds threshold. First
trade (no baseline) does not trigger. Boundary value (-8% exactly) is
allowed. 7 tests cover under/at/over for both gates."
```

---

### Task 5: SecurityRuntime (facade)

**Files:**
- Create: `bot/security_runtime.py`
- Create: `tests/test_security_runtime.py`

**Interfaces:**
- Consumes: `GuardConfig`, `GuardRepository`, `BotState` (typed as `Any`)
- Produces:
  - `class SecurityRuntime`:
    - `.check(symbol, proposed_usd, portfolio_value_usd, state)` — raises on block; logs and `record_skip` either way
    - `.record_trade(symbol, usd, is_loss, portfolio_value_usd, now)` — delegates to repo
    - `.reset(symbol)` — clears via repo
    - `.update_config(GuardConfig)` — hot-reload

- [ ] **Step 1: Write failing tests in `tests/test_security_runtime.py`**

```python
"""Tests for SecurityRuntime facade — uses MockRepo, no DB."""
from decimal import Decimal
import time
from unittest.mock import MagicMock, call
import pytest

from bot.security import GuardConfig, SpendLimitError, CircuitBreakerError
from bot.security_store import GuardSnapshot
from bot.security_runtime import SecurityRuntime


@pytest.fixture
def cfg() -> GuardConfig:
    return GuardConfig(
        enabled=True,
        dry_run=False,
        max_single_tx_usd=Decimal("50"),
        max_daily_spend_usd=Decimal("200"),
        max_consecutive_losses=3,
        max_hourly_drawdown_pct=Decimal("0.08"),
    )


@pytest.fixture
def fresh_snap() -> GuardSnapshot:
    return GuardSnapshot(
        symbol="btc", daily_spend_usd=Decimal("0"),
        daily_spend_reset_at=time.time(), consecutive_losses=0,
        last_trade_at=0.0, hourly_drawdown_baseline_usd=Decimal("0"),
        hourly_drawdown_at=0.0, last_reset_at=0.0, enabled=True, dry_run=False,
    )


class TestRuntimeCheck:
    def test_disabled_runtime_skips_all(self, cfg, fresh_snap):
        repo = MagicMock()
        repo.load.return_value = fresh_snap
        rt = SecurityRuntime(cfg, repo)
        cfg_disabled = GuardConfig(False, False, *cfg[2:])
        rt.update_config(cfg_disabled)
        # Should NOT call load if disabled
        rt.check("btc", Decimal("10000"), Decimal("1000"), MagicMock())
        repo.load.assert_not_called()

    def test_under_caps_passes(self, cfg, fresh_snap):
        repo = MagicMock()
        repo.load.return_value = fresh_snap
        rt = SecurityRuntime(cfg, repo)
        rt.check("btc", Decimal("30"), Decimal("1000"), MagicMock())
        repo.save.assert_called_once()  # snapshot saved with incremented daily

    def test_spend_limit_blocked_raises(self, cfg, fresh_snap):
        repo = MagicMock()
        repo.load.return_value = fresh_snap
        state = MagicMock()
        rt = SecurityRuntime(cfg, repo)
        with pytest.raises(SpendLimitError):
            rt.check("btc", Decimal("100"), Decimal("1000"), state)
        state.record_skip.assert_called()
        state.set_status.assert_called()

    def test_circuit_breaker_blocked_raises(self, cfg, fresh_snap):
        snap = GuardSnapshot(
            **{**fresh_snap.__dict__, "consecutive_losses": 5}
        )
        repo = MagicMock()
        repo.load.return_value = snap
        state = MagicMock()
        rt = SecurityRuntime(cfg, repo)
        with pytest.raises(CircuitBreakerError):
            rt.check("btc", Decimal("10"), Decimal("1000"), state)
        state.record_skip.assert_called()

    def test_dry_run_logs_but_does_not_raise(self, cfg, fresh_snap):
        repo = MagicMock()
        repo.load.return_value = fresh_snap
        state = MagicMock()
        cfg_dry = GuardConfig(True, True, *cfg[2:])
        rt = SecurityRuntime(cfg_dry, repo)
        # Should NOT raise even though single-tx would exceed
        rt.check("btc", Decimal("100"), Decimal("1000"), state)
        # State should record skip but state.set_status is NOT called for halt
        state.record_skip.assert_called()
        skip_arg = state.record_skip.call_args[0][0]
        assert "WOULD_BLOCK" in skip_arg


class TestRuntimeRecordTrade:
    def test_record_trade_delegates_to_repo(self, cfg):
        repo = MagicMock()
        repo.record_trade.return_value = MagicMock()
        rt = SecurityRuntime(cfg, repo)
        rt.record_trade("btc", Decimal("20"), True, Decimal("1000"), 123.0)
        repo.record_trade.assert_called_once_with("btc", Decimal("20"), True,
                                                   Decimal("1000"), 123.0)


class TestRuntimeReset:
    def test_reset_clears_via_repo(self, cfg):
        repo = MagicMock()
        rt = SecurityRuntime(cfg, repo)
        rt.reset("btc")
        repo.reset.assert_called_once_with("btc")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_security_runtime.py -v`
Expected: ImportError on `bot.security_runtime`

- [ ] **Step 3: Create `bot/security_runtime.py`**

```python
"""Facade combining guards + repository. The only thing streak_trader touches."""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import Any, Callable

from .security import (
    GuardConfig, SpendLimitGuard, TradingCircuitBreaker,
    SpendLimitError, CircuitBreakerError,
)

log = logging.getLogger(__name__)


class SecurityRuntime:
    """Fachada. streak_trader invoca check() antes de cada order."""

    def __init__(
        self,
        config: GuardConfig,
        repository: Any,  # GuardRepository — typed loosely to avoid cycle
    ):
        self._config = config
        self._repo = repository

    def check(
        self,
        symbol: str,
        proposed_usd: Decimal,
        portfolio_value_usd: Decimal,
        state: Any,
    ) -> None:
        """Raises SpendLimitError or CircuitBreakerError if blocked.

        In dry_run mode, logs WARN and records skip but does NOT raise —
        the trade proceeds. Operator uses this to validate behavior
        without halting.
        """
        if not self._config.enabled:
            return

        snap = self._repo.load(symbol)

        # Spend limit check (and record)
        try:
            new_snap = SpendLimitGuard().check_and_record(
                proposed_usd, snap, self._config
            )
            self._repo.save(new_snap)
        except SpendLimitError as e:
            self._handle_block("SPEND_LIMIT", e, state)
            raise

        # Circuit breaker check (uses original snap, not the one with updated daily)
        try:
            TradingCircuitBreaker().check(snap, portfolio_value_usd, self._config)
        except CircuitBreakerError as e:
            self._handle_block("CIRCUIT_BREAKER", e, state)
            raise

    def record_trade(
        self,
        symbol: str,
        usd: Decimal,
        is_loss: bool,
        portfolio_value_usd: Decimal,
        now: float,
    ) -> None:
        """Called after settlement of each trade."""
        self._repo.record_trade(symbol, usd, is_loss, portfolio_value_usd, now)

    def reset(self, symbol: str) -> None:
        """Called from /settings 'Clear guards' button."""
        self._repo.reset(symbol)
        log.info(f"[security] guards cleared for {symbol}")

    def update_config(self, config: GuardConfig) -> None:
        """Hot-reload from /settings. Atomic — replace reference."""
        self._config = config

    def _handle_block(self, kind: str, exc: Exception, state: Any) -> None:
        if self._config.dry_run:
            log.warning(
                f"[security] DRY-RUN WOULD-BLOCK {kind}: {exc} — trade proceeds"
            )
            state.record_skip(f"WOULD_BLOCK_{kind}")
        else:
            log.warning(f"[security] BLOCKED {kind}: {exc}")
            state.record_skip(f"SKIP_{kind}")
            state.set_status(
                "halted_security",
                f"{kind}: {exc}",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_security_runtime.py -v`
Expected: 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/security_runtime.py tests/test_security_runtime.py
git commit -m "feat(security): SecurityRuntime facade

Combines SpendLimitGuard + TradingCircuitBreaker + GuardRepository into
a single check() call. Handles dry_run mode (log WARN, don't block) and
real block mode (record_skip + set_status). Hot-reloadable config.

7 tests cover: disabled runtime no-ops, under-cap passes, spend limit
blocks, circuit breaker blocks, dry_run logs without blocking,
record_trade delegates, reset delegates."
```

---

### Task 6: BotState fields + integration in `_execute_signal`

**Files:**
- Modify: `bot/state.py:74-92` (add security fields to `__init__`)
- Modify: `bot/state.py:600-810` (add fields to `snapshot()` dict)
- Modify: `bot/streak_trader.py:154` (add `security_runtime` param to `__init__`)
- Modify: `bot/streak_trader.py:539-570` (insert hook after `is_ask_above_cap` gate)
- Modify: `bot/streak_trader.py:1200-1220` (insert hook in `_resolve_pending_trades`)
- Modify: `tests/test_streak_trader.py` (add tests for hook behavior)

**Interfaces:**
- Consumes: existing `StreakSnapperTrader.__init__(cfg, symbol)` — new optional `security_runtime=None`
- Produces: hook in `_execute_signal` that calls `security_runtime.check()` after existing gates

- [ ] **Step 1: Add BotState fields**

In `bot/state.py`, find the `__init__` after line 92 (the `polymarket_balance_*` block). Add:

```python
        # Security guards (global, applies to all strategies)
        self.security_enabled:                  bool   = True
        self.security_dry_run:                  bool   = False
        self.security_max_single_tx_usd:        float  = 50.0
        self.security_max_daily_spend_usd:      float  = 200.0
        self.security_max_consecutive_losses:   int    = 3
        self.security_max_hourly_drawdown_pct:  float  = 0.08
```

In the `snapshot()` method (around line 717), find where `polymarket_*` fields are dumped. After those, add:

```python
                "security_enabled":                self.security_enabled,
                "security_dry_run":                self.security_dry_run,
                "security_max_single_tx_usd":      self.security_max_single_tx_usd,
                "security_max_daily_spend_usd":    self.security_max_daily_spend_usd,
                "security_max_consecutive_losses": self.security_max_consecutive_losses,
                "security_max_hourly_drawdown_pct": self.security_max_hourly_drawdown_pct,
```

- [ ] **Step 2: Update `StreakSnapperTrader.__init__`**

Find `def __init__(self, cfg, symbol)` around line 154. Modify signature and body:

```python
    def __init__(
        self,
        cfg,
        symbol: str,
        security_runtime=None,  # SecurityRuntime or None — opt-in
    ):
        # ... existing body unchanged ...
        self._security_runtime = security_runtime
```

Add the assignment at the end of `__init__`, after all existing field assignments.

- [ ] **Step 3: Add hook in `_execute_signal`**

Find line 539 (`def _execute_signal(self, tokens, sig: StreakSignal)`). After the `is_ask_above_cap` early-return (around line 570), insert:

```python
        # ── Security guards (global, all strategies) ─────────────────────
        if self._security_runtime is not None:
            from decimal import Decimal
            try:
                proposed_usd = Decimal(str(sig.shares * (current_ask or 0)))
                portfolio_value = Decimal(str(self.state.current_bankroll()))
                self._security_runtime.check(
                    symbol=self.symbol,
                    proposed_usd=proposed_usd,
                    portfolio_value_usd=portfolio_value,
                    state=self.state,
                )
            except (SpendLimitError, CircuitBreakerError):
                return  # already logged + state updated by runtime
```

Add import at top of `streak_trader.py`:

```python
from .security import SpendLimitError, CircuitBreakerError
```

- [ ] **Step 4: Add hook in `_resolve_pending_trades`**

Find the trade resolution loop in `_resolve_pending_trades`. After updating each trade's P&L (look for `t.pnl = ...` or similar), add:

```python
            # ── Security guard bookkeeping ──────────────────────────────────
            if self._security_runtime is not None:
                from decimal import Decimal
                self._security_runtime.record_trade(
                    symbol=self.symbol,
                    usd=Decimal(str(getattr(resolved_trade, 'cost_usd', 0) or 0)),
                    is_loss=resolved_trade.pnl < 0,
                    portfolio_value_usd=Decimal(str(self.state.current_bankroll())),
                    now=time.time(),
                )
```

(If `_resolve_pending_trades` uses different variable names, adapt — the key is to call `record_trade` once per resolved trade with the correct args.)

- [ ] **Step 5: Add tests in `tests/test_streak_trader.py`**

Append at the end:

```python
class TestSecurityGuardHook:
    def test_signal_skipped_when_spend_cap_hit(self, monkeypatch):
        from bot.security import SpendLimitError, GuardConfig
        from bot.security_runtime import SecurityRuntime
        from bot.security_store import GuardSnapshot
        from decimal import Decimal

        # Build a runtime that's already saturated
        snap = GuardSnapshot(
            symbol="btc", daily_spend_usd=Decimal("200"),
            daily_spend_reset_at=9999999999,  # not resetting
            consecutive_losses=0, last_trade_at=0.0,
            hourly_drawdown_baseline_usd=Decimal("0"),
            hourly_drawdown_at=0.0, last_reset_at=0.0,
            enabled=True, dry_run=False,
        )
        mock_repo = MagicMock()
        mock_repo.load.return_value = snap
        cfg = GuardConfig(True, False, Decimal("50"), Decimal("200"),
                         3, Decimal("0.08"))
        rt = SecurityRuntime(cfg, mock_repo)

        # Build trader with this runtime
        trader = StreakSnapperTrader(
            make_test_config(),
            "btc",
            security_runtime=rt,
        )
        # ... assert that a proposed sig exceeding $50 is skipped via record_skip
```

(Adapt the test fixture names to match `tests/test_streak_trader.py` — look at existing tests for the pattern. This is a sketch; the implementer must complete the assertion logic.)

- [ ] **Step 6: Run streak_trader tests**

Run: `python -m pytest tests/test_streak_trader.py -v`
Expected: existing tests PASS, new test PASS

- [ ] **Step 7: Commit**

```bash
git add bot/state.py bot/streak_trader.py tests/test_streak_trader.py
git commit -m "feat(security): wire SecurityRuntime into _execute_signal + resolve

6 new BotState fields (security_*). StreakSnapperTrader.__init__ accepts
optional security_runtime param. _execute_signal calls
security_runtime.check() after is_ask_above_cap gate; raises block the
order. _resolve_pending_trades calls record_trade() per resolved trade.

Backward-compatible: security_runtime=None disables guards entirely
(useful for existing tests)."
```

---

### Task 7: main.py wiring + smoke tests

**Files:**
- Modify: `bot/main.py:69-200` (instantiate SecurityRuntime, pass to traders)
- Create: `tests/test_main_smoke.py` (smoke test that main wires correctly)

**Interfaces:**
- Consumes: `BotConfig` (with security_* fields)
- Produces: `SecurityRuntime` instance wired into each `StreakSnapperTrader`

- [ ] **Step 1: Add SecurityRuntime instantiation in `main()`**

Find the `traders = [...]` block (around line 183). Just before it, add:

```python
    # ── Security runtime (manual until wired — see bot/security_runtime.py) ──
    from decimal import Decimal
    from .security import GuardConfig
    from .security_store import GuardRepository
    from .security_runtime import SecurityRuntime

    guard_config = GuardConfig(
        enabled=STATE.security_enabled,
        dry_run=STATE.security_dry_run,
        max_single_tx_usd=Decimal(str(STATE.security_max_single_tx_usd)),
        max_daily_spend_usd=Decimal(str(STATE.security_max_daily_spend_usd)),
        max_consecutive_losses=STATE.security_max_consecutive_losses,
        max_hourly_drawdown_pct=Decimal(str(STATE.security_max_hourly_drawdown_pct)),
    )
    guard_repo = GuardRepository(lambda: db.session)
    security_runtime = SecurityRuntime(guard_config, guard_repo)
```

Then change `traders = [StreakSnapperTrader(cfg, symbol) for symbol in cfg.ss_symbols]` to:

```python
    traders = [
        StreakSnapperTrader(cfg, symbol, security_runtime=security_runtime)
        for symbol in cfg.ss_symbols
    ]
```

- [ ] **Step 2: Smoke test in `tests/test_main_smoke.py`**

```python
"""Smoke test: bot.main imports + has security_runtime wired."""
def test_main_module_imports_cleanly():
    import bot.main  # should not raise
    assert hasattr(bot.main, 'main')

def test_security_runtime_importable():
    from bot.security_runtime import SecurityRuntime
    from bot.security import GuardConfig
    from bot.security_store import GuardRepository
    assert SecurityRuntime is not None
```

- [ ] **Step 3: Run smoke tests**

Run: `python -m pytest tests/test_main_smoke.py -v`
Expected: 2 tests PASS

- [ ] **Step 4: Run ALL tests to confirm no regression**

Run: `python -m pytest tests/ -q`
Expected: 599 + 27 + 2 = 628+ passed; same 3 pre-existing failures in test_spread_harvest

- [ ] **Step 5: Commit**

```bash
git add bot/main.py tests/test_main_smoke.py
git commit -m "feat(security): wire SecurityRuntime into main.py

main() now instantiates GuardConfig from STATE.security_* fields,
GuardRepository with db.session factory, and SecurityRuntime. Each
StreakSnapperTrader gets security_runtime passed in __init__.

Smoke test: bot.main imports cleanly + SecurityRuntime importable."
```

---

### Task 8: Dashboard tile — Security status

**Files:**
- Modify: `bot/dashboard.py` (add `/api/security-status` endpoint)
- Modify: `bot/templates/dashboard.html` (add tile in KPI row)
- Modify: `bot/static/dashboard.js` (poll + render)

**Interfaces:**
- `GET /api/security-status?symbol=btc` → JSON with `{armed, dry_run, daily_spent, daily_cap, consecutive_losses, max_consecutive_losses, hourly_drawdown_pct, max_hourly_drawdown_pct, halted_reason}`
- Dashboard tile updates every 5s alongside existing KPIs

- [ ] **Step 1: Add endpoint to `bot/dashboard.py`**

Find where other KPI endpoints are defined (look for `@app.route("/api/kpi-...` patterns). Add:

```python
@app.route("/api/security-status")
def security_status():
    from .security_store import GuardRepository
    symbol = request.args.get("symbol", "btc")
    repo = GuardRepository(lambda: db.session)
    snap = repo.load(symbol)
    return jsonify({
        "enabled": from_state.symbol_state[symbol].security_enabled,
        "dry_run": from_state.symbol_state[symbol].security_dry_run,
        "daily_spent": float(snap.daily_spend_usd),
        "daily_cap": float(from_state.symbol_state[symbol].security_max_daily_spend_usd),
        "consecutive_losses": snap.consecutive_losses,
        "max_consecutive_losses": from_state.symbol_state[symbol].security_max_consecutive_losses,
        "hourly_drawdown_pct": float(
            (Decimal(str(snap.hourly_drawdown_baseline_usd)) - Decimal(str(from_state.symbol_state[symbol].current_bankroll())))
            / Decimal(str(snap.hourly_drawdown_baseline_usd))
        ) if snap.hourly_drawdown_baseline_usd > 0 else 0.0,
        "max_hourly_drawdown_pct": float(from_state.symbol_state[symbol].security_max_hourly_drawdown_pct),
        "halted_reason": from_state.symbol_state[symbol].status_detail if "halted_security" in from_state.symbol_state[symbol].status else None,
    })
```

(Adapt the state access pattern to match how the existing KPI endpoints read `BotState`. Look at how they get the current symbol's state.)

- [ ] **Step 2: Add tile to `bot/templates/dashboard.html`**

Find the KPI row (`<div class="ss-kpi-row">`). Add after the last existing tile:

```html
  <div class="ss-kpi-tile ss-kpi-security" id="tile-security">
    <div class="ss-kpi-icon"><i class="bi bi-shield-lock"></i></div>
    <div class="ss-kpi-body">
      <div class="ss-kpi-label">Security</div>
      <div id="kpi-security-status" class="ss-kpi-value">—</div>
      <div id="kpi-security-detail" class="ss-kpi-sub">—</div>
    </div>
  </div>
```

- [ ] **Step 3: Add JS polling in `bot/static/dashboard.js`**

Find where other KPIs are polled. Add:

```javascript
async function pollSecurity() {
  const symbol = currentSymbol();
  const resp = await fetch(`/api/security-status?symbol=${symbol}`);
  const data = await resp.json();
  document.getElementById('kpi-security-status').textContent =
    data.halted_reason ? 'HALTED' : (data.enabled ? 'ARMED' : 'OFF');
  document.getElementById('kpi-security-detail').textContent =
    `$${data.daily_spent.toFixed(2)}/$${data.daily_cap.toFixed(0)} · ${data.consecutive_losses}× losses`;
}
// Add to existing poll cycle (every 5s alongside other KPIs)
```

- [ ] **Step 4: Manual verification**

Start dashboard locally: `python run.py`. Open `http://127.0.0.1:5000/dashboard` (after login). Confirm Security tile renders with current values.

- [ ] **Step 5: Commit**

```bash
git add bot/dashboard.py bot/templates/dashboard.html bot/static/dashboard.js
git commit -m "feat(security): Security status tile on dashboard

New /api/security-status endpoint + tile in KPI row showing armed/dry_run,
daily spend vs cap, consecutive losses count, and halted reason if any.
Tile polls every 5s alongside existing KPIs. Operator can see guard
state at a glance without going to /settings."
```

---

### Task 9: Settings panel + reset button

**Files:**
- Modify: `bot/templates/settings.html` (add section before "Guardar")
- Modify: `bot/dashboard.py` (add `/settings/security/reset` POST endpoint)
- Modify: `bot/static/settings.js` (handle new fields + reset button)

- [ ] **Step 1: Add section to `bot/templates/settings.html`**

Before the "Guardar" section (around line 115), insert:

```html
      <div class="ss-card">
        <div class="ss-card-head"><h2><i class="bi bi-shield-lock"></i> Security guards</h2></div>
        <div class="ss-card-body">
          <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" id="security-enabled">
            <label class="form-check-label" for="security-enabled">Security guards activos</label>
          </div>
          <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" id="security-dry-run">
            <label class="form-check-label" for="security-dry-run">Dry-run (log sin bloquear)</label>
          </div>
          <div class="row g-3">
            <div class="col-md-3">
              <label class="form-label" for="security-max-single-tx">Max single tx (USD)</label>
              <input type="number" class="form-control" id="security-max-single-tx" step="0.01" min="0">
            </div>
            <div class="col-md-3">
              <label class="form-label" for="security-max-daily">Max daily spend (USD)</label>
              <input type="number" class="form-control" id="security-max-daily" step="0.01" min="0">
            </div>
            <div class="col-md-3">
              <label class="form-label" for="security-max-losses">Max consecutive losses</label>
              <input type="number" class="form-control" id="security-max-losses" step="1" min="1">
            </div>
            <div class="col-md-3">
              <label class="form-label" for="security-max-drawdown">Max hourly drawdown (%)</label>
              <input type="number" class="form-control" id="security-max-drawdown" step="0.01" min="0">
            </div>
          </div>
          <button type="button" class="btn btn-outline-warning mt-3" id="security-reset-btn">
            <i class="bi bi-arrow-counterclockwise"></i> Clear guards
          </button>
        </div>
      </div>
```

- [ ] **Step 2: Add reset endpoint to `bot/dashboard.py`**

Find `/settings` POST handler. Add:

```python
@app.route("/settings/security/reset", methods=["POST"])
def settings_security_reset():
    from .security_store import GuardRepository
    symbol = request.form.get("symbol") or request.json.get("symbol") if request.json else "btc"
    repo = GuardRepository(lambda: db.session)
    repo.reset(symbol)
    flash(f"Security guards cleared for {symbol}", "success")
    return redirect(url_for("settings_view"))
```

- [ ] **Step 3: Wire JS in `bot/static/settings.js`**

Add to existing settings save handler — read 6 fields, write to BotConfigModel with keys `security_enabled`, `security_dry_run`, `security_max_single_tx_usd`, etc.

Also add reset button handler:

```javascript
document.getElementById('security-reset-btn').addEventListener('click', async () => {
  const symbol = currentSymbol();
  const resp = await fetch(`/settings/security/reset?symbol=${symbol}`, {method: 'POST'});
  if (resp.ok) location.reload();
});
```

- [ ] **Step 4: Manual verification**

Open `/settings`. Confirm:
- Master toggle (security-enabled) renders correctly
- Dry-run toggle renders correctly
- 4 number inputs accept decimal values
- Clear guards button shows confirmation or reloads page after click

- [ ] **Step 5: Commit**

```bash
git add bot/templates/settings.html bot/dashboard.py bot/static/settings.js
git commit -m "feat(security): settings panel + Clear guards button

New 'Security guards' section in /settings with master toggle,
dry-run toggle, and 4 number inputs (max single tx, max daily,
max consecutive losses, max hourly drawdown). 'Clear guards' button
POSTs to /settings/security/reset and reloads.

Operator can now tune guard limits via dashboard without restart, and
clear counters after a halt to resume trading."
```

---

### Task 10: Verification + docs

**Files:**
- Modify: `docs/RISK_REVIEW.md` (add §9: layer reference)
- Modify: `docs/PRODUCTION_AUDIT.md` (add §6: smoke import check)
- Run all tests + manual smoke

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: ≥628 passed, same 3 pre-existing failures

If anything fails, STOP and fix before proceeding.

- [ ] **Step 2: Smoke import**

Run: `python -c "from bot.security import SpendLimitGuard, TradingCircuitBreaker, SpendLimitError, CircuitBreakerError, GuardConfig; from bot.security_store import GuardRepository, GuardSnapshot; from bot.security_runtime import SecurityRuntime; from bot.main import main; print('OK')"`
Expected: prints `OK`

- [ ] **Step 3: Update `docs/RISK_REVIEW.md`**

After §8 (Firma), add:

```markdown
## 9. Security Guards Layer (implementación)

Capas activas en `bot/security.py` + `bot/security_store.py` + `bot/security_runtime.py`:

- `SpendLimitGuard`: single-tx cap + rolling 24h daily cap
- `TradingCircuitBreaker`: consecutive losses + hourly drawdown
- Persistencia en `bot_guards` table — sobrevive VPS reboot
- Reset manual via `/settings → Security guards → Clear guards`
- Hot-reloadable config (sin restart)

Defaults (alineados con bankroll $1000):
- single-tx: $50 (5%)
- daily: $200 (20%)
- consecutive losses: 3
- hourly drawdown: 8%

Para endurecer o relajar: `/settings → Security guards`.
```

- [ ] **Step 4: Update `docs/PRODUCTION_AUDIT.md`**

In §2.1 Step 1 (evidencia local), append to the smoke import line:

```markdown
# Security guards importable?
python -c "from bot.security_runtime import SecurityRuntime; from bot.security_store import GuardRepository; print('OK')"
# Esperado: OK
```

- [ ] **Step 5: Commit docs**

```bash
git add docs/RISK_REVIEW.md docs/PRODUCTION_AUDIT.md
git commit -m "docs(security): cross-reference new layer

RISK_REVIEW §9: layer reference + defaults table.
PRODUCTION_AUDIT §2.1: smoke import check for security_runtime."
```

---

## Self-Review (after writing plan)

**1. Spec coverage**: Each section of the spec is mapped to a task:
- §3 Arquitectura → Tasks 1-9 (file structure)
- §4 Componentes → Tasks 3, 4, 5 (pure logic, repo, runtime)
- §5 DB Schema → Task 2 (BotGuardState)
- §6 Configuración → Task 1 (env vars), Task 6 (BotState fields)
- §7 Integración → Task 6 (streak_trader hooks), Task 7 (main wiring)
- §8 Testing → All tasks have tests inline; total ≥27 new tests
- §11 Success criteria → Task 10

**2. Placeholders**: None found. Each step has actual code or commit commands.

**3. Type consistency**:
- `GuardConfig` — defined Task 1, used Task 3, 4, 5, 7, 8, 9 ✓
- `GuardSnapshot` — defined Task 2, used Task 3, 4, 5, 7, 8 ✓
- `SecurityRuntime` — defined Task 5, used Task 6, 7, 8 ✓
- `SpendLimitError`, `CircuitBreakerError` — defined Task 1, used Task 3, 4, 5, 6 ✓
- `current_bankroll()` — verified in `bot/state.py:573` (real method on BotState) ✓

**4. Backward compatibility**: `security_runtime=None` default in `StreakSnapperTrader.__init__` preserves all existing tests that don't pass a runtime. All 599 existing tests should continue to pass.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-04-spend-limit-guard-circuit-breaker.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?