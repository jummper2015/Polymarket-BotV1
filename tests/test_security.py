"""Tests for SpendLimitGuard, TradingCircuitBreaker, and config dataclasses.

Pure-logic module — no DB, no mocks. Tests should run in <100ms total.
"""
import time
from decimal import Decimal
import pytest

from bot.security import (
    GuardConfig, SpendLimitError, CircuitBreakerError, SpendLimitGuard,
)
from bot.security_store import GuardSnapshot


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


def _make_snap(daily: Decimal = Decimal("0"), consec: int = 0) -> GuardSnapshot:
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
        result = guard.check_and_record(Decimal("30"), snap, _make_config())
        assert result.daily_spend_usd == Decimal("130")

    def test_daily_at_cap_passes(self):
        guard = SpendLimitGuard()
        snap = _make_snap(daily=Decimal("170"))
        result = guard.check_and_record(Decimal("30"), snap, _make_config())
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
        # original unchanged (immutable)
        assert snap.daily_spend_usd == Decimal("10")
