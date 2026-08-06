"""The real test: is there edge at the price available BEFORE the window opens?

A correction to scripts/streak_price_test.py. That script concluded the market
prices the streak exactly (53.8% accuracy at a price of 0.538, gap 0.000). It
sampled the price at a *median of +67 s into the window* — the CLOB
/prices-history endpoint has 1-minute fidelity, so "the first quote at or after
+15 s" actually lands around +60 s.

By then the price knows how the window itself is going, which is far better
information than any streak. Of course it looked efficient.

But the bot does not enter at +67 s. It enters within seconds of the open, and
the pre-open tape is flat: sampled windows sit at 0.505 for the whole 15 minutes
before the open, then jump the moment the window starts.

So the question that decides everything is:

    the pre-open quote is ~0.50 — does the streak signal beat 0.50?

If it does, the edge is real and the bot has been measuring itself against the
wrong price all along.

Usage:
    python -m scripts.preopen_edge
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

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.signal_search import GAMMA_CACHE, WINDOW, build_features, load_klines

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CACHE = "data/preopen_prices.json"

_lock = threading.Lock()


def _get(url: str, params: dict):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.0)
    return None


def fetch_preopen(window_ts: int) -> dict | None:
    """Last UP-token quote strictly BEFORE the window opened, plus the +60s one."""
    events = _get(f"{GAMMA_HOST}/events", {"slug": f"btc-updown-5m-{window_ts}"})
    if not isinstance(events, list) or not events:
        return None
    markets = events[0].get("markets") or []
    if not markets:
        return None
    try:
        up_token = json.loads(markets[0].get("clobTokenIds") or "[]")[0]
    except (ValueError, IndexError):
        return None

    history = _get(
        f"{CLOB_HOST}/prices-history",
        {"market": up_token, "startTs": window_ts - 1200, "endTs": window_ts + 300,
         "fidelity": 1},
    )
    if not isinstance(history, dict):
        return None
    points = history.get("history") or []
    if not points:
        return None

    before = [p for p in points if p["t"] < window_ts]
    after = [p for p in points if p["t"] >= window_ts]
    if not before:
        return None

    return {
        "pre_price": float(before[-1]["p"]),
        "pre_ts": int(before[-1]["t"]),
        "n_pre": len(before),
        "post_price": float(after[0]["p"]) if after else None,
        "post_ts": int(after[0]["t"]) if after else None,
    }


def collect(ts_list: list[int]) -> dict[int, dict]:
    cache: dict = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE) as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}

    todo = [ts for ts in ts_list if str(ts) not in cache]
    if todo:
        print(f"   descargando {len(todo)} ventanas ({len(ts_list)-len(todo)} en caché)...")
        done = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(fetch_preopen, ts): ts for ts in todo}
            for fut in as_completed(futs):
                ts = futs[fut]
                try:
                    res = fut.result()
                except Exception:
                    res = None
                with _lock:
                    cache[str(ts)] = res or {}
                done += 1
                if done % 200 == 0:
                    print(f"      {done}/{len(todo)}", flush=True)
                    with _lock:
                        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
                        with open(CACHE, "w") as fh:
                            json.dump(cache, fh)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as fh:
            json.dump(cache, fh)

    return {int(t): cache[str(t)] for t in ts_list if str(t) in cache and cache[str(t)]}


def stats(obs: list[tuple[float, bool]]) -> dict:
    n = len(obs)
    wr = sum(1 for _, w in obs if w) / n
    mp = sum(p for p, _ in obs) / n
    edges = [(1.0 if w else 0.0) - p for p, w in obs]
    gap = sum(edges) / n
    var = sum((e - gap) ** 2 for e in edges) / max(n - 1, 1)
    se = math.sqrt(var / n)
    return {"n": n, "wr": wr, "price": mp, "gap": gap,
            "t": gap / se if se else 0.0, "roi": gap / mp if mp else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser(description="Edge at the pre-open price")
    ap.add_argument("--max-windows", type=int, default=2500)
    args = ap.parse_args()

    with open(GAMMA_CACHE) as fh:
        outcomes = {int(k): v for k, v in json.load(fh).items() if v}
    lo, hi = min(outcomes), max(outcomes)
    feat = {f["ts"]: f for f in build_features(load_klines(lo - 200 * WINDOW, hi + WINDOW))}

    streak_ts = sorted(
        ts for ts, f in feat.items()
        if ts in outcomes and f.get("streak_len", 0) >= 3
    )[-args.max_windows:]

    print(f"\n{'='*78}")
    print("  ¿HAY EDGE AL PRECIO PREVIO A LA APERTURA?")
    print(f"{'='*78}\n")
    print(f"  {len(streak_ts)} ventanas con racha >=3")

    pre = collect(streak_ts)
    print(f"  {len(pre)} con cotización previa a la apertura\n")

    # How flat is the pre-open tape, and does it move with the streak?
    print(f"{'─'*78}")
    print("  A) ¿Descuenta el mercado la racha ANTES de abrir?")
    print(f"{'─'*78}")
    print(f"{'racha':<14}{'n':>6}{'precio UP previo':>20}{'precio del lado fade':>24}")
    for lo_len, hi_len in ((3, 4), (4, 5), (5, 6), (6, 99)):
        sel = [(feat[ts], pre[ts]) for ts in streak_ts
               if ts in pre and lo_len <= feat[ts]["streak_len"] < hi_len]
        if len(sel) < 30:
            continue
        up_avg = sum(p["pre_price"] for _, p in sel) / len(sel)
        fade_prices = [
            (p["pre_price"] if f["streak_dir"] == "DOWN" else round(1 - p["pre_price"], 4))
            for f, p in sel
        ]
        fade_avg = sum(fade_prices) / len(fade_prices)
        label = f">={lo_len}" if hi_len == 99 else f"{lo_len}"
        print(f"racha {label:<8}{len(sel):>6}{up_avg:>20.4f}{fade_avg:>24.4f}")
    print("\n  Si el mercado descontase la racha, el precio del lado fade subiría")
    print("  con la longitud de la racha. Plano = no la descuenta.")

    # The decisive economic test at the pre-open price.
    print(f"\n{'─'*78}")
    print("  B) P&L comprando el lado fade AL PRECIO PREVIO")
    print(f"{'─'*78}")
    print(f"{'señal':<20}{'n':>6}{'acierto':>10}{'precio':>9}{'brecha':>10}{'t':>8}{'ROI':>9}")

    results = {}
    for min_len in (3, 4, 5, 6):
        obs = []
        for ts in streak_ts:
            p = pre.get(ts)
            f = feat[ts]
            if not p or f["streak_len"] < min_len:
                continue
            call = "DOWN" if f["streak_dir"] == "UP" else "UP"
            price = p["pre_price"] if call == "UP" else round(1 - p["pre_price"], 4)
            if not (0.01 <= price <= 0.99):
                continue
            obs.append((price, call == outcomes[ts]))
        if len(obs) < 40:
            continue
        s = stats(obs)
        results[min_len] = s
        print(f"{'fade racha>='+str(min_len):<20}{s['n']:>6}{s['wr']*100:>9.1f}%"
              f"{s['price']:>9.3f}{s['gap']:>+10.4f}{s['t']:>+8.2f}{s['roi']*100:>+8.2f}%")

    # Control: follow the streak instead, at the same pre-open price.
    obs = []
    for ts in streak_ts:
        p, f = pre.get(ts), feat[ts]
        if not p or f["streak_len"] < 4:
            continue
        call = f["streak_dir"]
        price = p["pre_price"] if call == "UP" else round(1 - p["pre_price"], 4)
        if 0.01 <= price <= 0.99:
            obs.append((price, call == outcomes[ts]))
    if len(obs) >= 40:
        s = stats(obs)
        print(f"{'seguir racha>=4':<20}{s['n']:>6}{s['wr']*100:>9.1f}%"
              f"{s['price']:>9.3f}{s['gap']:>+10.4f}{s['t']:>+8.2f}{s['roi']*100:>+8.2f}%")

    # How much does waiting cost? Same windows, price one minute in.
    print(f"\n{'─'*78}")
    print("  C) EL COSTE DE ESPERAR — mismo trade, precio a +60s")
    print(f"{'─'*78}")
    obs_pre, obs_post = [], []
    for ts in streak_ts:
        p, f = pre.get(ts), feat[ts]
        if not p or f["streak_len"] < 4 or p.get("post_price") is None:
            continue
        call = "DOWN" if f["streak_dir"] == "UP" else "UP"
        won = call == outcomes[ts]
        pp = p["pre_price"] if call == "UP" else round(1 - p["pre_price"], 4)
        qq = p["post_price"] if call == "UP" else round(1 - p["post_price"], 4)
        if 0.01 <= pp <= 0.99:
            obs_pre.append((pp, won))
        if 0.01 <= qq <= 0.99:
            obs_post.append((qq, won))
    if obs_pre and obs_post:
        a, b = stats(obs_pre), stats(obs_post)
        print(f"  {'entrada previa a la apertura':<34}n={a['n']:>5}  precio={a['price']:.3f}"
              f"  brecha={a['gap']:+.4f}  ROI={a['roi']*100:+.2f}%")
        print(f"  {'entrada a +60s (lo medido antes)':<34}n={b['n']:>5}  precio={b['price']:.3f}"
              f"  brecha={b['gap']:+.4f}  ROI={b['roi']*100:+.2f}%")

    r4 = results.get(4)
    if r4:
        print(f"\n{'='*78}")
        print("  LECTURA")
        print(f"{'='*78}")
        print(f"  fade racha>=4 al precio previo: acierta {r4['wr']*100:.1f}%, "
              f"paga {r4['price']:.3f}")
        print(f"  Brecha = {r4['gap']:+.4f} por share  (t = {r4['t']:+.2f})")
        if r4["t"] > 1.96:
            print(f"\n  ✅ EDGE ESTADÍSTICAMENTE SIGNIFICATIVO al precio de entrada real.")
            print(f"     ROI bruto {r4['roi']*100:+.2f}% por operación, ANTES de spread.")
            print(f"     Falta comprobar: ¿se puede COMPRAR a ese precio, o el ask")
            print(f"     está muy por encima del mid? (ver medición del libro).")
        else:
            print(f"\n  Sin significancia (|t| < 1.96). El precio previo tampoco")
            print(f"  deja edge explotable.")


if __name__ == "__main__":
    main()
