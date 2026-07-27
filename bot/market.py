"""Market loading from the Polymarket Gamma API."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

from . import logger


def slug_for_timestamp(unix_seconds: int, symbol: str = "btc") -> Tuple[str, int]:
    window_ts = (unix_seconds // 300) * 300
    return f"{symbol}-updown-5m-{window_ts}", window_ts


def current_slug(symbol: str = "btc") -> Tuple[str, int]:
    return slug_for_timestamp(int(time.time()), symbol)


def slug_for_15m_timestamp(unix_seconds: int) -> Tuple[str, int]:
    """Return (slug, window_ts) for the 15-min BTC corridor window containing unix_seconds."""
    window_ts = (unix_seconds // 900) * 900
    return f"btc-updown-15m-{window_ts}", window_ts


def current_15m_slug() -> Tuple[str, int]:
    return slug_for_15m_timestamp(int(time.time()))


@dataclass
class MarketTokens:
    slug: str
    window_ts: int
    up_token_id: str
    down_token_id: str


def _extract_tokens(market_obj: dict) -> Optional[Tuple[str, str]]:
    """Try to find UP/DOWN token IDs from a market object."""
    tokens = market_obj.get("tokens")
    if isinstance(tokens, list) and tokens:
        up_id = down_id = None
        for tk in tokens:
            outcome = (tk.get("outcome") or "").strip().upper()
            tid = tk.get("token_id") or tk.get("tokenId")
            if not tid:
                continue
            if outcome == "UP":
                up_id = str(tid)
            elif outcome == "DOWN":
                down_id = str(tid)
        if up_id and down_id:
            return up_id, down_id

    # Fallback: clobTokenIds + outcomes (sometimes JSON-encoded strings).
    raw_ids = market_obj.get("clobTokenIds")
    raw_outcomes = market_obj.get("outcomes")
    if isinstance(raw_ids, str):
        try:
            raw_ids = json.loads(raw_ids)
        except json.JSONDecodeError:
            raw_ids = None
    if isinstance(raw_outcomes, str):
        try:
            raw_outcomes = json.loads(raw_outcomes)
        except json.JSONDecodeError:
            raw_outcomes = None

    if isinstance(raw_ids, list) and isinstance(raw_outcomes, list) and len(raw_ids) == len(raw_outcomes):
        up_id = down_id = None
        for tid, outcome in zip(raw_ids, raw_outcomes):
            outcome_norm = str(outcome or "").strip().upper()
            if outcome_norm == "UP":
                up_id = str(tid)
            elif outcome_norm == "DOWN":
                down_id = str(tid)
        if up_id and down_id:
            return up_id, down_id

    return None


def fetch_market(gamma_host: str, slug: str, *, timeout: float = 8.0) -> Optional[Tuple[str, str]]:
    """One-shot fetch. Returns (up_token_id, down_token_id) or None if not ready."""
    url = f"{gamma_host.rstrip('/')}/events"
    try:
        resp = requests.get(url, params={"slug": slug}, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"network error: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"gamma {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON from gamma: {exc}") from exc
    if not isinstance(data, list) or not data:
        return None
    markets = data[0].get("markets") or []
    if not markets:
        return None
    return _extract_tokens(markets[0])


def load_market_for_current_window(
    gamma_host: str,
    *,
    symbol: str = "btc",
    retry_seconds: int = 3,
    on_slug_change=None,
) -> MarketTokens:
    """Block until tokens for the current window are loaded.

    If the window rolls over while we're retrying, recompute the slug.
    `on_slug_change(new_slug, new_ts)` is invoked whenever the active slug changes.
    """
    slug, ts = current_slug(symbol)
    if on_slug_change:
        on_slug_change(slug, ts)

    while True:
        try:
            tokens = fetch_market(gamma_host, slug)
        except RuntimeError as exc:
            logger.warn(f"market load error for {slug}: {exc}; retrying in {retry_seconds}s")
            tokens = None

        if tokens:
            return MarketTokens(slug=slug, window_ts=ts, up_token_id=tokens[0], down_token_id=tokens[1])

        time.sleep(retry_seconds)
        new_slug, new_ts = current_slug()
        if new_slug != slug:
            logger.info(f"window rolled over while waiting; switching to {new_slug}", icon="↻")
            slug, ts = new_slug, new_ts
            if on_slug_change:
                on_slug_change(slug, ts)
