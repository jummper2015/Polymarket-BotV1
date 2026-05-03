"""Simple stdout logger that also pushes to shared state."""
from __future__ import annotations

import sys
import time
from typing import Optional

from .state import STATES


def _stamp() -> str:
    return time.strftime("[%H:%M:%S]")


def log(level: str, icon: str, message: str, *, transient: bool = False, symbol: Optional[str] = None) -> None:
    """Print a line and (unless transient) record it in the dashboard log.

    When *symbol* is given the event is stored in that market's state.
    Otherwise it is broadcast to every market's log.
    """
    line = f"{_stamp()} {icon} {message}"
    if transient:
        sys.stdout.write("\r" + line.ljust(120))
        sys.stdout.flush()
        return
    # Move past any transient line.
    sys.stdout.write("\r" + " " * 120 + "\r")
    print(line, flush=True)
    if symbol and symbol in STATES:
        STATES[symbol].log_event(level, message)
    else:
        for state in STATES.values():
            state.log_event(level, message)


def info(message: str, icon: str = "ℹ", *, symbol: Optional[str] = None) -> None:
    log("info", icon, message, symbol=symbol)


def ok(message: str, icon: str = "✔", *, symbol: Optional[str] = None) -> None:
    log("success", icon, message, symbol=symbol)


def warn(message: str, icon: str = "⚠", *, symbol: Optional[str] = None) -> None:
    log("warn", icon, message, symbol=symbol)


def err(message: str, icon: str = "✖", *, symbol: Optional[str] = None) -> None:
    log("error", icon, message, symbol=symbol)


def transient(message: str, icon: str = "·", *, symbol: Optional[str] = None) -> None:
    log("info", icon, message, transient=True, symbol=symbol)
