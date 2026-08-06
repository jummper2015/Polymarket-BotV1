"""Backtesting engine for Streak Snapper v2.

Simulates both strategy forms (Fade + Trend) against historical BTC 5-min
windows. Binance klines supply the *features* (streak history, 4h trend); Gamma
supplies the *labels* (who actually won).

Keeping those two apart matters. Polymarket settles from Chainlink, not Binance,
so deriving the outcome from the Binance candle mislabels 4.5% of windows
(measured over 3.016 windows, docs/CHAINLINK_TWAP.md §11.1.b). With a ×1.5
martingale that error doesn't average out — it compounds, because a mislabelled
window resizes every entry after it. `--labels binance` keeps the old behaviour
for comparison, but its numbers are not trustworthy.

Usage:
    python -m bot.backtest                      # 2000 windows, Gamma labels
    python -m bot.backtest --labels binance     # old behaviour (unreliable)
    python -m bot.backtest --windows 5000 --csv data/my_backtest.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from .config import min_recovering_factor


# ── Config ────────────────────────────────────────────────────────────────────

BINANCE_BASE = "https://api.binance.com/api/v3"

# Strategy defaults (same as production bot)
FADE_BASE_SHARES  = 5
FADE_LIMIT_CAP    = 0.60
FADE_STREAK_MIN   = 4
TREND_BASE_SHARES = 5
TREND_LIMIT_CAP   = 0.52
# Minimum |move| of a closed 4h candle to call it a trend. Override with
# --min-strength to sweep it.
TREND_MIN_STRENGTH = 0.008
# 2.1, not 1.5: a martingale only keeps recovering while factor > 1/(1-cap),
# which is 2.083 at the 0.52 trend cap (bot/config.py:min_recovering_factor).
MARTINGALE_FACTOR = 2.1

HISTORY_WINDOWS = 16  # windows of context for streak detection

CSV_COLUMNS = [
    "window_ts", "strategy", "signal_direction",
    "streak_len", "streak_dir", "trend_4h",
    "limit_cap", "entry_price", "shares", "cost",
    "shares_count", "multiplier", "loss_streak",
    "actual_direction", "label_source", "binance_direction", "won", "pnl",
]


# ── Binance data fetching ─────────────────────────────────────────────────────


def fetch_klines(interval: str, limit: int) -> Optional[List[dict]]:
    """Fetch klines from Binance. Handles >1000 limit by chaining requests."""
    all_candles: List[dict] = []
    remaining = limit
    end_time_ms: Optional[int] = None  # None = most recent
    max_per_request = 1000  # Binance hard cap

    while remaining > 0:
        batch_limit = min(remaining, max_per_request)
        params: dict = {
            "symbol": "BTCUSDT",
            "interval": interval,
            "limit": batch_limit,
        }
        if end_time_ms is not None:
            params["endTime"] = end_time_ms

        batch = None
        for attempt in range(3):
            try:
                r = requests.get(f"{BINANCE_BASE}/klines", params=params, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and len(data) >= 1:
                        batch = [
                            {
                                "ts": int(k[0]) // 1000,
                                "open": float(k[1]),
                                "high": float(k[2]),
                                "low": float(k[3]),
                                "close": float(k[4]),
                                "direction": "UP" if float(k[4]) >= float(k[1]) else "DOWN",
                            }
                            for k in data
                        ]
                        break
            except Exception:
                if attempt < 2:
                    time.sleep(2.0)

        if batch is None:
            print(f"   ⚠ Failed to fetch batch at {end_time_ms}, stopping")
            break

        # Set end_time to the earliest candle's open time minus 1ms
        # so the next batch gets the candles BEFORE this batch.
        end_time_ms = int(batch[0]["ts"]) * 1000 - 1
        all_candles = batch + all_candles  # prepend older data
        remaining -= len(batch)

        # If we got fewer than requested, there's no more data.
        if len(batch) < batch_limit and len(batch) < max_per_request:
            break

    if not all_candles:
        return None

    # Ensure sorted oldest → newest
    all_candles.sort(key=lambda c: c["ts"])
    return all_candles


# ── 4h trend lookup ───────────────────────────────────────────────────────────


FOUR_HOURS = 14400


def build_4h_signal_map(candles_4h: List[dict]) -> Dict[int, dict]:
    """Map each 5-min timestamp → the 4h candle that had already closed by then.

    A candle licenses the *following* 4h block, never its own. Mapping a window
    to the candle containing it — which is what this function used to do — reads
    a close that hadn't happened yet, so every trend trade was placed knowing
    how its own 4h candle would end. That inflated `ss_trend` in every result
    published before this change.

    Each entry carries the signed relative move so the caller can apply its own
    "is this a trend at all" threshold.
    """
    if not candles_4h:
        return {}

    signal_map: Dict[int, dict] = {}
    for c4 in candles_4h:
        if c4["open"] <= 0:
            continue
        signal = {
            "direction": c4["direction"],
            "strength": (c4["close"] - c4["open"]) / c4["open"],
            "anchor_ts": c4["ts"],
        }
        block_start = c4["ts"] + FOUR_HOURS
        for ts in range(block_start, block_start + FOUR_HOURS, 300):
            signal_map[ts] = signal

    return signal_map


# ── Martingale state machine ──────────────────────────────────────────────────


class MartingaleSim:
    """In-memory martingale state for backtesting (no DB)."""

    def __init__(self, base_shares: float, factor: float):
        self.base_shares = base_shares
        self.factor = factor
        self.multiplier = 1.0
        self.loss_streak = 0

    def current_shares(self) -> float:
        return max(5.0, round(self.base_shares * self.multiplier, 2))

    def on_win(self) -> None:
        self.multiplier = 1.0
        self.loss_streak = 0

    def on_loss(self) -> None:
        self.multiplier = round(self.multiplier * self.factor, 4)
        self.loss_streak += 1


# ── Signal detection (mirrors strategy_streak.py, no DB dependency) ───────────


def detect_streak(windows: List[dict]) -> Tuple[int, str]:
    """Count consecutive same-direction windows from most recent backward."""
    if not windows:
        return 0, "UP"
    streak_dir = windows[-1]["direction"]
    streak_len = 0
    for w in reversed(windows):
        if w["direction"] == streak_dir:
            streak_len += 1
        else:
            break
    return streak_len, streak_dir


def get_fade_signal(
    windows: List[dict],
    martingale: MartingaleSim,
    streak_min: int = FADE_STREAK_MIN,
    limit_cap: float = FADE_LIMIT_CAP,
) -> Optional[dict]:
    """Check if we have a fade signal (anti-streak)."""
    streak_len, streak_dir = detect_streak(windows)
    if streak_len < streak_min:
        return None

    fade_dir = "DOWN" if streak_dir == "UP" else "UP"
    return {
        "strategy": "ss_fade",
        "direction": fade_dir,
        "limit_cap": limit_cap,
        "shares": martingale.current_shares(),
        "multiplier": martingale.multiplier,
        "loss_streak": martingale.loss_streak,
        "signal_reason": f"racha {streak_len}x {streak_dir} → fade {fade_dir}",
        "streak_len": streak_len,
        "streak_dir": streak_dir,
    }


class TrendCycleSim:
    """The one-side-per-4h-block cycle, mirroring strategy_streak.py.

    A closed 4h candle that moved at least `min_strength` locks its direction
    for the following block. The lock outlives the block while the martingale
    still has losses to recover — the cycle runs until it wins.
    """

    def __init__(self, martingale: MartingaleSim, min_strength: float):
        self.mart = martingale
        self.min_strength = min_strength
        self.side: Optional[str] = None
        self.anchor_ts: Optional[int] = None
        self.extensions = 0
        self.cycles_opened = 0

    def side_for(self, ts: int, signal: Optional[dict]) -> Optional[str]:
        if self.side is not None and self.anchor_ts is not None:
            if ts < self.anchor_ts + 2 * FOUR_HOURS:
                return self.side
            if self.mart.multiplier > 1.0:
                self.extensions += 1
                return self.side
            self.side = None
            self.anchor_ts = None

        if signal is None or abs(signal["strength"]) < self.min_strength:
            return None

        self.side = signal["direction"]
        self.anchor_ts = signal["anchor_ts"]
        self.cycles_opened += 1
        return self.side

    def on_win(self) -> None:
        self.mart.on_win()
        self.side = None
        self.anchor_ts = None

    def on_loss(self) -> None:
        self.mart.on_loss()


def get_trend_signal(
    ts: int,
    signal_4h: Optional[dict],
    cycle: TrendCycleSim,
    limit_cap: float = TREND_LIMIT_CAP,
) -> Optional[dict]:
    """Check if we have a trend-following signal for this window."""
    side = cycle.side_for(ts, signal_4h)
    if side is None:
        return None

    strength = (signal_4h or {}).get("strength", 0.0)
    return {
        "strategy": "ss_trend",
        "direction": side,
        "limit_cap": limit_cap,
        "shares": cycle.mart.current_shares(),
        "multiplier": cycle.mart.multiplier,
        "loss_streak": cycle.mart.loss_streak,
        "signal_reason": f"ciclo 4h {side} ({strength * 100:+.2f}%)",
        "streak_len": 0,
        "streak_dir": "",
    }


# ── Main backtest loop ────────────────────────────────────────────────────────


def run_backtest(
    windows_5m: List[dict],
    signal_map: Dict[int, dict],
    mode: str = "both",  # "fade", "trend", "both"
    labels: Optional[Dict[int, Optional[str]]] = None,
    factor: float = MARTINGALE_FACTOR,
    min_strength: float = TREND_MIN_STRENGTH,
) -> Tuple[List[dict], dict]:
    """Run the backtest against historical 5-min windows.

    `labels` maps window_ts → "UP"/"DOWN" from Gamma, the settlement truth. When
    it's None the outcome falls back to the Binance candle direction, which is
    the old behaviour and carries ~4.5% mislabelling. Windows Gamma can't label
    are skipped rather than guessed — a guessed label is worse than no trade.

    Returns (trades_list, summary_dict).
    """
    # Skip first HISTORY_WINDOWS windows (need history for streak detection)
    start_idx = HISTORY_WINDOWS

    # Martingale states (independent per strategy)
    fade_mart  = MartingaleSim(FADE_BASE_SHARES, factor)
    trend_mart = MartingaleSim(TREND_BASE_SHARES, factor)
    trend_cycle = TrendCycleSim(trend_mart, min_strength)

    trades: List[dict] = []

    # Track P&L per strategy
    fade_pnl  = 0.0
    trend_pnl = 0.0
    fade_wins = fade_losses = 0
    trend_wins = trend_losses = 0

    # Track max martingale multiplier reached
    max_fade_mult  = 1.0
    max_trend_mult = 1.0

    # Label provenance — reported so a run always states what it trusted.
    labelled_gamma = 0
    labelled_binance = 0
    skipped_unlabelled = 0
    label_disagreements = 0

    for i in range(start_idx, len(windows_5m) - 1):
        # The "signal window" is windows[i-1] and earlier
        # The "execution window" is windows[i]
        # The "outcome window" is windows[i] (we know its direction)
        history = windows_5m[i - HISTORY_WINDOWS : i]  # previous 16 windows
        entry_candle = windows_5m[i]                    # candle we enter on
        outcome_candle = windows_5m[i]                  # same candle determines outcome

        entry_ts = entry_candle["ts"]
        binance_direction = outcome_candle["direction"]
        signal_4h = signal_map.get(entry_ts)
        trend_4h = signal_4h["direction"] if signal_4h else None

        if labels is None:
            actual_direction = binance_direction
            label_source = "binance"
            labelled_binance += 1
        else:
            actual_direction = labels.get(entry_ts)
            if actual_direction is None:
                skipped_unlabelled += 1
                continue
            label_source = "gamma"
            labelled_gamma += 1
            if actual_direction != binance_direction:
                label_disagreements += 1

        # ── Forma 1: Fade ──
        if mode in ("fade", "both"):
            sig = get_fade_signal(history, fade_mart)
            if sig is not None:
                # Entry at NEXT candle's open, capped at limit
                entry_price = min(sig["limit_cap"], entry_candle["open"])
                entry_price = max(0.01, round(entry_price, 4))
                shares = sig["shares"]
                cost = round(shares * entry_price, 4)

                # Outcome: did our direction match?
                won = sig["direction"] == actual_direction
                pnl = round(shares - cost, 4) if won else round(-cost, 4)

                trades.append({
                    "window_ts": entry_ts,
                    "strategy": "ss_fade",
                    "signal_direction": sig["direction"],
                    "streak_len": sig["streak_len"],
                    "streak_dir": sig.get("streak_dir", ""),
                    "trend_4h": trend_4h or "",
                    "limit_cap": sig["limit_cap"],
                    "entry_price": entry_price,
                    "shares": shares,
                    "cost": cost,
                    "shares_count": shares,
                    "multiplier": sig["multiplier"],
                    "loss_streak": sig["loss_streak"],
                    "actual_direction": actual_direction,
                    "label_source": label_source,
                    "binance_direction": binance_direction,
                    "won": won,
                    "pnl": pnl,
                })

                fade_pnl += pnl
                if won:
                    fade_wins += 1
                    fade_mart.on_win()
                else:
                    fade_losses += 1
                    fade_mart.on_loss()
                    max_fade_mult = max(max_fade_mult, fade_mart.multiplier)

        # ── Forma 2: Trend ──
        if mode in ("trend", "both"):
            sig = get_trend_signal(entry_ts, signal_4h, trend_cycle)
            if sig is not None:
                entry_price = min(sig["limit_cap"], entry_candle["open"])
                entry_price = max(0.01, round(entry_price, 4))
                shares = sig["shares"]
                cost = round(shares * entry_price, 4)

                won = sig["direction"] == actual_direction
                pnl = round(shares - cost, 4) if won else round(-cost, 4)

                trades.append({
                    "window_ts": entry_ts,
                    "strategy": "ss_trend",
                    "signal_direction": sig["direction"],
                    "streak_len": sig["streak_len"],
                    "streak_dir": sig.get("streak_dir", ""),
                    "trend_4h": trend_4h or "",
                    "limit_cap": sig["limit_cap"],
                    "entry_price": entry_price,
                    "shares": shares,
                    "cost": cost,
                    "shares_count": shares,
                    "multiplier": sig["multiplier"],
                    "loss_streak": sig["loss_streak"],
                    "actual_direction": actual_direction,
                    "label_source": label_source,
                    "binance_direction": binance_direction,
                    "won": won,
                    "pnl": pnl,
                })

                trend_pnl += pnl
                if won:
                    trend_wins += 1
                    trend_cycle.on_win()
                else:
                    trend_losses += 1
                    trend_cycle.on_loss()
                    max_trend_mult = max(max_trend_mult, trend_mart.multiplier)

    # ── Build summary ──
    fade_resolved  = fade_wins + fade_losses
    trend_resolved = trend_wins + trend_losses

    windows_labelled = labelled_gamma + labelled_binance
    summary = {
        "windows_tested": len(windows_5m) - start_idx - 1,
        "labels": {
            "source": "binance" if labels is None else "gamma",
            "gamma": labelled_gamma,
            "binance": labelled_binance,
            "skipped_unlabelled": skipped_unlabelled,
            "disagreements": label_disagreements,
            "disagreement_rate": (
                round(label_disagreements / windows_labelled, 4)
                if windows_labelled else 0.0
            ),
        },
        "fade": {
            "trades": fade_resolved,
            "wins": fade_wins,
            "losses": fade_losses,
            "win_rate": round(fade_wins / fade_resolved, 4) if fade_resolved else 0,
            "pnl": round(fade_pnl, 4),
            "max_multiplier": round(max_fade_mult, 4),
        },
        "trend": {
            "trades": trend_resolved,
            "wins": trend_wins,
            "losses": trend_losses,
            "win_rate": round(trend_wins / trend_resolved, 4) if trend_resolved else 0,
            "pnl": round(trend_pnl, 4),
            "max_multiplier": round(max_trend_mult, 4),
            "min_strength": min_strength,
            "cycles_opened": trend_cycle.cycles_opened,
            "windows_extended": trend_cycle.extensions,
        },
        "factor": factor,
        "combined_pnl": round(fade_pnl + trend_pnl, 4),
    }

    return trades, summary


# ── CSV output ────────────────────────────────────────────────────────────────


def write_csv(trades: List[dict], summary: dict, filepath: str) -> None:
    """Write trades and summary to a CSV file."""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        # Write summary as a comment-like row first
        f.write(f"# Backtest run: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"# Windows tested: {summary['windows_tested']}\n")
        lab = summary["labels"]
        f.write(f"# Labels: source={lab['source']} gamma={lab['gamma']} "
                f"binance={lab['binance']} skipped={lab['skipped_unlabelled']} "
                f"disagreements={lab['disagreements']} "
                f"({lab['disagreement_rate']:.2%})\n")
        f.write(f"# Fade  — trades={summary['fade']['trades']} wins={summary['fade']['wins']} losses={summary['fade']['losses']} wr={summary['fade']['win_rate']:.2%} pnl=${summary['fade']['pnl']:.2f} max_mult=×{summary['fade']['max_multiplier']:.2f}\n")
        f.write(f"# Trend — trades={summary['trend']['trades']} wins={summary['trend']['wins']} losses={summary['trend']['losses']} wr={summary['trend']['win_rate']:.2%} pnl=${summary['trend']['pnl']:.2f} max_mult=×{summary['trend']['max_multiplier']:.2f}\n")
        f.write(f"# Combined P&L: ${summary['combined_pnl']:.2f}\n")
        f.write("#\n")

        writer.writerows(trades)

    print(f"📄 CSV saved: {filepath}  ({len(trades)} trades)")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Streak Snapper v2 Backtest")
    parser.add_argument("--windows", type=int, default=2000,
                        help="Number of 5-min windows to backtest (default: 2000 ≈ 1 week)")
    parser.add_argument("--csv", type=str, default="data/backtest_result.csv",
                        help="Output CSV path")
    parser.add_argument("--mode", type=str, default="both",
                        choices=["fade", "trend", "both"],
                        help="Strategy mode to test")
    parser.add_argument("--labels", type=str, default="gamma",
                        choices=["gamma", "binance"],
                        help="Outcome source. 'gamma' is settlement truth; "
                             "'binance' mislabels ~4.5%% of windows (default: gamma)")
    parser.add_argument("--factor", type=float, default=MARTINGALE_FACTOR,
                        help=f"Martingale factor (default: {MARTINGALE_FACTOR})")
    parser.add_argument("--min-strength", type=float, default=TREND_MIN_STRENGTH,
                        dest="min_strength",
                        help="Min |move| of the closed 4h candle to trade its "
                             f"direction (default: {TREND_MIN_STRENGTH})")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Streak Snapper v2 — Backtest")
    print(f"  Windows: {args.windows} (~{args.windows * 5 / 60:.1f} hours)")
    print(f"  Mode: {args.mode}")
    print(f"  Base shares: Fade={FADE_BASE_SHARES} Trend={TREND_BASE_SHARES}")
    print(f"  Limit caps: Fade≤${FADE_LIMIT_CAP} Trend≤${TREND_LIMIT_CAP}")
    print(f"  Martingale: ×{args.factor}  (recupera a {TREND_LIMIT_CAP} si >×"
          f"{min_recovering_factor(TREND_LIMIT_CAP):.2f})")
    print(f"  Trend mín.: {args.min_strength * 100:.3f}% de la vela 4h cerrada")
    print(f"{'='*60}\n")

    # Fetch data
    print("📡 Fetching 5-min klines from Binance...")
    candles_5m = fetch_klines("5m", args.windows + HISTORY_WINDOWS + 2)
    if candles_5m is None or len(candles_5m) < HISTORY_WINDOWS + 10:
        print("❌ Failed to fetch enough 5-min klines")
        sys.exit(1)
    print(f"   Got {len(candles_5m)} candles  "
          f"({datetime.fromtimestamp(candles_5m[0]['ts'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} → "
          f"{datetime.fromtimestamp(candles_5m[-1]['ts'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')})")

    print("📡 Fetching 4h klines from Binance...")
    # Need enough 4h candles to cover the timestamp range + extra
    window_range_hours = (args.windows * 5) / 60
    candles_4h = fetch_klines("4h", max(10, int(window_range_hours / 4) + 10))
    if candles_4h is None or len(candles_4h) < 1:
        print("❌ Failed to fetch 4h klines — trend strategy will have no signals")
        candles_4h = []
    else:
        print(f"   Got {len(candles_4h)} 4h candles")

    signal_map = build_4h_signal_map(candles_4h)
    print(f"   Built 4h signal map with {len(signal_map)} entries\n")

    # Fetch outcome labels
    labels = None
    if args.labels == "gamma":
        from .gamma_history import coverage, fetch_outcomes
        print("📡 Fetching outcome labels from Gamma...")
        labels = fetch_outcomes([c["ts"] for c in candles_5m], progress=True)
        cov = coverage(labels)
        print(f"   Coverage: {cov['coverage']:.1%} "
              f"({cov['labelled']}/{cov['total']} windows)")
        if cov["coverage"] < 0.95:
            print("   ⚠ Low coverage — windows older than ~70 days lose "
                  "outcomePrices. Unlabelled windows are skipped.")
        print()
    else:
        print("⚠️  Using Binance candle direction as outcome — mislabels ~4.5% "
              "of windows.\n   These results are NOT reliable "
              "(docs/CHAINLINK_TWAP.md §11.1.b).\n")

    # Run backtest
    print("🔄 Running backtest...")
    trades, summary = run_backtest(
        candles_5m, signal_map, mode=args.mode, labels=labels,
        factor=args.factor, min_strength=args.min_strength,
    )

    # Print summary
    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  Windows tested: {summary['windows_tested']}")
    lab = summary["labels"]
    print(f"  Label source: {lab['source']}  "
          f"(gamma={lab['gamma']} binance={lab['binance']} "
          f"skipped={lab['skipped_unlabelled']})")
    if lab["source"] == "gamma":
        print(f"  Binance would have mislabelled: {lab['disagreements']} windows "
              f"({lab['disagreement_rate']:.2%})")
    print()
    print(f"  ── Fade (Anti-racha) ──")
    print(f"  Trades: {summary['fade']['trades']}")
    print(f"  Wins:   {summary['fade']['wins']}")
    print(f"  Losses: {summary['fade']['losses']}")
    print(f"  Win Rate: {summary['fade']['win_rate']:.1%}")
    print(f"  P&L:    ${summary['fade']['pnl']:+.2f}")
    print(f"  Max Martingale: ×{summary['fade']['max_multiplier']:.2f}")
    print()
    print(f"  ── Trend (Tendencia 4h) ──")
    print(f"  Trades: {summary['trend']['trades']}")
    print(f"  Wins:   {summary['trend']['wins']}")
    print(f"  Losses: {summary['trend']['losses']}")
    print(f"  Win Rate: {summary['trend']['win_rate']:.1%}")
    print(f"  P&L:    ${summary['trend']['pnl']:+.2f}")
    print(f"  Max Martingale: ×{summary['trend']['max_multiplier']:.2f}")
    print(f"  Ciclos abiertos: {summary['trend']['cycles_opened']}  "
          f"(ventanas prorrogadas tras el bloque: "
          f"{summary['trend']['windows_extended']})")
    print()
    print(f"  💰 Combined P&L: ${summary['combined_pnl']:+.2f}")
    print(f"{'='*60}\n")

    # Write CSV
    write_csv(trades, summary, args.csv)


if __name__ == "__main__":
    main()
