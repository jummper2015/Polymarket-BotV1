"""Tests for SecurityRuntime facade — uses MockRepo, no DB."""
from decimal import Decimal
import time
from unittest.mock import MagicMock
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
        from bot.security import GuardConfig as GC
        repo = MagicMock()
        repo.load.return_value = fresh_snap
        rt = SecurityRuntime(cfg, repo)
        cfg_disabled = GC(False, False, cfg.max_single_tx_usd,
                          cfg.max_daily_spend_usd, cfg.max_consecutive_losses,
                          cfg.max_hourly_drawdown_pct)
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
            symbol="btc", daily_spend_usd=Decimal("0"),
            daily_spend_reset_at=time.time(), consecutive_losses=5,
            last_trade_at=0.0, hourly_drawdown_baseline_usd=Decimal("0"),
            hourly_drawdown_at=0.0, last_reset_at=0.0, enabled=True, dry_run=False,
        )
        repo = MagicMock()
        repo.load.return_value = snap
        state = MagicMock()
        rt = SecurityRuntime(cfg, repo)
        with pytest.raises(CircuitBreakerError):
            rt.check("btc", Decimal("10"), Decimal("1000"), state)
        state.record_skip.assert_called()

    def test_dry_run_logs_but_does_not_raise(self, cfg, fresh_snap):
        from bot.security import GuardConfig as GC
        repo = MagicMock()
        repo.load.return_value = fresh_snap
        state = MagicMock()
        cfg_dry = GC(True, True, cfg.max_single_tx_usd,
                     cfg.max_daily_spend_usd, cfg.max_consecutive_losses,
                     cfg.max_hourly_drawdown_pct)
        rt = SecurityRuntime(cfg_dry, repo)
        # Should NOT raise even though single-tx would exceed
        rt.check("btc", Decimal("100"), Decimal("1000"), state)
        # State should record skip but set_status is NOT called for halt
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
