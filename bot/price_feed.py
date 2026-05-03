"""WebSocket price feed for the Polymarket CLOB."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, List, Optional

import websocket  # websocket-client

from . import logger


PriceCallback = Callable[[str, float], None]  # (side, mid_price)


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _best_from_levels(levels) -> Optional[List[float]]:
    """Return parsed prices from a list of {price, size} levels, or None."""
    if not isinstance(levels, list) or not levels:
        return None
    prices: List[float] = []
    for lvl in levels:
        p = _safe_float(lvl.get("price")) if isinstance(lvl, dict) else None
        if p is not None:
            prices.append(p)
    if not prices:
        return None
    return prices


def _mid_from_book(item: dict) -> Optional[float]:
    bids = item.get("bids")
    asks = item.get("asks")
    bid_prices = _best_from_levels(bids)
    ask_prices = _best_from_levels(asks)
    if not bid_prices or not ask_prices:
        return None
    best_bid = max(bid_prices)
    best_ask = min(ask_prices)
    if best_bid <= 0 or best_ask <= 0:
        return None
    return (best_bid + best_ask) / 2.0


def _mid_from_pair(item: dict) -> Optional[float]:
    bid = _safe_float(item.get("best_bid"))
    ask = _safe_float(item.get("best_ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


class PriceFeed:
    """WebSocket subscription with auto-reconnect (3s)."""

    def __init__(
        self,
        ws_url: str,
        up_token_id: str,
        down_token_id: str,
        on_price: PriceCallback,
        on_status: Callable[[bool], None] = lambda _connected: None,
        reconnect_seconds: float = 3.0,
    ) -> None:
        self.ws_url = ws_url
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.on_price = on_price
        self.on_status = on_status
        self.reconnect_seconds = reconnect_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._token_to_side = {up_token_id: "UP", down_token_id: "DOWN"}

    # ----- lifecycle -----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="price-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ----- internals -----

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warn(f"price feed crashed: {exc}; reconnecting in {self.reconnect_seconds}s")
            self.on_status(False)
            if self._stop.is_set():
                break
            time.sleep(self.reconnect_seconds)

    def _on_open(self, ws) -> None:
        logger.ok("price feed connected", icon="🔌")
        self.on_status(True)
        sub = {
            "assets_ids": [self.up_token_id, self.down_token_id],
            "type": "market",
            "custom_feature_enabled": True,
        }
        try:
            ws.send(json.dumps(sub))
        except Exception as exc:
            logger.warn(f"failed to send subscription: {exc}")

    def _on_close(self, ws, status_code, msg) -> None:
        self.on_status(False)
        logger.warn(f"price feed disconnected (code={status_code}); reconnecting in {self.reconnect_seconds}s")

    def _on_error(self, ws, error) -> None:
        logger.warn(f"price feed error: {error}")

    def _on_message(self, ws, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(payload, list):
            for item in payload:
                self._dispatch(item)
        elif isinstance(payload, dict):
            self._dispatch(payload)

    def _dispatch(self, item: dict) -> None:
        if not isinstance(item, dict):
            return
        event_type = item.get("event_type") or item.get("type")
        if event_type == "book":
            mid = _mid_from_book(item)
            asset = item.get("asset_id") or item.get("market") or item.get("token_id")
            side = self._token_to_side.get(str(asset))
            if side and mid is not None:
                self.on_price(side, mid)
            return
        if event_type == "price_change":
            changes = item.get("price_changes") or []
            if isinstance(changes, list):
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    mid = _mid_from_pair(change)
                    asset = change.get("asset_id") or change.get("market") or change.get("token_id") or item.get("asset_id")
                    side = self._token_to_side.get(str(asset))
                    if side and mid is not None:
                        self.on_price(side, mid)
            return
        if event_type == "best_bid_ask":
            mid = _mid_from_pair(item)
            asset = item.get("asset_id") or item.get("market") or item.get("token_id")
            side = self._token_to_side.get(str(asset))
            if side and mid is not None:
                self.on_price(side, mid)
            return
