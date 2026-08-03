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


def _get_klines(interval: str, limit: int, end_time_ms: int | None = None) -> Optional[List[dict]]:
    """Fetch klines from Binance. Returns list of [open_time, open, high, low, close, ...]."""
    params: dict = {
        "symbol": "BTCUSDT",
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


def get_5min_windows(n: int = 16) -> Optional[List[dict]]:
    """Return the last `n` completed 5-min windows with BTC direction.

    Each window dict:
      {ts: unix_start, open: float, close: float, direction: "UP"|"DOWN"}
    Ordered oldest → newest.
    """
    raw = _get_klines("5m", limit=n + 1)  # +1 to get the current (possibly incomplete) candle
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


def get_window_direction(window_ts: int, lookback: int = 24) -> Optional[str]:
    """Direction of the completed 5-min candle starting at `window_ts`.

    Used to settle a trade the moment its window closes — Gamma takes about
    three minutes to publish outcomePrices, which is longer than the window
    itself, so waiting for it leaves the martingale a window out of date.

    Returns None (defer to Gamma, the official source) when the candle isn't
    closed yet, isn't in the fetched range, or moved too little to call.
    """
    raw = _get_klines("5m", limit=lookback)
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


def get_4h_trend() -> Optional[dict]:
    """Get the current 4h candle to determine trend direction.

    Returns:
      {ts: unix_start, open: float, close: float, direction: "UP"|"DOWN"}
    or None on failure.
    """
    raw = _get_klines("4h", limit=1)
    if raw is None or len(raw) == 0:
        return None

    k = raw[0]
    ts = int(k[0]) // 1000
    open_px = float(k[1])
    close_px = float(k[4])

    return {
        "ts": ts,
        "open": open_px,
        "close": close_px,
        "direction": "UP" if close_px >= open_px else "DOWN",
    }


def get_4h_trend_cached(last_ts: int | None = None) -> Optional[dict]:
    """Get 4h trend, only re-fetching if the candle timestamp changed."""
    trend = get_4h_trend()
    if trend is None:
        return None

    if last_ts is not None and trend["ts"] == last_ts:
        trend["_cached"] = True
    return trend


def get_btc_spot_price() -> Optional[float]:
    """Get current BTC spot price from Binance ticker (for display only)."""
    for attempt in range(2):
        try:
            r = requests.get(
                f"{BINANCE_BASE}/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=5,
            )
            if r.status_code == 200:
                return float(r.json()["price"])
        except Exception:
            if attempt < 1:
                time.sleep(1.0)
    return None
