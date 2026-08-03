"""Calibration study for NEAR_FLAT_THRESHOLD — measured cost, in dollars.

The bot settles a window the moment it closes using the Binance candle, because
Gamma needs ~200 s and waiting would leave the martingale a window behind. When
the candle is too flat to call, `get_window_direction()` returns None and the
trade defers to Gamma. `NEAR_FLAT_THRESHOLD` is where that line sits, and it
trades one error off against another:

  - Threshold too LOW  → Binance calls windows it shouldn't, and sometimes calls
    them wrong. A wrong call moves the martingale in the wrong direction and the
    multiplier can't be reconstructed once later windows have used it.
  - Threshold too HIGH → more windows defer. A deferred window isn't resolved
    until the *next* tick (Gamma is ~200 s late, the tick is ~5 s after close),
    so the very next entry is sized with one result missing.

docs/CHAINLINK_TWAP.md §5 measured the first error and §11.1 measured the second,
but on different samples and without pricing either. This does both on one
sample, with Gamma as ground truth, and simulates the full martingale so the
answer comes out in dollars.

Usage:
    python -m bot.threshold_study                    # 3000 windows
    python -m bot.threshold_study --windows 5000
"""

from __future__ import annotations

import argparse
from typing import Dict, List, Optional

from .backtest import (
    FADE_BASE_SHARES,
    FADE_LIMIT_CAP,
    FADE_STREAK_MIN,
    HISTORY_WINDOWS,
    MARTINGALE_FACTOR,
    TREND_BASE_SHARES,
    TREND_LIMIT_CAP,
    MartingaleSim,
    build_4h_trend_map,
    detect_streak,
    fetch_klines,
)
from .gamma_history import coverage, fetch_outcomes


THRESHOLDS = [0.0, 1e-5, 2e-5, 5e-5, 1e-4, 1.5e-4, 2e-4, 3e-4]


def binance_call(candle: dict, threshold: float) -> Optional[str]:
    """What `get_window_direction()` would return for this candle.

    Mirrors bot/binance_api.py: below the threshold we don't guess, we defer.
    """
    open_px, close_px = candle["open"], candle["close"]
    if open_px <= 0:
        return None
    if abs(close_px - open_px) / open_px <= threshold:
        return None
    return "UP" if close_px > open_px else "DOWN"


def simulate(
    candles: List[dict],
    trend_map: Dict[int, str],
    outcomes: Dict[int, Optional[str]],
    threshold: float,
) -> dict:
    """Run the bot over the sample with a given threshold.

    Two truths are tracked separately, which is the whole point of the study:

      - P&L is always computed from Gamma, because that is what Polymarket
        actually pays. A wrong Binance call doesn't change what the trade earned,
        it changes what the bot *believed* it earned.
      - The martingale advances on what the bot believed and when it believed it
        — Binance's call if it made one, otherwise Gamma one entry late.

    `threshold=None` is the oracle case: perfect labels, applied instantly. The
    gap between each threshold and the oracle is the cost of settling imperfectly.
    """
    fade_mart = MartingaleSim(FADE_BASE_SHARES, MARTINGALE_FACTOR)
    trend_mart = MartingaleSim(TREND_BASE_SHARES, MARTINGALE_FACTOR)

    # Results whose martingale update is owed but not yet applied, per strategy.
    # A deferred window resolves on the *following* tick, so its win/loss lands
    # one entry later than it should.
    pending: Dict[str, Optional[bool]] = {"ss_fade": None, "ss_trend": None}

    stats = {
        "settled_binance": 0, "settled_wrong": 0, "deferred": 0,
        "deferred_won": 0, "deferred_lost": 0,
        "fade_pnl": 0.0, "trend_pnl": 0.0,
        "fade_trades": 0, "trend_trades": 0,
        "fade_wins": 0, "trend_wins": 0,
        "max_fade_mult": 1.0, "max_trend_mult": 1.0,
    }

    # Every entry's size, keyed by (window, strategy). The signals themselves
    # depend only on the Binance candles and the 4h trend — never on the
    # martingale — so the *set* of entries is identical across thresholds and
    # only the size differs. That makes sizes comparable entry-by-entry against
    # the oracle, which is the noise-free way to price a threshold.
    sizes: Dict[tuple, float] = {}

    for i in range(HISTORY_WINDOWS, len(candles)):
        candle = candles[i]
        ts = candle["ts"]
        truth = outcomes.get(ts)
        if truth is None:
            continue  # no ground-truth label — window can't score

        history = candles[i - HISTORY_WINDOWS : i]
        trend_4h = trend_map.get(ts)

        # ── what the bot believes about THIS window, and when ──
        if threshold is None:
            believed, deferred = truth, False
        else:
            call = binance_call(candle, threshold)
            if call is None:
                believed, deferred = truth, True   # Gamma, but a tick late
                stats["deferred"] += 1
            else:
                believed, deferred = call, False
                stats["settled_binance"] += 1
                if call != truth:
                    stats["settled_wrong"] += 1

        # ── enter positions using the CURRENT (possibly stale) multiplier ──
        entries = []

        streak_len, streak_dir = detect_streak(history)
        if streak_len >= FADE_STREAK_MIN:
            fade_dir = "DOWN" if streak_dir == "UP" else "UP"
            entries.append(("ss_fade", fade_dir, FADE_LIMIT_CAP, fade_mart))

        if trend_4h is not None:
            entries.append(("ss_trend", trend_4h, TREND_LIMIT_CAP, trend_mart))

        # Opposite-side signals cancel out (the pair pays exactly what it costs),
        # so production skips the window entirely. Mirror that here.
        if len({d for _, d, _, _ in entries}) > 1:
            entries = []

        for strat, direction, cap, mart in entries:
            shares = mart.current_shares()
            cost = round(shares * cap, 4)
            sizes[(ts, strat)] = shares

            # P&L is settled by Gamma — always, regardless of what Binance said.
            really_won = direction == truth
            pnl = round(shares - cost, 4) if really_won else round(-cost, 4)

            # The martingale moves on belief, which may be wrong or late.
            believed_won = direction == believed

            key = "fade" if strat == "ss_fade" else "trend"
            stats[f"{key}_pnl"] += pnl
            stats[f"{key}_trades"] += 1
            if really_won:
                stats[f"{key}_wins"] += 1

            if deferred:
                stats["deferred_won" if believed_won else "deferred_lost"] += 1

            # Apply the update owed from a previously deferred window first —
            # it arrives on this tick, after this entry was already sized.
            owed = pending[strat]
            if owed is not None:
                mart.on_win() if owed else mart.on_loss()
                pending[strat] = None

            if deferred:
                pending[strat] = believed_won   # lands next tick
            else:
                mart.on_win() if believed_won else mart.on_loss()

            stats[f"max_{key}_mult"] = max(stats[f"max_{key}_mult"], mart.multiplier)

    stats["combined_pnl"] = round(stats["fade_pnl"] + stats["trend_pnl"], 2)
    stats["sizes"] = sizes
    return stats


def sizing_damage(sizes: Dict[tuple, float], oracle_sizes: Dict[tuple, float]) -> dict:
    """How far each entry's size drifted from what perfect settlement would size it.

    This is the deterministic half of the study. Final P&L is dominated by where
    the losing streaks happen to land — with a ×1.5 martingale reaching ×57, a
    handful of streaks swamp any threshold effect and the ranking comes out
    non-monotonic, i.e. noise. Sizing error has no such luck component: it counts
    exactly how often, and by how much, the bot bet the wrong amount.

    Over-sizing and under-sizing are reported apart because they are not the same
    risk. Betting under just recovers slower. Betting over is the martingale
    failure mode that empties an account.
    """
    over = under = 0.0
    wrong = 0
    for key, ideal in oracle_sizes.items():
        actual = sizes.get(key)
        if actual is None:
            continue
        delta = actual - ideal
        if abs(delta) < 0.005:
            continue
        wrong += 1
        if delta > 0:
            over += delta
        else:
            under += -delta

    total = len(oracle_sizes)
    return {
        "entries": total,
        "wrong": wrong,
        "wrong_pct": wrong / total if total else 0.0,
        "over_shares": round(over, 2),
        "under_shares": round(under, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="NEAR_FLAT_THRESHOLD calibration study")
    ap.add_argument("--windows", type=int, default=3000)
    args = ap.parse_args()

    print(f"\n📡 Binance: descargando {args.windows + HISTORY_WINDOWS} velas 5m...")
    candles = fetch_klines("5m", args.windows + HISTORY_WINDOWS + 2)
    if not candles:
        raise SystemExit("❌ no se pudieron descargar klines")

    # Drop the final candle: it may still be open, and an open candle has no
    # settled direction to compare against.
    candles = candles[:-1]
    print(f"   {len(candles)} velas")

    hours = (len(candles) * 5) / 60
    candles_4h = fetch_klines("4h", max(10, int(hours / 4) + 10)) or []
    trend_map = build_4h_trend_map(candles_4h)

    print(f"📡 Gamma: etiquetas para {len(candles)} ventanas...")
    outcomes = fetch_outcomes([c["ts"] for c in candles], progress=True)
    cov = coverage(outcomes)
    print(f"   cobertura {cov['coverage']:.1%} ({cov['labelled']}/{cov['total']})\n")

    oracle = simulate(candles, trend_map, outcomes, None)

    print("=" * 100)
    print("  ERROR DE DIMENSIONADO — determinista, sin componente de suerte")
    print("=" * 100)
    print(f"{'umbral':>9} {'liq.':>6} {'err.liq':>9} {'difer.':>8} "
          f"{'mal dimens.':>13} {'sobre-apostado':>15} {'infra-apostado':>15}")
    print("-" * 100)

    rows = []
    for th in THRESHOLDS:
        s = simulate(candles, trend_map, outcomes, th)
        settled = s["settled_binance"]
        err = s["settled_wrong"] / settled if settled else 0.0
        total = settled + s["deferred"]
        defer_pct = s["deferred"] / total if total else 0.0
        dmg = sizing_damage(s["sizes"], oracle["sizes"])
        rows.append((th, err, defer_pct, s, dmg))

        mark = " ←actual" if th == 1e-5 else ""
        print(f"{th:>9.2g} {settled:>6} {err:>8.2%} {defer_pct:>7.1%} "
              f"{dmg['wrong']:>6} ({dmg['wrong_pct']:>5.1%}) "
              f"{dmg['over_shares']:>14.0f} {dmg['under_shares']:>15.0f}{mark}")

    print("=" * 100)
    print("  'sobre-apostado' = shares arriesgadas de más frente al dimensionado perfecto.")
    print("  Es el modo de fallo peligroso: el Martingale ×1.5 no perdona apostar de más.\n")

    # P&L is reported separately, and hedged, because it is not a reliable
    # ranking signal at this sample size — see the note printed below.
    print("=" * 100)
    print("  P&L FINAL — informativo, NO comparable entre umbrales")
    print("=" * 100)
    print(f"{'umbral':>9} {'P&L':>10} {'vs oráculo':>12} {'max×':>7}")
    print("-" * 100)
    for th, _, _, s, _ in rows:
        gap = s["combined_pnl"] - oracle["combined_pnl"]
        maxm = max(s["max_fade_mult"], s["max_trend_mult"])
        print(f"{th:>9.2g} {s['combined_pnl']:>+10.2f} {gap:>+12.2f} {maxm:>7.1f}")
    print(f"{'oráculo':>9} {oracle['combined_pnl']:>+10.2f} {0.0:>+12.2f} "
          f"{max(oracle['max_fade_mult'], oracle['max_trend_mult']):>7.1f}")
    print("=" * 100)
    print("  ⚠️  Con Martingale ×1.5 el P&L lo deciden unas pocas rachas, no el umbral.")
    print("     Si esta columna no es monótona respecto a 'err.liq', es ruido: úsala")
    print("     para el orden de magnitud del riesgo, nunca para elegir el umbral.\n")

    best = min(rows, key=lambda r: r[4]["over_shares"])
    print(f"  🏆 Menor sobre-apuesta: umbral {best[0]:.2g}  "
          f"(err.liq {best[1]:.2%}, diferidas {best[2]:.1%}, "
          f"{best[4]['over_shares']:.0f} shares de más)\n")


if __name__ == "__main__":
    main()
