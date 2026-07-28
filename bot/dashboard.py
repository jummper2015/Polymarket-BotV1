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
    "btc15": "bitcoin",    # same price feed as btc
}

_MARKET_LABELS = {
    "btc": "Bitcoin",
    "sol": "Solana",
    "eth": "Ethereum",
    "btc15": "Bitcoin 15m",
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


def _build_config_updates(data: dict, state) -> tuple:
    """Validate a config payload dict.

    Returns ``(updates_dict, None)`` on success or
    ``(None, flask_error_response_tuple)`` when the request must be rejected.
    All numeric bounds are enforced here so both the global and per-market
    config endpoints share identical validation.
    """
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

    if "mm_shares_per_leg" in data:
        try:
            v = float(data["mm_shares_per_leg"])
            if 1 <= v <= 100_000:
                updates["mm_shares_per_leg"] = v
        except (TypeError, ValueError):
            pass

    if "mm_arm_spread_sum" in data:
        try:
            v = float(data["mm_arm_spread_sum"])
            if 1.00 <= v <= 1.20:
                updates["mm_arm_spread_sum"] = v
        except (TypeError, ValueError):
            pass

    if "mm_bid_sum_cap" in data:
        try:
            v = float(data["mm_bid_sum_cap"])
            if 0.70 <= v <= 0.99:
                updates["mm_bid_sum_cap"] = v
        except (TypeError, ValueError):
            pass

    if "mm_quote_cutoff_sec" in data:
        try:
            v = int(data["mm_quote_cutoff_sec"])
            if 60 <= v <= 270:
                updates["mm_quote_cutoff_sec"] = v
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

    # ── Per-strategy enable flags ─────────────────────────────────────────────
    if "trigger_enabled" in data or "mm_enabled" in data:
        te = bool(data.get("trigger_enabled", state.trigger_enabled))
        me = bool(data.get("mm_enabled", state.mm_enabled))
        updates["trigger_enabled"] = te
        updates["mm_enabled"] = me
        # Derive active_strategy for backward-compat with trader.py
        if te and me:
            updates["active_strategy"] = "both"
        elif me:
            updates["active_strategy"] = "market_making"
        else:
            updates["active_strategy"] = "trigger"

    # ── Corridor Collector ────────────────────────────────────────────────────

    if "cc_enabled" in data:
        updates["cc_enabled"] = bool(data["cc_enabled"])

    if "cc_shares" in data:
        try:
            v = int(data["cc_shares"])
            if 1 <= v <= 100_000:
                updates["cc_shares"] = v
        except (TypeError, ValueError):
            pass

    if "cc_zone_lead_min" in data:
        try:
            v = float(data["cc_zone_lead_min"])
            if 1.0 <= v <= 100.0:
                updates["cc_zone_lead_min"] = v
        except (TypeError, ValueError):
            pass

    if "cc_zone_lead_max" in data:
        try:
            v = float(data["cc_zone_lead_max"])
            if 1.0 <= v <= 500.0:
                updates["cc_zone_lead_max"] = v
        except (TypeError, ValueError):
            pass

    if "cc_zone_min_atr" in data:
        try:
            v = float(data["cc_zone_min_atr"])
            if 0.1 <= v <= 10.0:
                updates["cc_zone_min_atr"] = v
        except (TypeError, ValueError):
            pass

    if "cc_edge" in data:
        try:
            v = float(data["cc_edge"])
            if 0.01 <= v <= 0.50:
                updates["cc_edge"] = v
        except (TypeError, ValueError):
            pass

    if "cc_ask5_cap" in data:
        try:
            v = float(data["cc_ask5_cap"])
            if 0.10 <= v <= 0.90:
                updates["cc_ask5_cap"] = v
        except (TypeError, ValueError):
            pass

    if "cc_ask15_cap" in data:
        try:
            v = float(data["cc_ask15_cap"])
            if 0.10 <= v <= 0.99:
                updates["cc_ask15_cap"] = v
        except (TypeError, ValueError):
            pass

    if "ee_shares" in data:
        try:
            v = float(data["ee_shares"])
            if 0.01 <= v <= 100_000:
                updates["ee_shares"] = v
        except (TypeError, ValueError):
            pass

    if "ee_tp_pct" in data:
        try:
            v = float(data["ee_tp_pct"])
            if 0.1 <= v <= 100.0:
                updates["ee_tp_pct"] = v
        except (TypeError, ValueError):
            pass

    if "ee_entry_seconds" in data:
        try:
            v = int(data["ee_entry_seconds"])
            if 5 <= v <= 270:
                updates["ee_entry_seconds"] = v
        except (TypeError, ValueError):
            pass

    if "mode" in data:
        v = str(data["mode"]).lower()
        if v in ("paper", "real"):
            if v == "real" and not state.has_credentials:
                rmr = state.real_mode_readiness()
                return None, (
                    jsonify({
                        "ok": False,
                        "error": "Credenciales incompletas para modo real",
                        "readiness": rmr,
                    }),
                    400,
                )
            updates["mode"] = v

    return updates, None


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
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
                "config": {
                    "trigger_price": snap["trigger_price"],
                    "buy_amount": snap["buy_amount"],
                    "max_trades_per_window": snap["max_trades_per_window"],
                    "hedge_threshold": snap["hedge_threshold"],
                    "last_minute_seconds": snap["last_minute_seconds"],
                    "active_strategy": snap["active_strategy"],
                    "trigger_enabled": snap["trigger_enabled"],
                    "mm_enabled": snap["mm_enabled"],
                    "mm_shares_per_leg": snap["mm_shares_per_leg"],
                    "mm_arm_spread_sum": snap["mm_arm_spread_sum"],
                    "mm_bid_sum_cap": snap["mm_bid_sum_cap"],
                    "mm_quote_cutoff_sec": snap["mm_quote_cutoff_sec"],
                    "early_entry_enabled": snap["early_entry_enabled"],
                    "ee_shares": snap["ee_shares"],
                    "ee_tp_pct": snap["ee_tp_pct"],
                    "ee_entry_seconds": snap["ee_entry_seconds"],
                    "cc_enabled": snap["cc_enabled"],
                    "cc_shares": snap["cc_shares"],
                    "cc_zone_lead_min": snap["cc_zone_lead_min"],
                    "cc_zone_lead_max": snap["cc_zone_lead_max"],
                    "cc_zone_min_atr": snap["cc_zone_min_atr"],
                    "cc_edge": snap["cc_edge"],
                    "cc_ask5_cap": snap["cc_ask5_cap"],
                    "cc_ask15_cap": snap["cc_ask15_cap"],
                    "cc_paused": snap.get("cc_paused", False),
                    "starting_bankroll": snap["starting_bankroll"],
                    "mode": snap["mode"],
                    "market_enabled": snap["market_enabled"],
                },
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
        for strat in ("trigger", "mm", "early_entry", "corridor"):
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
                "trigger_enabled": btc_snap["trigger_enabled"],
                "mm_enabled": btc_snap["mm_enabled"],
                "mm_shares_per_leg": btc_snap["mm_shares_per_leg"],
                "mm_arm_spread_sum": btc_snap["mm_arm_spread_sum"],
                "mm_bid_sum_cap": btc_snap["mm_bid_sum_cap"],
                "mm_quote_cutoff_sec": btc_snap["mm_quote_cutoff_sec"],
                "early_entry_enabled": btc_snap["early_entry_enabled"],
                "ee_shares": btc_snap["ee_shares"],
                "ee_tp_pct": btc_snap["ee_tp_pct"],
                "ee_entry_seconds": btc_snap["ee_entry_seconds"],
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
        """Apply config to all three markets simultaneously."""
        data = request.get_json(force=True, silent=True) or {}
        updates, err = _build_config_updates(data, STATES["btc"])
        if err:
            return err
        accepted = {}
        for st in STATES.values():
            accepted = st.update_runtime_config(**updates)
        logger.info(f"config (all markets) updated: {accepted}", icon="⚙")
        return jsonify({"ok": True, "updated": accepted})

    @app.post("/config/<sym>")
    def update_config_for_market(sym: str):
        """Apply config to a single market only."""
        sym = sym.lower()
        if sym not in STATES:
            return jsonify({"ok": False, "error": "Unknown market"}), 400
        data = request.get_json(force=True, silent=True) or {}
        updates, err = _build_config_updates(data, STATES[sym])
        if err:
            return err
        accepted = STATES[sym].update_runtime_config(**updates)
        logger.info(f"config [{sym.upper()}] updated: {accepted}", icon="⚙")
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
