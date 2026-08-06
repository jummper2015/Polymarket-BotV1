"""Per-market logger: writes to the BotState bound to the current thread.

Each trader thread (and its MM / EE sub-threads) must call
`logger.set_context(state)` once at the top of their entry function so that
log entries are routed to the correct market's log buffer.  Threads that never
call set_context fall back to the global BTC state (backward-compat alias).
"""
from __future__ import annotations

import sys
import threading
import time

from .state import STATE  # fallback: BTC state (backward-compat)


_local = threading.local()


def set_context(state) -> None:
    """Bind a BotState to the current thread for routing log events."""
    _local.state = state


def _current_state():
    return getattr(_local, "state", STATE)


def _stamp() -> str:
    return time.strftime("[%H:%M:%S]")


def _market_tag() -> str:
    """`[ETH] ` when more than one market is running, else empty.

    Only decorates the console line — the message recorded in `log_event` stays
    verbatim, because each market's buffer is already its own and repeating the
    symbol inside it would be noise on the dashboard.
    """
    from .state import STATES

    if len(STATES) < 2:
        return ""
    symbol = getattr(_current_state(), "symbol", "")
    return f"[{symbol.upper()}] " if symbol else ""


def log(level: str, icon: str, message: str, *, transient: bool = False) -> None:
    """Print a line and (unless transient) record it in the per-market log."""
    line = f"{_stamp()} {icon} {_market_tag()}{message}"
    if transient:
        sys.stdout.write("\r" + line.ljust(120))
        sys.stdout.flush()
        return
    sys.stdout.write("\r" + " " * 120 + "\r")
    print(line, flush=True)
    _current_state().log_event(level, message)


def info(message: str, icon: str = "ℹ") -> None:
    log("info", icon, message)


def ok(message: str, icon: str = "✔") -> None:
    log("success", icon, message)


def warn(message: str, icon: str = "⚠") -> None:
    log("warn", icon, message)


def err(message: str, icon: str = "✖") -> None:
    log("error", icon, message)


def transient(message: str, icon: str = "·") -> None:
    log("info", icon, message, transient=True)
