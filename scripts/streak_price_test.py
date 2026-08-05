"""Does the fade-streak signal beat the price it must pay? (SUPERSEDED)

⚠️ Its conclusion — "the market prices the streak exactly" — is an artifact.
CLOB /prices-history has 1-minute fidelity, so "the first quote at or after
+15 s" actually lands at a median of +67 s into the window, when the price
already knows how the window is going. That is not where the bot enters.

Use scripts/preopen_edge.py, which samples the last quote BEFORE the window
opens. At that price the answer flips: the pre-open tape is flat at ~0.505 and
does not price the streak. Kept because the +60 s comparison is still the
cleanest measurement of what entering late costs.

Original docstring follows.
─────────────────────────────────────────────────────────────────────────────
The decisive test: does the fade-streak signal beat the price it must pay?

Everything else has been settled. From scripts/oos_validation.py:

  - Signal rank persists out of sample (r=+0.727), so 5-min direction is NOT
    pure noise — there is a weak, stable reversion effect.
  - `fade streak>=4` is the strongest and most stable member of that family:
    54.3% in-sample, 53.4% out of sample.

And from scripts/price_calibration.py the market is calibrated overall
(z=+0.00, n=3.986). So the only question left that decides whether this bot can
make money is:

    when a streak fires, does the market ALREADY charge for the reversion?

54% accuracy is worthless at a price of 0.54 and excellent at 0.50. The earlier
run hinted the market knows (paid 0.522 for 51.8%) but only had n=280.

This collects prices for EVERY streak window across the full 35 days and tests
the gap directly, with the spread cost that live execution actually pays.

Usage:
    python -m scripts.streak_price_test
    python -m scripts.streak_price_test --spread 0.02
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.price_calibration import (
    CACHE_PATH, _load_cache, _save_cache, _cache_lock, fetch_entry_price,
)
from scripts.signal_search import GAMMA_CACHE, WINDOW, build_features, load_klines


def collect_prices(ts_list: list[int]) -> dict[int, dict]:
    """Download entry prices for `ts_list`, reusing and extending the shared cache."""
    cache = _load_cache()
    todo = [ts for ts in ts_list if str(ts) not in cache]

    if todo:
        print(f"   descargando {len(todo)} ventanas nuevas "
              f"({len(ts_list) - len(todo)} ya en caché)...")
        done = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
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
                if done % 200 == 0:
                    print(f"      {done}/{len(todo)}", flush=True)
                    with _cache_lock:
                        _save_cache(cache)
        _save_cache(cache)

    return {int(ts): cache[str(ts)] for ts in ts_list
            if str(ts) in cache and cache[str(ts)]}


def report(label: str, obs: list[tuple[float, bool]], spread: float) -> dict | None:
    """obs = [(price_paid, won)]. Prints accuracy vs price and the resulting edge."""
    n = len(obs)
    if n < 40:
        print(f"{label:<22}{n:>6}   (muestra insuficiente)")
        return None

    wins = sum(1 for _, w in obs if w)
    wr = wins / n
    mean_p = sum(p for p, _ in obs) / n

    edges = [(1.0 if w else 0.0) - p for p, w in obs]
    gap = sum(edges) / n
    var = sum((e - gap) ** 2 for e in edges) / max(n - 1, 1)
    se = math.sqrt(var / n)
    t = gap / se if se else 0.0

    # Live execution crosses the spread: you pay the ask, not the mid.
    edges_live = [(1.0 if w else 0.0) - (p + spread / 2) for p, w in obs]
    gap_live = sum(edges_live) / n
    roi_live = gap_live / (mean_p + spread / 2)

    print(f"{label:<22}{n:>6}{wr*100:>9.1f}%{mean_p:>9.3f}"
          f"{gap:>+9.3f}{t:>+7.2f}{roi_live*100:>+10.2f}%")
    return {"n": n, "wr": wr, "price": mean_p, "gap": gap, "t": t,
            "roi_live": roi_live}


def main() -> None:
    ap = argparse.ArgumentParser(description="Does the streak signal beat its price?")
    ap.add_argument("--spread", type=float, default=0.02,
                    help="round-trip spread assumed for live execution")
    args = ap.parse_args()

    with open(GAMMA_CACHE) as fh:
        outcomes = {int(k): v for k, v in json.load(fh).items() if v}

    lo, hi = min(outcomes), max(outcomes)
    candles = load_klines(lo - 200 * WINDOW, hi + WINDOW)
    feat_by_ts = {f["ts"]: f for f in build_features(candles)}

    print(f"\n{'='*76}")
    print("  ¿SUPERA LA SEÑAL DE RACHA AL PRECIO QUE DEBE PAGAR?")
    print(f"{'='*76}\n")

    # Every window where a streak of >=3 exists — the union of all fade variants.
    streak_ts = sorted(
        ts for ts, f in feat_by_ts.items()
        if ts in outcomes and f.get("streak_len", 0) >= 3
    )
    print(f"  {len(streak_ts)} ventanas con racha >=3 en {(hi-lo)/86400:.1f} días")

    prices = collect_prices(streak_ts)
    print(f"  {len(prices)} con precio recuperado\n")

    print(f"  Spread asumido en ejecución: {args.spread*100:.0f} centavos")
    print(f"  'brecha' = acierto − precio (edge por share, al mid)")
    print(f"  'ROI vivo' incluye cruzar medio spread\n")

    print(f"{'─'*76}")
    print(f"{'señal':<22}{'n':>6}{'acierto':>9}{'precio':>9}{'brecha':>9}"
          f"{'t':>7}{'ROI vivo':>10}")
    print(f"{'─'*76}")

    summary = {}
    for min_len in (3, 4, 5, 6):
        obs = []
        for ts in streak_ts:
            f, px = feat_by_ts[ts], prices.get(ts)
            if not px or f["streak_len"] < min_len:
                continue
            call = "DOWN" if f["streak_dir"] == "UP" else "UP"
            p_up = px["up_price"]
            p = p_up if call == "UP" else round(1.0 - p_up, 4)
            if not (0.01 <= p <= 0.99):
                continue
            obs.append((p, call == outcomes[ts]))
        summary[min_len] = report(f"fade racha>={min_len}", obs, args.spread)

    # Control: the same windows, betting WITH the streak instead of against it.
    print()
    for min_len in (4,):
        obs = []
        for ts in streak_ts:
            f, px = feat_by_ts[ts], prices.get(ts)
            if not px or f["streak_len"] < min_len:
                continue
            call = f["streak_dir"]
            p_up = px["up_price"]
            p = p_up if call == "UP" else round(1.0 - p_up, 4)
            if not (0.01 <= p <= 0.99):
                continue
            obs.append((p, call == outcomes[ts]))
        report(f"seguir racha>={min_len}", obs, args.spread)

    print(f"{'─'*76}")

    # ── the interpretation that matters ──────────────────────────────────────
    r4 = summary.get(4)
    if r4:
        print(f"\n  LECTURA — fade racha>=4")
        print(f"    Acierta el {r4['wr']*100:.1f}% de las veces.")
        print(f"    Paga {r4['price']:.3f} de media, que implica {r4['price']*100:.1f}%.")
        breakeven = r4["price"]
        print(f"\n    Punto de equilibrio: hace falta acertar > {breakeven*100:.1f}%.")
        if r4["wr"] > breakeven:
            print(f"    Acierta {r4['wr']*100:.1f}% → margen de "
                  f"{(r4['wr']-breakeven)*100:+.1f} puntos al mid.")
        else:
            print(f"    Acierta {r4['wr']*100:.1f}% → déficit de "
                  f"{(r4['wr']-breakeven)*100:+.1f} puntos, ANTES del spread.")
        print(f"\n    Significancia de la brecha: t = {r4['t']:+.2f} "
              f"(hace falta >1.96)")
        print(f"    ROI tras cruzar el spread: {r4['roi_live']*100:+.2f}%")

        need_n = (1.96 / max(abs(r4["t"]), 1e-9)) ** 2 * r4["n"] if r4["t"] else 0
        if abs(r4["t"]) < 1.96 and need_n:
            print(f"\n    Para que esta brecha fuese significativa harían falta "
                  f"~{need_n:,.0f} operaciones")
            print(f"    ({need_n/ (r4['n']/(hi-lo)*86400) if r4['n'] else 0:,.0f} días "
                  f"al ritmo actual).")


if __name__ == "__main__":
    main()
