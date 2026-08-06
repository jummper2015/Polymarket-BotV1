"""Tests for the Chainlink TWAP feed.

Focus is on the four things that would silently corrupt data or take the bot
down: E18 precision, the staleness bound, the pre-launch 500, and fail-open
behaviour when the feed has nothing to say.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from bot.chainlink_feed import (
    TOPIC_30,
    TOPIC_60,
    ChainlinkTwapFeed,
    parse_value,
)


def _payload(value_e18: str, *, symbol="btc/usd", ts=None, window=30) -> dict:
    return {
        "symbol": symbol,
        "value": float(Decimal(value_e18) / (Decimal(10) ** 18)),
        "full_accuracy_value": value_e18,
        "timestamp": ts if ts is not None else int(time.time() * 1000),
        "window_s": window,
    }


def _msg(topic: str, payload: dict) -> str:
    return json.dumps({
        "topic": topic, "type": "update",
        "timestamp": int(time.time() * 1000), "payload": payload,
    })


# ── precision ─────────────────────────────────────────────────────────────────


class TestParseValue:
    def test_parses_e18_integer_exactly(self):
        assert parse_value(_payload("65000500000000000000000")) == Decimal("65000.5")

    def test_preserves_digits_float_would_lose(self):
        # 18 significant digits: float64 holds ~15-17 and would round this.
        exact = "65000123456789012345678"
        value = parse_value(_payload(exact))
        assert value == Decimal(exact) / (Decimal(10) ** 18)
        assert str(value) != str(float(value))

    def test_falls_back_to_display_value(self):
        # A dropped tick is worse than a slightly less precise one.
        assert parse_value({"value": 65000.5}) == Decimal("65000.5")

    def test_returns_none_without_any_value(self):
        assert parse_value({"symbol": "btc/usd"}) is None

    def test_returns_none_on_garbage(self):
        assert parse_value({"full_accuracy_value": "abc", "value": "xyz"}) is None


# ── staleness ─────────────────────────────────────────────────────────────────


class TestStaleness:
    def test_accepts_fresh_tick(self):
        feed = ChainlinkTwapFeed(stale_seconds=15.0)
        feed._on_message(None, _msg(TOPIC_30, _payload("65000500000000000000000")))
        assert feed.get_twap(30) == Decimal("65000.5")

    def test_drops_stale_tick_on_arrival(self):
        feed = ChainlinkTwapFeed(stale_seconds=15.0)
        old = int((time.time() - 60) * 1000)
        feed._on_message(None, _msg(TOPIC_30, _payload("65000500000000000000000", ts=old)))
        assert feed.get_twap(30) is None
        assert feed.status()["stale_drops"] == 1

    def test_value_goes_stale_on_read(self):
        # A feed that froze still has a "latest" value; serving it is worse
        # than serving nothing.
        feed = ChainlinkTwapFeed(stale_seconds=1.0)
        feed._on_message(None, _msg(TOPIC_30, _payload("65000500000000000000000")))
        assert feed.get_twap(30) is not None
        time.sleep(1.2)
        assert feed.get_twap(30) is None

    def test_windows_are_tracked_separately(self):
        feed = ChainlinkTwapFeed()
        feed._on_message(None, _msg(TOPIC_30, _payload("65000000000000000000000")))
        feed._on_message(None, _msg(TOPIC_60, _payload("64000000000000000000000")))
        assert feed.get_twap(30) == Decimal(65000)
        assert feed.get_twap(60) == Decimal(64000)


# ── pre-launch rejection ──────────────────────────────────────────────────────


class TestSubscriptionRejection:
    def test_500_is_recorded_not_raised(self):
        # This is the live behaviour until 4-ago-2026. It must not be fatal.
        feed = ChainlinkTwapFeed()
        feed._on_message(None, json.dumps({
            "body": {"message": "leger AddSubscriptions error: ERROR #22001 "
                                "value too long for type character(16)"},
            "statusCode": 500,
        }))
        assert feed.status()["subscribe_failed"] is True
        assert feed.get_twap(30) is None

    def test_survives_malformed_messages(self):
        feed = ChainlinkTwapFeed()
        for junk in ["", "PONG", "not json", "[]", "null",
                     json.dumps({"topic": "unknown"}),
                     json.dumps({"topic": TOPIC_30, "payload": "nope"})]:
            feed._on_message(None, junk)   # must not raise
        assert feed.get_twap(30) is None


# ── divergence / fail-open ────────────────────────────────────────────────────


class TestDivergence:
    def test_computes_relative_gap(self):
        feed = ChainlinkTwapFeed()
        feed._on_message(None, _msg(TOPIC_30, _payload("65000000000000000000000")))
        assert feed.divergence(65650.0, 30) == pytest.approx(0.01, abs=1e-6)

    def test_sign_follows_spot(self):
        feed = ChainlinkTwapFeed()
        feed._on_message(None, _msg(TOPIC_30, _payload("65000000000000000000000")))
        assert feed.divergence(64350.0, 30) < 0

    def test_none_without_twap_so_callers_fail_open(self):
        # The filter may only veto. With no feed there is nothing to veto on,
        # and the bot must trade exactly as it does today.
        feed = ChainlinkTwapFeed()
        assert feed.divergence(65000.0, 30) is None

    def test_none_on_nonsense_spot(self):
        feed = ChainlinkTwapFeed()
        feed._on_message(None, _msg(TOPIC_30, _payload("65000000000000000000000")))
        assert feed.divergence(0.0, 30) is None


# ── symbol filtering ──────────────────────────────────────────────────────────


class TestSymbolFilter:
    def test_ignores_other_symbols(self):
        feed = ChainlinkTwapFeed(symbol="btc/usd")
        feed._on_message(None, _msg(TOPIC_30, _payload("3000000000000000000000",
                                                       symbol="eth/usd")))
        assert feed.get_twap(30) is None

    def test_subscribe_filter_json_is_compact(self):
        # The relay matches this string literally; spaces make it match nothing.
        sent = []

        class FakeWS:
            def send(self, msg):
                sent.append(msg)

        feed = ChainlinkTwapFeed(symbol="btc/usd")
        feed._on_open(FakeWS())

        subs = [json.loads(m) for m in sent if m != "PING"]
        assert len(subs) == 2
        for sub in subs:
            assert sub["subscriptions"][0]["filters"] == '{"symbol":"btc/usd"}'
        assert {s["subscriptions"][0]["topic"] for s in subs} == {TOPIC_30, TOPIC_60}


# ── status ────────────────────────────────────────────────────────────────────


class TestStatus:
    def test_reports_age_and_relay_lag(self):
        feed = ChainlinkTwapFeed()
        feed._on_message(None, _msg(TOPIC_30, _payload("65000000000000000000000")))
        win = feed.status()["windows"]["30"]
        assert win["value"] == pytest.approx(65000.0)
        assert win["age_s"] is not None and win["age_s"] >= 0
        assert win["stale"] is False

    def test_disabled_feed_reports_disconnected_not_error(self):
        feed = ChainlinkTwapFeed()
        status = feed.status()
        assert status["connected"] is False
        assert status["subscribe_failed"] is False
        assert status["windows"] == {}
