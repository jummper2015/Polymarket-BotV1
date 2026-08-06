"""Chainlink TWAP feed via Polymarket's real-time data service (RTDS).

Chainlink publishes two TWAP feeds per pair — 30 s and 60 s lookback windows —
designed to blunt manipulation by averaging instead of quoting spot. Polymarket
relays them, and the relay is the route this bot uses: no API key, no request
signing, and no ±5 s clock requirement, all of which the direct Chainlink route
demands (docs/CHAINLINK_TWAP.md §1).

What this feed is for, and what it is NOT for:

  ✅ Anti-manipulation filter. A 30 s TWAP lags spot by ~15 s by construction,
     so `spot − twap` measures how much of a recent move is wick.
  ✅ Recording tape. Chainlink serves no history and no replay, so a value not
     written down now can never be recovered (§6).
  ❌ Settlement. Markets resolve against the *plain* `btc-usd` stream, a
     different feed ID. A TWAP verdict is a better guess than Binance, not the
     truth (§3).
  ❌ Entry signal. Binance is faster and un-smoothed (§4).

⚠️  As of 3-ago-2026 both topics are rejected by the relay with a 500 (a 16-char
    column can't hold a 25-char topic). They launch 4-ago-2026. Everything here
    is off by default and a failed subscription is logged, never fatal.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Optional

import websocket  # websocket-client

from . import logger


RTDS_URL = "wss://ws-live-data.polymarket.com"

TOPIC_30 = "crypto_prices_twap_thirty"
TOPIC_60 = "crypto_prices_twap_sixty"

# The reports carry no symbol or window label of their own, so the topic a
# message arrived on is the only way to know which feed produced it.
TOPIC_WINDOWS: Dict[str, int] = {TOPIC_30: 30, TOPIC_60: 60}

# The relay expects a bare "PING" text frame, not a protocol-level ping.
HEARTBEAT_SECONDS = 5.0
RECONNECT_SECONDS = 3.0

# Note the slash: the TWAP topics use "btc/usd" while the non-TWAP
# `crypto_prices` topic uses "btcusdt". Mixing them yields silence, not an error.
DEFAULT_SYMBOL = "btc/usd"

E18 = Decimal(10) ** 18


# (symbol, window_s, value, observed_at_ms, received_at_ms)
TickCallback = Callable[[str, int, Decimal, int, int], None]


def parse_value(payload: dict) -> Optional[Decimal]:
    """Exact price from a TWAP payload.

    `full_accuracy_value` is an E18 integer and is the only field safe to
    compute with — `value` is a float for display and loses digits. Parsed with
    Decimal throughout; float would defeat the point of the E18 encoding.
    """
    raw = payload.get("full_accuracy_value")
    if raw is not None:
        try:
            return Decimal(str(raw)) / E18
        except (InvalidOperation, ValueError):
            pass  # fall through to the display value rather than dropping the tick

    display = payload.get("value")
    if display is None:
        return None
    try:
        return Decimal(str(display))
    except (InvalidOperation, ValueError):
        return None


class ChainlinkTwapFeed:
    """RTDS TWAP subscription with auto-reconnect and a freshness bound.

    Mirrors `price_feed.PriceFeed`: daemon thread, websocket-client, reconnect
    loop. Deliberately no new dependency — the official SDK is async-only beta,
    and these topics are already verified against the raw server (§7.8).
    """

    def __init__(
        self,
        *,
        symbol: str = DEFAULT_SYMBOL,
        stale_seconds: float = 15.0,
        on_tick: Optional[TickCallback] = None,
        url: str = RTDS_URL,
        reconnect_seconds: float = RECONNECT_SECONDS,
    ):
        self.symbol = symbol
        self.stale_seconds = stale_seconds
        self.on_tick = on_tick
        self.url = url
        self.reconnect_seconds = reconnect_seconds

        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocketApp] = None

        # Latest accepted tick per window, plus when we got it.
        self._latest: Dict[int, tuple[Decimal, int, int]] = {}
        self._connected = False
        self._subscribe_failed = False
        self._stale_drops = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="chainlink-twap", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                # A brand-new socket every attempt, never a re-subscribe on the
                # existing one: the docs warn a subscription rejected before
                # launch may not retry over an already-open socket (§2.3).
                self._ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever()
            except Exception as exc:
                logger.warn(f"[chainlink] feed crashed: {exc}")

            if self._stop.is_set():
                break
            time.sleep(self.reconnect_seconds)

    # ── websocket callbacks ───────────────────────────────────────────────────

    def _on_open(self, ws) -> None:
        with self._lock:
            self._connected = True
            self._subscribe_failed = False

        for topic in (TOPIC_30, TOPIC_60):
            try:
                ws.send(json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": topic,
                        "type": "update",
                        # Compact JSON, no spaces — the relay matches this string
                        # literally and silently drops anything else.
                        "filters": json.dumps(
                            {"symbol": self.symbol}, separators=(",", ":")
                        ),
                    }],
                }))
            except Exception as exc:
                logger.warn(f"[chainlink] subscribe {topic} falló: {exc}")

        self._start_heartbeat(ws)
        logger.info(f"[chainlink] suscrito TWAP 30s+60s  symbol={self.symbol}", icon="🔗")

    def _start_heartbeat(self, ws) -> None:
        if self._hb_thread and self._hb_thread.is_alive():
            return

        def _beat() -> None:
            while not self._stop.is_set():
                if self._stop.wait(HEARTBEAT_SECONDS):
                    return
                try:
                    ws.send("PING")
                except Exception:
                    return  # socket is gone; the run loop will rebuild it

        self._hb_thread = threading.Thread(
            target=_beat, name="chainlink-ping", daemon=True
        )
        self._hb_thread.start()

    def _on_message(self, ws, raw: str) -> None:
        if not raw or raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return

        # Pre-launch the relay answers subscriptions with a 500 body. Log it
        # once and keep the connection: this must never take the bot down.
        status = msg.get("statusCode") or msg.get("status_code")
        if status and int(status) >= 400:
            with self._lock:
                already = self._subscribe_failed
                self._subscribe_failed = True
            if not already:
                body = msg.get("body") or {}
                detail = body.get("message") if isinstance(body, dict) else body
                logger.warn(
                    f"[chainlink] suscripción rechazada ({status}): {detail}  "
                    f"— los topics TWAP lanzan el 4-ago-2026; el bot sigue sin ellos"
                )
            return

        topic = msg.get("topic")
        window_s = TOPIC_WINDOWS.get(topic)
        if window_s is None:
            return

        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return

        # No `filters` match is guaranteed when subscribing to several symbols,
        # so check the payload too.
        symbol = str(payload.get("symbol") or "").lower()
        if symbol and symbol != self.symbol:
            return

        value = parse_value(payload)
        if value is None or value <= 0:
            return

        observed_at = int(payload.get("timestamp") or 0)          # Chainlink's clock
        received_at = int(msg.get("timestamp") or time.time() * 1000)  # relay's

        # Chainlink publishes no cadence SLA, so a staleness bound isn't
        # optional — without it a frozen feed reads as a live one.
        age_s = (time.time() * 1000 - observed_at) / 1000.0
        if observed_at and age_s > self.stale_seconds:
            with self._lock:
                self._stale_drops += 1
            return

        with self._lock:
            self._latest[window_s] = (value, observed_at, received_at)

        if self.on_tick is not None:
            try:
                self.on_tick(symbol or self.symbol, window_s, value,
                             observed_at, received_at)
            except Exception as exc:
                logger.warn(f"[chainlink] on_tick falló: {exc}")

    def _on_error(self, ws, error) -> None:
        if self._stop.is_set():
            return
        logger.warn(f"[chainlink] error: {error}")

    def _on_close(self, ws, status_code, msg) -> None:
        with self._lock:
            self._connected = False
        if not self._stop.is_set():
            logger.info("[chainlink] conexión cerrada; reconectando", icon="↻")

    # ── readers ───────────────────────────────────────────────────────────────

    def get_twap(self, window_s: int = 30) -> Optional[Decimal]:
        """Latest TWAP for a window, or None if absent or stale.

        Staleness is re-checked on read: a feed that stopped publishing five
        minutes ago still has a "latest" value, and returning it would be worse
        than returning nothing.
        """
        with self._lock:
            entry = self._latest.get(window_s)
        if entry is None:
            return None
        value, observed_at, _ = entry
        if observed_at:
            age_s = (time.time() * 1000 - observed_at) / 1000.0
            if age_s > self.stale_seconds:
                return None
        return value

    def divergence(self, spot: float, window_s: int = 30) -> Optional[float]:
        """Relative gap `(spot − twap) / twap`, or None when the TWAP is unusable.

        Large magnitude means spot has run away from the smoothed average — a
        wick or a manipulation attempt, i.e. exactly the move the settlement
        price is likely to ignore.
        """
        twap = self.get_twap(window_s)
        if twap is None or twap <= 0 or spot <= 0:
            return None
        return float((Decimal(str(spot)) - twap) / twap)

    def status(self) -> dict:
        """Feed state for the dashboard.

        `age_s` is what makes the tile useful: it's the number that says whether
        to trust the value, and the one CL_TWAP_STALE_SECONDS gets tuned from.
        """
        now_ms = time.time() * 1000
        with self._lock:
            connected = self._connected
            failed = self._subscribe_failed
            drops = self._stale_drops
            latest = dict(self._latest)

        windows: Dict[str, dict] = {}
        for window_s, (value, observed_at, received_at) in latest.items():
            age_s = (now_ms - observed_at) / 1000.0 if observed_at else None
            windows[str(window_s)] = {
                "value": float(value),
                "observed_at": observed_at,
                "age_s": round(age_s, 3) if age_s is not None else None,
                # Relay overhead: how long the publisher took to hand it over.
                "relay_lag_s": (
                    round((received_at - observed_at) / 1000.0, 3)
                    if observed_at and received_at else None
                ),
                "stale": age_s is not None and age_s > self.stale_seconds,
            }

        return {
            "connected": connected,
            "subscribe_failed": failed,
            "stale_drops": drops,
            "windows": windows,
        }
