"""WebSocket price feed — Polymarket CLOBv2.

Subscribes to three channels per token pair:
  • book            — full order-book snapshot (sent once on subscribe)
  • last_trade_price — price of the most recently matched trade (real-time)
  • user            — user-level events (connected when credentials available)

URL pattern
  market data : wss://ws-subscriptions-clob.polymarket.com/ws/market
  user  data  : wss://ws-subscriptions-clob.polymarket.com/ws/user

Subscription envelope (CLOBv2)
  {
    "auth": null,
    "markets": [],
    "assets_ids": ["<token_id_A>", "<token_id_B>"],
    "type": "market"
  }

Event types handled
  book             → compute mid from best-bid / best-ask
  last_trade_price → use reported price directly (most real-time signal)
  price_change     → update cached bid/ask; recompute mid
  best_bid_ask     → direct mid computation
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Dict, Optional, Tuple

import websocket  # websocket-client

from . import logger


PriceCallback = Callable[[str, float, float, float], None]  # (side, bid, ask, mid)


# ── price helpers ─────────────────────────────────────────────────────────────

def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _best_from_levels(levels: list, fn) -> Optional[float]:
    """Return the single best price from a list of order-book level dicts.

    ``fn`` must be ``max`` for bids (highest bid) or ``min`` for asks
    (lowest ask).  Always returns ``Optional[float]`` — never a list.
    """
    prices = [_safe_float(l.get("price")) for l in levels if isinstance(l, dict)]
    prices = [p for p in prices if p and p > 0]
    return fn(prices) if prices else None


def _mid_from_book(item: dict) -> Optional[float]:
    """Compute mid from a full book snapshot (bids + asks lists)."""
    bids = item.get("bids") or []
    asks = item.get("asks") or []

    best_bid = _best_from_levels(bids, max)
    best_ask = _best_from_levels(asks, min)

    if best_bid is None or best_ask is None:
        return None
    if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
        return None
    return (best_bid + best_ask) / 2.0


def _mid_from_ba(best_bid, best_ask) -> Optional[float]:
    b = _safe_float(best_bid)
    a = _safe_float(best_ask)
    if b and a and b > 0 and a > 0:
        return (b + a) / 2.0
    return None


# ── PriceFeed ─────────────────────────────────────────────────────────────────

class PriceFeed:
    """CLOBv2 WebSocket subscription with auto-reconnect.

    Emits price updates via on_price(side, price) for both UP and DOWN tokens.
    Internally caches best bid/ask per token so each event type can contribute
    to the mid calculation.
    """

    # Reconnect delay (seconds)
    RECONNECT_DELAY = 3.0
    # Ping interval / timeout for keep-alive
    PING_INTERVAL = 20
    PING_TIMEOUT  = 10

    def __init__(
        self,
        ws_url: str,
        up_token_id: str,
        down_token_id: str,
        on_price: PriceCallback,
        on_status: Callable[[bool], None] = lambda _: None,
        reconnect_seconds: float = 3.0,
    ) -> None:
        self.ws_url         = ws_url
        self.up_token_id    = up_token_id
        self.down_token_id  = down_token_id
        self.on_price       = on_price
        self.on_status      = on_status
        self.reconnect_seconds = reconnect_seconds

        self._stop   = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocketApp] = None

        # token_id → "UP" / "DOWN"
        self._token_to_side: Dict[str, str] = {
            up_token_id:   "UP",
            down_token_id: "DOWN",
        }

        # Cached best bid/ask per token_id  { token_id: (best_bid, best_ask) }
        self._ba_cache: Dict[str, Tuple[float, float]] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="price-feed",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ── internal loop ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = self._on_error,
                    on_close   = self._on_close,
                )
                self._ws.run_forever(
                    ping_interval = self.PING_INTERVAL,
                    ping_timeout  = self.PING_TIMEOUT,
                )
            except Exception as exc:
                logger.warn(f"price feed crashed: {exc}; reconnecting in {self.reconnect_seconds}s")

            self.on_status(False)
            if self._stop.is_set():
                break
            time.sleep(self.reconnect_seconds)

    # ── WebSocket callbacks ───────────────────────────────────────────────────

    def _on_open(self, ws) -> None:
        logger.ok("price feed connected", icon="🔌")
        self.on_status(True)
        self._send_subscriptions(ws)

    def _send_subscriptions(self, ws) -> None:
        """Send CLOBv2 market subscription for book + last_trade_price channels."""
        # Primary subscription: both token IDs on the market endpoint
        market_sub = {
            "auth":       None,
            "markets":    [],
            "assets_ids": [self.up_token_id, self.down_token_id],
            "type":       "market",
        }
        try:
            ws.send(json.dumps(market_sub))
            logger.info(
                f"WS subscribed  UP={self.up_token_id[:12]}…  DOWN={self.down_token_id[:12]}…",
                icon="📡",
            )
        except Exception as exc:
            logger.warn(f"WS subscription failed: {exc}")

    def _on_close(self, ws, status_code, msg) -> None:
        self.on_status(False)
        logger.warn(
            f"price feed disconnected (code={status_code}); "
            f"reconnecting in {self.reconnect_seconds}s"
        )

    def _on_error(self, ws, error) -> None:
        logger.warn(f"price feed error: {error}")

    def _on_message(self, ws, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    self._dispatch(item)
        elif isinstance(payload, dict):
            self._dispatch(payload)

    # ── event dispatcher ──────────────────────────────────────────────────────

    def _dispatch(self, item: dict) -> None:
        event_type = (
            item.get("event_type")
            or item.get("type")
            or ""
        )

        # ── book: full order-book snapshot ────────────────────────────────────
        if event_type == "book":
            token_id = item.get("asset_id") or item.get("token_id")
            side     = self._token_to_side.get(str(token_id)) if token_id else None
            if not side:
                return

            bids = item.get("bids") or []
            asks = item.get("asks") or []
            best_bid = _best_from_levels(bids, max)
            best_ask = _best_from_levels(asks, min)

            if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0 and best_bid <= best_ask:
                mid = (best_bid + best_ask) / 2.0
                self._emit(side, best_bid, best_ask, mid)
            return

        # ── last_trade_price: most recent matched trade ───────────────────────
        if event_type == "last_trade_price":
            token_id = item.get("asset_id") or item.get("token_id")
            side     = self._token_to_side.get(str(token_id)) if token_id else None
            price    = _safe_float(item.get("price"))
            if side and price and price > 0:
                # last_trade_price has no spread info; use cached bid/ask if available,
                # otherwise treat the trade price as both bid and ask (mid = price)
                cached = self._ba_cache.get(str(token_id))
                if cached:
                    bid, ask = cached
                    mid = (bid + ask) / 2.0
                    self._emit(side, bid, ask, mid)
                else:
                    self._emit(side, price, price, price)
            return

        # ── price_change: incremental book update ────────────────────────────
        if event_type == "price_change":
            # Try format A: single asset update at top level
            token_id = item.get("asset_id") or item.get("token_id")
            if token_id and (item.get("best_bid") or item.get("best_ask")):
                b = _safe_float(item.get("best_bid"))
                a = _safe_float(item.get("best_ask"))
                side = self._token_to_side.get(str(token_id))
                if side and b and a and b > 0 and a > 0:
                    mid = (b + a) / 2.0
                    self._emit(side, b, a, mid)
                    return

            # Format B: price_changes array
            changes = item.get("price_changes") or []
            if isinstance(changes, list):
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    tid  = ch.get("asset_id") or ch.get("token_id") or item.get("asset_id")
                    side = self._token_to_side.get(str(tid)) if tid else None
                    b = _safe_float(ch.get("best_bid"))
                    a = _safe_float(ch.get("best_ask"))
                    if side and b and a and b > 0 and a > 0:
                        mid = (b + a) / 2.0
                        self._emit(side, b, a, mid)
            return

        # ── best_bid_ask: dedicated bid/ask snapshot ─────────────────────────
        if event_type == "best_bid_ask":
            token_id = item.get("asset_id") or item.get("token_id")
            side     = self._token_to_side.get(str(token_id)) if token_id else None
            b = _safe_float(item.get("best_bid"))
            a = _safe_float(item.get("best_ask"))
            if side and b and a and b > 0 and a > 0:
                mid = (b + a) / 2.0
                self._emit(side, b, a, mid)
            return

    # ── helpers ───────────────────────────────────────────────────────────────

    def _update_cache(self, token_id: str, best_bid: float, best_ask: float) -> None:
        self._ba_cache[token_id] = (best_bid, best_ask)
        side = self._token_to_side.get(token_id)
        if side:
            mid = (best_bid + best_ask) / 2.0
            self._emit(side, best_bid, best_ask, mid)

    def _emit(self, side: str, bid: float, ask: float, mid: float) -> None:
        """Emit a price update with bid, ask, and mid separated.

        - bid: best bid (used for sell-side reference)
        - ask: best ask (actual execution price when buying)
        - mid: (bid+ask)/2 used only for display and trigger signals
        """
        if mid > 0:
            self.on_price(side, round(bid, 6), round(ask, 6), round(mid, 6))
