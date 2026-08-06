"""Historical window outcomes from Gamma, with an on-disk cache.

Chainlink publishes no history — `getLatestReport()` returns the current value
and subscriptions start with the next update, so there is no way to replay the
prices that settled a past window (docs/CHAINLINK_TWAP.md §6). But backtesting
doesn't need the price, it needs *who won*, and Gamma serves that already
resolved. That is the settlement truth itself, not an approximation of it.

Depth measured 3-ago-2026: ~20.000 windows (~70 days) still carry
`outcomePrices`; 50.000 comes back empty.

Access quirks, all measured:
  - Only `GET /events?slug=btc-updown-5m-<ts>` works. `/markets?slug=` returns
    empty and `series_slug` is ignored (it answers with unrelated markets).
  - No batch endpoint exists — one request per window, so parallelise.
  - `urllib` without a User-Agent gets a 403. `requests` sets one by default.

Usage:
    from bot.gamma_history import fetch_outcomes

    outcomes = fetch_outcomes([1785178800, 1785179100, ...])
    outcomes[1785178800]   # "UP" | "DOWN" | None (unresolved / too old)
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional

import requests

from . import logger


GAMMA_HOST = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CACHE_PATH = os.getenv("GAMMA_CACHE_PATH", "data/gamma_outcomes.json")

# Gamma tolerates this comfortably; higher rates start returning 429.
MAX_WORKERS = 8
REQUEST_TIMEOUT = 10.0
MAX_RETRIES = 3

# Windows older than this have no outcomePrices left (measured: 20k ok, 50k empty).
MAX_HISTORY_WINDOWS = 20_000


# ── cache ─────────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()


def load_cache(path: str = CACHE_PATH) -> Dict[int, Optional[str]]:
    """Read the on-disk outcome cache. Returns {} when absent or corrupt."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    # JSON object keys are always strings; the rest of the module keys by int.
    return {int(k): v for k, v in raw.items()}


def save_cache(cache: Dict[int, Optional[str]], path: str = CACHE_PATH) -> None:
    """Write the cache atomically so a crash mid-write can't corrupt it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with _cache_lock:
        with open(tmp, "w") as f:
            json.dump({str(k): v for k, v in cache.items()}, f)
        os.replace(tmp, path)


# ── single-window fetch ───────────────────────────────────────────────────────


def _parse_outcome(payload: object) -> Optional[str]:
    """Extract UP/DOWN from a Gamma /events response.

    Returns None for anything that isn't an unambiguous 1/0 resolution — an
    open window, a void market, or a shape we don't recognise. Callers treat
    None as "no label", never as a direction.
    """
    if not isinstance(payload, list) or not payload:
        return None
    markets = payload[0].get("markets") or []
    if not markets:
        return None
    try:
        prices = json.loads(markets[0].get("outcomePrices", "[]") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if len(prices) != 2:
        return None
    try:
        up_price = float(prices[0])
    except (TypeError, ValueError):
        return None
    if up_price == 1.0:
        return "UP"
    if up_price == 0.0:
        return "DOWN"
    return None


def fetch_outcome(
    window_ts: int,
    *,
    gamma_host: str = GAMMA_HOST,
    symbol: str = "btc",
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """Resolved outcome for one 5-min window. None if unresolved or unavailable."""
    slug = f"{symbol}-updown-5m-{window_ts}"
    url = f"{gamma_host.rstrip('/')}/events"
    http = session or requests

    for attempt in range(MAX_RETRIES):
        try:
            r = http.get(url, params={"slug": slug}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(1.0 * (attempt + 1))  # back off, then retry
                continue
            if r.status_code != 200:
                return None
            return _parse_outcome(r.json())
        except (requests.RequestException, json.JSONDecodeError):
            if attempt < MAX_RETRIES - 1:
                time.sleep(0.5 * (attempt + 1))
    return None


# ── batch fetch ───────────────────────────────────────────────────────────────


def fetch_outcomes(
    window_ts_list: Iterable[int],
    *,
    gamma_host: str = GAMMA_HOST,
    symbol: str = "btc",
    cache_path: Optional[str] = CACHE_PATH,
    max_workers: int = MAX_WORKERS,
    progress: bool = False,
) -> Dict[int, Optional[str]]:
    """Resolved outcomes for many windows, cached on disk and fetched in parallel.

    Cached windows cost nothing. Only misses hit the network, so re-running a
    backtest over an overlapping range is free after the first pass.

    A window that resolves to None is cached too — that's usually "too old to
    have outcomePrices", which won't change. The exception is a window that is
    merely still open; pass `cache_path=None` if you're querying recent windows
    that may resolve later.
    """
    targets: List[int] = sorted(set(window_ts_list))
    if not targets:
        return {}

    cache = load_cache(cache_path) if cache_path else {}
    missing = [ts for ts in targets if ts not in cache]

    if missing:
        if progress:
            print(f"📡 Gamma: {len(targets) - len(missing)} en caché, "
                  f"descargando {len(missing)}...")

        done = 0
        # One Session per worker: requests.Session isn't thread-safe, but its
        # connection pooling is what keeps 20k requests from being glacial.
        local = threading.local()

        def _worker(ts: int) -> tuple[int, Optional[str]]:
            if not hasattr(local, "session"):
                local.session = requests.Session()
            return ts, fetch_outcome(
                ts, gamma_host=gamma_host, symbol=symbol, session=local.session
            )

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_worker, ts) for ts in missing]
            for fut in as_completed(futures):
                try:
                    ts, outcome = fut.result()
                except Exception as exc:  # a worker died; skip that window
                    logger.warn(f"[gamma_history] fetch falló: {exc}")
                    continue
                cache[ts] = outcome
                done += 1
                if progress and done % 100 == 0:
                    print(f"   {done}/{len(missing)}...")

        if cache_path:
            save_cache(cache, cache_path)
        if progress:
            print(f"   ✅ {done} descargados, caché en {cache_path}")

    return {ts: cache.get(ts) for ts in targets}


def coverage(outcomes: Dict[int, Optional[str]]) -> dict:
    """Label coverage stats — how much of a range Gamma could actually label."""
    total = len(outcomes)
    labelled = sum(1 for v in outcomes.values() if v in ("UP", "DOWN"))
    return {
        "total": total,
        "labelled": labelled,
        "missing": total - labelled,
        "coverage": round(labelled / total, 4) if total else 0.0,
    }
