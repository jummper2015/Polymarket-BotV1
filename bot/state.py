"""Thread-safe shared state for bot metrics and dashboard."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional


@dataclass
class Trade:
    id: int
    window_slug: str
    window_ts: int
    side: str  # "UP" or "DOWN"
    token_id: str
    price: float
    shares: float
    cost: float
    mode: str  # "paper" or "real"
    opened_at: float  # unix
    status: str = "open"  # "open", "won", "lost", "expired"
    final_price: Optional[float] = None
    pnl: Optional[float] = None
    order_id: Optional[str] = None
    note: str = ""


class BotState:
    """All mutable shared state lives here. Lock-protected."""

    MAX_LOG_LINES = 500
    MAX_PRICE_HISTORY = 600  # ~30s at 50ms

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started_at: float = time.time()
        self.mode: str = "paper"
        self.has_credentials: bool = False
        self.trigger_price: float = 0.0
        self.buy_amount: float = 0.0
        self.starting_bankroll: float = 0.0
        self.bot_status: str = "idle"  # idle, loading_market, watching, traded, error
        self.bot_message: str = ""
        self.current_slug: Optional[str] = None
        self.current_window_ts: Optional[int] = None
        self.current_window_ends_at: Optional[float] = None
        self.up_token_id: Optional[str] = None
        self.down_token_id: Optional[str] = None
        self.last_up_price: Optional[float] = None
        self.last_down_price: Optional[float] = None
        self.last_price_update: Optional[float] = None
        self.ws_connected: bool = False
        self.trades: List[Trade] = []
        self._next_trade_id: int = 1
        self.log: Deque[Dict[str, object]] = deque(maxlen=self.MAX_LOG_LINES)
        self.up_price_history: Deque[Dict[str, float]] = deque(maxlen=self.MAX_PRICE_HISTORY)
        self.down_price_history: Deque[Dict[str, float]] = deque(maxlen=self.MAX_PRICE_HISTORY)
        self.windows_observed: int = 0
        self.windows_traded: int = 0

    # ----- helpers -----

    def configure(
        self,
        mode: str,
        has_credentials: bool,
        trigger_price: float,
        buy_amount: float,
        starting_bankroll: float,
    ) -> None:
        with self._lock:
            self.mode = mode
            self.has_credentials = has_credentials
            self.trigger_price = trigger_price
            self.buy_amount = buy_amount
            self.starting_bankroll = starting_bankroll

    def set_status(self, status: str, message: str = "") -> None:
        with self._lock:
            self.bot_status = status
            self.bot_message = message

    def set_window(self, slug: str, ts: int) -> None:
        with self._lock:
            if slug != self.current_slug:
                self.windows_observed += 1
            self.current_slug = slug
            self.current_window_ts = ts
            self.current_window_ends_at = float(ts + 300)
            self.up_token_id = None
            self.down_token_id = None
            self.last_up_price = None
            self.last_down_price = None
            self.up_price_history.clear()
            self.down_price_history.clear()

    def set_tokens(self, up_token_id: str, down_token_id: str) -> None:
        with self._lock:
            self.up_token_id = up_token_id
            self.down_token_id = down_token_id

    def update_price(self, side: str, price: float) -> None:
        now = time.time()
        with self._lock:
            self.last_price_update = now
            entry = {"t": now, "p": price}
            if side == "UP":
                self.last_up_price = price
                self.up_price_history.append(entry)
            elif side == "DOWN":
                self.last_down_price = price
                self.down_price_history.append(entry)

    def set_ws_connected(self, connected: bool) -> None:
        with self._lock:
            self.ws_connected = connected

    def add_trade(self, trade: Trade) -> Trade:
        with self._lock:
            trade.id = self._next_trade_id
            self._next_trade_id += 1
            self.trades.append(trade)
            self.windows_traded += 1
            return trade

    def find_open_trade_for_window(self, slug: str) -> Optional[Trade]:
        with self._lock:
            for t in self.trades:
                if t.window_slug == slug and t.status == "open":
                    return t
        return None

    def resolve_trade(self, trade_id: int, status: str, final_price: float, pnl: float, note: str = "") -> None:
        with self._lock:
            for t in self.trades:
                if t.id == trade_id:
                    t.status = status
                    t.final_price = final_price
                    t.pnl = pnl
                    if note:
                        t.note = note
                    break

    def log_event(self, level: str, message: str) -> None:
        entry = {"t": time.time(), "level": level, "message": message}
        with self._lock:
            self.log.append(entry)

    # ----- snapshots for the dashboard -----

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            wins = sum(1 for t in self.trades if t.status == "won")
            losses = sum(1 for t in self.trades if t.status == "lost")
            open_count = sum(1 for t in self.trades if t.status == "open")
            resolved_pnl = sum((t.pnl or 0.0) for t in self.trades if t.status in ("won", "lost", "expired"))
            total_invested = sum(t.cost for t in self.trades)
            total_won = sum((t.pnl or 0.0) + t.cost for t in self.trades if t.status == "won")
            resolved = wins + losses
            win_rate = (wins / resolved) if resolved else 0.0
            roi = (resolved_pnl / total_invested) if total_invested else 0.0
            open_cost = sum(t.cost for t in self.trades if t.status == "open")
            bankroll = self.starting_bankroll + resolved_pnl
            available_cash = bankroll - open_cost
            now = time.time()
            return {
                "mode": self.mode,
                "has_credentials": self.has_credentials,
                "trigger_price": self.trigger_price,
                "buy_amount": self.buy_amount,
                "bot_status": self.bot_status,
                "bot_message": self.bot_message,
                "ws_connected": self.ws_connected,
                "current_slug": self.current_slug,
                "current_window_ts": self.current_window_ts,
                "current_window_ends_at": self.current_window_ends_at,
                "seconds_remaining": (
                    max(0.0, self.current_window_ends_at - now) if self.current_window_ends_at else None
                ),
                "up_token_id": self.up_token_id,
                "down_token_id": self.down_token_id,
                "last_up_price": self.last_up_price,
                "last_down_price": self.last_down_price,
                "last_price_update": self.last_price_update,
                "starting_bankroll": self.starting_bankroll,
                "stats": {
                    "trades": len(self.trades),
                    "open": open_count,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": win_rate,
                    "resolved_pnl": resolved_pnl,
                    "total_invested": total_invested,
                    "total_won": total_won,
                    "roi": roi,
                    "starting_bankroll": self.starting_bankroll,
                    "bankroll": bankroll,
                    "available_cash": available_cash,
                    "open_cost": open_cost,
                    "windows_observed": self.windows_observed,
                    "windows_traded": self.windows_traded,
                    "uptime_seconds": now - self.started_at,
                },
                "trades": [
                    {
                        "id": t.id,
                        "window_slug": t.window_slug,
                        "window_ts": t.window_ts,
                        "side": t.side,
                        "price": t.price,
                        "shares": t.shares,
                        "cost": t.cost,
                        "mode": t.mode,
                        "opened_at": t.opened_at,
                        "status": t.status,
                        "final_price": t.final_price,
                        "pnl": t.pnl,
                        "order_id": t.order_id,
                        "note": t.note,
                    }
                    for t in reversed(self.trades[-100:])
                ],
                "log": list(self.log)[-100:],
                "price_history": {
                    "up": list(self.up_price_history),
                    "down": list(self.down_price_history),
                },
            }


STATE = BotState()
