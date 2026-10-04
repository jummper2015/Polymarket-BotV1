"""DB persistence for security guard counters. Pure CRUD + bookkeeping.

Layered defense adapted from ECC's `llm-trading-agent-security` skill. The
repository owns the bot_guards table (one row per symbol) and exposes a
simple load/save/reset/record_trade API. The SecurityRuntime in
bot/security_runtime.py combines this with the pure-logic guards in
bot/security.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class GuardSnapshot:
    """Immutable view of guard state for one symbol.

    Decimal for USD fields — float drift is unacceptable for cap math.
    """
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
    """Per-symbol snapshot persistence. One row in `bot_guards` per symbol.

    Survives VPS reboot — operators can clear via /settings or call
    `reset(symbol)` directly. The 24h rolling daily reset happens in
    `record_trade` so the actual cap math in SpendLimitGuard stays simple.
    """

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
        """Apply the bookkeeping for one resolved trade.

        1. Rolling 24h daily reset (counter to 0 if >24h since last reset).
        2. Increment daily_spend_usd by trade cost.
        3. Increment or reset consecutive_losses based on is_loss.
        4. Initialize hourly baseline on first trade; roll it if >1h old.

        Returns the new snapshot. Caller is expected to call save() if it
        wants persistence (this method does save internally).
        """
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
