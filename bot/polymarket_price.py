"""Polymarket's own price for a 5-minute window: the strike and the live mark.

`GET polymarket.com/api/crypto/crypto-price` answers, for one window:

    {"openPrice": 64446.01, "closePrice": 64470.64, "completed": false, ...}

`openPrice` is the strike — the price the window is measured against — and it is
the one number the Gamma market object does not carry (checked: 82 fields, no
strike). So this endpoint is the only source for it.

Taking `closePrice` from the same call, rather than a spot price from somewhere
else, is deliberate. The cushion is `close − open`, and reading the two sides
from two different feeds means part of the difference is just the gap between
the feeds. `Revisar Estrategias/spread_harvest_maker/` compares a Hyperliquid
mark against this openPrice and eats that error; one call avoids it.

⚠️ This is a site API, not the documented Gamma/CLOB surface: it can change or
rate-limit without notice. Every failure path returns None, and a strategy that
cannot read the strike must decline to act rather than guess — with a maker
quote, not quoting is the safe direction.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from . import logger

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


def get_strike(window_ts: int, symbol: str = "btc") -> Optional[float]:
    """The window's price-to-beat. Cached: it cannot change once the window opens."""
    key = (symbol, int(window_ts))
    with _lock:
        cached = _STRIKE_CACHE.get(key)
    if cached is not None:
        return cached

    data = fetch_window_price(window_ts, symbol)
    if not data:
        return None
    try:
        strike = float(data.get("openPrice"))
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None

    with _lock:
        if len(_STRIKE_CACHE) >= _CACHE_LIMIT:
            # Windows are consumed in order, so the oldest key is the coldest.
            for stale in sorted(_STRIKE_CACHE)[: _CACHE_LIMIT // 2]:
                _STRIKE_CACHE.pop(stale, None)
        _STRIKE_CACHE[key] = strike
    return strike


def get_strike_and_mark(
    window_ts: int, symbol: str = "btc"
) -> tuple[Optional[float], Optional[float]]:
    """(strike, live mark) for one window, both from the same answer.

    The strike still comes from the cache when it is warm, but the mark forces a
    fresh call — it is the whole point of asking.
    """
    data = fetch_window_price(window_ts, symbol)
    if not data:
        # The strike may still be known from an earlier call even when this one
        # failed; a cushion needs both, so the caller decides what to do with a
        # half answer.
        return get_strike(window_ts, symbol), None

    def _f(key: str) -> Optional[float]:
        try:
            value = float(data.get(key))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    strike = _f("openPrice")
    if strike is not None:
        with _lock:
            _STRIKE_CACHE[(symbol, int(window_ts))] = strike
    return strike, _f("closePrice")
