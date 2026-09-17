"""Polymarket's own price for a 5-minute window: the strike and the live mark.

## Sources

### Strike (price-to-beat at window start)

Resolution order, in priority:

1. **Coinbase** — open of the 5-min candle that STARTS at `window_ts`.
   Per-window strike aligned to Polymarket's 5-min boundaries. Coinbase's
   REST endpoint returns the forming candle with its open frozen at the
   boundary, so this is the freshest possible per-window strike. Default
   primary since 2026-09-16, replacing Chainlink (whose ~30-min heartbeat
   was reusing the same strike across 6+ consecutive 5-min windows and
   inverting the bot's leader-side reads in volatile moves).

2. **Chainlink BTC/USD on Ethereum mainnet** (aggregator
   `0xF403...eE88c`, queried via public RPCs in
   `bot.chainlink_strike`). Closest match to Polymarket's display because
   Polymarket's BTC/USD TWAP stream aggregates the same exchanges the
   legacy Data Feed tracks. Used here as a *fallback* when Coinbase is
   unavailable and as a *sanity cross-check* (warn if Coinbase open and
   Chainlink latest differ by >0.3%).

3. **Polymarket's `crypto-price` endpoint** — used only as a tertiary
   fallback when both above fail. The `openPrice` is a 30-min aggregate
   open, validated against the previous 5-min window's cached strike to
   reject the leak signature (see `docs/TA_STRIKE_FIX_2026-09-11.md` v2).

4. All paths failed → `None`.

### Mark (live TWAP-style price during the window)
**Primary**: `fetch_window_price(...).closePrice` — the live closing price
of the 30-min aggregate containing the current window.

### Cache
Strikes are cached per `(symbol, window_ts)` in `_STRIKE_CACHE`. Each
window's strike is independent.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from . import chainlink_strike, coinbase_api, coinbase_ticker_feed, logger

HOST = "https://polymarket.com/api/crypto/crypto-price"
TIMEOUT = 8.0

# The strike is fixed for the whole window, so it is fetched once per window and
# never again. `closePrice` moves, which is why the cache is only consulted for
# a value that cannot go stale.
_STRIKE_CACHE: dict[tuple[str, int], float] = {}
_CACHE_LIMIT = 64
_lock = threading.Lock()


def _iso(window_ts: int) -> str:
    return datetime.fromtimestamp(int(window_ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _previous_cached_strike(symbol: str, window_ts: int) -> Optional[float]:
    """Look up the cached strike from the 5-min window that ended 300s ago.

    Used by `get_strike` to detect the 30-min-aggregate leak: if Polymarket
    returns the same `openPrice` as the previous 5-min window, the value is
    a 30-min aggregate, not the current window's strike.
    """
    prev = int(window_ts) - 300
    with _lock:
        return _STRIKE_CACHE.get((symbol, prev))


def fetch_window_price(window_ts: int, symbol: str = "btc") -> Optional[dict]:
    """Raw answer for one window, or None on any failure."""
    try:
        r = requests.get(
            HOST,
            params={"symbol": symbol, "eventStartTime": _iso(window_ts)},
            timeout=TIMEOUT,
        )
    except Exception as exc:
        logger.warn(f"[PM price] {symbol} {window_ts}: {type(exc).__name__}")
        return None

    if r.status_code != 200:
        logger.warn(f"[PM price] {symbol} {window_ts}: HTTP {r.status_code}")
        return None

    try:
        data = r.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _coinbase_fallback_strike(
    window_ts: int, symbol: str, reason: str
) -> Optional[float]:
    """Close of the prior 5-min Coinbase candle, used as the per-window strike
    when Polymarket's 30-min aggregate cannot be trusted.

    Caches the result so subsequent calls in the same window do not refetch.
    """
    # Import is at module level — see top-of-file import. Kept here as a
    # comment to make the dependency on `coinbase_api` obvious to anyone
    # reading just the fallback path.
    close = coinbase_api.get_5min_candle_close_at(window_ts, symbol)
    if close is None:
        return None

    logger.warn(
        f"[PM strike] {symbol} {window_ts}: usando fallback Coinbase "
        f"(close={close:.2f}) — motivo: {reason}",
        icon="⚠",
    )
    with _lock:
        if len(_STRIKE_CACHE) >= _CACHE_LIMIT:
            # Windows are consumed in order, so the oldest key is the coldest.
            for stale in sorted(_STRIKE_CACHE)[: _CACHE_LIMIT // 2]:
                _STRIKE_CACHE.pop(stale, None)
        _STRIKE_CACHE[(symbol, int(window_ts))] = close
    return close


def _looks_like_30min_aggregate(
    candidate: float, symbol: str, window_ts: int
) -> bool:
    """True if `candidate` is bit-identical to the previous 5-min window's
    cached strike. Polymarket's endpoint returns the same value for every
    5-min window inside the same closed 30-min aggregate, so equality here
    is a strong signal the value isn't the current window's strike."""
    prev = _previous_cached_strike(symbol, window_ts)
    return prev is not None and candidate == prev


def get_strike(window_ts: int, symbol: str = "btc") -> Optional[float]:
    """The window's price-to-beat — the BTC price at `window_ts`.

    Resolution order (see module docstring for full sourcing logic):

      1. Cache hit (validates against previous cached strike: if identical,
         the cached value is a stale aggregate leak; treat as miss and
         re-fetch from local TWAP).
      2. **Local TWAP-60s** — computed from a 90-second rolling buffer of
         Coinbase BTC-USD ticker ticks maintained by `coinbase_ticker_feed`.
         Closest approximation to the official Chainlink btc-usd-twap-60s
         stream that Polymarket uses for its strike. Gap vs official: ~$0-5.
         Added 2026-09-17 to close the ~$20-30 residual gap that Coinbase
         candle OPEN leaves (since OPEN is the spot at the boundary, not the
         60-second time-weighted average that ends at the boundary).
      3. **Coinbase 5-min candle OPEN** at `window_ts` — per-window strike
         aligned to Polymarket's 5-min boundaries; fallback when the ticker
         feed isn't ready (startup, reconnect) or doesn't have enough data.
      4. **Chainlink BTC/USD on Ethereum** — last-resort fallback; also used
         as a sanity cross-check when sources 2 or 3 succeed (>0.3% gap logs
         a warning).
      5. **Polymarket** — final fallback when all above fail; validated
         against the previous-window strike to reject 30-min aggregate leaks.
      6. All paths failed → `None`.
    """
    key = (symbol, int(window_ts))

    # 1. Cache hit (with leak validation).
    with _lock:
        cached = _STRIKE_CACHE.get(key)
    if cached is not None:
        if _looks_like_30min_aggregate(cached, symbol, int(window_ts)):
            logger.warn(
                f"[PM strike] {symbol} {window_ts}: cache hit {cached:.2f} "
                f"igual al strike previo — re-fetcheando desde Chainlink",
                icon="⚠",
            )
            with _lock:
                _STRIKE_CACHE.pop(key, None)
        else:
            return cached

    # 2. Primary: Local TWAP-60s from the Coinbase ticker feed. Closest
    #    approximation to the official Chainlink btc-usd-twap-60s stream.
    local_twap = coinbase_ticker_feed.get_twap60_at(int(window_ts))
    if local_twap is not None and local_twap > 0:
        # Cross-check vs Coinbase candle OPEN and Chainlink latest for
        # diagnostic logging only — local TWAP is the new ground truth.
        cb_open = coinbase_api.get_5min_candle_open_at(int(window_ts), symbol)
        if cb_open is not None and cb_open > 0:
            diff_pct = abs(local_twap - cb_open) / max(cb_open, 1.0) * 100.0
            if diff_pct > 0.3:
                logger.info(
                    f"[PM strike] {symbol} {window_ts}: "
                    f"local TWAP={local_twap:.2f} vs Coinbase open={cb_open:.2f} "
                    f"({diff_pct:.3f}% gap)",
                    icon="📊",
                )
        with _lock:
            if len(_STRIKE_CACHE) >= _CACHE_LIMIT:
                for stale in sorted(_STRIKE_CACHE)[: _CACHE_LIMIT // 2]:
                    _STRIKE_CACHE.pop(stale, None)
            _STRIKE_CACHE[key] = local_twap
        return local_twap

    # 3. Fallback: Coinbase 5-min candle OPEN at window_ts.
    cb_open = coinbase_api.get_5min_candle_open_at(int(window_ts), symbol)
    if cb_open is not None and cb_open > 0:
        # Cross-check vs Chainlink for diagnostic logging only.
        cl_price = chainlink_strike.get_strike_at(int(window_ts), symbol)
        if cl_price is not None and cl_price > 0:
            diff_pct = abs(cb_open - cl_price) / max(cl_price, 1.0) * 100.0
            if diff_pct > 0.3:
                logger.info(
                    f"[PM strike] {symbol} {window_ts}: "
                    f"Coinbase open={cb_open:.2f} vs Chainlink={cl_price:.2f} "
                    f"({diff_pct:.3f}% gap) — usando Coinbase open (TWAP feed no listo)",
                    icon="📊",
                )
        with _lock:
            if len(_STRIKE_CACHE) >= _CACHE_LIMIT:
                for stale in sorted(_STRIKE_CACHE)[: _CACHE_LIMIT // 2]:
                    _STRIKE_CACHE.pop(stale, None)
            _STRIKE_CACHE[key] = cb_open
        return cb_open

    # 4. Fallback: Chainlink BTC/USD on Ethereum.
    logger.warn(
        f"[PM strike] {symbol} {window_ts}: TWAP feed no listo y Coinbase open "
        f"no disponible — fallback a Chainlink",
        icon="⚠",
    )
    cl_price = chainlink_strike.get_strike_at(int(window_ts), symbol)
    if cl_price is not None and cl_price > 0:
        with _lock:
            if len(_STRIKE_CACHE) >= _CACHE_LIMIT:
                for stale in sorted(_STRIKE_CACHE)[: _CACHE_LIMIT // 2]:
                    _STRIKE_CACHE.pop(stale, None)
            _STRIKE_CACHE[key] = cl_price
        return cl_price

    # 5. Last resort: Polymarket (validated against previous-window strike).
    data = fetch_window_price(int(window_ts), symbol)
    if data:
        try:
            strike = float(data.get("openPrice"))
        except (TypeError, ValueError):
            strike = None

        if strike is not None and strike > 0:
            if not _looks_like_30min_aggregate(strike, symbol, int(window_ts)):
                with _lock:
                    if len(_STRIKE_CACHE) >= _CACHE_LIMIT:
                        for stale in sorted(_STRIKE_CACHE)[: _CACHE_LIMIT // 2]:
                            _STRIKE_CACHE.pop(stale, None)
                    _STRIKE_CACHE[key] = strike
                return strike
            logger.warn(
                f"[PM strike] {symbol} {window_ts}: Polymarket {strike:.2f} "
                f"coincide con strike previo — descartando (30-min leak)",
                icon="⚠",
            )

    return None


def _fetch_polymarket_open_only(window_ts: int, symbol: str) -> Optional[float]:
    """Just the `openPrice` from Polymarket, no caching, used for cross-checks."""
    data = fetch_window_price(window_ts, symbol)
    if not data:
        return None
    try:
        return float(data.get("openPrice"))
    except (TypeError, ValueError):
        return None


def get_strike_and_mark(
    window_ts: int, symbol: str = "btc"
) -> tuple[Optional[float], Optional[float]]:
    """(strike, live mark) for one window.

    The strike comes from `get_strike` (Coinbase primary, Polymarket fallback)
    — see that function for the sourcing logic.

    The mark is the live closing price of the 30-min aggregate containing the
    current window, fetched fresh on every call. The mark updates throughout
    the window and reflects the live BTC price (Polymarket's `closePrice` of
    the forming 30-min). Returns `None` for the mark when Polymarket is
    unavailable; the strike still works without it.
    """
    strike = get_strike(window_ts, symbol)

    data = fetch_window_price(window_ts, symbol)
    if not data:
        return strike, None

    try:
        close_mark = float(data.get("closePrice"))
    except (TypeError, ValueError):
        close_mark = None
    if close_mark is not None and close_mark <= 0:
        close_mark = None

    return strike, close_mark
