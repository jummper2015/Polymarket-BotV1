"""Security guards for trade-level spend caps and portfolio circuit breakers.

Layered defense, adapted from ECC's `llm-trading-agent-security`. All USD
arithmetic uses Decimal; never float. Stateless — receives snapshots and
config, returns decisions or raises typed exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


class SpendLimitGuard:
    """Stateless. Verifies a proposed spend against single-tx and daily caps.

    The daily cap operates on the snapshot's `daily_spend_usd` — the rolling
    24h reset is handled upstream in `GuardRepository.record_trade` so this
    guard only deals with the already-reset counter.
    """

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
        return replace(snapshot, daily_spend_usd=new_daily)
