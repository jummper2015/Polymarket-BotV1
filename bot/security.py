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
