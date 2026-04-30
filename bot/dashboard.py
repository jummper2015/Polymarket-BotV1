"""Flask web dashboard exposing live bot metrics."""
from __future__ import annotations

from flask import Flask, jsonify, render_template
from flask_cors import CORS

from .state import STATE


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

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
