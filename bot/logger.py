"""Simple stdout logger that also pushes to shared state."""
from __future__ import annotations

import sys
import time
from typing import Optional

from .state import STATE


def _stamp() -> str:
    return time.strftime("[%H:%M:%S]")


def log(level: str, icon: str, message: str, *, transient: bool = False) -> None:
    """Print a line and (unless transient) record it in the dashboard log."""
    line = f"{_stamp()} {icon} {message}"
    if transient:
        sys.stdout.write("\r" + line.ljust(120))
        sys.stdout.flush()
        return
    # Move past any transient line.
    sys.stdout.write("\r" + " " * 120 + "\r")
    print(line, flush=True)
    STATE.log_event(level, message)


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
