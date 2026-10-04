"""Polymarket account balance + positions fetcher.

Talks to `data-api.polymarket.com`. No auth needed for the public read-only
endpoints, so the only env var we need is PROXY_WALLET.

The bot runs a polling loop in main.py that updates BotState every 30 s.
The /state payload then exposes the live value to the dashboard, which is
what makes the "real balance" tile meaningful — without this, the dashboard
only ever shows the synthetic `STARTING_BANKROLL + Σpnl` calculation, which
drifts from the real on-chain USDC.e balance as soon as fees, gas, dust or
manual transfers enter the picture.

API endpoints used (verified shape 2026-09-15):
  GET /value?user=<addr>     → [{"user": "...", "value": <float>}]
  GET /positions?user=<addr> → [{position, size, currentValue, ...}, ...]
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests

from . import logger

DATA_API = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 8  # seconds; tight so a hang can't stall the poller thread


def fetch_account_value(wallet: str) -> Optional[float]:
    """Total account value in USDC for `wallet`. None on any error.

    The endpoint returns a JSON array (one entry per linked account); a single
    EOA/proxy-wallet bot only ever has one. The schema occasionally returns
    `value` as a string — coerce through float and let the caller decide.
    """
    if not wallet:
        return None
    try:
        r = requests.get(
            f"{DATA_API}/value",
            params={"user": wallet},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warn(
                f"[balance] /value devolvió status={r.status_code} "
                f"para wallet={wallet[:8]}… — no se actualizó"
            )
            return None
        body = r.json()
        if not isinstance(body, list) or not body:
            return None
        v = body[0].get("value")
        return float(v) if v is not None else None
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warn(f"[balance] /value falló: {exc}")
        return None


def fetch_positions(wallet: str) -> Optional[list[dict]]:
    """Open positions for `wallet`. None on transport error, [] on empty.

    Each entry is a dict from the data API; we only sum `currentValue` (USDC)
    here and leave any deeper structure for callers that care.
    """
    if not wallet:
        return None
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": wallet},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warn(
                f"[balance] /positions devolvió status={r.status_code} "
                f"para wallet={wallet[:8]}…"
            )
            return None
        body = r.json()
        return body if isinstance(body, list) else []
    except requests.RequestException as exc:
        logger.warn(f"[balance] /positions falló: {exc}")
        return None


def positions_value(positions: list[dict]) -> float:
    """Sum the `currentValue` field across positions. Defensive on missing keys."""
    total = 0.0
    for p in positions or []:
        v = p.get("currentValue")
        if v is None:
            continue
        try:
            total += float(v)
        except (TypeError, ValueError):
            continue
    return round(total, 4)


# ── polling loop ─────────────────────────────────────────────────────────────

# Polling intervals. Conservative by default; the data API is fine at higher
# rates but the dashboard only redraws the balance tile once per second, so
# polling faster than 10 s is wasted.
BALANCE_POLL_SECONDS = 30
BALANCE_POLL_JITTER  = 5    # ± jitter so multiple bot instances don't align


def _balance_loop(wallet: str) -> None:
    """Background thread target. Updates BotState on every poll.

    Imports BotState lazily so importing this module from a test that has no
    Flask app doesn't try to pull in the whole stack.
    """
    from .state import active_states

    # Small initial delay so the rest of main() finishes before we fire a
    # request to a third-party API.
    time.sleep(2.0)
    next_tick = time.time()
    while True:
        value = fetch_account_value(wallet)
        positions = fetch_positions(wallet)
        pos_val = positions_value(positions) if positions is not None else None

        now = time.time()
        for state in active_states().values():
            state.update_polymarket_balance(
                usdc=value,
                positions_value=pos_val,
                positions_count=(len(positions) if positions else 0),
                updated_at=now,
            )

        # Stagger next tick with jitter so the bot doesn't pin a single wall-clock
        # second across many instances.
        next_tick = now + BALANCE_POLL_SECONDS + (BALANCE_POLL_JITTER * 0.5 - 5)
        # Cheap clamp so a slow request doesn't make us busy-loop.
        sleep_for = max(5.0, next_tick - time.time())
        time.sleep(sleep_for)


def start_balance_poller(wallet: str) -> None:
    """Spawn the polling daemon thread. Returns immediately.

    No-op if `wallet` is empty (paper mode without credentials, or any setup
    where PROXY_WALLET wasn't set). Safe to call from main(); thread is a
    daemon so it doesn't block process exit.
    """
    import threading

    # Paper mode gate — added 2026-10-03 to stop the pre-existing crash
    # that fired every 30s. The poller is only meaningful when actually
    # trading against real funds; in paper it would (a) waste API quota
    # on data we never display and (b) crash on state.update_polymarket_balance
    # because the matching state.py fields are off by design in paper mode.
    if os.getenv("TRADING_MODE", "paper") != "real":
        logger.info(
            "[balance] TRADING_MODE != real — poller de saldo real inactivo",
            icon="💼",
        )
        return

    if not wallet:
        logger.info(
            "[balance] PROXY_WALLET no configurado — poller de saldo real inactivo",
            icon="💼",
        )
        return

    t = threading.Thread(
        target=_balance_loop,
        args=(wallet,),
        name="polymarket-balance-poller",
        daemon=True,
    )
    t.start()
    logger.ok(
        f"[balance] poller iniciado para wallet={wallet[:8]}…{wallet[-4:]} "
        f"(cada {BALANCE_POLL_SECONDS}s ± {BALANCE_POLL_JITTER}s)",
        icon="💼",
    )
