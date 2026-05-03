"""Flask web dashboard exposing live bot metrics and config controls."""
from __future__ import annotations

import threading
import time

import requests as _requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from . import logger
from .state import STATES

_PRICE_FETCH_INTERVAL = 10  # seconds between CoinGecko polls

_COINGECKO_IDS = {
    "btc": "bitcoin",
    "sol": "solana",
    "eth": "ethereum",
}

_MARKET_LABELS = {
    "btc": "Bitcoin",
    "sol": "Solana",
    "eth": "Ethereum",
}


def _prices_loop() -> None:
    ids = ",".join(_COINGECKO_IDS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    while True:
        try:
            r = _requests.get(url, timeout=8)
            if r.ok:
                data = r.json()
                for sym, coin_id in _COINGECKO_IDS.items():
                    if coin_id in data:
                        price = float(data[coin_id]["usd"])
                        STATES[sym].update_spot_price(price)
        except Exception:
            pass
        time.sleep(_PRICE_FETCH_INTERVAL)


def start_price_fetcher() -> None:
    t = threading.Thread(target=_prices_loop, name="price-fetcher", daemon=True)
    t.start()


def start_btc_fetcher() -> None:
    start_price_fetcher()


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    CORS(app)

    @app.after_request
    def _no_cache(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/")
    def index():
        return render_template("dashboard.html")

    @app.get("/settings")
    def settings():
        return render_template("settings.html")

    @app.get("/state")
    def get_state():
        snapshots = {sym: st.snapshot() for sym, st in STATES.items()}
        markets_data = {}
        all_logs = []

        for sym, snap in snapshots.items():
            markets_data[sym] = {
                "symbol": sym,
                "label": _MARKET_LABELS[sym],
                "bot_status": snap["bot_status"],
                "bot_message": snap["bot_message"],
                "ws_connected": snap["ws_connected"],
                "current_slug": snap["current_slug"],
                "seconds_remaining": snap["seconds_remaining"],
                "last_up_price": snap["last_up_price"],
                "last_down_price": snap["last_down_price"],
                "spot_price": snap["spot_price"],
                "stats": snap["stats"],
                "strategy_stats": snap["strategy_stats"],
                "trades": snap["trades"],
                "price_history": snap["price_history"],
                "market_enabled": snap["market_enabled"],
            }
            for entry in snap["log"]:
                all_logs.append({"market": sym, **entry})

        all_logs.sort(key=lambda x: x["t"])
        all_logs = all_logs[-150:]

        btc_snap = snapshots["btc"]

        wins = sum(snapshots[s]["stats"]["wins"] for s in STATES)
        losses = sum(snapshots[s]["stats"]["losses"] for s in STATES)
        trades = sum(snapshots[s]["stats"]["trades"] for s in STATES)
        open_count = sum(snapshots[s]["stats"]["open"] for s in STATES)
        resolved_pnl = sum(snapshots[s]["stats"]["resolved_pnl"] for s in STATES)
        total_invested = sum(snapshots[s]["stats"]["total_invested"] for s in STATES)
        open_cost = sum(snapshots[s]["stats"]["open_cost"] for s in STATES)
        resolved = wins + losses
        win_rate = (wins / resolved) if resolved else 0.0
        roi = (resolved_pnl / total_invested) if total_invested else 0.0
        starting_bankroll = btc_snap["starting_bankroll"]
        bankroll = starting_bankroll + resolved_pnl
        available_cash = bankroll - open_cost

        # Combined per-strategy stats across all markets
        combined_strategy_stats = {}
        for strat in ("trigger", "mm", "early_entry"):
            strat_wins = sum(snapshots[s]["strategy_stats"][strat]["wins"] for s in STATES)
            strat_losses = sum(snapshots[s]["strategy_stats"][strat]["losses"] for s in STATES)
            strat_trades = sum(snapshots[s]["strategy_stats"][strat]["trades"] for s in STATES)
            strat_pnl = sum(snapshots[s]["strategy_stats"][strat]["pnl"] for s in STATES)
            strat_invested = sum(snapshots[s]["strategy_stats"][strat]["invested"] for s in STATES)
            strat_resolved = strat_wins + strat_losses
            combined_strategy_stats[strat] = {
                "trades": strat_trades,
                "wins": strat_wins,
                "losses": strat_losses,
                "win_rate": (strat_wins / strat_resolved) if strat_resolved else 0.0,
                "pnl": strat_pnl,
                "invested": strat_invested,
                "roi": (strat_pnl / strat_invested) if strat_invested else 0.0,
            }

        return jsonify({
            "markets": markets_data,
            "config": {
                "mode": btc_snap["mode"],
                "has_credentials": btc_snap["has_credentials"],
                "trigger_price": btc_snap["trigger_price"],
                "buy_amount": btc_snap["buy_amount"],
                "max_trades_per_window": btc_snap["max_trades_per_window"],
                "hedge_threshold": btc_snap["hedge_threshold"],
                "last_minute_seconds": btc_snap["last_minute_seconds"],
                "active_strategy": btc_snap["active_strategy"],
                "mm_shares": btc_snap["mm_shares"],
                "mm_last_seconds": btc_snap["mm_last_seconds"],
                "mm_max_price": btc_snap["mm_max_price"],
                "early_entry_enabled": btc_snap["early_entry_enabled"],
                "starting_bankroll": btc_snap["starting_bankroll"],
            },
            "combined_stats": {
                "wins": wins,
                "losses": losses,
                "trades": trades,
                "open": open_count,
                "win_rate": win_rate,
                "resolved_pnl": resolved_pnl,
                "total_invested": total_invested,
                "roi": roi,
                "starting_bankroll": starting_bankroll,
                "bankroll": bankroll,
                "available_cash": available_cash,
                "uptime_seconds": btc_snap["stats"]["uptime_seconds"],
            },
            "combined_strategy_stats": combined_strategy_stats,
            "log": all_logs,
            "real_mode_readiness": btc_snap["real_mode_readiness"],
        })

    @app.post("/config")
    def update_config():
        data = request.get_json(force=True, silent=True) or {}
        updates: dict = {}

        if "trigger_price" in data:
            try:
                v = float(data["trigger_price"])
                if 0.01 <= v <= 0.99:
                    updates["trigger_price"] = v
            except (TypeError, ValueError):
                pass

        if "buy_amount" in data:
            try:
                v = float(data["buy_amount"])
                if 0.50 <= v <= 100_000:
                    updates["buy_amount"] = v
            except (TypeError, ValueError):
                pass

        if "max_trades_per_window" in data:
            try:
                v = int(data["max_trades_per_window"])
                if 1 <= v <= 10:
                    updates["max_trades_per_window"] = v
            except (TypeError, ValueError):
                pass

        if "hedge_threshold" in data:
            try:
                v = float(data["hedge_threshold"])
                if 0.50 <= v <= 0.99:
                    updates["hedge_threshold"] = v
            except (TypeError, ValueError):
                pass

        if "last_minute_seconds" in data:
            try:
                v = int(data["last_minute_seconds"])
                if 10 <= v <= 240:
                    updates["last_minute_seconds"] = v
            except (TypeError, ValueError):
                pass

        if "active_strategy" in data:
            v = str(data["active_strategy"]).lower()
            if v in ("trigger", "market_making", "both"):
                updates["active_strategy"] = v

        if "mm_shares" in data:
            try:
                v = float(data["mm_shares"])
                if 1 <= v <= 100_000:
                    updates["mm_shares"] = v
            except (TypeError, ValueError):
                pass

        if "mm_last_seconds" in data:
            try:
                v = int(data["mm_last_seconds"])
                if 5 <= v <= 120:
                    updates["mm_last_seconds"] = v
            except (TypeError, ValueError):
                pass

        if "mm_max_price" in data:
            try:
                v = float(data["mm_max_price"])
                if 0.50 <= v <= 0.99:
                    updates["mm_max_price"] = v
            except (TypeError, ValueError):
                pass

        if "starting_bankroll" in data:
            try:
                v = float(data["starting_bankroll"])
                if 1.0 <= v <= 10_000_000:
                    updates["starting_bankroll"] = v
            except (TypeError, ValueError):
                pass

        if "early_entry_enabled" in data:
            updates["early_entry_enabled"] = bool(data["early_entry_enabled"])

        if "mode" in data:
            v = str(data["mode"]).lower()
            if v in ("paper", "real"):
                if v == "real" and not STATES["btc"].has_credentials:
                    rmr = STATES["btc"].real_mode_readiness()
                    return jsonify({
                        "ok": False,
                        "error": "Credenciales incompletas para modo real",
                        "readiness": rmr,
                    }), 400
                updates["mode"] = v

        accepted = {}
        for st in STATES.values():
            accepted = st.update_runtime_config(**updates)
        logger.info(f"config updated via dashboard: {accepted}", icon="⚙")
        return jsonify({"ok": True, "updated": accepted})

    @app.post("/toggle-market/<sym>")
    def toggle_market(sym: str):
        sym = sym.lower()
        if sym not in STATES:
            return jsonify({"ok": False, "error": "Unknown market"}), 400
        enabled = STATES[sym].toggle_market()
        logger.info(f"market [{sym.upper()}] {'activado' if enabled else 'desactivado'}", icon="🔘")
        return jsonify({"ok": True, "sym": sym, "enabled": enabled})

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
