"""Does filtering by market regime turn the fade signal into a profitable one?

Two competing intuitions, both testable against the same 35 days:

  A) "Trade only when the market is sideways / low volatility, so a trend can't
     run us over."  — avoid the regime where reversion fails.

  B) The ORIGINAL Streak Snapper filter, which the production bot never
     implemented: trade only when the streak is OVEREXTENDED relative to recent
     range (|cumulative move over 4 windows| > 3 × ATR of the last hour).
     Revisar Estrategias/RESUMEN_STREAK_SNAPPER.md claims this is the whole
     edge: 54.3% with the filter, 50.7% without it, over 104.762 windows.

These are not the same idea. (A) says "trade when nothing is happening".
(B) says "trade when the recent move is big relative to normal" — a stretched
rubber band, which is a volatility *ratio*, not a volatility *level*.

Prices are the pre-open quote (scripts/preopen_edge.py), plus half of the
measured 1-cent spread, which is what the bot can actually buy at.

Usage:
    python -m scripts.regime_filter
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.signal_search import GAMMA_CACHE, WINDOW, build_features, load_klines

PREOPEN_CACHE = "data/preopen_prices.json"
HALF_SPREAD = 0.005


def enrich(candles: list[dict], feats: list[dict]) -> None:
    """Add the original strategy's ATR-stretch features, in place.

    atr12     mean |relative move| over the last 12 windows (1 hour)
    cum4      signed relative move summed over the last 4 windows
    stretch   |cum4| / atr12 — how far the streak has run vs its recent normal
    range24   (high-low)/close over the last 24 windows — "is it sideways?"
    """
    for i, f in enumerate(feats):
        hist = candles[:i]
        if len(hist) < 30:
            continue

        moves = [(h["close"] - h["open"]) / h["open"] for h in hist[-12:]]
        atr12 = sum(abs(m) for m in moves) / len(moves)
        f["atr12"] = atr12

        cum4 = sum((h["close"] - h["open"]) / h["open"] for h in hist[-4:])
        f["cum4"] = cum4
        f["stretch"] = abs(cum4) / atr12 if atr12 > 0 else 0.0

        seg = hist[-24:]
        hi = max(h["high"] for h in seg)
        lo = min(h["low"] for h in seg)
        f["range24"] = (hi - lo) / seg[-1]["close"] if seg[-1]["close"] else 0.0


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
    ap = argparse.ArgumentParser(description="Regime filters on the fade signal")
    ap.add_argument("--streak-min", type=int, default=4)
    args = ap.parse_args()

    with open(GAMMA_CACHE) as fh:
        outcomes = {int(k): v for k, v in json.load(fh).items() if v}
    with open(PREOPEN_CACHE) as fh:
        pre = {int(k): v for k, v in json.load(fh).items() if v}

    lo, hi = min(outcomes), max(outcomes)
    candles = load_klines(lo - 200 * WINDOW, hi + WINDOW)
    feats = build_features(candles)
    enrich(candles, feats)
    feat = {f["ts"]: f for f in feats}

    days = (hi - lo) / 86400

    def collect(pred) -> list[tuple[float, bool]]:
        """pred(f) -> True to trade this window. Always fades the streak."""
        obs = []
        for ts, f in feat.items():
            if ts not in outcomes or ts not in pre:
                continue
            if f.get("streak_len", 0) < args.streak_min:
                continue
            if not pred(f):
                continue
            call = "DOWN" if f["streak_dir"] == "UP" else "UP"
            p = pre[ts]["pre_price"]
            px = (p if call == "UP" else round(1 - p, 4)) + HALF_SPREAD
            if 0.01 <= px <= 0.99:
                obs.append((px, call == outcomes[ts]))
        return obs

    def row(label: str, pred, note: str = "") -> dict | None:
        obs = collect(pred)
        if len(obs) < 40:
            print(f"{label:<30}{len(obs):>6}   (muestra insuficiente)")
            return None
        s = stats(obs)
        print(f"{label:<30}{s['n']:>6}{s['wr']*100:>9.1f}%{s['price']:>8.3f}"
              f"{s['roi']*100:>+9.2f}%{s['t']:>+7.2f}{s['n']/days:>8.1f}  {note}")
        return s

    print(f"\n{'='*88}")
    print(f"  FILTROS DE RÉGIMEN SOBRE EL FADE (racha>={args.streak_min})")
    print(f"  Precio previo a la apertura + medio spread. {days:.1f} días.")
    print(f"{'='*88}\n")
    print(f"{'filtro':<30}{'n':>6}{'acierto':>9}{'precio':>8}{'ROI':>9}{'t':>7}{'op/día':>8}")
    print(f"{'─'*88}")

    base = row("sin filtro (producción)", lambda f: True)

    print(f"\n  ── B) Filtro ATR de la estrategia original ──")
    for mult in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
        row(f"stretch > {mult:.1f}x ATR", lambda f, m=mult: f.get("stretch", 0) > m)

    print(f"\n  ── A) Tu idea: mercado lateral / poca volatilidad ──")
    vols = sorted(f["atr12"] for f in feats if f.get("atr12"))
    if vols:
        q = lambda x: vols[int(len(vols) * x)]
        for name, lo_q, hi_q in (
            ("vol MUY baja (<p25)", 0.0, 0.25),
            ("vol baja (<p40)", 0.0, 0.40),
            ("vol media (p25-p75)", 0.25, 0.75),
            ("vol alta (>p60)", 0.60, 1.0),
            ("vol MUY alta (>p75)", 0.75, 1.0),
        ):
            a = q(lo_q) if lo_q > 0 else 0.0
            b = q(hi_q) if hi_q < 1.0 else 9.9
            row(name, lambda f, a=a, b=b: a <= f.get("atr12", 0) < b)

    print(f"\n  ── A') Lateral medido por rango de 2h ──")
    rngs = sorted(f["range24"] for f in feats if f.get("range24"))
    if rngs:
        qr = lambda x: rngs[int(len(rngs) * x)]
        for name, lo_q, hi_q in (
            ("rango estrecho (<p30)", 0.0, 0.30),
            ("rango medio (p30-p70)", 0.30, 0.70),
            ("rango amplio (>p70)", 0.70, 1.0),
        ):
            a = qr(lo_q) if lo_q > 0 else 0.0
            b = qr(hi_q) if hi_q < 1.0 else 9.9
            row(name, lambda f, a=a, b=b: a <= f.get("range24", 0) < b)

    print(f"\n  ── Combinado: sobreextensión DENTRO de un rango estrecho ──")
    if rngs:
        med = qr(0.5)
        row("stretch>2 y rango<mediana",
            lambda f: f.get("stretch", 0) > 2.0 and f.get("range24", 9) < med)
        row("stretch>2 y rango>mediana",
            lambda f: f.get("stretch", 0) > 2.0 and f.get("range24", 0) >= med)

    print(f"\n  ── Hora del día (UTC) ──")
    for name, a, b in (
        ("Asia 00-08h", 0, 8),
        ("Europa 07-13h", 7, 13),
        ("US 13-21h", 13, 21),
        ("noche US 21-24h", 21, 24),
    ):
        row(name, lambda f, a=a, b=b: a <= f.get("hour_utc", -1) < b)

    print(f"\n{'─'*88}")
    if base:
        print(f"  Referencia sin filtro: {base['roi']*100:+.2f}% ROI, "
              f"t={base['t']:+.2f}, {base['n']/days:.1f} op/día")
        print("\n  Un filtro solo sirve si sube el ROI *y* mantiene suficientes")
        print("  operaciones para que el resultado sea medible. Subir el ROI")
        print("  cortando la muestra a 3 op/día no demuestra nada en 35 días.")


if __name__ == "__main__":
    main()
