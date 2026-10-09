"""Analyze historic Polymarket 5m trades by UTC hour bucket.

Reads trades table from a sqlite DB (default: local Codespace backup or VPS via sshpull),
groups by UTC hour and strategy, computes P&L statistics, and emits:
  - Markdown table to stdout
  - JSON to data/hour_analysis_<UTC>.json

Usage:
  python scripts/analyze_trade_hours.py [--db PATH] [--days N] [--min-trades N]
                                        [--min-winrate F] [--symbols btc,eth,sol]

The whitelist is heuristic — meant for /settings to override, not for hard enforcement.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class HourBucket:
    utc_hour: int          # 0..23
    symbol: str            # "btc" | "eth" | "sol"
    strategy: str          # strategy column value
    n_trades: int
    net_pnl: float
    avg_pnl: float
    win_rate: float        # 0..1
    max_loss: float
    max_win: float


def fetch_trades(db_path: str, days: int | None) -> list[tuple]:
    """Return list of (created_at_epoch, symbol, strategy, pnl) tuples from trades table."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    sql = (
        "SELECT created_at, symbol, strategy, pnl FROM trades "
        "WHERE pnl IS NOT NULL"
    )
    params: list = []
    if days is not None:
        cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        sql += " AND created_at >= ?"
        params.append(cutoff)
    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [(r["created_at"], (r["symbol"] or "").lower(), r["strategy"], r["pnl"])
            for r in rows]


def bucket_by_hour(rows: list[tuple]) -> list[HourBucket]:
    """Group rows into (utc_hour, symbol, strategy) buckets with stats."""
    acc: dict[tuple[int, str, str], list[float]] = defaultdict(list)
    for created_at, sym, strat, pnl in rows:
        hr = datetime.fromtimestamp(created_at, tz=timezone.utc).hour
        if not sym or not strat or pnl is None:
            continue
        acc[(hr, sym, strat)].append(float(pnl))
    out: list[HourBucket] = []
    for (hr, sym, strat), pnls in acc.items():
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        out.append(HourBucket(
            utc_hour=hr,
            symbol=sym,
            strategy=strat,
            n_trades=n,
            net_pnl=round(sum(pnls), 4),
            avg_pnl=round(sum(pnls) / n, 4),
            win_rate=round(wins / n, 4),
            max_loss=round(min(pnls), 4),
            max_win=round(max(pnls), 4),
        ))
    return out


def recommend_whitelist(buckets: list[HourBucket],
                        min_trades: int, min_winrate: float) -> dict[str, list[int]]:
    """Per symbol, return sorted list of UTC hours to whitelist.

    Rule: utc_hour is included iff there exists at least one strategy bucket
    with n_trades >= min_trades AND win_rate >= min_winrate (AND net_pnl > 0).
    Hours with no qualifying bucket are excluded.
    """
    by_hour_symbol: dict[tuple[int, str], list[HourBucket]] = defaultdict(list)
    for b in buckets:
        by_hour_symbol[(b.utc_hour, b.symbol)].append(b)
    out: dict[str, set[int]] = defaultdict(set)
    for (hr, sym), bs in by_hour_symbol.items():
        if any(b.net_pnl > 0 and b.n_trades >= min_trades and b.win_rate >= min_winrate
               for b in bs):
            out[sym].add(hr)
    return {sym: sorted(hrs) for sym, hrs in out.items()}


def render_markdown(buckets: list[HourBucket],
                    whitelist: dict[str, list[int]],
                    min_trades: int, min_winrate: float) -> str:
    lines = [
        f"# Hourly P&L Analysis (UTC)",
        f"",
        f"Filter: min_trades={min_trades}, min_winrate={min_winrate}",
        f"",
        f"## Per-bucket stats",
        f"",
        f"| UTC hour | Symbol | Strategy | N | Net P&L | Avg P&L | Win rate | Max loss | Max win |",
        f"|---|---|---|---|---|---|---|---|---|",
    ]
    sorted_b = sorted(buckets, key=lambda b: (b.symbol, b.utc_hour, b.strategy))
    for b in sorted_b:
        lines.append(
            f"| {b.utc_hour:02d} | {b.symbol} | {b.strategy} | {b.n_trades} | "
            f"{b.net_pnl:+.2f} | {b.avg_pnl:+.4f} | {b.win_rate:.1%} | "
            f"{b.max_loss:+.2f} | {b.max_win:+.2f} |"
        )
    lines += ["", "## Recommended whitelist (heuristic)", ""]
    if not whitelist:
        lines.append("_No symbols have enough qualifying data. Whitelist will be empty._")
    for sym in sorted(whitelist):
        hrs = whitelist[sym]
        lines.append(f"- **{sym}**: hours {hrs}" if hrs
                     else f"- **{sym}**: (no qualifying hours — empty whitelist = no filter)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/opt/polymarket-bot/data/streak_snapper.db",
                        help="Path to streak_snapper.db")
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in days (None = all)")
    parser.add_argument("--min-trades", type=int, default=20,
                        help="Min trades per (hour, symbol, strategy) bucket to consider")
    parser.add_argument("--min-winrate", type=float, default=0.50,
                        help="Min win rate for a bucket to qualify its hour")
    parser.add_argument("--symbols", default="btc,eth,sol",
                        help="Comma-separated symbols to include")
    parser.add_argument("--out-json",
                        help="Output JSON path (default: data/hour_analysis_<UTC>.json)")
    parser.add_argument("--out-md",
                        help="Output Markdown path (default: data/hour_analysis_<UTC>.md)")
    args = parser.parse_args(argv)

    rows = fetch_trades(args.db, args.days if args.days > 0 else None)
    wanted_symbols = {s.strip().lower() for s in args.symbols.split(",") if s.strip()}
    rows = [r for r in rows if r[1] in wanted_symbols]
    buckets = bucket_by_hour(rows)
    whitelist = recommend_whitelist(buckets, args.min_trades, args.min_winrate)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = Path(args.out_json or f"data/hour_analysis_{ts}.json")
    md_path = Path(args.out_md or f"data/hour_analysis_{ts}.md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at_utc": ts,
        "lookback_days": args.days,
        "filters": {"min_trades": args.min_trades, "min_winrate": args.min_winrate},
        "symbols": sorted(wanted_symbols),
        "buckets": [asdict(b) for b in buckets],
        "recommended_whitelist": whitelist,
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    md_path.write_text(render_markdown(buckets, whitelist, args.min_trades, args.min_winrate))

    print(f"Wrote {json_path} and {md_path}")
    print(f"Found {len(buckets)} (hour, symbol, strategy) buckets across "
          f"{len(rows)} trades.")
    print(f"Recommended whitelist: {whitelist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
