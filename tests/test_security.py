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
