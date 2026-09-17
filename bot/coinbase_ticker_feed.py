"""Background WebSocket feed for Coinbase BTC-USD ticker — local TWAP-60s source.

Provides a rolling buffer of `(timestamp_ms, price)` updated via Coinbase's
public WebSocket. Used by `bot.polymarket_price.get_strike` as the new
priority #1 source — computing a local 60-second TWAP from continuous ticker
data closes the ~$20-30 gap between Coinbase candle OPEN (spot at boundary)
and the official Polymarket strike (Chainlink btc-usd-twap-60s stream).

## Why local TWAP

Polymarket's "price at the beginning of the range" comes from the Chainlink
btc-usd-twap-60s stream (the same feed used for resolution). It's a 60-second
time-weighted average ending at each 5-min boundary, NOT a spot snapshot.
Coinbase candle OPEN is the spot at the boundary — close but not equal,
typically $20-50 off depending on intra-window volatility.

A local TWAP-60s computed from Coinbase ticker ticks approximates the
official TWAP within $0-5.

## Lifecycle

The feed starts in `main.main()` after `_apply_persisted_overrides()` and
runs as a daemon thread. It survives WebSocket disconnects with exponential
backoff (1s → 30s). Queries return `None` when:
  - the feed hasn't accumulated at least 30 seconds of data;
  - the buffer is stale (no ticks in the last 30s);
  - there are fewer than 10 ticks in the requested 60-second window;
  - the feed is not connected.

When `get_twap60_at` returns None, callers fall back to Coinbase candle
OPEN → Chainlink → Polymarket, in that order.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

import websocket  # websocket-client, already in requirements.txt

logger = logging.getLogger(__name__)

# Coinbase's public exchange WebSocket — ticker channel is public, no auth.
WS_URL = "wss://ws-feed.exchange.coinbase.com"
PRODUCT_ID = "BTC-USD"

# Buffer holds 90s of ticks: 60s for the TWAP window plus 30s margin for late
# ticks and reconnection recovery. At ~1-10 ticks/sec, that's 90-900 entries.
_BUFFER_SECONDS = 90
_TWAP_WINDOW_SECONDS = 60

# If no tick for this long, queries return None (avoid stale data after
# disconnect that hasn't been detected yet).
_STALE_AFTER_SECONDS = 30

# Minimum buffer age before `is_ready()` returns True. 30s of data is
# enough to compute a meaningful TWAP-60s at the next 5-min boundary.
_MIN_READY_SECONDS = 30

# Minimum tick count in the 60s window — below this the average is too
# noisy (would happen right after startup or after long disconnects).
_MIN_TICKS_FOR_TWAP = 10

# Reconnect backoff: start at 1s, double up to 30s.
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0

# WebSocket keepalive.
_PING_INTERVAL = 20
_PING_TIMEOUT = 10

_buffer: Deque[Tuple[int, float]] = deque()
_lock = threading.Lock()
_last_tick_at: float = 0.0
_connected: bool = False
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_started = False
_start_lock = threading.Lock()


def _add_tick(price: float) -> None:
    """Append a tick to the buffer and trim entries older than the buffer window."""
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - _BUFFER_SECONDS * 1000
    with _lock:
        global _last_tick_at
        _last_tick_at = time.time()
        _buffer.append((now_ms, price))
        while _buffer and _buffer[0][0] < cutoff_ms:
            _buffer.popleft()


def _on_message(ws, raw: str) -> None:
    """Parse a Coinbase ticker message and append the price to the buffer."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if data.get("type") != "ticker":
        return
    if data.get("product_id") != PRODUCT_ID:
        return
    try:
        price = float(data.get("price", 0))
    except (TypeError, ValueError):
        return
    if price > 0:
        _add_tick(price)


def _on_open(ws) -> None:
    """Subscribe to the ticker channel once the socket is open."""
    global _connected
    _connected = True
    logger.ok("Coinbase ticker feed connected", icon="🔌")
    try:
        ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": [PRODUCT_ID],
            "channels": ["ticker"],
        }))
    except Exception as exc:
        logger.warn(f"Coinbase ticker feed subscribe failed: {exc}")


def _on_close(ws, code, msg) -> None:
    global _connected
    _connected = False
    if _stop_event.is_set():
        logger.info("Coinbase ticker feed closed (intentional)", icon="🔌")
    else:
        logger.warn(
            f"Coinbase ticker feed disconnected (code={code}); reconnecting"
        )


def _on_error(ws, error) -> None:
    logger.warn(f"Coinbase ticker feed error: {error}")


def _run_loop() -> None:
    """Main loop with exponential backoff. Runs until stop() is called."""
    backoff = _BACKOFF_INITIAL
    while not _stop_event.is_set():
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            ws.run_forever(ping_interval=_PING_INTERVAL, ping_timeout=_PING_TIMEOUT)
        except Exception as exc:
            logger.warn(f"Coinbase ticker feed crashed: {exc}")
        global _connected
        _connected = False
        if _stop_event.is_set():
            break
        # Reset backoff after a successful run that lasted >60s.
        if _last_tick_at and (time.time() - _last_tick_at) > 60:
            backoff = _BACKOFF_INITIAL
        wait = min(backoff, _BACKOFF_MAX)
        logger.info(f"Coinbase ticker feed: reconnecting in {wait:.1f}s")
        if _stop_event.wait(wait):
            break
        backoff = min(backoff * 2, _BACKOFF_MAX)
    logger.info("Coinbase ticker feed stopped", icon="🛑")


def start() -> None:
    """Start the background ticker feed (idempotent). Safe to call from main()."""
    global _thread, _started
    with _start_lock:
        if _started:
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_run_loop,
            name="coinbase-ticker-feed",
            daemon=True,
        )
        _thread.start()
        _started = True
        logger.info("Coinbase ticker feed starting...", icon="🔌")


def stop() -> None:
    """Signal the feed thread to stop. Idempotent. Safe to call at shutdown."""
    global _started
    with _start_lock:
        if not _started:
            return
        _stop_event.set()
        _started = False


def is_ready() -> bool:
    """True when the feed is connected, the buffer has >=30s of data, and
    ticks have arrived within the last 30s. Used by callers to decide
    whether to consult the feed or fall back to other strike sources."""
    if not _connected:
        return False
    now = time.time()
    with _lock:
        if not _buffer:
            return False
        oldest_ms, _ = _buffer[0]
        last_tick = _last_tick_at
    age_seconds = (now * 1000 - oldest_ms) / 1000.0
    if age_seconds < _MIN_READY_SECONDS:
        return False
    if (now - last_tick) > _STALE_AFTER_SECONDS:
        return False
    return True


def get_twap60_at(window_ts: int) -> Optional[float]:
    """Local TWAP-60s at `window_ts` — arithmetic mean of BTC-USD ticker prices
    in the 60-second window `[window_ts-60, window_ts]`.

    Returns None if the feed isn't ready (no recent data) or there aren't
    enough ticks in the requested window. Callers MUST handle None and fall
    back to other strike sources (Coinbase candle OPEN, Chainlink, etc.).
    """
    if not is_ready():
        return None

    cutoff_ms = (int(window_ts) - _TWAP_WINDOW_SECONDS) * 1000
    end_ms = int(window_ts) * 1000

    with _lock:
        relevant = [(t, p) for t, p in _buffer if cutoff_ms <= t <= end_ms]

    if len(relevant) < _MIN_TICKS_FOR_TWAP:
        return None

    prices = [p for _, p in relevant]
    return sum(prices) / len(prices)
