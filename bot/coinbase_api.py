"""Coinbase Advanced Trade API client — BTC klines for ME$IRVE signal generation.

No API key required (public endpoints only).
Drop-in replacement for binance_api.py to work around Hostinger IP blocks.
"""

from __future__ import annotations

import time
from typing import List, Optional

import requests

from . import logger

COINBASE_BASE = "https://api.exchange.coinbase.com"

# Polymarket slugs use the short name ("btc-updown-5m-…"); Coinbase wants the
# product ID format BTC-USD.
SYMBOL_PAIRS = {
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "sol": "SOL-USD",
}

SUPPORTED_SYMBOLS = tuple(SYMBOL_PAIRS)


def pair_for(symbol: str) -> str:
    """Coinbase product ID for a Polymarket symbol. Unknown symbols fall back to BTC."""
    return SYMBOL_PAIRS.get((symbol or "").strip().lower(), "BTC-USD")


def _get_candles(
    granularity: int,
    product_id: str = "BTC-USD",
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> Optional[List[List]]:
    """Fetch candles from Coinbase.
    
    Args:
        granularity: Seconds per candle (60, 300, 900, 3600, 21600, 86400)
        product_id: e.g. "BTC-USD"
        start: Unix timestamp (optional)
        end: Unix timestamp (optional)
    
    Returns list of [timestamp, low, high, open, close, volume]
    Note: Coinbase returns newest first, we reverse to match Binance (oldest first)
    """
    params: dict = {"granularity": granularity}
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    for attempt in range(3):
        try:
            r = requests.get(
                f"{COINBASE_BASE}/products/{product_id}/candles",
                params=params,
                timeout=10,
                headers={"User-Agent": "PolymarketBot/1.0"}
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) >= 1:
                    # Coinbase returns newest → oldest, reverse to match Binance
                    return list(reversed(data))
        except Exception as exc:
            if attempt < 2:
                time.sleep(2.0)
            else:
                logger.warn(f"[Coinbase] candles({granularity}s) error: {exc}")
    return None


def get_5min_windows(n: int = 16, symbol: str = "btc") -> Optional[List[dict]]:
    """Return the last `n` completed 5-min windows with direction.

    Each window dict:
      {ts: unix_start, open: float, close: float, direction: "UP"|"DOWN"}
    Ordered oldest → newest.
    """
    # Coinbase returns max 300 candles per request
    # Get n+1 to ensure we have n completed (drop current forming candle)
    raw = _get_candles(300, product_id=pair_for(symbol))  # 300s = 5min
    if raw is None:
        return None

    # Take the last n+1 candles, drop the last one (forming)
    # Coinbase format: [timestamp, low, high, open, close, volume]
    candles_slice = raw[-(n+1):-1] if len(raw) > n else raw[:-1]
    
    windows: List[dict] = []
    for candle in candles_slice:
        ts = int(candle[0])  # Already in unix seconds
        open_px = float(candle[3])
        close_px = float(candle[4])
        windows.append({
            "ts": ts,
            "open": open_px,
            "close": close_px,
            "direction": "UP" if close_px >= open_px else "DOWN",
        })

    # Ensure we have enough data
    if len(windows) < 4:
        logger.warn(f"[Coinbase] only {len(windows)} windows available — need at least 4")
        return None

    return windows


# Same threshold as Binance — calibrated against Gamma outcomes
NEAR_FLAT_THRESHOLD = 5e-5   # relative: 0.005%


def get_window_direction(
    window_ts: int, lookback: int = 24, symbol: str = "btc"
) -> Optional[str]:
    """Direction of the completed 5-min candle starting at `window_ts`.

    Returns None (defer to Gamma) when the candle isn't closed yet,
    isn't in the fetched range, or moved too little to call.
    """
    raw = _get_candles(300, product_id=pair_for(symbol))
    if raw is None:
        return None

    now_sec = int(time.time())
    
    for candle in raw:
        candle_ts = int(candle[0])
        if candle_ts != window_ts:
            continue
        
        # Check if candle is fully closed (5min = 300s)
        if now_sec < candle_ts + 300:
            return None

        open_px = float(candle[3])
        close_px = float(candle[4])
        
        if open_px <= 0:
            return None

        if abs(close_px - open_px) / open_px <= NEAR_FLAT_THRESHOLD:
            logger.warn(
                f"[Coinbase] ventana {window_ts} casi plana "
                f"({open_px:.2f} → {close_px:.2f}) — resolución diferida a Gamma"
            )
            return None

        return "UP" if close_px > open_px else "DOWN"

    return None


FOUR_HOURS = 4 * 3600


def get_last_closed_4h_candle(symbol: str = "btc") -> Optional[dict]:
    """The most recently *closed* 4h candle, and the block it licenses.

    Returns:
      {ts, open, close, direction, strength, block_start, block_end}
    """
    raw = _get_candles(14400, product_id=pair_for(symbol))  # 14400s = 4h
    if raw is None or len(raw) < 2:
        return None

    # Take second-to-last (last closed candle)
    # Coinbase: [timestamp, low, high, open, close, volume]
    candle = raw[-2]
    ts = int(candle[0])
    open_px = float(candle[3])
    close_px = float(candle[4])
    
    if open_px <= 0:
        return None

    return {
        "ts": ts,
        "open": open_px,
        "close": close_px,
        "direction": "UP" if close_px >= open_px else "DOWN",
        "strength": (close_px - open_px) / open_px,
        "block_start": ts + FOUR_HOURS,
        "block_end": ts + 2 * FOUR_HOURS,
    }


def get_5min_candles(n: int = 600, symbol: str = "btc") -> Optional[List[dict]]:
    """Last `n` closed 5-min candles with full OHLC, oldest → newest.

    Used by regime filters that need highs/lows for range calculations.
    """
    # Coinbase limits to 300 candles per request
    # For more than 300, we'd need pagination (not implemented yet)
    fetch_count = min(n, 300)
    
    raw = _get_candles(300, product_id=pair_for(symbol))
    if raw is None:
        return None

    candles: List[dict] = []
    # Drop last candle (still forming), take up to n
    for candle in raw[-(fetch_count+1):-1]:
        # Coinbase: [timestamp, low, high, open, close, volume]
        candles.append({
            "ts": int(candle[0]),
            "open": float(candle[3]),
            "high": float(candle[2]),
            "low": float(candle[1]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        })
    
    return candles or None


def get_atr4(symbol: str = "btc") -> Optional[float]:
    """Mean high−low of the last 4 *completed* 1-minute candles, in dollars.

    Volatility yardstick for COA calculations.
    """
    raw = _get_candles(60, product_id=pair_for(symbol))  # 60s = 1min
    if raw is None or len(raw) < 2:
        return None

    # Take last 5, drop forming candle, keep 4 completed
    closed = raw[-5:-1]
    if len(closed) < 1:
        return None

    try:
        # Coinbase: [timestamp, low, high, open, close, volume]
        ranges = [float(candle[2]) - float(candle[1]) for candle in closed]
    except (TypeError, ValueError, IndexError):
        return None

    atr = sum(ranges) / len(ranges)
    return atr if atr > 0 else None


def get_btc_spot_price(symbol: str = "btc") -> Optional[float]:
    """Get current spot price from Coinbase ticker (for display only)."""
    product_id = pair_for(symbol)
    
    for attempt in range(2):
        try:
            r = requests.get(
                f"{COINBASE_BASE}/products/{product_id}/ticker",
                timeout=5,
                headers={"User-Agent": "PolymarketBot/1.0"}
            )
            if r.status_code == 200:
                data = r.json()
                return float(data["price"])
        except Exception:
            if attempt < 1:
                time.sleep(1.0)
    return None


def get_current_window_open(symbol: str = "btc", window_ts: Optional[int] = None) -> Optional[float]:
    """Open price of the 5-min candle that starts at `window_ts`.

    This is the Temporal Arb "strike": the BTC price Polymarket's oracle will
    compare the close against.
    """
    raw = _get_candles(300, product_id=pair_for(symbol))
    if raw is None or len(raw) < 1:
        return None

    # Get the most recent (forming) candle
    candle = raw[-1]
    candle_ts = int(candle[0])

    if window_ts is not None and candle_ts != window_ts:
        # Timestamp mismatch — retry next tick
        return None

    open_px = float(candle[3])
    return open_px if open_px > 0 else None


def get_5min_candle_open_at(
    window_ts: int, symbol: str = "btc"
) -> Optional[float]:
    """Open of the 5-min candle that STARTS at `window_ts` — the strike.

    This is the price Polymarket compares the close against to resolve the
    up/down market for the window starting at `window_ts`. Coinbase candles
    are 5-min aligned on the same grid Polymarket uses, and the REST endpoint
    returns the forming candle with the open frozen at the boundary, so this
    is the freshest per-window strike available.

    Differs from `get_5min_candle_close_at` in two ways:
      - asks for a 1-second slice starting AT `window_ts` (not the prior
        300-second window), so the returned candle is the one that *begins*
        at the boundary, not the one that ends there.
      - returns OPEN (the boundary price) instead of CLOSE (the live price
        of the forming candle, which moves).

    Added 2026-09-16 to replace Chainlink as primary strike source after a
    production bug surfaced where Chainlink's ~30-min heartbeat caused the
    same strike to be reused across 6+ consecutive 5-min windows, inverting
    the bot's leader-side reads in volatile moves.

    Returns None on any failure (network, missing candle, non-positive open).
    """
    start = int(window_ts)
    end   = int(window_ts) + 1   # +1 to include the boundary candle itself
    raw = _get_candles(300, product_id=pair_for(symbol), start=start, end=end)
    if not raw:
        return None

    # raw is oldest-first; prefer the candle whose ts == window_ts.
    for candle in raw:
        if int(candle[0]) == int(window_ts):
            open_px = float(candle[3])
            return open_px if open_px > 0 else None

    # Fallback: most recent candle in the returned range.
    last = raw[-1]
    open_px = float(last[3])
    return open_px if open_px > 0 else None


def get_5min_candle_close_at(
    window_ts: int, symbol: str = "btc"
) -> Optional[float]:
    """Close of the COMPLETED 5-min candle whose [start, end] window ends at `window_ts`.

    Added 2026-09-13 as the fallback when `polymarket_price.get_strike` detects
    that the upstream 5-min strike from Polymarket's crypto-price endpoint is
    actually a 30-min aggregate (same value across multiple 5-min windows).

    Coinbase only returns COMPLETED candles, so this fetches the candle that
    starts at `window_ts - 300` and ends at `window_ts`. Its close is the BTC
    price at `window_ts` minus a few seconds — close enough to use as the
    strike for the 5-min Polymarket market starting at `window_ts`.

    Returns None on any failure (network, missing candle, non-positive price).
    """
    start = int(window_ts) - 300
    end   = int(window_ts) + 1   # +1 to include the boundary candle itself
    raw = _get_candles(300, product_id=pair_for(symbol), start=start, end=end)
    if not raw:
        return None

    # raw is oldest-first; prefer the candle whose ts == window_ts (boundary
    # candle from the prior window's close).
    for candle in raw:
        if int(candle[0]) == int(window_ts):
            close = float(candle[4])
            return close if close > 0 else None

    # Fallback: most recent candle in the returned range.
    last = raw[-1]
    close = float(last[4])
    return close if close > 0 else None
