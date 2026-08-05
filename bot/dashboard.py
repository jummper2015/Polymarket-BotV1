"""Flask web dashboard exposing live Streak Snapper metrics and config controls.

Simplified for Streak Snapper — single market (BTC), single strategy system.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from datetime import datetime, timezone

import requests as _requests
from flask import (
    Flask, Response, jsonify, redirect, render_template, request, session, url_for,
)
from flask_cors import CORS
from sqlalchemy import func

from . import logger
from .auth import SESSION_KEY, auth_enabled, check_password, init_auth
from . import strategies
from .config import PERSISTABLE_FIELDS, RUNTIME_FIELDS, load_config
from .db import TradeModel, clear_config, db, get_all_config, set_many_config
from .state import STATE, active_states
from .trade_queries import (
    DEFAULT_PER_PAGE, iter_trades_for_export, metric_series, paginate_trades,
)


_PRICE_FETCH_INTERVAL = 10  # seconds between CoinGecko polls

# Column order for the CSV export. Fixed rather than derived from to_dict() so
# adding a DB field doesn't silently reshuffle everyone's saved spreadsheets.
CSV_COLUMNS = [
    "id", "strategy", "direction", "window_slug", "window_ts",
    "limit_cap", "entry_price", "shares", "shares_count", "cost",
    "multiplier", "loss_streak", "mode",
    "status", "outcome", "won", "pnl", "resolution_source",
    "opened_at", "resolved_at", "note",
]


def _prices_loop() -> None:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    while True:
        try:
            r = _requests.get(url, timeout=8)
            if r.ok:
                data = r.json()
                if "bitcoin" in data:
                    price = float(data["bitcoin"]["usd"])
                    STATE.update_spot_price(price)
        except Exception:
            pass
        time.sleep(_PRICE_FETCH_INTERVAL)


def start_price_fetcher() -> None:
    t = threading.Thread(target=_prices_loop, name="price-fetcher", daemon=True)
    t.start()


def _build_config_updates(data: dict) -> tuple:
    """Validate a config payload dict. Returns (updates_dict, None) or (None, error_response).

    Parsing and range checks live in RUNTIME_FIELDS so that this handler and the
    persisted-override loader can't drift apart. `mode` is handled separately:
    it needs a credentials check and is deliberately never persisted.
    """
    updates: dict = {}

    if "mode" in data:
        v = str(data["mode"]).lower()
        if v in ("paper", "real"):
            if v == "real" and not STATE.has_credentials:
                return None, (
                    jsonify({
                        "ok": False,
                        "error": "Credenciales incompletas para modo real",
                        "readiness": STATE.real_mode_readiness(),
                    }),
                    400,
                )
            updates["mode"] = v

    for key, field in RUNTIME_FIELDS.items():
        if key not in data:
            continue
        ok, parsed = field.coerce(data[key])
        if ok:
            updates[key] = parsed

    return updates, None


def _aggregate_db_stats(
    starting_bankroll: float, symbol: str | None = None
) -> tuple[dict, dict, dict]:
    """Aggregate trade stats over the whole `trades` table via SQL.

    Must be called inside an app context.
    Returns (overall_stats, per_strategy, per_symbol).

    `symbol` restricts every figure to one market. Without it the totals span
    all of them, which is right for a portfolio view and wrong for judging a
    strategy — hence the third return value, which keeps the markets apart.
    """
    query = db.session.query(
        TradeModel.strategy,
        TradeModel.symbol,
        TradeModel.status,
        func.count(TradeModel.id),
        func.coalesce(func.sum(TradeModel.pnl), 0.0),
        func.coalesce(func.sum(TradeModel.cost), 0.0),
    )
    if symbol:
        query = query.filter(TradeModel.symbol == symbol)
    rows = query.group_by(
        TradeModel.strategy, TradeModel.symbol, TradeModel.status
    ).all()

    if not rows:
        return {}, {}, {}

    def _blank() -> dict:
        return {"trades": 0, "open": 0, "wins": 0, "losses": 0,
                "resolved_pnl": 0.0, "total_invested": 0.0, "committed": 0.0}

    overall = _blank()
    per_strategy: dict = {}
    per_symbol: dict = {}

    for strategy, sym, status, count, pnl_sum, cost_sum in rows:
        bucket = per_strategy.setdefault(strategy, _blank())
        sym_bucket = per_symbol.setdefault(sym or "btc", _blank())
        for acc in (overall, bucket, sym_bucket):
            acc["trades"] += count
            acc["total_invested"] += float(cost_sum or 0.0)
            if status == "won":
                acc["wins"] += count
                acc["resolved_pnl"] += float(pnl_sum or 0.0)
            elif status == "lost":
                acc["losses"] += count
                acc["resolved_pnl"] += float(pnl_sum or 0.0)
            else:
                acc["open"] += count
                # Money already spent on positions that haven't resolved. It has
                # left the account but isn't in resolved_pnl yet, so without
                # tracking it the balance looks unchanged after opening a trade.
                acc["committed"] += float(cost_sum or 0.0)

    resolved = overall["wins"] + overall["losses"]
    bankroll = starting_bankroll + overall["resolved_pnl"]
    stats = {
        "trades": overall["trades"],
        "open": overall["open"],
        "wins": overall["wins"],
        "losses": overall["losses"],
        "win_rate": (overall["wins"] / resolved) if resolved else 0.0,
        "resolved_pnl": overall["resolved_pnl"],
        "total_invested": overall["total_invested"],
        "roi": (overall["resolved_pnl"] / overall["total_invested"])
        if overall["total_invested"] else 0.0,
        "bankroll": bankroll,
        "committed": overall["committed"],
        "available": bankroll - overall["committed"],
    }

    def _summarise(acc: dict) -> dict:
        r = acc["wins"] + acc["losses"]
        return {
            "trades": acc["trades"],
            "open": acc["open"],
            "wins": acc["wins"],
            "losses": acc["losses"],
            "win_rate": (acc["wins"] / r) if r else 0.0,
            "pnl": acc["resolved_pnl"],
            "total_invested": acc["total_invested"],
            "roi": (acc["resolved_pnl"] / acc["total_invested"])
            if acc["total_invested"] else 0.0,
        }

    # Registered strategies always appear, even with no trades, so the dashboard
    # renders an empty card instead of dropping the strategy off the page. Any
    # other name found in the table is included too — a strategy that has been
    # retired still has history worth showing.
    known = list(strategies.ids())
    for name in sorted(per_strategy):
        if name not in known:
            known.append(name)
    strat_stats = {k: _summarise(per_strategy.get(k, _blank())) for k in known}

    symbol_stats = {s: _summarise(acc) for s, acc in sorted(per_symbol.items())}

    return stats, strat_stats, symbol_stats


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Same-origin only. This used to be a bare CORS(app), which let any page on
    # the internet call /config — and /config can switch the bot to real money.
    CORS(app, origins=[], supports_credentials=True)

    init_auth(app)

    # Every template needs this to decide whether to show a logout link.
    @app.context_processor
    def _inject_auth():
        return {"auth_enabled": auth_enabled()}

    # Initialize DB with the Flask app
    from .db import init_db
    try:
        init_db(app)
    except Exception as exc:
        logger.warn(f"dashboard DB init failed: {exc} — stats will be in-memory only")

    @app.after_request
    def _no_cache(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        # With no password configured the gate is off entirely; a login page
        # would just be a dead end.
        if not auth_enabled():
            return redirect(url_for("index"))

        next_url = request.values.get("next") or ""
        # Only relative paths — an absolute URL here would be an open redirect.
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = ""

        if request.method == "POST":
            if check_password(request.form.get("password", "")):
                session[SESSION_KEY] = True
                session.permanent = False
                return redirect(next_url or url_for("index"))
            logger.warn(f"[dashboard] intento de acceso fallido desde {request.remote_addr}")
            return render_template(
                "login.html", error="Contraseña incorrecta", next_url=next_url
            ), 401

        return render_template("login.html", next_url=next_url)

    @app.get("/logout")
    def logout():
        session.pop(SESSION_KEY, None)
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template("dashboard.html", active_page="dashboard")

    @app.get("/settings")
    def settings():
        return render_template("settings.html", active_page="settings")

    @app.get("/state")
    def get_state():
        # `?symbol=eth` switches which market the live panel describes. Absent or
        # unknown, it stays on the first one — BTC — so old clients and
        # bookmarked URLs behave exactly as before.
        states = active_states()
        requested = (request.args.get("symbol") or "").strip().lower()
        active_symbol = requested if requested in states else next(iter(states))
        snap = states[active_symbol].snapshot()

        # Stats aggregate over EVERY trade in SQL, never over a page — otherwise
        # P&L / win rate / ROI silently drift once the table grows.
        db_stats: dict = {}
        db_strat_stats: dict = {}
        db_symbol_stats: dict = {}
        try:
            with app.app_context():
                db_stats, db_strat_stats, db_symbol_stats = _aggregate_db_stats(
                    snap["stats"]["starting_bankroll"], symbol=active_symbol
                )
        except Exception:
            db_stats = {}
            db_strat_stats = {}
            db_symbol_stats = {}

        # Merge: DB stats override in-memory for resolved trades
        merged_stats = dict(snap["stats"])
        if db_stats:
            merged_stats.update({k: v for k, v in db_stats.items() if k in merged_stats})
        merged_strat = dict(snap["strategy_stats"])
        if db_strat_stats:
            for key in merged_strat:
                if key in db_strat_stats:
                    merged_strat[key] = db_strat_stats[key]

        # Which fields come from a saved override rather than the .env.
        try:
            overridden = sorted(k for k in get_all_config() if k in PERSISTABLE_FIELDS)
        except Exception:
            overridden = []

        # Portfolio-wide figures, so the per-market view can be read against the
        # account as a whole rather than in isolation.
        all_symbol_stats: dict = {}
        try:
            with app.app_context():
                _, _, all_symbol_stats = _aggregate_db_stats(
                    snap["stats"]["starting_bankroll"]
                )
        except Exception:
            all_symbol_stats = {}

        return jsonify({
            "config_overrides": overridden,
            "symbol": active_symbol,
            "symbols": list(states),
            "symbol_stats": all_symbol_stats or db_symbol_stats,
            "skips": snap.get("skips", {}),
            # The registry, so /settings can render one card per strategy
            # instead of a hand-written block per strategy.
            "strategies": snap.get("strategies", []),
            # Declaration of every runtime field: type, range, label, hint.
            # /settings renders from this, so a parameter added to
            # RUNTIME_FIELDS shows up in the UI without touching the template.
            "fields": {name: f.to_json() for name, f in RUNTIME_FIELDS.items()},
            # Every runtime-editable field, straight from RUNTIME_FIELDS. This
            # used to be a hand-copied list, which is how ss_sizing and the
            # regime filters ended up configurable by POST but invisible in the
            # UI. `mode` is not a RuntimeField on purpose (never persisted) and
            # is added separately.
            "config": {
                "mode": snap["mode"],
                **{name: snap[name] for name in RUNTIME_FIELDS if name in snap},
            },
            "martingale": {
                "fade": {
                    "multiplier": snap["ss_fade_martingale_mult"],
                    "loss_streak": snap["ss_fade_loss_streak"],
                },
                "trend": {
                    "multiplier": snap["ss_trend_martingale_mult"],
                    "loss_streak": snap["ss_trend_loss_streak"],
                    # The locked side and the 4h candle that chose it. Null
                    # between cycles, which the UI shows as "sin ciclo".
                    "cycle_side": snap["ss_trend_cycle_side"],
                    "cycle_anchor_ts": snap["ss_trend_cycle_anchor_ts"],
                    "last_strength": snap["ss_trend_last_strength"],
                },
            },
            "status": {
                "bot_status": snap["bot_status"],
                "bot_message": snap["bot_message"],
                "ws_connected": snap["ws_connected"],
                "current_slug": snap["current_slug"],
                "seconds_remaining": snap["seconds_remaining"],
                "spot_price": snap["spot_price"],
                # The frontend reads status.chainlink; leaving it out here kept
                # the CL badge stuck on "off" no matter what the feed was doing.
                "chainlink": snap["chainlink"],
            },
            "prices": {
                "up_mid": snap.get("last_up_price"),
                "down_mid": snap.get("last_down_price"),
                "up_bid": snap.get("last_up_bid"),
                "up_ask": snap.get("last_up_ask"),
                "down_bid": snap.get("last_down_bid"),
                "down_ask": snap.get("last_down_ask"),
            },
            "order_book": snap["order_book"],
            "stats": merged_stats,
            "strategy_stats": merged_strat,
            # Trades are NOT here any more — they live at /api/trades, fetched on
            # demand. This endpoint is polled every second, and shipping 100 rows
            # per second to render 50 was pure waste.
            "price_history": snap["price_history"],
            "log": snap["log"],
            "real_mode_readiness": snap["real_mode_readiness"],
        })

    def _filters_from_request() -> dict:
        return {
            key: request.args.get(key, "")
            for key in ("strategy", "status", "mode", "direction",
                        "resolution_source", "from", "to", "q")
        }

    @app.get("/api/trades")
    def api_trades():
        """One page of trades. Replaces the trades block that used to ride
        along with /state on every 1 s poll."""
        try:
            with app.app_context():
                return jsonify(paginate_trades(
                    _filters_from_request(),
                    page=request.args.get("page", 1, type=int),
                    per_page=request.args.get("per_page", DEFAULT_PER_PAGE, type=int),
                    sort=request.args.get("sort", "id"),
                    order=request.args.get("order", "desc"),
                ))
        except Exception as exc:
            logger.warn(f"[dashboard] /api/trades falló: {exc}")
            return jsonify({"items": [], "total": 0, "page": 1,
                            "per_page": DEFAULT_PER_PAGE, "pages": 1,
                            "error": str(exc)}), 500

    @app.get("/api/trades.csv")
    def api_trades_csv():
        """Same filters as /api/trades, streamed out as a spreadsheet."""
        try:
            with app.app_context():
                rows = iter_trades_for_export(_filters_from_request())
        except Exception as exc:
            logger.warn(f"[dashboard] export CSV falló: {exc}")
            return jsonify({"ok": False, "error": str(exc)}), 500

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for trade in rows:
            writer.writerow(trade.to_dict())

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="streak-snapper-trades-{stamp}.csv"'
            },
        )

    @app.get("/api/metrics/series")
    def api_metrics_series():
        """Equity, drawdown and win-rate series for the charts."""
        try:
            with app.app_context():
                return jsonify(metric_series(
                    STATE.starting_bankroll,
                    rolling_window=request.args.get("rolling", 50, type=int),
                ))
        except Exception as exc:
            logger.warn(f"[dashboard] /api/metrics/series falló: {exc}")
            return jsonify({"equity": [], "drawdown": [], "win_rate": [],
                            "error": str(exc)}), 500

    @app.post("/config")
    def update_config():
        data = request.get_json(force=True, silent=True) or {}
        updates, err = _build_config_updates(data)
        if err:
            return err
        accepted = STATE.update_runtime_config(**updates)

        # Persist everything except `mode`, so the settings survive a restart.
        persisted: list[str] = []
        to_store = {
            key: PERSISTABLE_FIELDS[key].serialize(value)
            for key, value in accepted.items()
            if key in PERSISTABLE_FIELDS
        }
        if to_store:
            try:
                set_many_config(to_store)
                persisted = sorted(to_store)
            except Exception as exc:
                logger.warn(f"config guardada en memoria pero no en DB: {exc}")

        logger.info(f"config updated: {accepted}", icon="⚙")
        return jsonify({"ok": True, "updated": accepted, "persisted": persisted})

    @app.post("/config/reset")
    def reset_config():
        """Drop saved overrides and fall back to the .env values."""
        data = request.get_json(force=True, silent=True) or {}
        requested = data.get("keys")
        keys = (
            [k for k in requested if k in PERSISTABLE_FIELDS]
            if isinstance(requested, list)
            else None
        )

        try:
            removed = clear_config(keys)
        except Exception as exc:
            logger.err(f"no se pudieron borrar los ajustes guardados: {exc}")
            return jsonify({"ok": False, "error": str(exc)}), 500

        # Re-read the environment and re-apply the affected fields.
        env_cfg = load_config()
        restored = {
            key: getattr(env_cfg, key)
            for key in (keys if keys is not None else PERSISTABLE_FIELDS)
            if hasattr(env_cfg, key)
        }
        applied = STATE.update_runtime_config(**restored)

        logger.ok(
            f"ajustes guardados descartados ({removed}) — se vuelve a .env: {applied}",
            icon="↩",
        )
        return jsonify({"ok": True, "removed": removed, "restored": applied})

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
