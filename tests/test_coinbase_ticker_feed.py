"""Tests for `bot.coinbase_ticker_feed` — local TWAP-60s source.

The feed is a background WebSocket thread that maintains a rolling 90s
buffer of `(timestamp_ms, price)` ticks for BTC-USD. The pure-functional
parts (buffer add/trim, TWAP calculation, readiness checks) are tested
without spinning up the WebSocket thread — the on_message parser is tested
in isolation with a fake ws object.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear module-level state before AND after each test to avoid cross-pollution."""
    from bot import coinbase_ticker_feed as feed
    with feed._lock:
        feed._buffer.clear()
    feed._last_tick_at = 0.0
    feed._connected = False
    feed._stop_event.clear()
    yield
    with feed._lock:
        feed._buffer.clear()
    feed._last_tick_at = 0.0
    feed._connected = False
    feed._stop_event.clear()


# ── _add_tick / buffer mechanics ─────────────────────────────────────────────


def test_add_tick_appends_to_buffer():
    from bot import coinbase_ticker_feed as feed

    feed._add_tick(75_000.0)
    feed._add_tick(75_010.0)
    feed._add_tick(75_020.0)

    with feed._lock:
        prices = [p for _, p in feed._buffer]
    assert prices == [75_000.0, 75_010.0, 75_020.0]


def test_add_tick_trims_entries_older_than_buffer_window():
    """Entries older than 90s should be pruned on each add."""
    from bot import coinbase_ticker_feed as feed

    # Insert an old tick (5 minutes ago)
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 5 * 60 * 1000
    with feed._lock:
        feed._buffer.append((old_ms, 70_000.0))

    # Adding a fresh tick should trim the old one
    feed._add_tick(75_000.0)

    with feed._lock:
        ts_values = [t for t, _ in feed._buffer]
    assert all(t > now_ms - feed._BUFFER_SECONDS * 1000 for t in ts_values)


def test_add_tick_updates_last_tick_at():
    from bot import coinbase_ticker_feed as feed

    assert feed._last_tick_at == 0.0
    before = time.time()
    feed._add_tick(75_000.0)
    after = time.time()
    assert before <= feed._last_tick_at <= after


# ── _on_message parser ───────────────────────────────────────────────────────


def test_on_message_ignores_non_ticker_messages():
    from bot import coinbase_ticker_feed as feed

    feed._on_message(None, json.dumps({"type": "subscriptions"}))
    assert len(feed._buffer) == 0


def test_on_message_ignores_other_products():
    from bot import coinbase_ticker_feed as feed

    feed._on_message(None, json.dumps({
        "type": "ticker", "product_id": "ETH-USD", "price": "3000.00",
    }))
    assert len(feed._buffer) == 0


def test_on_message_ignores_invalid_json():
    from bot import coinbase_ticker_feed as feed

    feed._on_message(None, "{not valid json")
    assert len(feed._buffer) == 0


def test_on_message_adds_btc_usd_tick():
    from bot import coinbase_ticker_feed as feed

    feed._on_message(None, json.dumps({
        "type": "ticker", "product_id": "BTC-USD", "price": "75123.45",
    }))
    with feed._lock:
        prices = [p for _, p in feed._buffer]
    assert prices == [75_123.45]


def test_on_message_ignores_zero_or_negative_prices():
    from bot import coinbase_ticker_feed as feed

    for bad_price in ("0", "-1", "", "abc"):
        feed._on_message(None, json.dumps({
            "type": "ticker", "product_id": "BTC-USD", "price": bad_price,
        }))
    assert len(feed._buffer) == 0


# ── is_ready ─────────────────────────────────────────────────────────────────


def test_is_ready_false_when_disconnected():
    from bot import coinbase_ticker_feed as feed

    feed._connected = False
    feed._add_tick(75_000.0)
    feed._last_tick_at = time.time()
    assert feed.is_ready() is False


def test_is_ready_false_with_empty_buffer():
    from bot import coinbase_ticker_feed as feed

    feed._connected = True
    feed._last_tick_at = time.time()
    assert feed.is_ready() is False


def test_is_ready_false_with_buffer_too_young():
    """Buffer must contain at least 30s of data."""
    from bot import coinbase_ticker_feed as feed

    feed._connected = True
    # Add one tick — buffer is 0s old
    feed._add_tick(75_000.0)
    assert feed.is_ready() is False


def test_is_ready_false_when_stale():
    """If no tick in the last 30s, mark as stale (disconnect detection)."""
    from bot import coinbase_ticker_feed as feed

    feed._connected = True
    # Backdate _last_tick_at
    feed._last_tick_at = time.time() - 60.0
    # Buffer has 60s of old ticks
    now_ms = int(time.time() * 1000)
    with feed._lock:
        for i in range(60):
            feed._buffer.append((now_ms - 60_000 + i * 1000, 75_000.0))
    assert feed.is_ready() is False


def test_is_ready_true_with_full_buffer():
    from bot import coinbase_ticker_feed as feed

    feed._connected = True
    now_ms = int(time.time() * 1000)
    with feed._lock:
        for i in range(60):
            feed._buffer.append((now_ms - 60_000 + i * 1000, 75_000.0))
    feed._last_tick_at = time.time()
    assert feed.is_ready() is True


# ── get_twap60_at ────────────────────────────────────────────────────────────


def _fill_buffer_with_constant_price(price: float, count: int = 60,
                                    window_seconds: int = 60) -> None:
    """Helper: fill the buffer with `count` ticks at `price`, spanning
    `window_seconds` ending now."""
    from bot import coinbase_ticker_feed as feed
    now_ms = int(time.time() * 1000)
    with feed._lock:
        feed._buffer.clear()
        for i in range(count):
            ts_ms = now_ms - (window_seconds * 1000) + int(i * window_seconds * 1000 / count)
            feed._buffer.append((ts_ms, price))
    feed._last_tick_at = time.time()
    feed._connected = True


def test_get_twap60_at_returns_none_when_not_ready():
    from bot import coinbase_ticker_feed as feed

    assert feed.get_twap60_at(int(time.time())) is None


def test_get_twap60_at_constant_price_returns_that_price():
    from bot import coinbase_ticker_feed as feed

    _fill_buffer_with_constant_price(75_000.0, count=60, window_seconds=60)
    twap = feed.get_twap60_at(int(time.time()))
    assert twap == pytest.approx(75_000.0, abs=1e-6)


def test_get_twap60_at_averages_multiple_prices():
    from bot import coinbase_ticker_feed as feed

    now_ms = int(time.time() * 1000)
    with feed._lock:
        feed._buffer.clear()
        # 60 ticks alternating between 75000 and 76000, average = 75500
        for i in range(60):
            ts_ms = now_ms - 60_000 + i * 1000
            price = 75_000.0 if i % 2 == 0 else 76_000.0
            feed._buffer.append((ts_ms, price))
    feed._last_tick_at = time.time()
    feed._connected = True

    twap = feed.get_twap60_at(int(time.time()))
    assert twap == pytest.approx(75_500.0, abs=1e-6)


def test_get_twap60_at_only_uses_ticks_in_window():
    """Ticks outside [window_ts-60, window_ts] are excluded from the average."""
    from bot import coinbase_ticker_feed as feed

    now = int(time.time())
    target_ts = now  # boundary at "now"
    with feed._lock:
        feed._buffer.clear()
        # 30 ticks at price 70000 INSIDE the window [now-60, now]
        for i in range(30):
            ts_ms = (target_ts - 60 + i * 2) * 1000
            feed._buffer.append((ts_ms, 70_000.0))
        # 30 ticks at price 80000 OUTSIDE the window (before now-60)
        for i in range(30):
            ts_ms = (target_ts - 120 + i * 2) * 1000
            feed._buffer.append((ts_ms, 80_000.0))
        # 30 ticks at price 90000 OUTSIDE the window (after now)
        for i in range(30):
            ts_ms = (target_ts + 1 + i * 2) * 1000
            feed._buffer.append((ts_ms, 90_000.0))
    feed._last_tick_at = time.time()
    feed._connected = True

    twap = feed.get_twap60_at(target_ts)
    # Only the 30 in-window ticks (all 70000) should contribute.
    assert twap == pytest.approx(70_000.0, abs=1e-6)


def test_get_twap60_at_returns_none_with_too_few_ticks():
    """Below _MIN_TICKS_FOR_TWAP, return None (too noisy)."""
    from bot import coinbase_ticker_feed as feed

    _fill_buffer_with_constant_price(75_000.0, count=5, window_seconds=60)
    assert feed.get_twap60_at(int(time.time())) is None


# ── start/stop lifecycle ────────────────────────────────────────────────────


def test_start_is_idempotent():
    from bot import coinbase_ticker_feed as feed

    # start() spawns a real thread. To avoid creating one in unit tests,
    # just verify that calling start() twice doesn't error and that
    # _started is set on first call.
    feed._started = False
    feed._thread = None
    feed.start()
    try:
        first_thread = feed._thread
        feed.start()  # second call should be no-op
        assert feed._thread is first_thread
    finally:
        feed.stop()


def test_stop_is_idempotent_and_resets_started():
    from bot import coinbase_ticker_feed as feed

    feed._started = True
    feed.stop()
    feed.stop()  # second call should be no-op
    assert feed._started is False
