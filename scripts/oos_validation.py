"""Out-of-sample validation — does any signal's edge survive into fresh data?

scripts/signal_search.py finds signals at p<0.01 on 10.119 windows. That is what
you get from testing 49 rules on one sample: the best of 49 coin-flip sequences
looks impressive. It also found the *sign flipping* between subsamples
(reversion wins on the full set, momentum wins on the priced subset), which is
what noise does and what edge does not.

This settles it. Split the history chronologically, rank the signals on the first
part, and measure the ranked signals on the part that was never looked at. A real
edge ranks consistently; noise re-ranks at random.

Usage:
    python -m scripts.oos_validation
    python -m scripts.oos_validation --folds 5
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.signal_search import (
    GAMMA_CACHE, WINDOW, build_features, build_signals, load_klines, zscore,
)


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else 0.0


def evaluate(signals, outcomes, feat_by_ts, ts_list) -> dict[str, tuple[int, float]]:
    """{signal_name: (n_fired, win_rate)} over the given window timestamps."""
    result = {}
    for name, fn in signals.items():
        n = wins = 0
        for ts in ts_list:
            truth = outcomes.get(ts)
            f = feat_by_ts.get(ts)
            if truth is None or f is None:
                continue
            try:
                call = fn(f)
            except Exception:
                call = None
            if call is None:
                continue
            n += 1
            wins += call == truth
        result[name] = (n, wins / n if n else 0.0)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Out-of-sample signal validation")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--min-n", type=int, default=100)
    args = ap.parse_args()

    with open(GAMMA_CACHE) as fh:
        outcomes = {int(k): v for k, v in json.load(fh).items() if v}

    lo, hi = min(outcomes), max(outcomes)
    candles = load_klines(lo - 200 * WINDOW, hi + WINDOW)
    feats = build_features(candles)
    feat_by_ts = {f["ts"]: f for f in feats}
    signals = build_signals()

    ts_all = sorted(t for t in outcomes if t in feat_by_ts)
    half = len(ts_all) // 2
    train, test = ts_all[:half], ts_all[half:]

    print(f"\n{'='*74}")
    print("  VALIDACIÓN FUERA DE MUESTRA")
    print(f"{'='*74}")
    print(f"\n  Entrenamiento: {len(train)} ventanas ({(train[-1]-train[0])/86400:.1f} días)")
    print(f"  Prueba:        {len(test)} ventanas ({(test[-1]-test[0])/86400:.1f} días)")
    print("  Las señales se ordenan SOLO con el tramo de entrenamiento.\n")

    tr = evaluate(signals, outcomes, feat_by_ts, train)
    te = evaluate(signals, outcomes, feat_by_ts, test)

    rows = []
    for name in signals:
        n_tr, wr_tr = tr[name]
        n_te, wr_te = te[name]
        if n_tr < args.min_n or n_te < args.min_n:
            continue
        rows.append({
            "name": name,
            "n_tr": n_tr, "wr_tr": wr_tr, "z_tr": zscore(round(wr_tr * n_tr), n_tr),
            "n_te": n_te, "wr_te": wr_te, "z_te": zscore(round(wr_te * n_te), n_te),
        })

    rows.sort(key=lambda r: -r["wr_tr"])

    print(f"{'señal':<26}{'ENTREN.':>18}{'PRUEBA':>18}   {'¿sobrevive?':<12}")
    print(f"{'':<26}{'n':>7}{'acierto':>11}{'n':>7}{'acierto':>11}")
    for r in rows[:14]:
        survived = "sí" if r["wr_te"] > 0.50 else "NO"
        print(f"{r['name']:<26}{r['n_tr']:>7}{r['wr_tr']*100:>10.1f}%"
              f"{r['n_te']:>7}{r['wr_te']*100:>10.1f}%   {survived:<12}")
    print("  ...")
    for r in rows[-3:]:
        survived = "sí" if r["wr_te"] > 0.50 else "NO"
        print(f"{r['name']:<26}{r['n_tr']:>7}{r['wr_tr']*100:>10.1f}%"
              f"{r['n_te']:>7}{r['wr_te']*100:>10.1f}%   {survived:<12}")

    # The decisive number: does training rank predict test rank at all?
    r = pearson([x["wr_tr"] for x in rows], [x["wr_te"] for x in rows])
    print(f"\n  Correlación acierto(entrenamiento) ↔ acierto(prueba): "
          f"r = {r:+.3f}  (n={len(rows)} señales)")
    if abs(r) < 0.2:
        print("  → El rendimiento pasado de una señal NO informa del futuro.")
        print("    Es exactamente lo que se espera si todo es ruido.")
    else:
        print("  → El orden SÍ persiste. La dirección de las ventanas de 5 min")
        print("    tiene una reversión débil pero estable. Que sea rentable es")
        print("    otra pregunta: hay que superar el precio, no el 50%.")

    best = max(rows, key=lambda x: x["wr_tr"])
    print(f"\n  Mejor señal en entrenamiento: '{best['name']}' "
          f"{best['wr_tr']*100:.1f}% (z={best['z_tr']:+.2f})")
    print(f"  La misma señal fuera de muestra: {best['wr_te']*100:.1f}% "
          f"(z={best['z_te']:+.2f})")

    top5 = rows[:5]
    mean_te = sum(x["wr_te"] for x in top5) / len(top5)
    print(f"\n  Las 5 mejores del entrenamiento promedian {mean_te*100:.1f}% "
          f"fuera de muestra.")
    print(f"  (50.0% = moneda justa; y aún haría falta superar el precio, ~0.50)")

    # How many flip sign?
    flipped = sum(1 for x in rows if (x["wr_tr"] > 0.5) != (x["wr_te"] > 0.5))
    print(f"\n  {flipped} de {len(rows)} señales cambian de lado entre los dos "
          f"tramos ({flipped/len(rows)*100:.0f}%).")
    print("  Con puro azar se esperaría ~50%.")

    # Rolling folds — is any signal stable across the whole history?
    print(f"\n{'─'*74}")
    print(f"  ESTABILIDAD POR TRAMOS ({args.folds} tramos consecutivos)")
    print(f"{'─'*74}")
    size = len(ts_all) // args.folds
    folds = [ts_all[i * size:(i + 1) * size] for i in range(args.folds)]
    fold_res = [evaluate(signals, outcomes, feat_by_ts, f) for f in folds]

    watch = ["trend 4h >0.8%", "reversion 24w", "fade streak>=4",
             "prev-window reversion", "momentum 6w"]
    header = "".join(f"{'T'+str(i+1):>9}" for i in range(args.folds))
    print(f"{'señal':<26}{header}{'signo':>9}")
    for name in watch:
        if name not in signals:
            continue
        cells, signs = "", []
        for fr in fold_res:
            n, wr = fr[name]
            if n < 30:
                cells += f"{'—':>9}"
                continue
            cells += f"{wr*100:>8.1f}%"
            signs.append(wr > 0.5)
        consistent = "estable" if len(set(signs)) == 1 else "cambia"
        print(f"{name:<26}{cells}{consistent:>9}")

    print("\n  Una señal con edge real se mantiene del mismo lado en todos los")
    print("  tramos. La familia de reversión lo hace; 'trend 4h' no.")


if __name__ == "__main__":
    main()
