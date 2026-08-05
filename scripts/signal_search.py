"""Signal search — does ANY directional rule beat a coin flip on 5-min BTC windows?

The 4h trend signal measures 47–49% (docs/RUTA.md, Fase 4.7). Before replacing it
we need to know whether the *category* is empty: is the direction of a 5-min
Polymarket window predictable from public price history at all?

Two questions, in order:

  1. STATISTICAL  — does any signal call direction better than 50%?
  2. ECONOMIC     — does any signal make money at the price the market charges?

(2) is the one that matters and it is strictly harder: the market is calibrated
(scripts/price_calibration.py, z=+0.00 over 3.986 observations), so a signal must
beat not 50% but the price it forces you to pay.

Labels come from Gamma (settlement truth). Features come from Binance klines and
use only candles that closed strictly before the traded window opened.

Usage:
    python -m scripts.signal_search
    python -m scripts.signal_search --min-n 200
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Callable, Dict, List, Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BINANCE_BASE = "https://api.binance.com/api/v3"
KLINE_CACHE = "data/klines_5m_cache.json"
GAMMA_CACHE = "data/gamma_outcomes.json"
PRICE_CACHE = "data/price_calibration.json"

WINDOW = 300


# ── data ──────────────────────────────────────────────────────────────────────


def fetch_klines(interval: str, start_ts: int, end_ts: int) -> List[dict]:
    """All klines of `interval` covering [start_ts, end_ts], chained past the 1000 cap."""
    out: List[dict] = []
    cursor = start_ts * 1000
    end_ms = end_ts * 1000

    while cursor <= end_ms:
        params = {
            "symbol": "BTCUSDT",
            "interval": interval,
            "limit": 1000,
            "startTime": cursor,
        }
        batch = None
        for attempt in range(4):
            try:
                r = requests.get(f"{BINANCE_BASE}/klines", params=params, timeout=20)
                if r.status_code == 200:
                    batch = r.json()
                    break
                time.sleep(1.5 * (attempt + 1))
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        if not batch:
            break

        for k in batch:
            ts = int(k[0]) // 1000
            if ts > end_ts:
                break
            out.append({
                "ts": ts,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "trades": int(k[8]),
                "close_ms": int(k[6]),
            })
        if len(batch) < 1000:
            break
        cursor = int(batch[-1][0]) + 1
        print(f"      {len(out)} velas...", flush=True)

    return out


def load_klines(start_ts: int, end_ts: int) -> List[dict]:
    if os.path.exists(KLINE_CACHE):
        try:
            with open(KLINE_CACHE) as fh:
                cached = json.load(fh)
            if cached and cached[0]["ts"] <= start_ts and cached[-1]["ts"] >= end_ts:
                print(f"   {len(cached)} velas 5m en caché")
                return cached
        except (OSError, ValueError, KeyError, IndexError):
            pass

    print("   descargando velas 5m de Binance...")
    candles = fetch_klines("5m", start_ts, end_ts)
    os.makedirs(os.path.dirname(KLINE_CACHE), exist_ok=True)
    with open(KLINE_CACHE, "w") as fh:
        json.dump(candles, fh)
    print(f"   {len(candles)} velas descargadas y cacheadas")
    return candles


# ── stats ─────────────────────────────────────────────────────────────────────


def zscore(wins: int, n: int, p0: float = 0.5) -> float:
    if n == 0:
        return 0.0
    return (wins / n - p0) / math.sqrt(p0 * (1 - p0) / n)


# ── feature construction ──────────────────────────────────────────────────────


def build_features(candles: List[dict]) -> List[dict]:
    """Per-window features using only candles that closed BEFORE it opened.

    Index i describes the window candles[i] — everything read comes from
    candles[:i], never from candles[i] itself.
    """
    feats: List[dict] = []

    for i, c in enumerate(candles):
        f: dict = {"ts": c["ts"], "i": i}
        hist = candles[:i]

        if len(hist) < 60:
            feats.append(f)
            continue

        prev = hist[-1]
        f["prev_dir"] = "UP" if prev["close"] >= prev["open"] else "DOWN"
        f["prev_ret"] = (prev["close"] - prev["open"]) / prev["open"]

        # streak of consecutive same-direction closed windows
        streak_dir = f["prev_dir"]
        streak = 0
        for h in reversed(hist):
            d = "UP" if h["close"] >= h["open"] else "DOWN"
            if d == streak_dir:
                streak += 1
            else:
                break
        f["streak_len"] = streak
        f["streak_dir"] = streak_dir

        # cumulative return over the last n windows
        for n in (2, 3, 6, 12, 24, 48, 72, 144):
            if len(hist) >= n:
                seg = hist[-n:]
                f[f"mom{n}"] = (seg[-1]["close"] - seg[0]["open"]) / seg[0]["open"]

        # realised volatility: mean |return| of the last 24 windows (2h)
        rets = [abs((h["close"] - h["open"]) / h["open"]) for h in hist[-24:]]
        f["vol24"] = sum(rets) / len(rets)

        # ATR14 on 5-min candles, relative
        trs = []
        for j in range(len(hist) - 14, len(hist)):
            h = hist[j]
            pc = hist[j - 1]["close"]
            tr = max(h["high"] - h["low"], abs(h["high"] - pc), abs(h["low"] - pc))
            trs.append(tr / pc)
        f["atr14"] = sum(trs) / len(trs)

        # position of last close inside the recent range (0=low, 1=high)
        seg = hist[-24:]
        hi = max(h["high"] for h in seg)
        lo = min(h["low"] for h in seg)
        if hi > lo:
            f["range_pos"] = (prev["close"] - lo) / (hi - lo)

        # volume ratio: last window vs 24-window mean
        vols = [h["volume"] for h in hist[-24:]]
        mv = sum(vols) / len(vols)
        if mv > 0:
            f["vol_ratio"] = prev["volume"] / mv

        # 4h trend from the last CLOSED 4h block (the production signal)
        block = (c["ts"] // (4 * 3600)) * (4 * 3600)
        prev_block_start = block - 4 * 3600
        seg4 = [h for h in hist if prev_block_start <= h["ts"] < block]
        if len(seg4) >= 40:
            f["trend4h"] = (seg4[-1]["close"] - seg4[0]["open"]) / seg4[0]["open"]

        # 1h trend from the last CLOSED hour
        hour = (c["ts"] // 3600) * 3600
        seg1 = [h for h in hist if hour - 3600 <= h["ts"] < hour]
        if len(seg1) >= 10:
            f["trend1h"] = (seg1[-1]["close"] - seg1[0]["open"]) / seg1[0]["open"]

        f["hour_utc"] = time.gmtime(c["ts"]).tm_hour
        feats.append(f)

    return feats


# ── signal definitions ────────────────────────────────────────────────────────
# Each returns "UP" / "DOWN" / None (no trade) from a feature dict.

def _sig_momentum(key: str, thr: float) -> Callable:
    def s(f: dict) -> Optional[str]:
        v = f.get(key)
        if v is None or abs(v) < thr:
            return None
        return "UP" if v > 0 else "DOWN"
    return s


def _sig_reversion(key: str, thr: float) -> Callable:
    def s(f: dict) -> Optional[str]:
        v = f.get(key)
        if v is None or abs(v) < thr:
            return None
        return "DOWN" if v > 0 else "UP"
    return s


def _sig_fade_streak(min_len: int) -> Callable:
    def s(f: dict) -> Optional[str]:
        n = f.get("streak_len")
        if n is None or n < min_len:
            return None
        return "DOWN" if f["streak_dir"] == "UP" else "UP"
    return s


def _sig_follow_streak(min_len: int) -> Callable:
    def s(f: dict) -> Optional[str]:
        n = f.get("streak_len")
        if n is None or n < min_len:
            return None
        return f["streak_dir"]
    return s


def _sig_trend4h(thr: float) -> Callable:
    return _sig_momentum("trend4h", thr)


def _sig_cond(base: Callable, key: str, lo: float, hi: float) -> Callable:
    """Only trade `base` when feature `key` sits inside [lo, hi)."""
    def s(f: dict) -> Optional[str]:
        v = f.get(key)
        if v is None or not (lo <= v < hi):
            return None
        return base(f)
    return s


def build_signals() -> Dict[str, Callable]:
    sigs: Dict[str, Callable] = {}

    sigs["prev-window momentum"] = _sig_momentum("prev_ret", 0.0)
    sigs["prev-window reversion"] = _sig_reversion("prev_ret", 0.0)

    for n in (2, 3, 6, 12, 24, 48, 72, 144):
        sigs[f"momentum {n}w"] = _sig_momentum(f"mom{n}", 0.0)
        sigs[f"reversion {n}w"] = _sig_reversion(f"mom{n}", 0.0)

    for n in (3, 4, 5, 6, 7):
        sigs[f"fade streak>={n}"] = _sig_fade_streak(n)
        sigs[f"follow streak>={n}"] = _sig_follow_streak(n)

    for thr in (0.0, 0.003, 0.005, 0.008, 0.012, 0.02):
        sigs[f"trend 4h >{thr*100:.1f}%"] = _sig_trend4h(thr)
    for thr in (0.0, 0.002, 0.004):
        sigs[f"trend 1h >{thr*100:.1f}%"] = _sig_momentum("trend1h", thr)
        sigs[f"fade 1h >{thr*100:.1f}%"] = _sig_reversion("trend1h", thr)

    # conditioned variants: does momentum work only in a volatility regime?
    sigs["mom12w | low vol"] = _sig_cond(_sig_momentum("mom12", 0.0), "vol24", 0.0, 0.0006)
    sigs["mom12w | high vol"] = _sig_cond(_sig_momentum("mom12", 0.0), "vol24", 0.0012, 9.0)
    sigs["rev12w | high vol"] = _sig_cond(_sig_reversion("mom12", 0.0), "vol24", 0.0012, 9.0)
    sigs["mom12w | range top"] = _sig_cond(_sig_momentum("mom12", 0.0), "range_pos", 0.8, 1.01)
    sigs["rev12w | range top"] = _sig_cond(_sig_reversion("mom12", 0.0), "range_pos", 0.8, 1.01)
    sigs["mom12w | vol spike"] = _sig_cond(_sig_momentum("mom12", 0.0), "vol_ratio", 2.0, 99.0)
    sigs["prev-mom | vol spike"] = _sig_cond(_sig_momentum("prev_ret", 0.0), "vol_ratio", 2.0, 99.0)
    sigs["prev-rev | vol spike"] = _sig_cond(_sig_reversion("prev_ret", 0.0), "vol_ratio", 2.0, 99.0)

    # session effects
    sigs["always UP"] = lambda f: "UP"
    sigs["always DOWN"] = lambda f: "DOWN"
    sigs["UP | US hours"] = _sig_cond(lambda f: "UP", "hour_utc", 13, 21)
    sigs["UP | Asia hours"] = _sig_cond(lambda f: "UP", "hour_utc", 0, 8)

    return sigs


# ── evaluation ────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="Directional signal search")
    ap.add_argument("--min-n", type=int, default=150,
                    help="skip signals that fire fewer times than this")
    args = ap.parse_args()

    with open(GAMMA_CACHE) as fh:
        outcomes = {int(k): v for k, v in json.load(fh).items() if v}
    with open(PRICE_CACHE) as fh:
        prices = {int(k): v for k, v in json.load(fh).items() if v}

    lo, hi = min(outcomes), max(outcomes)
    print(f"\n{'='*74}")
    print("  BÚSQUEDA DE SEÑAL DIRECCIONAL — ventanas BTC 5m de Polymarket")
    print(f"{'='*74}")
    print(f"\n  Etiquetas (Gamma): {len(outcomes)} ventanas, {(hi-lo)/86400:.1f} días")
    print(f"  Precios (CLOB):    {len(prices)} ventanas\n")

    # need history before the first labelled window
    candles = load_klines(lo - 200 * WINDOW, hi + WINDOW)
    by_ts = {c["ts"]: c for c in candles}
    print(f"   emparejadas: {sum(1 for t in outcomes if t in by_ts)} ventanas\n")

    feats = build_features(candles)
    feat_by_ts = {f["ts"]: f for f in feats}

    signals = build_signals()

    rows = []
    for name, fn in signals.items():
        n = wins = 0
        pnl = 0.0
        cost_total = 0.0
        n_priced = 0
        for ts, truth in outcomes.items():
            f = feat_by_ts.get(ts)
            if f is None:
                continue
            try:
                call = fn(f)
            except Exception:
                call = None
            if call is None:
                continue
            n += 1
            won = call == truth
            wins += won

            # economic test: buy that side at the market's own price
            px = prices.get(ts)
            if px:
                p_up = px["up_price"]
                p = p_up if call == "UP" else round(1.0 - p_up, 4)
                if 0.01 <= p <= 0.99:
                    n_priced += 1
                    cost = 5 * p
                    cost_total += cost
                    pnl += (5 - cost) if won else -cost

        if n < args.min_n:
            continue
        rows.append({
            "name": name, "n": n, "wr": wins / n, "z": zscore(wins, n),
            "n_priced": n_priced, "pnl": pnl,
            "roi": (pnl / cost_total) if cost_total else 0.0,
        })

    rows.sort(key=lambda r: -abs(r["z"]))

    print(f"{'─'*74}")
    print("  1) PRUEBA ESTADÍSTICA — ¿acierta la dirección más del 50%?")
    print(f"{'─'*74}")
    print(f"{'señal':<26}{'n':>7}{'acierto':>10}{'z':>8}   {'veredicto':<18}")
    for r in rows:
        verdict = "SIN EDGE"
        if abs(r["z"]) > 2.58:
            verdict = "*** p<0.01"
        elif abs(r["z"]) > 1.96:
            verdict = "** p<0.05"
        print(f"{r['name']:<26}{r['n']:>7}{r['wr']*100:>9.1f}%{r['z']:>+8.2f}   {verdict:<18}")

    n_tested = len(rows)
    n_sig = sum(1 for r in rows if abs(r["z"]) > 1.96)
    print(f"\n  {n_tested} señales probadas. Con ruido puro se esperan "
          f"{n_tested*0.05:.1f} por azar a p<0.05; observadas: {n_sig}.")

    print(f"\n{'─'*74}")
    print("  2) PRUEBA ECONÓMICA — P&L comprando al precio real del mercado")
    print(f"     (5 shares, sin martingale, subconjunto con precio)")
    print(f"{'─'*74}")
    print(f"{'señal':<26}{'n':>7}{'acierto':>10}{'P&L':>11}{'ROI':>9}")
    econ = [r for r in rows if r["n_priced"] >= 100]
    econ.sort(key=lambda r: -r["roi"])
    for r in econ:
        print(f"{r['name']:<26}{r['n_priced']:>7}{r['wr']*100:>9.1f}%"
              f"{r['pnl']:>+11.2f}{r['roi']*100:>+8.2f}%")

    if econ:
        best = econ[0]
        print(f"\n  Mejor ROI: {best['name']} ({best['roi']*100:+.2f}% sobre "
              f"{best['n_priced']} operaciones)")
        print("  Nota: el ROI se mide al precio publicado (mid). Ejecutar cruza el")
        print("  spread, así que el resultado real es peor que esta cifra.")

    # ── 3) why: the price already contains the signal ─────────────────────────
    print(f"\n{'─'*74}")
    print("  3) ¿POR QUÉ? — acierto frente a precio pagado")
    print("     Para ganar hace falta acierto > precio. La columna 'brecha' es")
    print("     el edge por share; t es su significancia.")
    print(f"{'─'*74}")
    print(f"{'señal':<26}{'n':>6}{'acierto':>9}{'precio':>8}{'brecha':>9}{'t':>7}")

    gap_rows = []
    for name, fn in signals.items():
        edges: List[float] = []
        prices_paid: List[float] = []
        wins = 0
        for ts, truth in outcomes.items():
            f = feat_by_ts.get(ts)
            px = prices.get(ts)
            if f is None or not px:
                continue
            try:
                call = fn(f)
            except Exception:
                call = None
            if call is None:
                continue
            p_up = px["up_price"]
            p = p_up if call == "UP" else round(1.0 - p_up, 4)
            if not (0.01 <= p <= 0.99):
                continue
            won = call == truth
            wins += won
            prices_paid.append(p)
            edges.append((1.0 if won else 0.0) - p)

        n = len(edges)
        if n < 100:
            continue
        mean_edge = sum(edges) / n
        var = sum((e - mean_edge) ** 2 for e in edges) / max(n - 1, 1)
        se = math.sqrt(var / n)
        gap_rows.append({
            "name": name, "n": n, "wr": wins / n,
            "price": sum(prices_paid) / n,
            "gap": mean_edge, "t": mean_edge / se if se else 0.0,
        })

    gap_rows.sort(key=lambda r: -r["gap"])
    for r in gap_rows:
        print(f"{r['name']:<26}{r['n']:>6}{r['wr']*100:>8.1f}%{r['price']:>8.3f}"
              f"{r['gap']:>+9.3f}{r['t']:>+7.2f}")

    best_t = max(abs(r["t"]) for r in gap_rows)
    print(f"\n  |t| máximo sobre {len(gap_rows)} señales: {best_t:.2f} "
          f"(hace falta >1.96 para p<0.05, y con {len(gap_rows)} pruebas "
          f"hace falta ~{2.9:.1f} por Bonferroni).")

    # Mirror pairs: a real edge is one-sided. Noise is symmetric — one side of
    # the pair wins exactly what the other loses.
    print("\n  ─ Pares espejo (comprar A vs comprar lo contrario de A) ─")
    print("    Si la brecha fuese edge real, no sumaría cero.")
    print(f"    {'par':<40}{'suma brechas':>14}")
    by_name = {r["name"]: r for r in gap_rows}
    for a, b in (
        ("prev-window momentum", "prev-window reversion"),
        ("momentum 6w", "reversion 6w"),
        ("momentum 24w", "reversion 24w"),
        ("follow streak>=4", "fade streak>=4"),
        ("always UP", "always DOWN"),
    ):
        ra, rb = by_name.get(a), by_name.get(b)
        if ra and rb:
            print(f"    {a + ' + ' + b:<40}{ra['gap'] + rb['gap']:>+14.4f}")

    # How small an edge could this sample even detect?
    n_full = max(r["n"] for r in gap_rows)
    se_full = 0.5 / math.sqrt(n_full)
    print(f"\n  Resolución del muestreo: con n={n_full} el error estándar de la")
    print(f"  brecha es ~{se_full:.4f}/share. A 2σ este dataset NO puede")
    print(f"  distinguir un edge menor que ±{2*se_full/0.5*100:.1f}% de ROI.")


if __name__ == "__main__":
    main()
