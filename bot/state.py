"""Thread-safe shared state for Streak Snapper bot.

Simplified from the original multi-strategy state. Keeps only what Streak Snapper needs:
  - Trade data (in-memory + DB)
  - Price tracking (bid/ask/mid via WebSocket)
  - Live order book (from WS book events)
  - Streak Snapper configuration
  - Dashboard snapshot
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class Trade:
    """In-memory trade record (mirrors DB for fast access)."""

    window_slug: str
    window_ts: int
    side: str               # "UP" or "DOWN"
    token_id: str
    price: float
    shares: float
    cost: float
    mode: str               # "paper" or "real"
    opened_at: float        # unix
    id: int = 0
    status: str = "open"    # "open", "won", "lost"
    final_price: Optional[float] = None
    pnl: Optional[float] = None
    order_id: Optional[str] = None
    note: str = ""
    strategy: str = "ss_fade"  # "ss_fade" | "ss_trend"
    multiplier: float = 1.0    # martingale multiplier at entry
    limit_cap: float = 0.60    # max entry price


class BotState:
    """All mutable shared state lives here. Lock-protected."""

    MAX_LOG_LINES    = 500
    MAX_PRICE_HISTORY = 600

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started_at: float = time.time()

        # --- runtime-mutable config ---
        self.mode: str = "paper"
        self.has_credentials: bool = False
        self.starting_bankroll: float = 1000.0

        # ── Streak Snapper config ─────────────────────────────────────────────
        self.ss_enabled: bool  = True
        self.ss_mode: str      = "both"   # "fade" | "trend" | "both"

        # Forma 1 — Fade (anti-racha)
        self.ss_fade_base_shares: float   = 5.0
        self.ss_fade_limit_cap: float     = 0.60
        self.ss_fade_streak_min: int      = 4

        # Forma 2 — Trend (seguir tendencia 4h)
        self.ss_trend_base_shares: float  = 5.0
        self.ss_trend_limit_cap: float    = 0.52

        # Martingale global
        self.ss_martingale_mult_factor: float = 1.5

        # Runtime martingale state (synced with DB)
        self.ss_fade_martingale_mult: float  = 1.0
        self.ss_fade_loss_streak: int        = 0
        self.ss_trend_martingale_mult: float = 1.0
        self.ss_trend_loss_streak: int       = 0

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
        self.last_up_bid: Optional[float] = None
        self.last_up_ask: Optional[float] = None
        self.last_down_bid: Optional[float] = None
        self.last_down_ask: Optional[float] = None
        self.last_price_update: Optional[float] = None
        self.ws_connected: bool = False

        # --- spot price (BTC) ---
        self.spot_price: Optional[float] = None
        self.spot_price_updated_at: Optional[float] = None

        # --- Chainlink TWAP feed ---
        # Whole status dict from ChainlinkTwapFeed.status(), stored as-is so the
        # feed owns its own shape. Empty when the feed is off, which the
        # dashboard must render as "disabled", not as an error.
        self.cl_enabled: bool = False
        self.cl_status: Dict[str, object] = {}
        self.cl_divergence: Optional[float] = None

        # Runtime-editable Chainlink settings. Mirrored here so /settings can
        # display and change them; the feed itself reads the ones it needs at
        # startup, so toggling cl_twap_enabled takes effect on restart.
        self.cl_twap_enabled: bool = False
        self.cl_twap_window: str = "30"
        self.cl_twap_stale_seconds: float = 15.0
        self.cl_divergence_max: float = 0.0
        self.cl_record_ticks: bool = False

        # --- live order book (from WS book events) ---
        self.order_book: Dict[str, dict] = {}

        # --- trade data (in-memory cache) ---
        self.trades: List[Trade] = []
        self._next_trade_id: int = 1
        self.log: Deque[Dict[str, object]] = deque(maxlen=self.MAX_LOG_LINES)
        self.up_price_history: Deque[Dict[str, float]] = deque(maxlen=self.MAX_PRICE_HISTORY)
        self.down_price_history: Deque[Dict[str, float]] = deque(maxlen=self.MAX_PRICE_HISTORY)
        self.windows_observed: int = 0
        self.windows_traded: int = 0

    # ── config helpers ────────────────────────────────────────────────────────

    def configure(self, **kwargs) -> None:
        allowed = {
            "mode", "has_credentials", "starting_bankroll",
            "ss_enabled", "ss_mode",
            "ss_fade_base_shares", "ss_fade_limit_cap", "ss_fade_streak_min",
            "ss_trend_base_shares", "ss_trend_limit_cap",
            "ss_martingale_mult_factor",
            "cl_enabled",
            "cl_twap_enabled", "cl_twap_window", "cl_twap_stale_seconds",
            "cl_divergence_max", "cl_record_ticks",
        }
        with self._lock:
            for key, val in kwargs.items():
                if key in allowed:
                    setattr(self, key, val)

    def update_runtime_config(self, **kwargs) -> Dict[str, object]:
        allowed = {
            "mode", "starting_bankroll",
            "ss_enabled", "ss_mode",
            "ss_fade_base_shares", "ss_fade_limit_cap", "ss_fade_streak_min",
            "ss_trend_base_shares", "ss_trend_limit_cap",
            "ss_martingale_mult_factor",
            "cl_twap_enabled", "cl_twap_window", "cl_twap_stale_seconds",
            "cl_divergence_max", "cl_record_ticks",
        }
        accepted: Dict[str, object] = {}
        with self._lock:
            for key, val in kwargs.items():
                if key in allowed:
                    setattr(self, key, val)
                    accepted[key] = val
        return accepted

    # ── status helpers ────────────────────────────────────────────────────────

    def set_status(self, status: str, message: str = "") -> None:
        with self._lock:
            self.bot_status = status
            self.bot_message = message

    def set_window(self, slug: str, ts: int, window_duration: int = 300) -> None:
        with self._lock:
            if slug != self.current_slug:
                self.windows_observed += 1
            self.current_slug = slug
            self.current_window_ts = ts
            self.current_window_ends_at = float(ts + window_duration)
            self.up_token_id = None
            self.down_token_id = None
            self.last_up_price = None
            self.last_down_price = None
            self.last_up_bid = None
            self.last_up_ask = None
            self.last_down_bid = None
            self.last_down_ask = None
            self.up_price_history.clear()
            self.down_price_history.clear()

    def set_tokens(self, up_token_id: str, down_token_id: str) -> None:
        with self._lock:
            self.up_token_id = up_token_id
            self.down_token_id = down_token_id

    def update_price(self, side: str, bid: float, ask: float, mid: float) -> None:
        now = time.time()
        with self._lock:
            self.last_price_update = now
            entry = {"t": now, "p": mid, "bid": bid, "ask": ask}
            if side == "UP":
                self.last_up_price = mid
                self.last_up_bid   = bid
                self.last_up_ask   = ask
                self.up_price_history.append(entry)
            elif side == "DOWN":
                self.last_down_price = mid
                self.last_down_bid   = bid
                self.last_down_ask   = ask
                self.down_price_history.append(entry)

    def get_prices(self) -> Tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self.last_up_price, self.last_down_price

    def get_asks(self) -> Tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self.last_up_ask, self.last_down_ask

    def get_bids(self) -> Tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self.last_up_bid, self.last_down_bid

    def update_spot_price(self, price: float) -> None:
        with self._lock:
            self.spot_price = price
            self.spot_price_updated_at = time.time()

    def set_chainlink_status(
        self, status: Dict[str, object], divergence: Optional[float] = None
    ) -> None:
        """Publish the Chainlink feed's own status dict for the dashboard.

        Stored verbatim rather than unpacked into fields: the feed's shape is
        expected to shift while the topics are still in soak testing, and the
        dashboard renders whatever arrives.
        """
        with self._lock:
            self.cl_status = dict(status)
            self.cl_divergence = divergence

    def set_ws_connected(self, connected: bool) -> None:
        with self._lock:
            self.ws_connected = connected

    # ── order book ────────────────────────────────────────────────────────────

    def update_order_book(self, side: str, bids: list, asks: list) -> None:
        """Update live order book data from WebSocket book events."""
        with self._lock:
            total_volume = 0.0
            for lvl in bids:
                if isinstance(lvl, dict):
                    try:
                        total_volume += float(lvl.get("size", 0))
                    except (TypeError, ValueError):
                        pass
            for lvl in asks:
                if isinstance(lvl, dict):
                    try:
                        total_volume += float(lvl.get("size", 0))
                    except (TypeError, ValueError):
                        pass
            self.order_book[side] = {
                "bids": bids[:10],
                "asks": asks[:10],
                "volume": round(total_volume, 2),
                "updated_at": time.time(),
            }

    # ── trade helpers ─────────────────────────────────────────────────────────

    def add_trade(self, trade: Trade) -> Trade:
        with self._lock:
            if trade.id == 0:
                trade.id = self._next_trade_id
                self._next_trade_id += 1
            else:
                # Keep _next_trade_id ahead of manually assigned ids
                self._next_trade_id = max(self._next_trade_id, trade.id + 1)
            self.trades.append(trade)
            self.windows_traded += 1
            return trade

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

    def load_trades_from_db(self, db_trades: list) -> int:
        """Pre-load trades from DB into in-memory cache (survives restarts)."""
        with self._lock:
            count = 0
            for db_trade in db_trades:
                # Avoid duplicates
                if any(t.id == db_trade.get("id") for t in self.trades):
                    continue
                mem = Trade(
                    id=db_trade.get("id", 0),
                    window_slug=db_trade.get("window_slug", ""),
                    window_ts=db_trade.get("window_ts", 0),
                    side=db_trade.get("direction", ""),
                    token_id=db_trade.get("token_id", ""),
                    price=db_trade.get("entry_price", 0),
                    shares=db_trade.get("shares", 0),
                    cost=db_trade.get("cost", 0),
                    mode=db_trade.get("mode", "paper"),
                    opened_at=time.time(),  # we don't need exact opened_at in memory
                    status=db_trade.get("status", "open"),
                    pnl=db_trade.get("pnl"),
                    strategy=db_trade.get("strategy", "ss_fade"),
                    multiplier=db_trade.get("multiplier", 1.0),
                    limit_cap=db_trade.get("limit_cap", 0.60),
                    note=db_trade.get("note", ""),
                )
                if mem.id > 0:
                    self._next_trade_id = max(self._next_trade_id, mem.id + 1)
                self.trades.append(mem)
                count += 1
            return count

    def log_event(self, level: str, message: str) -> None:
        entry = {"t": time.time(), "level": level, "message": message}
        with self._lock:
            self.log.append(entry)

    # ── real-mode readiness ───────────────────────────────────────────────────

    def real_mode_readiness(self) -> Dict[str, object]:
        pk = bool((os.getenv("PRIVATE_KEY") or "").strip())
        pw = bool((os.getenv("PROXY_WALLET") or "").strip())
        ready = pk and pw
        missing = []
        if not pk:
            missing.append("PRIVATE_KEY")
        if not pw:
            missing.append("PROXY_WALLET")
        return {
            "ready": ready,
            "has_private_key": pk,
            "has_proxy_wallet": pw,
            "missing": missing,
        }

    # ── snapshot for dashboard ────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            wins   = sum(1 for t in self.trades if t.status == "won")
            losses = sum(1 for t in self.trades if t.status == "lost")
            open_count = sum(1 for t in self.trades if t.status == "open")
            resolved_pnl = sum((t.pnl or 0.0) for t in self.trades if t.status in ("won", "lost"))
            total_invested = sum(t.cost for t in self.trades)
            resolved = wins + losses
            win_rate = (wins / resolved) if resolved else 0.0
            roi = (resolved_pnl / total_invested) if total_invested else 0.0
            bankroll = self.starting_bankroll + resolved_pnl
            # Cash tied up in positions that haven't resolved. Spent already, but
            # not yet reflected in resolved_pnl — so `bankroll` alone looks
            # unchanged right after opening a trade.
            committed = sum(t.cost for t in self.trades if t.status == "open")
            now = time.time()

            # Per-strategy stats
            fade_trades = [t for t in self.trades if t.strategy == "ss_fade"]
            trend_trades = [t for t in self.trades if t.strategy == "ss_trend"]

            def _strat_stats(tlist):
                w = sum(1 for t in tlist if t.status == "won")
                l = sum(1 for t in tlist if t.status == "lost")
                r = w + l
                return {
                    "trades": len(tlist),
                    "wins": w,
                    "losses": l,
                    "win_rate": (w / r) if r else 0.0,
                    "pnl": sum((t.pnl or 0.0) for t in tlist if t.status in ("won", "lost")),
                }

            # Order book snapshot
            ob = {}
            for side in ("UP", "DOWN"):
                if side in self.order_book:
                    entry = self.order_book[side]
                    ob[side] = {
                        "bids": entry["bids"],
                        "asks": entry["asks"],
                        "volume": entry["volume"],
                        "updated_at": entry["updated_at"],
                    }

            return {
                "mode": self.mode,
                "has_credentials": self.has_credentials,
                # SS config
                "ss_enabled": self.ss_enabled,
                "ss_mode": self.ss_mode,
                "ss_fade_base_shares": self.ss_fade_base_shares,
                "ss_fade_limit_cap": self.ss_fade_limit_cap,
                "ss_fade_streak_min": self.ss_fade_streak_min,
                "ss_trend_base_shares": self.ss_trend_base_shares,
                "ss_trend_limit_cap": self.ss_trend_limit_cap,
                "ss_martingale_mult_factor": self.ss_martingale_mult_factor,
                # Runtime martingale
                "ss_fade_martingale_mult": self.ss_fade_martingale_mult,
                "ss_fade_loss_streak": self.ss_fade_loss_streak,
                "ss_trend_martingale_mult": self.ss_trend_martingale_mult,
                "ss_trend_loss_streak": self.ss_trend_loss_streak,
                # General
                "starting_bankroll": self.starting_bankroll,
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
                "last_up_bid": self.last_up_bid,
                "last_up_ask": self.last_up_ask,
                "last_down_bid": self.last_down_bid,
                "last_down_ask": self.last_down_ask,
                "last_price_update": self.last_price_update,
                "spot_price": self.spot_price,
                "chainlink": {
                    "enabled": self.cl_enabled,
                    "divergence": self.cl_divergence,
                    **self.cl_status,
                },
                "cl_twap_enabled": self.cl_twap_enabled,
                "cl_twap_window": self.cl_twap_window,
                "cl_twap_stale_seconds": self.cl_twap_stale_seconds,
                "cl_divergence_max": self.cl_divergence_max,
                "cl_record_ticks": self.cl_record_ticks,
                "real_mode_readiness": self.real_mode_readiness(),
                "order_book": ob,
                "stats": {
                    "trades": len(self.trades),
                    "open": open_count,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": win_rate,
                    "resolved_pnl": resolved_pnl,
                    "total_invested": total_invested,
                    "roi": roi,
                    "starting_bankroll": self.starting_bankroll,
                    "bankroll": bankroll,
                    "committed": committed,
                    "available": bankroll - committed,
                    "windows_observed": self.windows_observed,
                    "windows_traded": self.windows_traded,
                    "uptime_seconds": now - self.started_at,
                },
                "strategy_stats": {
                    "ss_fade": _strat_stats(fade_trades),
                    "ss_trend": _strat_stats(trend_trades),
                },
                "trades": [
                    {
                        "id": t.id,
                        "window_slug": t.window_slug,
                        "direction": t.side,
                        "price": t.price,
                        "shares": t.shares,
                        "cost": t.cost,
                        "mode": t.mode,
                        "status": t.status,
                        "pnl": t.pnl,
                        "strategy": t.strategy,
                        "multiplier": t.multiplier,
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


# ── Global state (single market: BTC) ────────────────────────────────────────

STATE = BotState()
STATES = {"btc": STATE}   # Kept as dict for backward-compat with dashboard
