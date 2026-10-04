"""Facade combining guards + repository. The only thing streak_trader touches.

Layered defense, adapted from ECC's `llm-trading-agent-security`. Holds the
GuardConfig and the GuardRepository, exposes a single check() call that runs
both guards and updates counters via the repository. In dry-run mode logs
what would be blocked but never raises — used in paper mode to validate
behavior without halting trades.
"""

from __future__ import annotations

import logging
from typing import Any

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
        proposed_usd,
        portfolio_value_usd,
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
            if self._handle_block("SPEND_LIMIT", e, state):
                raise

        # Circuit breaker check (uses original snap, not the one with updated daily)
        try:
            TradingCircuitBreaker().check(snap, portfolio_value_usd, self._config)
        except CircuitBreakerError as e:
            if self._handle_block("CIRCUIT_BREAKER", e, state):
                raise

    def record_trade(
        self,
        symbol: str,
        usd,
        is_loss: bool,
        portfolio_value_usd,
        now: float,
    ) -> None:
        """Called after settlement of each trade. Delegates to repo for bookkeeping."""
        self._repo.record_trade(symbol, usd, is_loss, portfolio_value_usd, now)

    def reset(self, symbol: str) -> None:
        """Called from /settings 'Clear guards' button."""
        self._repo.reset(symbol)
        log.info(f"[security] guards cleared for {symbol}")

    def update_config(self, config: GuardConfig) -> None:
        """Hot-reload from /settings. Atomic — replace reference."""
        self._config = config

    def _handle_block(self, kind: str, exc: Exception, state: Any) -> bool:
        """Returns True if the caller should re-raise (real block).

        In dry_run mode, logs WARN + records skip but returns False so
        the trade proceeds. In real block mode, records skip + sets
        halted status + returns True so caller re-raises.
        """
        if self._config.dry_run:
            log.warning(
                f"[security] DRY-RUN WOULD-BLOCK {kind}: {exc} — trade proceeds"
            )
            state.record_skip(f"WOULD_BLOCK_{kind}")
            return False
        log.warning(f"[security] BLOCKED {kind}: {exc}")
        state.record_skip(f"SKIP_{kind}")
        state.set_status(
            "halted_security",
            f"{kind}: {exc}",
        )
        return True
