"""Binance public API client — BTC klines for Streak Snapper signal generation.

No API key required (public endpoints only).
Used by both Forma 1 (Fade) and Forma 2 (Trend).
"""

from __future__ import annotations

import time
from typing import List, Optional

import requests

from . import logger

BINANCE_BASE = "https://api.binance.com/api/v3"

# Polymarket slugs use the short name ("btc-updown-5m-…"); Binance wants the
# pair. One map so the two vocabularies only meet in one place.
#
# Only these three are supported. XRP and DOGE have 5-min markets too, but their
# books quote 3-6 cents wide against 1 cent for these, and the whole measured
# edge is under 2 cents — the spread would eat it before the signal spoke.
SYMBOL_PAIRS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
}

SUPPORTED_SYMBOLS = tuple(SYMBOL_PAIRS)


def pair_for(symbol: str) -> str:
    """Binance pair for a Polymarket symbol. Unknown symbols fall back to BTC."""
    return SYMBOL_PAIRS.get((symbol or "").strip().lower(), "BTCUSDT")


def _get_klines(
    interval: str,
    limit: int,
    end_time_ms: int | None = None,
    symbol: str = "BTCUSDT",
) -> Optional[List[dict]]:
    """Fetch klines from Binance. Returns list of [open_time, open, high, low, close, ...]."""
    params: dict = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    if end_time_ms:
        params["endTime"] = end_time_ms

    for attempt in range(3):
        try:
            r = requests.get(f"{BINANCE_BASE}/klines", params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) >= 1:
                    return data
        except Exception as exc:
            if attempt < 2:
                time.sleep(2.0)
            else:
                logger.warn(f"[Binance] klines({interval}) error: {exc}")
    return None


def get_5min_windows(n: int = 16, symbol: str = "btc") -> Optional[List[dict]]:
    """Return the last `n` completed 5-min windows with direction.

    Each window dict:
      {ts: unix_start, open: float, close: float, direction: "UP"|"DOWN"}
    Ordered oldest → newest.
    """
    # +1 to get the current (possibly incomplete) candle
    raw = _get_klines("5m", limit=n + 1, symbol=pair_for(symbol))
    if raw is None:
        return None

    windows: List[dict] = []
    for i, k in enumerate(raw[:-1]):  # drop the current/incomplete candle
        ts = int(k[0]) // 1000
        open_px = float(k[1])
        close_px = float(k[4])
        windows.append({
            "ts": ts,
            "open": open_px,
            "close": close_px,
            "direction": "UP" if close_px >= open_px else "DOWN",
        })

    # Ensure we have enough data
    if len(windows) < 4:
        logger.warn(f"[Binance] only {len(windows)} windows available — need at least 4")
        return None

    return windows


# Polymarket settles btc-updown from Chainlink's BTC/USD stream, not Binance.
# The two feeds track each other closely, so they agree on the direction of any
# normal 5-min window — the median window moves 0.031%. They can disagree on a
# near-flat one: an observed window moved 0.00014% ($0.09 on $63,082) and the
# feeds split. Below this threshold we don't guess, we wait for Gamma.
#
# Calibrated over 3.016 windows labelled by Gamma (3-ago-2026), balancing the
# two errors this threshold trades off — see docs/CHAINLINK_TWAP.md §11.1.b and
# `python -m bot.threshold_study` to re-measure:
#
#   too low  → Binance calls windows it can't call, and gets some wrong. A wrong
#              call moves the martingale the wrong way, and the multiplier can't
#              be reconstructed once later windows have used it.
#   too high → more windows defer to Gamma, which is ~200 s late. The trade then
#              resolves a tick later, so the very next entry is sized with one
#              result missing — and if the deferred window was a *win*, that
#              entry skips its reset and bets high.
#
#   threshold   mis-settled   deferred   over-sized (shares vs perfect sizing)
#   1e-5             3.64%        2.1%   770
#   5e-5             1.43%        9.1%   533   ← minimum
#   1.5e-4           0.40%       24.1%   951
#
# Raising it further keeps cutting mis-settlement but costs more than it saves:
# 1.5e-4 over-bets 24% more than 1e-5 did. Re-run the study every few weeks —
# the optimum moves with the volatility regime.
NEAR_FLAT_THRESHOLD = 5e-5   # relative: 0.005%


def get_window_direction(
    window_ts: int, lookback: int = 24, symbol: str = "btc"
) -> Optional[str]:
    """Direction of the completed 5-min candle starting at `window_ts`.

    Used to settle a trade the moment its window closes — Gamma takes about
    three minutes to publish outcomePrices, which is longer than the window
    itself, so waiting for it leaves the martingale a window out of date.

    Returns None (defer to Gamma, the official source) when the candle isn't
    closed yet, isn't in the fetched range, or moved too little to call.
    """
    raw = _get_klines("5m", limit=lookback, symbol=pair_for(symbol))
    if raw is None:
        return None

    now_ms = time.time() * 1000
    for k in raw:
        if int(k[0]) // 1000 != window_ts:
            continue
        # k[6] is the kline close time; only trust a fully closed candle.
        if float(k[6]) >= now_ms:
            return None

        open_px = float(k[1])
        close_px = float(k[4])
        if open_px <= 0:
            return None

        if abs(close_px - open_px) / open_px <= NEAR_FLAT_THRESHOLD:
            logger.warn(
                f"[Binance] ventana {window_ts} casi plana "
                f"({open_px:.2f} → {close_px:.2f}) — resolución diferida a Gamma"
            )
            return None

        # Polymarket resolves "Up" on close >= open; ties are deferred above.
        return "UP" if close_px > open_px else "DOWN"

    return None


FOUR_HOURS = 4 * 3600


def get_last_closed_4h_candle(symbol: str = "btc") -> Optional[dict]:
    """The most recently *closed* 4h candle, and the block it licenses.

    The trend cycle reads this one, not the candle in progress: at the moment a
    4h candle opens its close equals its open, so there is no trend to detect
    yet.  Measuring the candle that just finished and trading its direction over
    the next four hours is the only version of "4h trend" that a backtest can
    reproduce without seeing the future.

    Returns:
      {ts, open, close, direction, strength, block_start, block_end}
    where `strength` is the signed relative move (close-open)/open, and
    [block_start, block_end) is the 4h span this candle's direction applies to.
    """
    raw = _get_klines("4h", limit=2, symbol=pair_for(symbol))
    if raw is None or len(raw) < 2:
        return None

    # raw[-1] is the candle still forming; raw[-2] is the last closed one.
    k = raw[-2]
    ts = int(k[0]) // 1000
    open_px = float(k[1])
    close_px = float(k[4])
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

    `get_5min_windows` only carries open/close/direction, which is all the
    streak needs. The regime filters need highs and lows to measure a range, and
    enough history to place the current reading in a percentile of its own past.
    """
    raw = _get_klines("5m", limit=min(n, 1000) + 1, symbol=pair_for(symbol))
    if raw is None:
        return None

    candles: List[dict] = []
    for k in raw[:-1]:      # drop the candle still forming
        candles.append({
            "ts": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles or None


def get_btc_spot_price(symbol: str = "btc") -> Optional[float]:
    """Get current spot price from Binance ticker (for display only)."""
    for attempt in range(2):
        try:
            r = requests.get(
                f"{BINANCE_BASE}/ticker/price",
                params={"symbol": pair_for(symbol)},
                timeout=5,
            )
            if r.status_code == 200:
                return float(r.json()["price"])
        except Exception:
            if attempt < 1:
                time.sleep(1.0)
    return None
