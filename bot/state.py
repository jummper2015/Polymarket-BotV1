"""Thread-safe shared state for bot metrics and dashboard."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
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
    status: str = "open"  # "open", "won", "lost", "expired", "sold"
    final_price: Optional[float] = None
    pnl: Optional[float] = None
    order_id: Optional[str] = None
    note: str = ""
    is_hedge: bool = False
    strategy: str = "trigger"  # "trigger" or "mm"


class BotState:
    """All mutable shared state lives here. Lock-protected."""

    MAX_LOG_LINES = 500
    MAX_PRICE_HISTORY = 600

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started_at: float = time.time()

        # --- runtime-mutable config ---
        self.mode: str = "paper"
        self.has_credentials: bool = False
        self.trigger_price: float = 0.90
        self.buy_amount: float = 5.0
        self.starting_bankroll: float = 1000.0
        self.max_trades_per_window: int = 1
        self.hedge_threshold: float = 0.96
        self.last_minute_seconds: int = 60

        # --- active strategy + market-making config ---
        self.active_strategy: str = "trigger"
        self.mm_shares: float = 20.0
        self.mm_last_seconds: int = 30
        self.mm_max_price: float = 0.95

        # --- bot status ---
        self.bot_status: str = "idle"
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

        # --- spot price (from CoinGecko, per market asset) ---
        self.spot_price: Optional[float] = None
        self.spot_price_updated_at: Optional[float] = None

        # --- trade data ---
        self.trades: List[Trade] = []
        self._next_trade_id: int = 1
        self.log: Deque[Dict[str, object]] = deque(maxlen=self.MAX_LOG_LINES)
        self.up_price_history: Deque[Dict[str, float]] = deque(maxlen=self.MAX_PRICE_HISTORY)
        self.down_price_history: Deque[Dict[str, float]] = deque(maxlen=self.MAX_PRICE_HISTORY)
        self.windows_observed: int = 0
        self.windows_traded: int = 0

    # ----- config helpers -----

    def configure(
        self,
        mode: str,
        has_credentials: bool,
        trigger_price: float,
        buy_amount: float,
        starting_bankroll: float,
        max_trades_per_window: int = 1,
        hedge_threshold: float = 0.96,
        last_minute_seconds: int = 60,
    ) -> None:
        with self._lock:
            self.mode = mode
            self.has_credentials = has_credentials
            self.trigger_price = trigger_price
            self.buy_amount = buy_amount
            self.starting_bankroll = starting_bankroll
            self.max_trades_per_window = max_trades_per_window
            self.hedge_threshold = hedge_threshold
            self.last_minute_seconds = last_minute_seconds

    def update_runtime_config(self, **kwargs) -> Dict[str, object]:
        allowed = {
            "trigger_price", "buy_amount", "max_trades_per_window",
            "mode", "hedge_threshold", "last_minute_seconds",
            "active_strategy", "mm_shares", "mm_last_seconds", "mm_max_price",
            "starting_bankroll",
        }
        accepted: Dict[str, object] = {}
        with self._lock:
            for key, val in kwargs.items():
                if key in allowed:
                    setattr(self, key, val)
                    accepted[key] = val
        return accepted

    # ----- status helpers -----

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

    def update_spot_price(self, price: float) -> None:
        with self._lock:
            self.spot_price = price
            self.spot_price_updated_at = time.time()

    # Keep old method name as alias for compatibility
    def update_btc_price(self, price: float) -> None:
        self.update_spot_price(price)

    def set_ws_connected(self, connected: bool) -> None:
        with self._lock:
            self.ws_connected = connected

    # ----- trade helpers -----

    def add_trade(self, trade: Trade) -> Trade:
        with self._lock:
            trade.id = self._next_trade_id
            self._next_trade_id += 1
            already_traded = any(
                t for t in self.trades if t.window_slug == trade.window_slug
            )
            self.trades.append(trade)
            if not already_traded:
                self.windows_traded += 1
            return trade

    def find_open_trade_for_window(self, slug: str) -> Optional[Trade]:
        with self._lock:
            for t in self.trades:
                if t.window_slug == slug and t.status == "open" and not t.is_hedge:
                    return t
        return None

    def count_initial_trades_for_window(self, slug: str) -> int:
        with self._lock:
            return sum(
                1 for t in self.trades
                if t.window_slug == slug and not t.is_hedge
            )

    def has_initial_trade_for_side(self, slug: str, side: str) -> bool:
        with self._lock:
            return any(
                t for t in self.trades
                if t.window_slug == slug and t.side == side and not t.is_hedge
            )

    def find_hedge_for_window(self, slug: str) -> Optional[Trade]:
        with self._lock:
            for t in self.trades:
                if t.window_slug == slug and t.is_hedge:
                    return t
        return None

    def find_all_open_trades_for_window(self, slug: str) -> List[Trade]:
        with self._lock:
            return [t for t in self.trades if t.window_slug == slug and t.status == "open"]

    def count_mm_trades_for_window(self, slug: str) -> int:
        with self._lock:
            return sum(
                1 for t in self.trades
                if t.window_slug == slug and t.strategy == "mm"
            )

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

    # ----- real-mode readiness -----

    def real_mode_readiness(self) -> Dict[str, object]:
        pk = bool((os.getenv("PRIVATE_KEY") or "").strip())
        pw = bool((os.getenv("PROXY_WALLET") or "").strip())
        ready = pk and pw
        missing = []
        steps = []
        if not pk:
            missing.append("PRIVATE_KEY")
            steps.append({
                "done": False,
                "text": "Agregar PRIVATE_KEY como secret de Replit",
                "detail": "Ve a Secrets → Agregar secreto: PRIVATE_KEY = tu clave privada de la wallet de Polygon (Polymarket).",
            })
        if not pw:
            missing.append("PROXY_WALLET")
            steps.append({
                "done": False,
                "text": "Agregar PROXY_WALLET como secret de Replit",
                "detail": "Ve a Secrets → Agregar secreto: PROXY_WALLET = dirección del proxy de Polymarket (empieza con 0x).",
            })
        steps.append({
            "done": ready and self.mode == "real",
            "text": "Cambiar modo a 'real' en Configuración y reiniciar el bot",
            "detail": "En el panel de Configuración selecciona modo Real y pulsa Aplicar. Luego reinicia el workflow.",
        })
        return {
            "ready": ready,
            "has_private_key": pk,
            "has_proxy_wallet": pw,
            "missing": missing,
            "steps": steps,
        }

    # ----- snapshot for the dashboard -----

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            wins = sum(1 for t in self.trades if t.status == "won")
            losses = sum(1 for t in self.trades if t.status == "lost")
            open_count = sum(1 for t in self.trades if t.status == "open")
            resolved_pnl = sum((t.pnl or 0.0) for t in self.trades if t.status in ("won", "lost", "expired", "sold"))
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
                "max_trades_per_window": self.max_trades_per_window,
                "hedge_threshold": self.hedge_threshold,
                "last_minute_seconds": self.last_minute_seconds,
                "active_strategy": self.active_strategy,
                "mm_shares": self.mm_shares,
                "mm_last_seconds": self.mm_last_seconds,
                "mm_max_price": self.mm_max_price,
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
                "spot_price": self.spot_price,
                "spot_price_updated_at": self.spot_price_updated_at,
                "real_mode_readiness": self.real_mode_readiness(),
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
                        "is_hedge": t.is_hedge,
                        "strategy": t.strategy,
                    }
                    for t in reversed(self.trades[-100:])
                ],
                "log": list(self.log)[-100:],
                "price_history": {
                    "up": list(self.up_price_history),
                    "down": list(self.down_price_history),
                },
            }


STATES = {
    "btc": BotState(),
    "sol": BotState(),
    "eth": BotState(),
}
STATE = STATES["btc"]  # backward compat alias
