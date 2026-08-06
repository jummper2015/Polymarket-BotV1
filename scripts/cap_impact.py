"""¿Cuánto cambia el edge de ss_fade al aplicar el cap como filtro de entrada?

`docs/RUTA.md` Fase 8 mide ss_fade **sin cap**: +3,74% por operación sobre 1.150
señales. Pero el bot no opera esa estrategia: opera la que descarta la ventana
cuando el ask supera `SS_FADE_LIMIT_CAP`. Este script mide esa segunda, que es
la que corre.

La distinción no es cosmética. Antes de `SKIP_ASK_ABOVE_CAP` el trader hacía
`min(cap, ask)`, o sea dejaba una puja *por debajo* del ask cuando la ventana
estaba cara: en paper eso se anotaba como un llenado que nunca habría ocurrido,
y en real dejaba una GTC que el bot ni verifica ni cancela. Las dos lecturas del
cap —filtro contra puja pasiva— son estrategias distintas y miden distinto.

Todo sale de los caches en disco, sin red:
    data/gamma_outcomes.json   — resultado oficial por ventana
    data/preopen_prices.json   — última cotización ANTES de la apertura
    data/klines_5m_cache.json  — velas para reconstruir la racha

Uso:
    python scripts/cap_impact.py
    python scripts/cap_impact.py --min-len 5 --caps 0.50,0.52,0.54
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.signal_search import WINDOW, build_features, load_klines

GAMMA_CACHE = "data/gamma_outcomes.json"
PREOPEN_CACHE = "data/preopen_prices.json"

# La cotización previa es el mid; el bot paga el ask. El spread medido en el
# libro real de estas ventanas es de 1 centavo (docs/RUTA.md Fase 8), así que
# medio spread es el ajuste correcto para pasar de mid a ask.
HALF_SPREAD = 0.005


def load() -> tuple[dict, dict, dict]:
    with open(GAMMA_CACHE) as fh:
        gamma = {int(k): v for k, v in json.load(fh).items()}
    with open(PREOPEN_CACHE) as fh:
        pre = {int(k): v for k, v in json.load(fh).items()}
    ts = sorted(gamma)
    candles = load_klines(ts[0] - 200 * WINDOW, ts[-1] + WINDOW)
    feats = {f["ts"]: f for f in build_features(candles)}
    return gamma, pre, feats


def observations(gamma, pre, feats, min_len, cap, lo=None, hi=None, follow=False):
    """(precio pagado, ganó) por ventana con señal y precio conocido."""
    out = []
    for ts in sorted(gamma):
        if lo is not None and ts < lo:
            continue
        if hi is not None and ts >= hi:
            continue
        f = feats.get(ts)
        p = pre.get(ts)
        if not f or not p or p.get("pre_price") is None:
            continue
        if not f.get("streak_len") or f["streak_len"] < min_len:
            continue
        call = f["streak_dir"] if follow else ("DOWN" if f["streak_dir"] == "UP" else "UP")
        up = p["pre_price"]
        price = round(up if call == "UP" else 1 - up, 4) + HALF_SPREAD
        if cap is not None and price > cap:
            continue
        out.append((price, call == gamma[ts]))
    return out


def stats(obs) -> dict | None:
    """ROI por operación y su t. Cada operación arriesga `precio` y paga 1 o 0."""
    n = len(obs)
    if n < 3:
        return None
    rois = [((1.0 if won else 0.0) - px) / px for px, won in obs]
    mu = sum(rois) / n
    var = sum((r - mu) ** 2 for r in rois) / (n - 1)
    sd = math.sqrt(var)
    return {
        "n": n,
        "win_rate": sum(1 for _, w in obs if w) / n * 100,
        "avg_price": sum(px for px, _ in obs) / n,
        "roi": mu * 100,
        "t": mu / (sd / math.sqrt(n)) if sd else 0.0,
        "sd": sd,
        "mu": mu,
    }


def show(label: str, s: dict | None) -> None:
    if not s:
        print(f"{label:34s} n<3 — sin muestra")
        return
    print(
        f"{label:34s} n={s['n']:5d} acierto={s['win_rate']:5.1f}% "
        f"precio={s['avg_price']:.3f} ROI/op={s['roi']:+6.2f}% t={s['t']:+5.2f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-len", type=int, default=4, help="racha mínima (SS_FADE_STREAK_MIN)")
    ap.add_argument("--caps", default="0.60,0.56,0.54,0.52,0.50,0.48")
    args = ap.parse_args()
    caps = [float(c) for c in args.caps.split(",")]

    gamma, pre, feats = load()
    ts = sorted(gamma)
    days = (ts[-1] - ts[0]) / 86400
    m = args.min_len

    total = sum(
        1 for t in gamma
        if feats.get(t) and feats[t].get("streak_len") and feats[t]["streak_len"] >= m
    )
    base = observations(gamma, pre, feats, m, None)
    print(f"muestra: {len(gamma)} ventanas Gamma, {days:.1f} días")
    print(
        f"racha>={m}: {total} señales ({total/days:.1f}/día); "
        f"con precio previo: {len(base)} ({len(base)/total*100:.1f}% de cobertura)\n"
    )

    print("── el cap como filtro de entrada ──")
    show(f"fade>={m} sin cap", stats(base))
    for cap in caps:
        obs = observations(gamma, pre, feats, m, cap)
        frac = len(obs) / len(base) * 100 if base else 0.0
        show(f"fade>={m} cap={cap:.2f} ({frac:4.1f}% ejec.)", stats(obs))

    print("\n── control: comprar sin señal a ese mismo precio ──")
    # Si el edge viniera de "comprar barato" y no de la señal, esto también daría
    # positivo. No lo da.
    for cap in (0.52,):
        for side in ("UP", "DOWN"):
            obs = []
            for t in sorted(gamma):
                p = pre.get(t)
                if not p or p.get("pre_price") is None:
                    continue
                px = round(p["pre_price"] if side == "UP" else 1 - p["pre_price"], 4)
                px += HALF_SPREAD
                if px > cap:
                    continue
                obs.append((px, side == gamma[t]))
            show(f"{side} siempre, cap={cap:.2f}", stats(obs))

    print("\n── la contraparte: seguir la racha (lo que hace ss_trend) ──")
    for cap in (None, 0.52):
        obs = observations(gamma, pre, feats, m, cap, follow=True)
        show(f"follow>={m} cap={cap}", stats(obs))

    print("\n── estabilidad: cuartos de ~8,8 días ──")
    lo, hi = ts[0], ts[-1] + WINDOW
    q = (hi - lo) // 4
    for cap in (0.60, 0.52):
        print(f"  cap {cap:.2f}:")
        for i in range(4):
            obs = observations(gamma, pre, feats, m, cap, lo + i * q, lo + (i + 1) * q)
            show(f"    T{i+1}", stats(obs))

    print("\n── cuánta muestra hace falta ──")
    for cap in (0.60, 0.52):
        obs = observations(gamma, pre, feats, m, cap)
        s = stats(obs)
        if not s:
            continue
        rate = len(obs) / days
        if s["mu"] <= 0:
            print(f"cap={cap:.2f}: ROI medio {s['roi']:+.2f}% — ninguna n lo salva")
            continue
        need = (1.96 * s["sd"] / s["mu"]) ** 2
        print(
            f"cap={cap:.2f}: {rate:5.1f} ops/día → {need:.0f} ops para t=1,96 "
            f"= {need/rate:.0f} días"
        )

    print(
        "\nAviso: dentro de muestra. El 0,52 se eligió por el valor justo de la\n"
        "señal (0,538), no con este cálculo, y la consistencia por cuartos es la\n"
        "mitigación — pero docs/RUTA.md avisa de que se probaron ~20 filtros\n"
        "sobre estos mismos 35 días, así que t=+2,40 es nominal."
    )


if __name__ == "__main__":
    main()
