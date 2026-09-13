"""Technical indicators for trading signal enhancement.

Provides ATR (Average True Range) and RSI (Relative Strength Index) calculations
using Coinbase 1-minute candle data. Results are cached with 60s TTL to avoid
excessive API calls.
"""

from __future__ import annotations

import time
from typing import Optional

from . import logger


# Cache structure: {symbol: {"atr": (value, timestamp), "rsi": (value, timestamp)}}
_CACHE: dict[str, dict[str, tuple[float, float]]] = {}
_CACHE_TTL = 60.0  # seconds


def get_atr(symbol: str, period: int = 14) -> Optional[float]:
    """Calculate Average True Range over the last `period` 1-minute candles.

    ATR measures volatility by averaging the true range (max of high-low,
    abs(high-prev_close), abs(low-prev_close)) over N periods.

    Args:
        symbol: Asset symbol (e.g., "btc")
        period: Number of candles to average (default 14)

    Returns:
        ATR in USD, or None if data unavailable
    """
    now = time.time()

    # Check cache
    if symbol in _CACHE and "atr" in _CACHE[symbol]:
        cached_value, cached_time = _CACHE[symbol]["atr"]
        if now - cached_time < _CACHE_TTL:
            return cached_value

    try:
        from .coinbase_api import _get_candles, pair_for

        # Get 1-minute candles. Need period+1 to have prev_close for first TR.
        # Coinbase format: [timestamp, low, high, open, close, volume]
        klines = _get_candles(60, product_id=pair_for(symbol))
        if not klines or len(klines) < period + 1:
            return None

        # Take last period+1 candles
        klines = klines[-(period + 1):]

        # Calculate True Range for each candle
        true_ranges = []
        for i in range(1, len(klines)):
            current = klines[i]
            previous = klines[i - 1]

            high = float(current[2])
            low = float(current[1])
            prev_close = float(previous[4])

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            true_ranges.append(tr)

        # ATR is the average of the last `period` true ranges
        if len(true_ranges) < period:
            return None

        atr = sum(true_ranges[-period:]) / period

        # Cache result
        if symbol not in _CACHE:
            _CACHE[symbol] = {}
        _CACHE[symbol]["atr"] = (atr, now)

        return round(atr, 2)

    except Exception as exc:
        logger.warn(f"[INDICATORS] ATR calculation failed for {symbol}: {exc}", icon="⚠")
        return None


def get_rsi(symbol: str, period: int = 14) -> Optional[float]:
    """Calculate Relative Strength Index over the last `period` 1-minute candles.

    RSI measures momentum by comparing average gains to average losses.
    Values range from 0-100:
      - RSI > 70: overbought (potential reversal down)
      - RSI < 30: oversold (potential reversal up)

    Args:
        symbol: Asset symbol (e.g., "btc")
        period: Number of candles to calculate RSI (default 14)

    Returns:
        RSI value (0-100), or None if data unavailable
    """
    now = time.time()

    # Check cache
    if symbol in _CACHE and "rsi" in _CACHE[symbol]:
        cached_value, cached_time = _CACHE[symbol]["rsi"]
        if now - cached_time < _CACHE_TTL:
            return cached_value

    try:
        from .coinbase_api import _get_candles, pair_for

        # Get 1-minute candles. Need period+1 to calculate period price changes.
        # Coinbase format: [timestamp, low, high, open, close, volume]
        klines = _get_candles(60, product_id=pair_for(symbol))
        if not klines or len(klines) < period + 1:
            return None

        # Take last period+1 candles
        klines = klines[-(period + 1):]

        # Calculate price changes (close - prev_close)
        gains = []
        losses = []

        for i in range(1, len(klines)):
            current_close = float(klines[i][4])
            prev_close = float(klines[i - 1][4])
            change = current_close - prev_close

            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        if len(gains) < period:
            return None

        # Calculate average gain and average loss over period
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        # Handle division by zero
        if avg_loss == 0:
            rsi = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # Cache result
        if symbol not in _CACHE:
            _CACHE[symbol] = {}
        _CACHE[symbol]["rsi"] = (rsi, now)

        return round(rsi, 1)

    except Exception as exc:
        logger.warn(f"[INDICATORS] RSI calculation failed for {symbol}: {exc}", icon="⚠")
        return None


def get_volume_ratio(symbol: str, lookback: int = 10) -> Optional[float]:
    """Calculate current volume ratio vs average of last N 1-minute candles.

    A ratio > 1.5 indicates above-average volume (stronger signal).

    Args:
        symbol: Asset symbol (e.g., "btc")
        lookback: Number of candles to average for baseline (default 10)

    Returns:
        Volume ratio (current / avg), or None if data unavailable
    """
    now = time.time()

    # Check cache
    if symbol in _CACHE and "volume_ratio" in _CACHE[symbol]:
        cached_value, cached_time = _CACHE[symbol]["volume_ratio"]
        if now - cached_time < _CACHE_TTL:
            return cached_value

    try:
        from .coinbase_api import _get_candles, pair_for

        # Get 1-minute candles. Need lookback+1 (current + historical average).
        # Coinbase format: [timestamp, low, high, open, close, volume]
        klines = _get_candles(60, product_id=pair_for(symbol))
        if not klines or len(klines) < lookback + 1:
            return None

        # Take last lookback+1 candles
        klines = klines[-(lookback + 1):]

        # Last candle is current, previous N are for average
        volumes = [float(k[5]) for k in klines]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])

        if avg_volume == 0:
            return None

        ratio = current_volume / avg_volume

        # Cache result
        if symbol not in _CACHE:
            _CACHE[symbol] = {}
        _CACHE[symbol]["volume_ratio"] = (ratio, now)

        return round(ratio, 2)

    except Exception as exc:
        logger.warn(f"[INDICATORS] Volume ratio calculation failed for {symbol}: {exc}", icon="⚠")
        return None


def clear_cache(symbol: Optional[str] = None) -> None:
    """Clear indicator cache for a symbol or all symbols.

    Args:
        symbol: If provided, clear only this symbol. If None, clear all.
    """
    if symbol:
        _CACHE.pop(symbol, None)
    else:
        _CACHE.clear()
