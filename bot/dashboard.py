"""Flask web dashboard exposing live bot metrics and config controls."""
from __future__ import annotations

import threading
import time
from typing import Optional

import requests as _requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from . import logger
from .state import STATE

_BTC_FETCH_INTERVAL = 10  # seconds between Binance price polls


def _btc_price_loop() -> None:
    """Background daemon: keeps STATE.btc_price up to date from CoinGecko."""
    # CoinGecko simple price — no API key needed, accessible from Replit
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    while True:
        try:
            r = _requests.get(url, timeout=8)
            if r.ok:
                price = float(r.json()["bitcoin"]["usd"])
                STATE.update_btc_price(price)
        except Exception:
            pass  # silently retry next interval
        time.sleep(_BTC_FETCH_INTERVAL)


def start_btc_fetcher() -> None:
    t = threading.Thread(target=_btc_price_loop, name="btc-price", daemon=True)
    t.start()


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

    # Note: do NOT use the `/api/*` prefix — the Replit workspace proxy reserves
    # that path for the (unused) api-server artifact, which causes 502s.
    @app.get("/state")
    def state():
        return jsonify(STATE.snapshot())

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

        if "mm_buy_amount" in data:
            try:
                v = float(data["mm_buy_amount"])
                if 0.50 <= v <= 100_000:
                    updates["mm_buy_amount"] = v
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

        if "mode" in data:
            v = str(data["mode"]).lower()
            if v in ("paper", "real"):
                if v == "real" and not STATE.has_credentials:
                    rmr = STATE.real_mode_readiness()
                    return jsonify({
                        "ok": False,
                        "error": "Credenciales incompletas para modo real",
                        "readiness": rmr,
                    }), 400
                updates["mode"] = v

        accepted = STATE.update_runtime_config(**updates)
        logger.info(f"config updated via dashboard: {accepted}", icon="⚙")
        return jsonify({"ok": True, "updated": accepted})

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
