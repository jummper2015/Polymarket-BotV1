# 🛡️ Spend Limit Guard + Trading Circuit Breaker — Design Spec

> **Status:** Approved (brainstorming 2026-10-04)
> **Origin:** Adaptado de la skill `llm-trading-agent-security` de ECC
> **Owner:** Operador
> **Critical:** Esta capa debe estar implementada **antes** del flip
> `TRADING_MODE=paper → real`. Ver `docs/RISK_REVIEW.md` §8 firma gate.

---

## 1. Contexto y motivación

El bot ME$IRVE opera ventanas up/down 5-min de BTC en Polymarket con
bankroll inicial de **$1000** (`bot/state.py:starting_bankroll`). Las 4
estrategias activas (`box_builder`, `coin_flip_dog`, `temporal_arb`,
`near_res`) ejecutan trades vía `bot/streak_trader.py:_execute_signal`.

**Hoy** existen controles de riesgo **por estrategia**:
- `temporal_arb.py` tiene `_execute_stop_loss()` con 3 disparadores
  (TIME_THRESHOLD, TRAILING_STOP, CATASTROPHIC_LOSS) — solo para TA
- Las otras estrategias no tienen stop-loss explícito
- **No existe** un límite de gasto por transacción o diario
- **No existe** circuit breaker global por pérdidas consecutivas o drawdown

**Hoy + 1 falla operativa** puede amplificar la pérdida:
- Loop bug en una estrategia → N orders enviados en segundos
- Latency spike → slippage masivo → pérdida mayor al `ta_stop_loss_threshold`
- Trend persistente en contra → múltiples stops disparados → pérdida agregada > bankroll

**Referencia externa**: ECC's `llm-trading-agent-security` prescribe capas
independientes de defensa: spend limits, circuit breakers, pre-send simulation.
Esta spec implementa las primeras dos.

---

## 2. Decisiones de brainstorming (resumen)

| Pregunta | Decisión |
|---|---|
| Alcance | **Global**, capa ortogonal sobre todas las estrategias |
| Comportamiento al disparar | **Skip + log + halt-trading-hasta-reset manual** via `/settings` |
| Persistencia | **Rolling 24h + DB persistente** (nueva tabla `bot_guards`) |
| Defaults | **Conservador**: $50/tx, $200/día, 3 losses, 8% drawdown/hora |
| Mode | **Activo por default**, `security_dry_run` opcional para validar sin bloquear |
| Arquitectura | **3 archivos** — `bot/security.py` + `bot/security_store.py` + `bot/security_runtime.py` |

---

## 3. Arquitectura

### 3.1 Archivos nuevos

```
bot/security.py          # SpendLimitGuard, TradingCircuitBreaker (lógica pura)
bot/security_store.py    # GuardRepository, GuardSnapshot (DB I/O)
bot/security_runtime.py  # SecurityRuntime (combina a guarded + repo, fachada)
```

### 3.2 Archivos modificados (mínima invasión)

```
bot/db.py                # + clase BotGuardState (tabla `bot_guards`)
bot/state.py             # + 6 campos security_* + security_enabled + security_dry_run
bot/streak_trader.py     # + 4 líneas en _execute_signal (hook antes del buy)
                         # + 3 líneas en _resolve_pending_trades (post-settlement hook)
bot/main.py              # + 12 líneas (crear SecurityRuntime + pasarlo a traders)
bot/dashboard.py         # + 1 tile "Security status" en /dashboard
bot/templates/settings.html  # + 1 sección "Security guards" en /settings
bot/config.py            # + 6 env vars SPEND_*, SECURITY_*
```

### 3.3 Tests nuevos

```
tests/test_security.py          # ~12 tests, guards puros (sin DB)
tests/test_security_store.py    # ~8 tests, repo (SQLite in-memory)
tests/test_security_runtime.py  # ~7 tests, integration (con mocks)
```

### 3.4 Diagrama de flujo (cada trade)

```
_execute_signal(tokens, sig)
  │
  ├── gates existentes (ask-above-cap, ask-valid, etc.)
  │
  ├── NEW: security_runtime.check(symbol, proposed_usd, portfolio_value, state)
  │     │
  │     ├── if not security_enabled: return  # skip guards
  │     │
  │     ├── snap = repo.load(symbol)
  │     │
  │     ├── SpendLimitGuard.check_and_record(proposed_usd, snap, config)
  │     │     ├── OK → new_snap, repo.save(new_snap)
  │     │     └── raises SpendLimitError
  │     │           ├── if dry_run: log WARN, record_skip("WOULD_BLOCK_*")
  │     │           └── else:     state.record_skip("SKIP_SPEND_LIMIT")
  │     │                            state.set_status("halted_security", ...)
  │     │                            → return (no order placed)
  │     │
  │     └── TradingCircuitBreaker.check(snap, portfolio_value, config)
  │           ├── OK → continue
  │           └── raises CircuitBreakerError (same dry_run/else pattern)
  │
  ├── existing order placement (unchanged)
```

### 3.5 Diagrama de flujo (post-settlement)

```
_resolve_pending_trades(...)
  │
  ├── existing: update trade.status, pnl
  │
  ├── NEW: security_runtime.record_trade(symbol, trade, portfolio_value)
  │     └── repo.record_trade() — bookkeeping:
  │           ├── daily_spend_usd += trade.cost_usd
  │           ├── if now > daily_spend_reset_at + 86400: reset
  │           ├── if trade.pnl < 0: consecutive_losses += 1
  │           ├── else: consecutive_losses = 0
  │           ├── if first trade of hour: set hourly baseline
  │           └── save snapshot
```

---

## 4. Componentes en detalle

### 4.1 `bot/security.py` — lógica pura

```python
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class GuardConfig:
    enabled: bool
    dry_run: bool
    max_single_tx_usd: Decimal
    max_daily_spend_usd: Decimal
    max_consecutive_losses: int
    max_hourly_drawdown_pct: Decimal


class SpendLimitError(Exception):
    """Single-tx o daily-cap excedido. kind in {'single_tx', 'daily'}."""
    def __init__(self, kind: str, attempted_usd: Decimal, cap_usd: Decimal,
                 spent_usd: Decimal, msg: str):
        super().__init__(msg)
        self.kind = kind
        self.attempted_usd = attempted_usd
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd


class CircuitBreakerError(Exception):
    """Consecutive losses o hourly drawdown excedido."""
    def __init__(self, kind: str, msg: str, current: int | Decimal, cap: int | Decimal):
        super().__init__(msg)
        self.kind = kind
        self.current = current
        self.cap = cap


class SpendLimitGuard:
    """Stateless. Recibe snapshot inmutable, retorna snapshot nuevo."""

    def check_and_record(
        self,
        proposed_usd: Decimal,
        snapshot: "GuardSnapshot",
        config: GuardConfig,
    ) -> "GuardSnapshot":
        # 1. Single-tx check
        if proposed_usd > config.max_single_tx_usd:
            raise SpendLimitError(
                kind="single_tx",
                attempted_usd=proposed_usd,
                cap_usd=config.max_single_tx_usd,
                spent_usd=snapshot.daily_spend_usd,
                msg=f"single tx ${proposed_usd} > cap ${config.max_single_tx_usd}",
            )
        # 2. Daily check (with rolling 24h logic — handled by repo before passing in)
        new_daily = snapshot.daily_spend_usd + proposed_usd
        if new_daily > config.max_daily_spend_usd:
            raise SpendLimitError(
                kind="daily",
                attempted_usd=proposed_usd,
                cap_usd=config.max_daily_spend_usd,
                spent_usd=snapshot.daily_spend_usd,
                msg=f"daily ${snapshot.daily_spend_usd} + ${proposed_usd} > ${config.max_daily_spend_usd}",
            )
        # 3. Return updated snapshot
        return replace(snapshot, daily_spend_usd=new_daily)


class TradingCircuitBreaker:
    """Stateless. Verifica snapshot contra portfolio actual."""

    def check(
        self,
        snapshot: "GuardSnapshot",
        portfolio_value_usd: Decimal,
        config: GuardConfig,
    ) -> None:
        # 1. Consecutive losses
        if snapshot.consecutive_losses >= config.max_consecutive_losses:
            raise CircuitBreakerError(
                kind="consecutive_losses",
                msg=f"{snapshot.consecutive_losses} consecutive losses ≥ {config.max_consecutive_losses}",
                current=snapshot.consecutive_losses,
                cap=config.max_consecutive_losses,
            )
        # 2. Hourly drawdown (only if we have one)
        if snapshot.hourly_drawdown_baseline_usd > 0:
            drawdown = (portfolio_value_usd - snapshot.hourly_drawdown_baseline_usd) / snapshot.hourly_drawdown_baseline_usd
            if drawdown < -config.max_hourly_drawdown_pct:
                raise CircuitBreakerError(
                    kind="hourly_drawdown",
                    msg=f"hourly drawdown {drawdown:.1%} < -{config.max_hourly_drawdown_pct:.0%}",
                    current=drawdown,
                    cap=-config.max_hourly_drawdown_pct,
                )
```

### 4.2 `bot/security_store.py` — DB I/O

```python
from dataclasses import dataclass
from decimal import Decimal
import time
from sqlalchemy.orm import sessionmaker

from .db import BotGuardState


@dataclass(frozen=True)
class GuardSnapshot:
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
    """Una fila por symbol. Survive VPS reboot."""

    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    def load(self, symbol: str) -> GuardSnapshot:
        with self._session_factory() as s:
            row = s.get(BotGuardState, symbol)
            if row is None:
                return self._fresh_snapshot(symbol)
            return row.to_snapshot()

    def save(self, snap: GuardSnapshot) -> None:
        with self._session_factory() as s:
            row = BotGuardState.from_snapshot(snap)
            s.merge(row)
            s.commit()

    def reset(self, symbol: str) -> None:
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

        # 1. Daily cap rolling reset (24h)
        if snap.daily_spend_reset_at > 0 and (now - snap.daily_spend_reset_at) > 86400:
            snap = replace(snap, daily_spend_usd=Decimal("0"), daily_spend_reset_at=now)
        if snap.daily_spend_reset_at == 0:
            snap = replace(snap, daily_spend_reset_at=now)

        # 2. Increment daily spend
        snap = replace(snap, daily_spend_usd=snap.daily_spend_usd + usd)

        # 3. Consecutive losses
        new_consec = snap.consecutive_losses + 1 if is_loss else 0
        snap = replace(snap, consecutive_losses=new_consec)

        # 4. Hourly baseline — initialize if first trade, roll if >1h
        HOUR = 3600.0
        if snap.hourly_drawdown_at == 0:
            snap = replace(snap, hourly_drawdown_baseline_usd=portfolio_value_usd,
                          hourly_drawdown_at=now)
        elif (now - snap.hourly_drawdown_at) > HOUR:
            snap = replace(snap, hourly_drawdown_baseline_usd=portfolio_value_usd,
                          hourly_drawdown_at=now)

        snap = replace(snap, last_trade_at=now)
        self.save(snap)
        return snap

    def _fresh_snapshot(self, symbol: str) -> GuardSnapshot:
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

### 4.3 `bot/security_runtime.py` — fachada

```python
from dataclasses import replace
from decimal import Decimal
import logging

from .security import (
    GuardConfig, SpendLimitGuard, TradingCircuitBreaker,
    SpendLimitError, CircuitBreakerError,
)
from .security_store import GuardRepository, GuardSnapshot

log = logging.getLogger(__name__)


class SecurityRuntime:
    """Fachada que streak_trader invoca. Combina guards + repo."""

    def __init__(
        self,
        config: GuardConfig,
        repository: GuardRepository,
    ):
        self._config = config
        self._repo = repository

    def check(
        self,
        symbol: str,
        proposed_usd: Decimal,
        portfolio_value_usd: Decimal,
        state: Any,  # BotState — typed as Any para evitar ciclo de imports
    ) -> None:
        """Raises SpendLimitError o CircuitBreakerError si bloquea."""
        if not self._config.enabled:
            return

        snap = self._repo.load(symbol)

        # Spend limit
        try:
            new_snap = SpendLimitGuard().check_and_record(
                proposed_usd, snap, self._config
            )
            self._repo.save(new_snap)
        except SpendLimitError as e:
            self._handle_block("SPEND_LIMIT", e, snap, state)
            raise

        # Circuit breaker (uses ORIGINAL snap, before incrementing spend)
        try:
            TradingCircuitBreaker().check(snap, portfolio_value_usd, self._config)
        except CircuitBreakerError as e:
            self._handle_block("CIRCUIT_BREAKER", e, snap, state)
            raise

    def record_trade(
        self,
        symbol: str,
        usd: Decimal,
        is_loss: bool,
        portfolio_value_usd: Decimal,
        now: float,
    ) -> None:
        self._repo.record_trade(symbol, usd, is_loss, portfolio_value_usd, now)

    def reset(self, symbol: str) -> None:
        self._repo.reset(symbol)
        log.info(f"[security] guards cleared for {symbol}")

    def update_config(self, config: GuardConfig) -> None:
        """Hot-reload desde /settings."""
        self._config = config

    def _handle_block(self, kind: str, exc: Exception, snap: GuardSnapshot, state: Any):
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

---

## 5. DB Schema

Tabla nueva en `bot/db.py`:

```python
class BotGuardState(Base):
    __tablename__ = "bot_guards"

    symbol:             Mapped[str]   = mapped_column(String(16), primary_key=True)
    daily_spend_usd:    Mapped[float] = mapped_column(Float, default=0.0)
    daily_spend_reset_at: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int]   = mapped_column(Integer, default=0)
    last_trade_at:      Mapped[float] = mapped_column(Float, default=0.0)
    hourly_drawdown_baseline_usd: Mapped[float] = mapped_column(Float, default=0.0)
    hourly_drawdown_at: Mapped[float] = mapped_column(Float, default=0.0)
    last_reset_at:      Mapped[float] = mapped_column(Float, default=0.0)
    enabled:            Mapped[bool]  = mapped_column(Boolean, default=True)
    dry_run:            Mapped[bool]  = mapped_column(Boolean, default=False)

    def to_snapshot(self) -> GuardSnapshot: ...
    @classmethod
    def from_snapshot(cls, snap: GuardSnapshot) -> "BotGuardState": ...
```

**Migración**: usar `Base.metadata.create_all(engine)` en `init_db()` (ya
existe). Idempotente — no requiere ALTER TABLE.

---

## 6. Configuración (env vars + bot_config)

**Env vars** en `bot/config.py`:
```python
SECURITY_ENABLED                  = os.getenv("SECURITY_ENABLED", "true").lower() == "true"
SECURITY_DRY_RUN                  = os.getenv("SECURITY_DRY_RUN", "false").lower() == "true"
SECURITY_MAX_SINGLE_TX_USD        = float(os.getenv("SECURITY_MAX_SINGLE_TX_USD", "50.0"))
SECURITY_MAX_DAILY_SPEND_USD      = float(os.getenv("SECURITY_MAX_DAILY_SPEND_USD", "200.0"))
SECURITY_MAX_CONSECUTIVE_LOSSES   = int(os.getenv("SECURITY_MAX_CONSECUTIVE_LOSSES", "3"))
SECURITY_MAX_HOURLY_DRAWDOWN_PCT  = float(os.getenv("SECURITY_MAX_HOURLY_DRAWDOWN_PCT", "0.08"))
```

**BotState fields** (defaults via config):
```python
self.security_enabled:                  bool   = True
self.security_dry_run:                  bool   = False
self.security_max_single_tx_usd:        float  = 50.0
self.security_max_daily_spend_usd:      float  = 200.0
self.security_max_consecutive_losses:   int    = 3
self.security_max_hourly_drawdown_pct:  float  = 0.08
```

Persistidos en `bot_config` table (vía `coerce_overrides`), editables en
`/settings`.

---

## 7. Integración mínima

### 7.1 `bot/main.py:main()`

Después de `init_db()`, antes de crear threads:

```python
from .security import GuardConfig
from .security_store import GuardRepository
from .security_runtime import SecurityRuntime

guard_config = GuardConfig(
    enabled=state.security_enabled,
    dry_run=state.security_dry_run,
    max_single_tx_usd=Decimal(str(state.security_max_single_tx_usd)),
    max_daily_spend_usd=Decimal(str(state.security_max_daily_spend_usd)),
    max_consecutive_losses=state.security_max_consecutive_losses,
    max_hourly_drawdown_pct=Decimal(str(state.security_max_hourly_drawdown_pct)),
)
guard_repo = GuardRepository(lambda: db.session)
security_runtime = SecurityRuntime(guard_config, guard_repo)
```

Y modificar `StreakSnapperTrader(cfg, symbol)` para aceptar
`security_runtime=security_runtime`.

### 7.2 `bot/streak_trader.py:_execute_signal`

Insertar después de `if is_ask_above_cap(...): return`:

```python
# ── Security guards ─────────────────────────────────────────────────
if self._security_runtime:
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
        return  # ya loggeado y halt state actualizado por runtime
```

### 7.3 `bot/streak_trader.py:_resolve_pending_trades`

Después de actualizar `trade.status` y P&L:

```python
# ── Security guard bookkeeping ──────────────────────────────────────
if hasattr(self, "_security_runtime") and self._security_runtime:
    self._security_runtime.record_trade(
        symbol=self.symbol,
        usd=Decimal(str(resolved_trade.cost_usd or 0)),
        is_loss=resolved_trade.pnl < 0,
        portfolio_value_usd=Decimal(str(self.state.current_bankroll())),
        now=time.time(),
    )
```

### 7.4 UI

**`/dashboard` tile** "Security status":
```
Security status: ● ARMED (dry-run)
  Daily spend: $34.50 / $200.00
  Consec losses: 0 / 3
  Hourly drawdown: -1.2% / -8.0%
```

Si bloqueado:
```
Security status: ⛔ HALTED — SPEND_LIMIT
  Daily spend: $200.00 / $200.00  (CAP REACHED)
  [Clear guards]  → POST /settings/security/reset
```

**`/settings` sección** "Security guards":
- Master toggle: `security_enabled` (default ON)
- Dry-run toggle: `security_dry_run` (default OFF)
- 4 number inputs: max_single_tx_usd, max_daily_spend_usd, max_consecutive_losses, max_hourly_drawdown_pct
- Botón "Clear guards for <symbol>"

---

## 8. Testing strategy

**`tests/test_security.py`** — 12+ tests, lógica pura:
- SpendLimitGuard: single_tx under/at/over cap, daily under/over cap, daily reset after 24h
- TradingCircuitBreaker: consecutive losses under/at/over, win resets, hourly drawdown under/over, baseline initialization

**`tests/test_security_store.py`** — 8+ tests, SQLite in-memory:
- load returns fresh snapshot when no row
- save/load preserves all fields
- reset deletes row
- record_trade increments daily_spend
- record_loss increments consecutive_losses
- record_win resets consecutive_losses
- daily_spend resets after 24h
- hourly baseline initialized on first trade

**`tests/test_security_runtime.py`** — 7+ tests, con mocks:
- disabled runtime is no-op
- spend limit blocked → record_skip → set_status
- dry-run logs WARN, proceeds
- reset clears via repo
- StreakTrader integration: signal skipped when cap hit

**Casos edge críticos**:
- [ ] Daily reset: >24h desde `daily_spend_reset_at` → counter a 0
- [ ] Hourly baseline: si `hourly_drawdown_at == 0` (primer trade) → no dispara
- [ ] Consecutive losses reset on win
- [ ] DB session failure no rompe el trade (degraded: log + skip check)
- [ ] `dry_run=True` con cap excedido: log WARN, trade prosigue
- [ ] Multi-symbol: cada symbol tiene su propio row

**Cobertura objetivo**: ≥85% líneas en `bot/security.py` + `bot/security_runtime.py`; ≥80% en `bot/security_store.py`.

---

## 9. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Lock contention en DB (cada trade hace SELECT+INSERT) | `GuardRepository` usa session corta; no locks largos. Si DB está saturada, `record_trade` se hace best-effort y el trade continúa. |
| Decimal vs float drift | Toda aritmética USD con `Decimal`. Cast explícito al leer de DB (que es float). |
| Config hot-reload race | `SecurityRuntime.update_config` re-asigna atómicamente; el check en curso usa el config que vio al inicio. |
| DB schema migration si ya existe `bot_guards` | `Base.metadata.create_all` es idempotente — no falla si la tabla existe. Para upgrade de columnas nuevas en futuro, agregar `ALTER TABLE` con guard. |
| `security_dry_run=True` se olvida activo | El dashboard muestra banner amarillo permanente; el operador lo ve. |

---

## 10. Out of scope (no se hace)

- ❌ Pre-send simulation (ECC's `safe_execute`) — requiere simulator de CLOB. Pendiente para fase 2.
- ❌ Per-strategy overrides (e.g. TA puede gastar más que CFD) — diseño global
- ❌ Webhook / alert externo cuando se dispara — solo log + dashboard
- ❌ Auto-reset después de N horas sin trades — solo manual reset
- ❌ MEV protection / private RPC — bot opera en Polymarket CLOB v2, no en chain directa
- ❌ Wallet isolation enforcement más allá del `.env` actual

---

## 11. Success criteria

✅ **Done cuando**:
- `pytest tests/test_security.py tests/test_security_store.py tests/test_security_runtime.py -q` → 27+ tests passed
- `pytest tests/ -q` → 626+ passed total (no regressions en los 599 actuales + 27 nuevos)
- Smoke: `python -c "from bot.security import SpendLimitGuard, TradingCircuitBreaker, SpendLimitError, CircuitBreakerError; from bot.security_store import GuardRepository, GuardSnapshot; from bot.security_runtime import SecurityRuntime, GuardConfig"`
- En paper mode: dashboard muestra "Security status: ARMED"
- En paper mode: configurar `security_dry_run=True`, simular un cap > max en código → log `WOULD-BLOCK`, trade continúa
- En paper mode: configurar `security_max_daily_spend_usd=10`, ejecutar trades hasta que se bloquee → dashboard muestra "HALTED"
- `/settings` permite editar y persiste los 6 valores + reset
- Bot corre ≥24h en paper sin false positives

✅ **Real-mode gate** (cuando se haga el flip TRADING_MODE=real):
- Haber ejecutado este checklist durante ≥1 semana en paper
- Haber revisado `bot.log` y confirmado que `WOULD_BLOCK_*` aparece cuando corresponde
- Firmar la sección 8 de `docs/RISK_REVIEW.md`

---

## 12. Referencias

- ECC skill `llm-trading-agent-security` (origen de los patrones)
- `docs/RISK_REVIEW.md` §4 Security (gate pre real-mode)
- `docs/PRODUCTION_AUDIT.md` §2.1 (tests rojos = no deploy)
- `bot/state.py:starting_bankroll` (baseline para portfolio_value)
- `bot/streak_trader.py:_execute_signal:539` (punto de integración)
- `bot/db.py:init_db` (migración automática via `create_all`)
- `bot/config.py:332` (patrón de TRADING_MODE para SECURITY_*)