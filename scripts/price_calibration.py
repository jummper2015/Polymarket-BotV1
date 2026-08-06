"""Is the Polymarket price a fair estimate of P(win)?

Everything measured so far says the *direction* of a 5-min window is
unpredictable from past direction at any horizon (docs/RUTA.md, Fase 4.7). That
leaves one place an edge could still live: the price itself. If the market is
well calibrated — a token quoted at 0.52 wins 52% of the time — then no entry
rule can profit, because every trade is a fair coin bought at fair odds. If it
is miscalibrated in a stable way, that bias *is* the edge.

This script pulls, for each 5-min window:
  - the UP token's quoted price shortly after the window opens (CLOB
    /prices-history, ~1-min resolution)
  - who actually won (Gamma, settlement truth, already cached by gamma_history)

and reports the calibration curve plus the P&L of buying at that price.

Usage:
    python -m scripts.price_calibration --windows 2000
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.gamma_history import fetch_outcomes

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CACHE_PATH = "data/price_calibration.json"

MAX_WORKERS = 8
TIMEOUT = 15.0

# How far into the window to sample the price. The bot warms the WebSocket for
# 2 s and then buys, so the first quote at or after this offset is what it would
# realistically have paid.
ENTRY_OFFSET_S = 15

_cache_lock = threading.Lock()


def _load_cache() -> Dict[str, dict]:
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cache, fh)
    os.replace(tmp, CACHE_PATH)


def _get(url: str, params: dict) -> Optional[object]:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
        except Exception:
            if attempt < 2:
                time.sleep(1.0)
    return None


def fetch_entry_price(window_ts: int) -> Optional[dict]:
    """UP-token price shortly after `window_ts` opens, plus book context."""
    events = _get(f"{GAMMA_HOST}/events", {"slug": f"btc-updown-5m-{window_ts}"})
    if not isinstance(events, list) or not events:
        return None
    markets = events[0].get("markets") or []
    if not markets:
        return None
    market = markets[0]

    try:
        up_token = json.loads(market.get("clobTokenIds") or "[]")[0]
    except (ValueError, IndexError):
        return None

    history = _get(
        f"{CLOB_HOST}/prices-history",
        {"market": up_token, "startTs": window_ts - 60, "endTs": window_ts + 300,
         "fidelity": 1},
    )
    if not isinstance(history, dict):
        return None
    points = history.get("history") or []
    if not points:
        return None

    # First quote at or after the entry offset; fall back to the last one before
    # it (a window with no tick after the offset still had a standing price).
    target = window_ts + ENTRY_OFFSET_S
    after = [p for p in points if p["t"] >= target]
    point = after[0] if after else points[-1]

    return {
        "up_price": float(point["p"]),
        "price_ts": int(point["t"]),
        "volume": float(market.get("volume") or 0.0),
        "ticks": len(points),
    }


def collect(window_ts_list: list[int]) -> Dict[int, dict]:
    cache = _load_cache()
    todo = [ts for ts in window_ts_list if str(ts) not in cache]

    if todo:
        print(f"   descargando {len(todo)} ventanas ({len(cache)} en caché)...")
        done = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_entry_price, ts): ts for ts in todo}
            for fut in as_completed(futures):
                ts = futures[fut]
                try:
                    result = fut.result()
                except Exception:
                    result = None
                with _cache_lock:
                    cache[str(ts)] = result or {}
                done += 1
                if done % 250 == 0:
                    print(f"      {done}/{len(todo)}", flush=True)
                    with _cache_lock:
                        _save_cache(cache)
        _save_cache(cache)

    return {
        int(ts): v for ts, v in cache.items()
        if v and int(ts) in set(window_ts_list)
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Market price calibration study")
    parser.add_argument("--windows", type=int, default=2000)
    args = parser.parse_args()

    now = int(time.time())
    latest = (now // 300) * 300 - 600          # leave the live window alone
    window_ts_list = [latest - i * 300 for i in range(args.windows)][::-1]

    print(f"\n{'='*64}")
    print("  ¿Está el precio de Polymarket bien calibrado?")
    print(f"  {args.windows} ventanas de 5 min, entrada a +{ENTRY_OFFSET_S}s del inicio")
    print(f"{'='*64}\n")

    print("📡 Resultados (Gamma, verdad de liquidación)...")
    outcomes = fetch_outcomes(window_ts_list, progress=False)

    print("📡 Precios de entrada (CLOB /prices-history)...")
    prices = collect(window_ts_list)

    rows = []
    for ts in window_ts_list:
        out = outcomes.get(ts)
        px = prices.get(ts)
        if not out or not px:
            continue
        rows.append({"ts": ts, "p_up": px["up_price"], "won_up": out == "UP",
                     "volume": px["volume"]})

    if not rows:
        print("❌ sin datos emparejados")
        return

    print(f"\n   {len(rows)} ventanas con precio y resultado\n")

    # ── Calibration curve ────────────────────────────────────────────────────
    # Each window gives two observations: the UP token at p, and the DOWN token
    # at 1-p. Folding both in doubles the sample and makes the curve symmetric
    # by construction, which is what "is the market fair?" actually asks.
    obs = []
    for r in rows:
        obs.append((r["p_up"], r["won_up"]))
        obs.append((round(1.0 - r["p_up"], 4), not r["won_up"]))

    edges = [0.0, 0.20, 0.35, 0.45, 0.50, 0.55, 0.65, 0.80, 1.01]
    print(f"{'precio':>14} {'n':>6} {'implícito':>10} {'real':>8} {'sesgo':>8} {'z':>7}")
    for lo, hi in zip(edges, edges[1:]):
        bucket = [(p, w) for p, w in obs if lo <= p < hi]
        if len(bucket) < 30:
            continue
        n = len(bucket)
        implied = sum(p for p, _ in bucket) / n
        actual = sum(1 for _, w in bucket if w) / n
        z = (actual - implied) / math.sqrt(max(implied * (1 - implied), 1e-9) / n)
        print(f"{lo:>6.2f}-{hi:<6.2f} {n:>6} {implied:>9.3f} {actual:>7.3f} "
              f"{actual - implied:>+8.3f} {z:>+7.2f}")

    n = len(obs)
    implied = sum(p for p, _ in obs)/n
    actual = sum(1 for _, w in obs if w)/n
    print(f"\n{'TOTAL':>14} {n:>6} {implied:>9.3f} {actual:>7.3f} "
          f"{actual-implied:>+8.3f} "
          f"{(actual-implied)/math.sqrt(implied*(1-implied)/n):>+7.2f}")

    # ── What buying at that price would have paid ────────────────────────────
    print("\n─ P&L de comprar 5 shares a ese precio, sin martingale ─")
    for label, pick in (
        ("siempre UP", lambda r: (r["p_up"], r["won_up"])),
        ("siempre DOWN", lambda r: (1 - r["p_up"], not r["won_up"])),
        ("el favorito", lambda r: (max(r["p_up"], 1 - r["p_up"]),
                                   r["won_up"] if r["p_up"] > 0.5 else not r["won_up"])),
        ("el underdog", lambda r: (min(r["p_up"], 1 - r["p_up"]),
                                   r["won_up"] if r["p_up"] < 0.5 else not r["won_up"])),
    ):
        pnl = 0.0
        wins = 0
        for r in rows:
            p, won = pick(r)
            cost = 5 * p
            pnl += (5 - cost) if won else -cost
            wins += bool(won)
        roi = pnl / sum(5 * pick(r)[0] for r in rows)
        print(f"  {label:<14} win={wins/len(rows)*100:5.1f}%  "
              f"P&L=${pnl:>8.2f}  ROI={roi*100:>+6.2f}%")


if __name__ == "__main__":
    main()
