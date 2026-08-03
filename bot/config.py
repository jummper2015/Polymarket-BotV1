"""Runtime configuration for the Streak Snapper bot.

Simplified — only Streak Snapper parameters plus core CLOB/chain settings.
"""

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


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class RuntimeField:
    """A parameter that can be changed at runtime from /settings.

    Single source of truth for parsing and range-checking, shared by the POST
    /config handler (JSON values) and the persisted-override loader (strings
    read back from `bot_config`).
    """

    name: str
    kind: str                              # "float" | "int" | "bool" | "choice"
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    persist: bool = True                   # survives a restart via bot_config

    def coerce(self, value: object) -> tuple[bool, object]:
        """Parse and validate. Returns (ok, parsed_value)."""
        try:
            if self.kind == "bool":
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
                        return False, None
                    return True, lowered in ("1", "true", "yes", "on")
                return True, bool(value)

            if self.kind == "choice":
                parsed = str(value).strip().lower()
                return (parsed in self.choices), parsed

            parsed = int(value) if self.kind == "int" else float(value)
        except (TypeError, ValueError):
            return False, None

        if self.minimum is not None and parsed < self.minimum:
            return False, None
        if self.maximum is not None and parsed > self.maximum:
            return False, None
        return True, parsed

    def serialize(self, value: object) -> str:
        if self.kind == "bool":
            return "true" if value else "false"
        return str(value)


# `mode` (paper/real) is deliberately absent: persisting it would let a setting
# made weeks ago start the bot trading with real money after a restart.
RUNTIME_FIELDS: dict[str, RuntimeField] = {
    f.name: f
    for f in (
        RuntimeField("ss_enabled", "bool"),
        RuntimeField("ss_mode", "choice", choices=("fade", "trend", "both")),
        RuntimeField("ss_fade_base_shares", "float", minimum=1, maximum=100_000),
        RuntimeField("ss_fade_limit_cap", "float", minimum=0.10, maximum=0.99),
        RuntimeField("ss_fade_streak_min", "int", minimum=2, maximum=20),
        RuntimeField("ss_trend_base_shares", "float", minimum=1, maximum=100_000),
        RuntimeField("ss_trend_limit_cap", "float", minimum=0.10, maximum=0.99),
        RuntimeField("ss_martingale_mult_factor", "float", minimum=1.01, maximum=10.0),
        RuntimeField("starting_bankroll", "float", minimum=1.0, maximum=10_000_000),
        # ── Chainlink TWAP (docs/CHAINLINK_TWAP.md §7.6) ──────────────────────
        # All default to off: the feed launches 4-ago-2026 and the divergence
        # thresholds can't be calibrated until weeks of tape exist.
        RuntimeField("cl_twap_enabled", "bool"),
        RuntimeField("cl_twap_window", "choice", choices=("30", "60")),
        RuntimeField("cl_twap_stale_seconds", "float", minimum=1.0, maximum=120.0),
        RuntimeField("cl_divergence_max", "float", minimum=0.0, maximum=0.05),
        RuntimeField("cl_record_ticks", "bool"),
    )
}

PERSISTABLE_FIELDS = {k: f for k, f in RUNTIME_FIELDS.items() if f.persist}


def coerce_overrides(raw: dict[str, str]) -> tuple[dict[str, object], list[str]]:
    """Turn stored `bot_config` strings into typed runtime values.

    Returns (valid_overrides, rejected_keys). A row that no longer parses — an
    old key, or a value outside the current range — is reported, not applied.
    """
    overrides: dict[str, object] = {}
    rejected: list[str] = []

    for key, raw_value in raw.items():
        field = PERSISTABLE_FIELDS.get(key)
        if field is None:
            rejected.append(key)
            continue
        ok, parsed = field.coerce(raw_value)
        if ok:
            overrides[key] = parsed
        else:
            rejected.append(key)

    return overrides, rejected


@dataclass
class Config:
    # Core
    mode: str                     # "paper" or "real"
    starting_bankroll: float

    # CLOB / Chain
    chain_id: int
    signature_type: int
    private_key: str
    proxy_wallet: str
    clob_host: str
    gamma_host: str
    ws_url: str

    # Dashboard
    dashboard_port: int
    dashboard_host: str

    # Market loading
    poll_interval_ms: int
    market_load_retry_seconds: int
    first_price_timeout_seconds: int

    # ── Streak Snapper ────────────────────────────────────────────────────────
    ss_enabled: bool
    ss_mode: str                    # "fade" | "trend" | "both"

    # Forma 1 — Fade
    ss_fade_base_shares: float
    ss_fade_limit_cap: float
    ss_fade_streak_min: int

    # Forma 2 — Trend
    ss_trend_base_shares: float
    ss_trend_limit_cap: float

    # Martingale
    ss_martingale_mult_factor: float

    # ── Chainlink TWAP ────────────────────────────────────────────────────────
    cl_twap_enabled: bool
    cl_twap_window: str             # "30" | "60" — only these two feeds exist
    cl_twap_stale_seconds: float
    cl_divergence_max: float        # 0 = veto disabled
    cl_record_ticks: bool
    cl_tick_retention_days: int

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
        # Core
        mode=mode,
        starting_bankroll=_env_float("STARTING_BANKROLL", 1000.0),

        # CLOB / Chain
        chain_id=_env_int("CHAIN_ID", 137),
        signature_type=_env_int("SIGNATURE_TYPE", 2),
        private_key=(os.getenv("PRIVATE_KEY") or "").strip(),
        proxy_wallet=(os.getenv("PROXY_WALLET") or "").strip(),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        gamma_host=os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"),
        ws_url=os.getenv("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),

        # Dashboard
        dashboard_port=_env_int("PORT", 5000),
        dashboard_host=os.getenv("DASHBOARD_HOST", "0.0.0.0"),

        # Market loading
        poll_interval_ms=_env_int("POLL_INTERVAL_MS", 50),
        market_load_retry_seconds=_env_int("MARKET_RETRY_SECONDS", 3),
        first_price_timeout_seconds=_env_int("FIRST_PRICE_TIMEOUT", 5),

        # ── Streak Snapper ────────────────────────────────────────────────────
        ss_enabled=_env_bool("SS_ENABLED", True),
        ss_mode=(os.getenv("SS_MODE") or "both").strip().lower(),

        # Forma 1 — Fade
        ss_fade_base_shares=_env_float("SS_FADE_BASE_SHARES", 5.0),
        ss_fade_limit_cap=_env_float("SS_FADE_LIMIT_CAP", 0.60),
        ss_fade_streak_min=_env_int("SS_FADE_STREAK_MIN", 4),

        # Forma 2 — Trend
        ss_trend_base_shares=_env_float("SS_TREND_BASE_SHARES", 5.0),
        ss_trend_limit_cap=_env_float("SS_TREND_LIMIT_CAP", 0.52),

        # Martingale
        ss_martingale_mult_factor=_env_float("SS_MARTINGALE_MULT", 1.5),

        # ── Chainlink TWAP ────────────────────────────────────────────────────
        cl_twap_enabled=_env_bool("CL_TWAP_ENABLED", False),
        cl_twap_window=(os.getenv("CL_TWAP_WINDOW") or "30").strip(),
        # 15 s is a guess until the real cadence is measured — Chainlink
        # publishes no SLA, so the docs insist on a staleness bound + fallback.
        cl_twap_stale_seconds=_env_float("CL_TWAP_STALE_SECONDS", 15.0),
        cl_divergence_max=_env_float("CL_DIVERGENCE_MAX", 0.0),
        cl_record_ticks=_env_bool("CL_RECORD_TICKS", False),
        # 2 ticks/s × 2 windows ≈ 172.800 rows/day. Without a bound this fills
        # the disk on a VPS in weeks (docs/CHAINLINK_TWAP.md §11.3).
        cl_tick_retention_days=_env_int("CL_TICK_RETENTION_DAYS", 30),
    )
