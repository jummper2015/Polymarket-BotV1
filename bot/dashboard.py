"""Flask web dashboard exposing live bot metrics."""
from __future__ import annotations

from flask import Flask, jsonify, render_template
from flask_cors import CORS

from .state import STATE


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    CORS(app)

    @app.get("/")
    def index():
        return render_template("dashboard.html")

    @app.get("/api/state")
    def state():
        return jsonify(STATE.snapshot())

    @app.get("/api/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
