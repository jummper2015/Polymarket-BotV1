"""Market hours filter — gates strategies by UTC hour whitelist.

Spec 2026-10-08: hours are derived from production trade history analysis
(see scripts/analyze_trade_hours.py and data/hour_analysis_<TS>.json).
Per-symbol whitelist is loaded from BotState fields
`whitelist_hours_<symbol>` (CSV like "0,3,7,12,21"). Empty whitelist =
no filter (degradacion segura).

This is registered at priority 90 so it runs FIRST — before any
strategy evaluate() — and short-circuits the cycle when the current
hour is not in the whitelist.
"""

from __future__ import annotations

import time
from typing import Optional

from .base import StrategyContext, StrategyDescriptor
from ..runtime_field import RuntimeField


def is_hour_whitelisted(utc_hour: int, whitelist_csv: str) -> bool:
    """Return True if utc_hour is in the CSV whitelist (or list empty).

    Whitelist values: integers in 0..23, comma-separated. Whitespace
    tolerated. Empty string or None -> whitelist is open (no filter).
    Invalid entries are silently ignored (test catches the empty case).
    """
    if not whitelist_csv or not whitelist_csv.strip():
        return True
    hours = set()
    for raw in whitelist_csv.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            h = int(raw)
        except ValueError:
            continue
        if 0 <= h <= 23:
            hours.add(h)
    return utc_hour in hours


def current_utc_hour() -> int:
    """Return current UTC hour (0..23). Pure helper, deterministic given time.time()."""
    ts = time.gmtime()
    return ts.tm_hour


class _FilterStrategy:
    """Marker — the actual gate logic lives in evaluate."""
    pass


def _filter_evaluate(ctx: StrategyContext) -> Optional[list]:
    """Descriptor callback. Returns [] when current hour is blocked,
    None when allowed (so the descriptor doesn't bias other strategies).
    """
    state = ctx.state
    symbol = (ctx.symbol or "").lower()
    field_name = f"whitelist_hours_{symbol}"
    whitelist_csv = getattr(state, field_name, "") or ""
    hour = current_utc_hour()
    if is_hour_whitelisted(hour, whitelist_csv):
        return None
    return []


DESCRIPTOR = StrategyDescriptor(
    id="market_hours_filter",
    name="Market Hours Filter",
    description=(
        "Bloquea toda la operativa si la hora UTC actual no esta en la "
        "whitelist por simbolo. Whitelist vacia = sin filtro. "
        "Whitelist derivada del analisis de P&L historico."
    ),
    notes="Prioridad 90 — corre antes que cualquier otra estrategia.",
    enabled_when={"field": "mhf_enabled", "values": [True]},
    evaluate=_filter_evaluate,
    observe=lambda ctx: None,
    is_enabled=lambda state: True,  # always on; whitelist emptiness decides
    priority=90,
    params=(
        RuntimeField("whitelist_hours_btc", "string",
                     label="Whitelist horas BTC (CSV)",
                     hint="Horas UTC 0-23 separadas por coma. Vacio = sin filtro."),
        RuntimeField("whitelist_hours_eth", "string",
                     label="Whitelist horas ETH (CSV)",
                     hint="Horas UTC 0-23 separadas por coma. Vacio = sin filtro."),
        RuntimeField("whitelist_hours_sol", "string",
                     label="Whitelist horas SOL (CSV)",
                     hint="Horas UTC 0-23 separadas por coma. Vacio = sin filtro."),
    ),
)
