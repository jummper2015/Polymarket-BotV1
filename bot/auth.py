"""Dashboard authentication.

The dashboard can flip the bot from paper to real trading (`POST /config`), so an
unauthenticated panel on a public port is a way to lose money, not just a privacy
problem. Everything is behind a session except `/login` and `/healthz`.

Deliberately minimal: one shared password from the environment, a signed Flask
session cookie, no user table. This is a single-operator panel — a user model
would be more moving parts guarding the same one secret.
"""

from __future__ import annotations

import os
import secrets
from functools import wraps
from typing import Callable, Optional

from flask import Flask, redirect, request, session, url_for

from . import logger


SESSION_KEY = "authed"

# Loopback-only hosts. Binding to one of these means the panel isn't reachable
# from off-box, which is the one case where running without a password is sane.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Endpoints reachable without a session. `healthz` stays open so process monitors
# and container probes don't need credentials.
PUBLIC_ENDPOINTS = {"login", "healthz", "static"}


def get_password() -> str:
    return (os.getenv("DASHBOARD_PASSWORD") or "").strip()


def auth_enabled() -> bool:
    return bool(get_password())


def check_password(candidate: str) -> bool:
    """Compare against the configured password in constant time.

    `compare_digest` rather than `==` so response timing doesn't leak how much of
    a guess was right.
    """
    expected = get_password()
    if not expected:
        return False
    return secrets.compare_digest(candidate or "", expected)


def verify_startup_config(host: str) -> Optional[str]:
    """Return an error message if this host/password combination is unsafe.

    Refusing to start beats starting wide open. A dashboard with no password on
    0.0.0.0 is reachable by anything that can route to the box, and it exposes a
    switch that puts real money on the line.
    """
    if auth_enabled():
        return None
    if host in _LOCAL_HOSTS:
        return None
    return (
        f"el dashboard escucharía en {host} sin contraseña.\n"
        f"  Cualquiera que alcance el puerto podría cambiar el bot a modo REAL.\n"
        f"  Añade a tu .env:\n"
        f"      DASHBOARD_PASSWORD=<una contraseña>\n"
        f"  O, para desarrollo local, escucha solo en tu máquina:\n"
        f"      DASHBOARD_HOST=127.0.0.1"
    )


def init_auth(app: Flask) -> None:
    """Wire session signing and the before-request gate onto the app."""
    secret = (os.getenv("DASHBOARD_SECRET_KEY") or "").strip()
    if not secret:
        # A random key still signs cookies correctly; it just doesn't survive a
        # restart, which logs everyone out. Fine as a default, worth warning about.
        secret = secrets.token_hex(32)
        if auth_enabled():
            logger.warn(
                "DASHBOARD_SECRET_KEY no está definido — se generó uno aleatorio. "
                "Las sesiones se cerrarán en cada reinicio; define uno en .env para evitarlo."
            )
    app.secret_key = secret

    # Session cookie hardening. SECURE is left off because most VPS setups here
    # serve plain HTTP; turning it on without TLS would break login silently.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    @app.before_request
    def _require_login():
        if not auth_enabled():
            return None
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if session.get(SESSION_KEY):
            return None
        # XHR gets a status it can act on; a browser gets the login page.
        if request.path.startswith(("/api/", "/state", "/config")):
            return {"ok": False, "error": "no autenticado"}, 401
        return redirect(url_for("login", next=request.path))


def login_required(view: Callable) -> Callable:
    """Belt-and-braces guard for individual views.

    `init_auth`'s before_request already covers everything; this exists so a view
    added later is still protected if it lands in PUBLIC_ENDPOINTS by mistake.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if auth_enabled() and not session.get(SESSION_KEY):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper
