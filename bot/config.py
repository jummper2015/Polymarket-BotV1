"""Runtime configuration for the Polymarket BTC up/down bot."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    trigger_price: float
    buy_amount: float
    starting_bankroll: float
    chain_id: int
    signature_type: int
    private_key: str
    proxy_wallet: str
    mode: str  # "paper" or "real"
    clob_host: str
    gamma_host: str
    ws_url: str
    dashboard_port: int
    dashboard_host: str
    poll_interval_ms: int
    market_load_retry_seconds: int
    first_price_timeout_seconds: int
    last_minute_seconds: int
    hedge_threshold: float

    @property
    def is_real(self) -> bool:
        return self.mode == "real"

    @property
    def has_credentials(self) -> bool:
        return bool(self.private_key) and bool(self.proxy_wallet)


def load_config() -> Config:
    mode = (os.getenv("TRADING_MODE") or "paper").strip().lower()
    if mode not in ("paper", "real"):
        mode = "paper"
    return Config(
        trigger_price=_env_float("TRIGGER_PRICE", 0.90),
        buy_amount=_env_float("BUY_AMOUNT", 20.0),
        starting_bankroll=_env_float("STARTING_BANKROLL", 1000.0),
        chain_id=_env_int("CHAIN_ID", 137),
        signature_type=_env_int("SIGNATURE_TYPE", 2),
        private_key=(os.getenv("PRIVATE_KEY") or "").strip(),
        proxy_wallet=(os.getenv("PROXY_WALLET") or "").strip(),
        mode=mode,
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        gamma_host=os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"),
        ws_url=os.getenv(
            "CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        ),
        dashboard_port=_env_int("PORT", 5000),
        dashboard_host=os.getenv("DASHBOARD_HOST", "0.0.0.0"),
        poll_interval_ms=_env_int("POLL_INTERVAL_MS", 50),
        market_load_retry_seconds=_env_int("MARKET_RETRY_SECONDS", 3),
        first_price_timeout_seconds=_env_int("FIRST_PRICE_TIMEOUT", 5),
        last_minute_seconds=_env_int("LAST_MINUTE_SECONDS", 60),
        hedge_threshold=_env_float("HEDGE_THRESHOLD", 0.96),
    )
