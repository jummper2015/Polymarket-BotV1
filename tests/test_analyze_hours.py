"""Tests for analyze_trade_hours.py using a synthetic sqlite DB.

No real Polymarket data — we generate trades by hand to validate the
aggregation, whitelist recommendation, and CLI plumbing.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from scripts.analyze_trade_hours import (
    bucket_by_hour, fetch_trades, recommend_whitelist, render_markdown,
)


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "trades.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, created_at INTEGER, "
        "symbol TEXT, strategy TEXT, pnl REAL)"
    )
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [
        # hour 14, btc, temporal_arb: 25 trades, 16 winners, net +8.50
        *[(int((base.replace(hour=14, minute=i)).timestamp()), "BTC", "temporal_arb",
            0.30 if i % 5 != 0 else -0.10)
           for i in range(25)],
        # hour 14, btc, temporal_arb: 5 more losers only
        *[(int((base + timedelta(days=1, hours=14, minutes=i)).timestamp()), "BTC",
            "temporal_arb", -0.20)
           for i in range(5)],
        # hour 3, eth, coin_flip_dog: 22 trades, 11 winners, net -3.00
        *[(int((base + timedelta(days=i // 10, hours=3, minutes=i % 10)).timestamp()),
            "ETH", "coin_flip_dog", 0.10 if i % 2 == 0 else -0.30)
           for i in range(22)],
        # hour 7, btc, coin_flip_dog: 30 trades, 22 winners, net +5.00
        *[(int((base + timedelta(days=i // 6, hours=7)).timestamp()), "BTC",
            "coin_flip_dog", 0.20 if i % 8 != 0 else -0.10)
           for i in range(30)],
    ]
    conn.executemany("INSERT INTO trades (created_at, symbol, strategy, pnl) VALUES (?,?,?,?)",
                     rows)
    conn.commit()
    conn.close()
    return db


def test_bucket_by_hour_groups_correctly(tmp_path: Path):
    db = _make_db(tmp_path)
    rows = fetch_trades(str(db), days=None)
    assert len(rows) == 25 + 5 + 22 + 30
    buckets = bucket_by_hour(rows)
    by_key = {(b.utc_hour, b.symbol, b.strategy): b for b in buckets}
    assert (14, "btc", "temporal_arb") in by_key
    b = by_key[(14, "btc", "temporal_arb")]
    assert b.n_trades == 30
    assert b.net_pnl > 0, "expected hour-14 BTC temporal_arb to net positive"


def test_recommend_whitelist_excludes_low_trades(tmp_path: Path):
    db = _make_db(tmp_path)
    rows = fetch_trades(str(db), days=None)
    buckets = bucket_by_hour(rows)
    wl = recommend_whitelist(buckets, min_trades=20, min_winrate=0.50)
    # hour 7 BTC coin_flip_dog: 30 trades, high winrate → included
    assert 7 in wl.get("btc", [])
    # hour 14 BTC temporal_arb: 30 trades, high winners → included
    assert 14 in wl.get("btc", [])
    # hour 3 ETH coin_flip_dog: 22 trades, 50% winrate but net negative → excluded
    assert 3 not in wl.get("eth", [])
    # in our synthetic data, eth has only this one bucket → eth may not be in dict
    assert wl.get("eth", []) in ([], [])  # negative net_pnl for eth → empty


def test_recommend_whitelist_empty_when_no_data(tmp_path: Path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, created_at INTEGER, "
        "symbol TEXT, strategy TEXT, pnl REAL)"
    )
    conn.close()
    rows = fetch_trades(str(db), days=None)
    buckets = bucket_by_hour(rows)
    wl = recommend_whitelist(buckets, min_trades=20, min_winrate=0.50)
    assert wl == {}


def test_render_markdown_contains_whitelist():
    md = render_markdown([], {"btc": [7, 14], "eth": []}, min_trades=20, min_winrate=0.50)
    assert "btc" in md
    assert "[7, 14]" in md
    assert "no qualifying hours" in md


def test_cli_runs(tmp_path: Path):
    db = _make_db(tmp_path)
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    rc = subprocess.run(
        [sys.executable, "scripts/analyze_trade_hours.py",
         "--db", str(db), "--days", "0",
         "--out-json", str(out_json), "--out-md", str(out_md)],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    assert rc.returncode == 0, rc.stderr
    payload = json.loads(out_json.read_text())
    assert "recommended_whitelist" in payload
    assert "btc" in payload["recommended_whitelist"]
    md_text = out_md.read_text()
    assert "# Hourly P&L Analysis" in md_text
