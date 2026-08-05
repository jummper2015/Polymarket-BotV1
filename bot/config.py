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


def min_recovering_factor(limit_cap: float) -> float:
    """Smallest martingale factor whose cycle still recovers at `limit_cap`.

    Buying `s` shares at price `p` costs `s·p` and pays `s` on a win, so the
    profit of a win is `s(1−p)` while the losses behind it total
    `s_base·p·(fⁿ−1)/(f−1)`. Winning keeps clearing the cycle only while

        f > 1 / (1 − p)

    Below that the accumulated losses outgrow what the next win pays back, and
    "keep going until you win" turns a small loss into a large one instead of
    recovering it. At p=0.52 the floor is 2.083; at p=0.60 it is 2.5.
    """
    if limit_cap >= 1.0:
        return float("inf")
    return 1.0 / (1.0 - limit_cap)


def kelly_fraction(win_prob: float, price: float) -> float:
    """Fraction of bankroll Kelly stakes on a binary bought at `price`.

    Buying a share at `p` risks `p` to win `1-p`, so the net odds are
    `b = (1-p)/p` and the Kelly stake is `(win_prob·(1+b) − 1) / b`. At the
    measured 53.8% accuracy and a 0.519 fill this is 4.03% of bankroll — the
    number that says the martingale's $983 single-window bet is off by two
    orders of magnitude.

    Returns 0 when the bet has no edge, so a losing signal sizes to nothing
    instead of going negative.
    """
    if not (0.0 < price < 1.0):
        return 0.0
    b = (1.0 - price) / price
    return max(0.0, (win_prob * (1.0 + b) - 1.0) / b)


@dataclass(frozen=True)
class RuntimeField:
    """A parameter that can be changed at runtime from /settings.

    Single source of truth for parsing and range-checking, shared by the POST
    /config handler (JSON values) and the persisted-override loader (strings
    read back from `bot_config`).
    """

    name: str
    kind: str                              # "float" | "int" | "bool" | "choice" | "hours"
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

            if self.kind == "hours":
                # Imported here: bot.regime imports nothing from this module, but
                # keeping the dependency local documents that this is the only
                # place config needs to know what an hours spec looks like.
                from .regime import is_valid_hours_spec

                parsed = str(value).strip()
                return is_valid_hours_spec(parsed), parsed

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
        RuntimeField("ss_trend_min_strength", "float", minimum=0.0, maximum=0.10),
        RuntimeField("ss_martingale_mult_factor", "float", minimum=1.01, maximum=10.0),
        # How the stake grows. `flat` is the default because the measured edge
        # (+3.74%/trade at 53.8% accuracy, docs/RUTA.md Fase 8) justifies about
        # 4% of bankroll per trade, and a martingale bets ~100× that.
        RuntimeField("ss_sizing", "choice", choices=("flat", "kelly", "martingale")),
        RuntimeField("ss_kelly_fraction", "float", minimum=0.05, maximum=1.0),
        # ── Regime filters (bot/regime.py) ────────────────────────────────────
        # All default to off. The measured differences by session and volatility
        # band are suggestive, not significant — ~20 filters were tested, so the
        # best of them looks good by construction. Off by default means the
        # dashboard can compare "with" against "without" on live data.
        RuntimeField("ss_trading_hours", "hours"),
        RuntimeField("ss_vol_min_pct", "float", minimum=0.0, maximum=100.0),
        RuntimeField("ss_vol_max_pct", "float", minimum=0.0, maximum=100.0),
        RuntimeField("ss_range_max_pct", "float", minimum=0.0, maximum=100.0),
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
    ss_trend_min_strength: float    # min |move| of the 4h candle to call a trend

    # Sizing
    ss_sizing: str                  # "flat" | "kelly" | "martingale"
    ss_kelly_fraction: float
    ss_martingale_mult_factor: float

    # Markets to trade, in order. One trader thread each.
    ss_symbols: tuple[str, ...]

    # Regime filters. Sentinels rather than separate toggles: "" / 0 / 100 mean
    # "no restriction", so the off state is the identity value.
    ss_trading_hours: str           # "13-21,22-24"; "" = every hour
    ss_vol_min_pct: float           # 0   = no lower bound
    ss_vol_max_pct: float           # 100 = no upper bound
    ss_range_max_pct: float         # 100 = no restriction

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

    # A typo here would otherwise fall through to whatever branch sizing takes
    # last, which is the martingale — the one setting we don't want reached by
    # accident.
    sizing = (os.getenv("SS_SIZING") or "flat").strip().lower()
    if sizing not in ("flat", "kelly", "martingale"):
        sizing = "flat"

    ss_mode = (os.getenv("SS_MODE") or "fade").strip().lower()
    if ss_mode not in ("fade", "trend", "both"):
        ss_mode = "fade"

    # Unknown symbols are dropped rather than defaulted: silently trading BTC
    # because "bitcoin" was typed would be worse than trading nothing. An empty
    # result falls back to BTC so the bot always has one market.
    from .binance_api import SUPPORTED_SYMBOLS

    requested = [
        s.strip().lower()
        for s in (os.getenv("SS_SYMBOLS") or "btc").split(",")
        if s.strip()
    ]
    symbols = tuple(dict.fromkeys(s for s in requested if s in SUPPORTED_SYMBOLS))
    if not symbols:
        symbols = ("btc",)

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
        # "fade", not "both": measured over 3.734 windows with a pre-open quote,
        # ss_trend loses 4.22% per trade (7.08% once windows that also carry a
        # streak are excluded, t=-2.61). It bets on continuation, and 5-min
        # windows revert. See docs/RUTA.md Fase 8; the strategy stays available.
        ss_mode=ss_mode,

        # Forma 1 — Fade
        ss_fade_base_shares=_env_float("SS_FADE_BASE_SHARES", 5.0),
        # 0.52, not 0.60. The signal wins 53.8% of the time, so its fair price
        # is 0.538 — paying 0.60 is a 6.2-point loss bought on purpose. The five
        # real ss_fade trades in the DB filled at 0.558 average.
        ss_fade_limit_cap=_env_float("SS_FADE_LIMIT_CAP", 0.52),
        ss_fade_streak_min=_env_int("SS_FADE_STREAK_MIN", 4),

        # Forma 2 — Trend
        ss_trend_base_shares=_env_float("SS_TREND_BASE_SHARES", 5.0),
        ss_trend_limit_cap=_env_float("SS_TREND_LIMIT_CAP", 0.52),
        # A 4h candle that closed $1 away from its open is not a trend. Without
        # a floor, `close >= open` calls every candle — including a flat one,
        # which the `>=` scores as UP.
        #
        # 0.8% measured over 10.000 windows (34,7 días, etiquetas de Gamma;
        # `python -m bot.backtest --mode trend --min-strength X`). It is NOT an
        # edge-maximising choice — there wasn't one to make. Win rate came out
        # BELOW 50% at every threshold tested (47,3%–49,1%, z entre −1,1 y −1,9),
        # so this signal has no measurable edge and the martingale is what
        # decides the P&L. 0.8% is the smallest threshold whose worst case still
        # fits the default $1.000 bankroll: max drawdown −$892 and largest single
        # trade $983, versus −$36.509 and $40.163 with no threshold at all.
        ss_trend_min_strength=_env_float("SS_TREND_MIN_STRENGTH", 0.008),

        # Sizing. `flat` by default: a martingale changes variance, never
        # expected value, and the measured edge sizes at ~4% of bankroll
        # (Kelly), against the $983 single-window bet the martingale backtest
        # reached. `martingale` remains selectable for comparison.
        ss_sizing=sizing,
        # Quarter Kelly. Full Kelly on an edge measured at t=+1.32 would size an
        # unconfirmed hypothesis as if it were certain.
        ss_kelly_fraction=_env_float("SS_KELLY_FRACTION", 0.25),
        # Martingale. 2.1, not 1.5: see min_recovering_factor() — at the 0.52
        # trend cap a ×1.5 cycle stops recovering on the third attempt.
        ss_martingale_mult_factor=_env_float("SS_MARTINGALE_MULT", 2.1),

        # ── Regime filters ────────────────────────────────────────────────────
        # Off by default. US 13-21h UTC measured +9.22% against −5.00% for
        # 08-13h, positive in all four 8.8-day folds, but at t=+1.93 across ~20
        # filters tested that is a lead to verify, not a setting to ship on.
        ss_symbols=symbols,
        ss_trading_hours=(os.getenv("SS_TRADING_HOURS") or "").strip(),
        ss_vol_min_pct=_env_float("SS_VOL_MIN_PCT", 0.0),
        ss_vol_max_pct=_env_float("SS_VOL_MAX_PCT", 100.0),
        ss_range_max_pct=_env_float("SS_RANGE_MAX_PCT", 100.0),

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
