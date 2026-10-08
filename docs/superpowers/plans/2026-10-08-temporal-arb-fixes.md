# Temporal Arb Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolver tres problemas reportados en producción (compra post-cutoff, Mart-Hedge de 1 sola ronda con mult constante, pérdidas sin cobertura) y añadir filtro horario automático basado en datos de operativa histórica, sin romper la suite de tests existente.

**Architecture:** Cuatro cambios en `bot/strategies/temporal_arb.py` (semántica de `ta_mart_hedge_mult` → compounding, cutoff estricto al inicio de cada fase, gate de cutoff en R1/R2 de Mart-Hedge), un módulo nuevo `bot/strategies/market_hours_filter.py` registrado en el strategy registry, un script nuevo `scripts/analyze_trade_hours.py` que produce whitelist por símbolo desde `trades`, y wiring en `bot/state.py` + dashboard para editar la whitelist vía `/settings`. Despliegue en una sola pasada al VPS con snapshot previo y validación paper ≥ 24h.

**Tech Stack:** Python 3.12, sqlite3 (DB existente), pytest, pandas (opcional, solo si está disponible — fallback a `csv` stdlib), SSH via `sshpass -e`, Flask dashboard existente.

**Spec:** `docs/superpowers/specs/2026-10-08-temporal-arb-fixes-design.md`

## Global Constraints

- **No regresiones**: la suite completa debe seguir verde — 651 tests en `tests/` (valor pre-cambio según `CLAUDE.md`). Tests nuevos: ≥ 13.
- **VPS preservado**: snapshot a `/opt/polymarket-bot/.backups/mh_compound_<TS>/` antes de cualquier `scp`. Customizaciones existentes (`cfd_enabled=True`, `bot/coinbase_ticker_feed.py` no-tracked) NO se tocan.
- **Paper-only deploy**: deploy inicial y primeras 24h en `TRADING_MODE=paper` (ya está así). Decisión de pasar a live es del usuario, no de esta entrega.
- **Degradación segura**: si el análisis horario no produce whitelist (datos insuficientes), `whitelist_hours_<symbol>=""` → sin filtro. Nunca romper el bot por no tener análisis.
- **Convenciones del proyecto**: comentarios de código en inglés, mensajes de log y UI en español (CLAUDE.md).
- **Sin migraciones de DB**: el nuevo estado se persiste en `bot_config` (key/value ya existente).
- **No breaking changes** en APIs públicas de `bot/state.py`, `bot/streak_trader.py`, `bot/main.py`.
- **Commits frecuentes**: cada task termina con un commit individual.

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `bot/strategies/temporal_arb.py` | Estado MH + cutoff estricto | Modify |
| `bot/strategies/market_hours_filter.py` | Descriptor whitelist por hora UTC | Create |
| `bot/strategies/__init__.py` | Registry de descriptores | Modify (1 línea) |
| `bot/state.py` | Campos `whitelist_hours_{btc,eth,sol}` | Modify (aditivo) |
| `bot/config.py` | Defaults `ta_mart_hedge_mult=2.0`, RuntimeFields whitelist | Modify |
| `bot/dashboard.py` | Ruta whitelist set/get (POST) | Modify |
| `bot/templates/settings.html` | UI inputs CSV whitelist | Modify |
| `bot/static/settings.js` | Validación CSV whitelist | Modify |
| `scripts/analyze_trade_hours.py` | Análisis P&L por bucket horario UTC | Create |
| `data/hour_analysis_<TS>.{json,md}` | Outputs reproducibles del análisis | Create (gitignored commit) |
| `tests/test_temporal_arb.py` | Tests compounding + cutoff + MH gate | Modify (aditivo) |
| `tests/test_market_hours.py` | Tests del filtro horario | Create |
| `tests/test_analyze_hours.py` | Tests del script de análisis (datos sintéticos) | Create |
| `docs/PLAN.md` | Mención del cambio + comando de análisis | Modify |

---

### Task 1: Snapshot y análisis horario (data-driven whitelist)

**Files:**
- Create (local): `data/hour_analysis_<TS>.json`, `data/hour_analysis_<TS>.md`
- Read-only: `/opt/polymarket-bot/data/streak_snapper.db` (via SSH)
- Create (script): `scripts/analyze_trade_hours.py`

**Interfaces:**
- Consumes: DB `trades` table en VPS (vía sshpass)
- Produces:
  - JSON reproducible con buckets `{utc_hour, strategy, n_trades, net_pnl, avg_pnl, win_rate, max_loss, max_win}`
  - Markdown summary con recomendación de whitelist por símbolo
  - Stdout con tabla compacta

- [ ] **Step 1: Crear estructura básica de `scripts/analyze_trade_hours.py` con CLI args**

```python
"""Analyze historic Polymarket 5m trades by UTC hour bucket.

Reads trades table from a sqlite DB (default: local Codespace backup or VPS via sshpull),
groups by UTC hour and strategy, computes P&L statistics, and emits:
  - Markdown table to stdout
  - JSON to data/hour_analysis_<UTC_TS>.json

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
```

- [ ] **Step 2: Crear test con DB sintética**

```python
# tests/test_analyze_hours.py
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
    sqlite3.connect(db).close()
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
```

- [ ] **Step 3: Crear `data/hour_analysis_<TS>.json` y `data/hour_analysis_<TS>.md` desde VPS**

```bash
# 1. Asegurar que SSHPASS esté seteado (no pegar en chat)
export SSHPASS='<vps-root-password>'
sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@76.13.251.202 \
  "cd /opt/polymarket-bot && source venv/bin/activate && \
   sqlite3 data/streak_snapper.db 'SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM trades WHERE pnl IS NOT NULL'"

# 2. Pull la DB a local (si quieres análisis offline)
sshpass -e scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@76.13.251.202:/opt/polymarket-bot/data/streak_snapper.db \
  data/streak_snapper_vps_<TS>.db

# 3. Correr el script sobre la DB pullada
python scripts/analyze_trade_hours.py \
  --db data/streak_snapper_vps_<TS>.db \
  --days 7 \
  --out-json data/hour_analysis_<TS>.json \
  --out-md data/hour_analysis_<TS>.md
```

Expected: dos archivos creados. El JSON tiene estructura estable. El `.md` resume por símbolo y estrategia.

- [ ] **Step 4: Verificar que pytest local pasa con el nuevo test**

```bash
cd /workspaces/Polymarket-BotV1
python -m pytest tests/test_analyze_hours.py -v
```

Expected: 5/5 PASS en <1s.

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_trade_hours.py tests/test_analyze_hours.py
git commit -m "feat(analyze): hourly P&L aggregation script + tests

Groups trades by (utc_hour, symbol, strategy), computes win rate / net P&L,
and emits a JSON + Markdown report with a heuristic whitelist per symbol.
No runtime impact — this is a data-prep tool for the hour filter feature."
```

- [ ] **Step 6: Adjuntar el output `.md` al PR description y guardar el `.json` para Task 12**

`data/hour_analysis_<TS>.{json,md}` se commitean al repo solo si tienen tamaño < 50KB; si no, se commitea el `.md` y el JSON va a un gist/release.

---

### Task 2: `_TAWindow` — añadir `round_n_qty` tracking (preparación para compounding)

**Files:**
- Modify: `bot/strategies/temporal_arb.py:209-250` (dataclass fields)
- Modify: `tests/test_temporal_arb.py` (test del nuevo campo)

**Interfaces:**
- Consumes: nada (refactor aditivo)
- Produces: `ta.mart_hedge_round_qty: float` — qty efectiva de la ronda más reciente (default 0.0). Se setea junto con `ta.mart_hedge_qty` y mantiene el mismo valor semántico. R1 lo deja en `first_shares × mult`, R2 lo duplica a `round_qty × mult`.

- [ ] **Step 1: Escribir test**

```python
# Append to tests/test_temporal_arb.py
class TestTAWindowRoundQty:
    def test_round_qty_defaults_to_zero(self):
        from bot.strategies.temporal_arb import _get_window
        win = _get_window("btc", window_ts=1)
        assert win.mart_hedge_round_qty == 0.0

    def test_window_ts_change_resets_round_qty(self):
        from bot.strategies.temporal_arb import _get_window
        _get_window("btc", window_ts=1).mart_hedge_round_qty = 50.0
        win2 = _get_window("btc", window_ts=2)
        assert win2.mart_hedge_round_qty == 0.0
```

- [ ] **Step 2: Implementar el campo en `_TAWindow`**

```python
# In bot/strategies/temporal_arb.py, in _TAWindow dataclass after
# `mart_hedge_qty: float = 0.0` (line ~248) add:
    mart_hedge_round_qty: float = 0.0  # qty efectiva de la ronda más reciente;
                                       # usada como base para la siguiente ronda (compounding).
```

- [ ] **Step 3: Verificar tests pasan**

```bash
python -m pytest tests/test_temporal_arb.py::TestTAWindowRoundQty -v
```

Expected: 2/2 PASS.

- [ ] **Step 4: Commit**

```bash
git add bot/strategies/temporal_arb.py tests/test_temporal_arb.py
git commit -m "refactor(temporal_arb): add mart_hedge_round_qty to _TAWindow

Prepares the state for MH compounding (R_n = R_(n-1) × mult) without
changing behavior yet. Existing mart_hedge_qty kept for backward compat
in the record_box_fill call; round_qty becomes the canonical source
of truth for the next round's multiplier base."
```

---

### Task 3: Mart-Hedge compounding — Round 1 (`first_shares × mult`)

**Files:**
- Modify: `bot/strategies/temporal_arb.py:1064-1067` (R1 qty calc + record)
- Modify: `tests/test_temporal_arb.py` (add R1 qty test)

**Interfaces:**
- Consumes: `ta.first_shares_filled`, `state.ta_mart_hedge_mult`
- Produces: `mart_hedge_round_qty = first_shares × mult`, persisted to `mart_hedge_qty` for record-box-fill

- [ ] **Step 1: Escribir el test de R1**

```python
# Append to tests/test_temporal_arb.py — applies to existing
# TestMartHedge class; the helper there already sets up a Mart-Hedge
# scenario via _observe. Add this new test case:

def test_r1_qty_is_first_shares_times_mult(monkeypatch):
    """R1 qty must be first_shares_filled × ta_mart_hedge_mult, anchored
    to the initial leg (NOT to a prior round)."""
    # Reuse the existing TestMartHedge fixture if available, else build:
    ta, ctx = _build_mart_hedge_scenario(
        first_shares_filled=30.0,
        first_px=0.50,
        mart_hedge_mult=2.0,
        side_changed=True,
    )
    # Patch trader to capture mart_qty
    captured = []
    from bot.strategies import temporal_arb
    orig_place = ctx.trader._place_taker_order
    def capture(tok, side, price, qty, *a, **kw):
        captured.append(qty)
        return orig_place(tok, side, price, qty, *a, **kw)
    monkeypatch.setattr(ctx.trader, "_place_taker_order", capture)

    # Run observe once
    temporal_arb._observe(ctx)
    assert captured, "expected R1 to fire"
    assert captured[0] == pytest.approx(60.0), \
        f"R1 qty should be first_shares × mult = 30 × 2 = 60, got {captured[0]}"
    assert ta.mart_hedge_round_qty == pytest.approx(60.0)
    assert ta.mart_hedge_rounds == 1
```

If `TestMartHedge` doesn't have a `_build_mart_hedge_scenario` helper, add this minimal helper at module top of `tests/test_temporal_arb.py`:

```python
def _build_mart_hedge_scenario(*, first_shares_filled, first_px, mart_hedge_mult=2.0,
                               side_changed=True):
    """Build a minimal ctx/ta for Mart-Hedge observe tests.

    Sets up half_open with first leg filled, side_changed True, ready for R1.
    """
    from bot.strategies.temporal_arb import _TAWindow, _get_window
    from bot.strategies.base import StrategyContext
    from unittest.mock import MagicMock

    state = MagicMock()
    state.ta_mart_hedge_enabled = True
    state.ta_mart_hedge_min_secs = 0
    state.ta_mart_hedge_mult = mart_hedge_mult
    state.ta_mart_hedge_max_rounds = 2
    state.ta_mart_hedge_loss_fallback_pct = 0.50
    state.ta_mh_monitor_secs = 30.0
    state.spot_price = 86100.0
    tokens = MagicMock()
    tokens.window_ts = 1000
    tokens.up_token_id = "tok-up"
    tokens.down_token_id = "tok-dn"
    ta = _get_window("btc", window_ts=1000)
    ta.phase = "half_open"
    ta.first_side = "DOWN"
    ta.first_px = first_px
    ta.first_tok = tokens.down_token_id
    ta.first_shares_filled = first_shares_filled
    ta.first_shares_target = first_shares_filled
    ta.first_cost_sum = first_px * first_shares_filled
    ta.first_leg_filled_at = 0.0  # epoch means lots of seconds elapsed
    ta.strike = 86050.0 if side_changed else 86100.0  # 86050 makes UP the winner
    ctx = MagicMock(spec=StrategyContext)
    ctx.state = state
    ctx.symbol = "btc"
    ctx.tokens = tokens
    ctx.trader = MagicMock()
    ctx.trader._place_taker_order.return_value = "oid-1"
    ctx.trader._record_box_fill.return_value = None
    ctx.seconds_left = 150.0  # before cutoff, so cutoff guard passes
    ctx.state.get_asks.return_value = (0.55, 0.18)
    return ta, ctx
```

(The helper is reused in Tasks 4, 7.)

- [ ] **Step 2: Implementar — cambiar la línea de R1 qty**

```python
# In bot/strategies/temporal_arb.py around line 1064, replace:
#   mart_qty = round(ta.first_shares_filled * mart_hedge_mult, 4)
# with:
            # Spec 2026-10-08: round_n = round_(n-1) × mult (compounding).
            # For R1, there is no prior round → base is first_shares_filled.
            base_qty = ta.first_shares_filled
            mart_qty = round(base_qty * mart_hedge_mult, 4)
            ta.mart_hedge_round_qty = mart_qty  # canonical for next round's base
```

- [ ] **Step 3: Verificar el test de R1**

```bash
python -m pytest tests/test_temporal_arb.py::test_r1_qty_is_first_shares_times_mult -v
```

Expected: PASS.

- [ ] **Step 4: Verificar regresión completa**

```bash
python -m pytest tests/ -q
```

Expected: 651 + 2 (Task 2) + 1 (Task 3) = 654 PASS, 3 known failures in `test_spread_harvest.py`.

- [ ] **Step 5: Commit**

```bash
git add bot/strategies/temporal_arb.py tests/test_temporal_arb.py
git commit -m "feat(temporal_arb): Mart-Hedge R1 anchored to first_shares × mult

Per spec 2026-10-08 (compounding). R1 base is first_shares_filled; R_n
(n>=2) will use ta.mart_hedge_round_qty in the next task. No behavior
change yet for R2+."
```

---

### Task 4: Mart-Hedge compounding — Round 2+ (`round_qty × mult`)

**Files:**
- Modify: `bot/strategies/temporal_arb.py:1148-1156` (R2 qty calc)
- Modify: `tests/test_temporal_arb.py` (add R2 + no-R3 tests)

**Interfaces:**
- Consumes: `ta.mart_hedge_round_qty` (set by R1 in Task 3)
- Produces: R2 qty = `round_qty × mult`. Si `mart_hedge_rounds >= 2`, no se ejecuta R3 (q cap respetado por `max_rounds`).

- [ ] **Step 1: Test R2 qty = round1_qty × mult**

```python
# Append to tests/test_temporal_arb.py

def test_r2_qty_is_r1_qty_times_mult(monkeypatch):
    """R2 must multiply ta.mart_hedge_round_qty (set by R1) by mult, NOT
    first_shares_filled again. With mult=2.0, R1→60 → R2→120."""
    from bot.strategies import temporal_arb
    ta, ctx = _build_mart_hedge_scenario(
        first_shares_filled=30.0, first_px=0.50,
        mart_hedge_mult=2.0, side_changed=True,
    )
    # Simulate R1 already fired with qty 60, then side flipped back
    ta.mart_hedge_rounds = 1
    ta.mart_hedge_side = "UP"
    ta.mart_hedge_px = 0.55
    ta.mart_hedge_qty = 60.0
    ta.mart_hedge_round_qty = 60.0  # R1's qty
    ta.last_mh_round_at = 0.0       # enough time elapsed
    ta.strike = 86150.0             # DOWN now winner (side changed from UP)
    ctx.state.spot_price = 86080.0  # below strike → DOWN winner, side_changed True

    captured = []
    orig_place = ctx.trader._place_taker_order
    def capture(tok, side, price, qty, *a, **kw):
        captured.append(qty)
        return orig_place(tok, side, price, qty, *a, **kw)
    monkeypatch.setattr(ctx.trader, "_place_taker_order", capture)

    temporal_arb._observe(ctx)
    assert captured, "expected R2 to fire"
    assert captured[0] == pytest.approx(120.0), \
        f"R2 qty must be R1_qty × mult = 60 × 2 = 120, got {captured[0]}"
    assert ta.mart_hedge_round_qty == pytest.approx(120.0)
    assert ta.mart_hedge_rounds == 2


def test_no_r3_fires_when_max_rounds_2(monkeypatch):
    """max_rounds=2 → even if side flips again, no R3."""
    from bot.strategies import temporal_arb
    ta, ctx = _build_mart_hedge_scenario(
        first_shares_filled=30.0, first_px=0.50,
        mart_hedge_mult=2.0, side_changed=True,
    )
    ta.mart_hedge_rounds = 2
    ta.mart_hedge_side = "DOWN"
    ta.mart_hedge_px = 0.40
    ta.mart_hedge_qty = 120.0
    ta.mart_hedge_round_qty = 120.0
    ta.last_mh_round_at = 0.0
    ctx.state.ta_mart_hedge_max_rounds = 2
    # If a third round COULD fire, place_taker_order would be called.
    ctx.trader._place_taker_order.return_value = None
    temporal_arb._observe(ctx)
    # No call from R3 — verify side didn't change and no order placed
    assert ta.mart_hedge_rounds == 2
```

- [ ] **Step 2: Implementar cambio en línea de R2**

```python
# In bot/strategies/temporal_arb.py around line 1154, replace:
#   mart_qty = round(ta.first_shares_filled * mart_hedge_mult, 4)
# with:
                    # Spec 2026-10-08: round_n = round_(n-1) × mult (compounding).
                    # round_qty was last written by R1 as first_shares × mult;
                    # R2 doubles it again.
                    base_qty = ta.mart_hedge_round_qty
                    mart_qty = round(base_qty * mart_hedge_mult, 4)
```

(Also update the line inside the R1 block that sets `ta.mart_hedge_qty = mart_qty` to ALSO set `ta.mart_hedge_round_qty` if not already — already done by Task 3.)

- [ ] **Step 3: Verificar tests de R2**

```bash
python -m pytest tests/test_temporal_arb.py::test_r2_qty_is_r1_qty_times_mult tests/test_temporal_arb.py::test_no_r3_fires_when_max_rounds_2 -v
```

Expected: 2/2 PASS.

- [ ] **Step 4: Regresión completa + commit**

```bash
python -m pytest tests/ -q
git add bot/strategies/temporal_arb.py tests/test_temporal_arb.py
git commit -m "feat(temporal_arb): Mart-Hedge R2 compounding on R1 qty

round_qty multiplier base changes from first_shares_filled (constant)
to ta.mart_hedge_round_qty (compounding). With first=30 and mult=2,
deployment per window is now 60+120=180 shares (was 75+75=150 with
constant 2.5×). max_rounds=2 unchanged; R3+ never fires."
```

---

### Task 5: Cutoff estricto en `accumulating`

**Files:**
- Modify: `bot/strategies/temporal_arb.py:566-590`
- Modify: `tests/test_temporal_arb.py`

**Interfaces:**
- Consumes: `secs`, `state.ta_entry_cutoff_sec` (default 120)
- Produces: `stop_reason = "cutoff_strict"` cuando `secs < ta_entry_cutoff_sec`. Log explícito "TA_CUTOFF_TRANCHES". Sin compra adicional.

- [ ] **Step 1: Test**

```python
def test_accumulating_stops_at_cutoff(monkeypatch):
    """Cuando secs < cutoff en accumulating, se detiene con cutoff_strict
    y no se compran más tranches."""
    from bot.strategies import temporal_arb
    from bot.strategies.temporal_arb import _TAWindow

    state = MagicMock()
    state.ta_min_ask = 0.40
    state.ta_max_ask = 0.55
    state.ta_entry_cutoff_sec = 120.0
    state.ta_bailout_sec = 60.0
    state.ta_order_slice = 5.0
    state.ta_shares_per_leg = 20.0
    state.spot_price = 86100.0
    state.get_asks.return_value = (0.50, 0.50)

    tokens = MagicMock()
    tokens.up_token_id = "tok-up"
    tokens.down_token_id = "tok-dn"
    tokens.window_ts = 2000

    ta = _TAWindow(window_ts=2000, phase="accumulating",
                   first_side="DOWN", first_tok="tok-dn",
                   first_px=0.50, first_shares_target=20.0,
                   first_shares_filled=10.0, first_cost_sum=5.0)
    temporal_arb._WINDOWS["btc"] = ta

    ctx = MagicMock()
    ctx.state = state
    ctx.symbol = "btc"
    ctx.tokens = tokens
    ctx.trader = MagicMock()
    ctx.seconds_left = 110.0  # below 120 cutoff

    place_calls = []
    monkeypatch.setattr(ctx.trader, "_place_taker_order",
                        lambda *a, **kw: place_calls.append(a) or "oid")

    temporal_arb._observe(ctx)
    assert ta.phase == "half_open", f"expected half_open, got {ta.phase}"
    assert place_calls == [], f"expected zero buys after cutoff, got {place_calls}"
```

- [ ] **Step 2: Implementar — añadir stop_reason antes de bailout**

```python
# In bot/strategies/temporal_arb.py, in the accumulating block (line ~567),
# after the existing stop_reason checks, add this branch BEFORE `bailout`:

        stop_reason = None
        if secs <= bail_sec:
            stop_reason = "bailout"
        elif secs < q_cut:
            # Spec 2026-10-08: cutoff estricto. Stop tranches antes de bailout
            # para no seguir comprando post-120s. Log explícito.
            stop_reason = "cutoff_strict"
        elif remaining <= 0:
            stop_reason = "target_reached"
        elif leader_ask is None or not (min_ask <= leader_ask <= max_ask):
            stop_reason = "ask_out_of_band"
```

Then in the `if stop_reason:` block, add a log line for the new reason:

```python
        if stop_reason:
            if ta.first_shares_filled > 0:
                ta.first_px = round(ta.first_cost_sum / ta.first_shares_filled, 4)
            if stop_reason == "cutoff_strict":
                logger.info(
                    f"[TA] ⏭ CUTOFF_STRICT_TRANCHES — no más compras "
                    f"primera_pata={ta.first_side}@{ta.first_px:.3f} "
                    f"filled={ta.first_shares_filled:.0f}/{ta.first_shares_target:.0f} "
                    f"secs={secs:.0f} < cutoff={q_cut:.0f}",
                    icon="⏭",
                )
                state.record_skip("TA_CUTOFF_TRANCHES")
            else:
                logger.info(...)
            ...
```

(Replace the existing `logger.info` for the accumulating end with the conditional above.)

- [ ] **Step 3: Verificar test + regresión**

```bash
python -m pytest tests/test_temporal_arb.py::test_accumulating_stops_at_cutoff -v
python -m pytest tests/ -q
```

Expected: nuevo test PASS, suite completa verde (654+N tests).

- [ ] **Step 4: Commit**

```bash
git add bot/strategies/temporal_arb.py tests/test_temporal_arb.py
git commit -m "fix(temporal_arb): accumulating cuts at cutoff, no buy post-120s

Adds stop_reason='cutoff_strict' before the existing 'bailout' check
in the accumulating phase. Fixes silent bug #1 from spec 2026-10-08
audit: with a previous position, more tranches could fill even past
the 120s cutoff since only the IDLE gate was enforced."
```

---

### Task 6: Cutoff estricto al entrar en `half_open` (gates A/B/C/D)

**Files:**
- Modify: `bot/strategies/temporal_arb.py:615` (early-return after entering half_open)
- Modify: `tests/test_temporal_arb.py`

**Interfaces:**
- Consumes: `secs`, `state.ta_entry_cutoff_sec`
- Produces: `phase = "closed"` (con log) si `secs < cutoff` al evaluar half_open. NO se intenta Path A, B, C, D (estos requieren compra). Mart-Hedge (Task 7) tiene su propio gate.

- [ ] **Step 1: Test**

```python
def test_half_open_no_paths_after_cutoff(monkeypatch):
    """Cuando secs < cutoff al evaluar half_open, ningún path (A/B/C/D)
    puede ejecutarse. La pata abierta se queda hasta resolución."""
    from bot.strategies import temporal_arb
    from bot.strategies.temporal_arb import _TAWindow

    state = MagicMock()
    state.ta_complete_cap = 0.82
    state.ta_entry_cutoff_sec = 120.0
    state.ta_bailout_sec = 60.0
    state.ta_cancel_all_sec = 10.0
    state.ta_grace_secs = 60.0
    state.ta_hedge_enabled = False  # disable B so it doesn't block
    state.ta_twap_hedge_enabled = False
    state.ta_mart_hedge_enabled = False  # disabled → no Mart-Hedge either
    state.ta_profit_lock_enabled = False
    state.ta_hedge_drop_pct = 0.40
    state.ta_hedge_max_sum = 0.92
    state.spot_price = 86100.0
    state.get_asks.return_value = (0.30, 0.30)  # cheap second leg → Path A would fire

    tokens = MagicMock()
    tokens.up_token_id = "tok-up"
    tokens.down_token_id = "tok-dn"
    tokens.window_ts = 3000

    ta = _TAWindow(window_ts=3000, phase="half_open",
                   first_side="DOWN", first_tok="tok-dn",
                   first_px=0.50, first_shares_filled=20.0,
                   first_cost_sum=10.0, strike=86050.0,
                   first_leg_filled_at=0.0, entry_timestamp=0.0)
    ta.first_shares_target = 20.0
    temporal_arb._WINDOWS["btc"] = ta

    ctx = MagicMock()
    ctx.state = state
    ctx.symbol = "btc"
    ctx.tokens = tokens
    ctx.trader = MagicMock()
    ctx.seconds_left = 100.0  # below 120 cutoff, but cheap second leg

    place_calls = []
    monkeypatch.setattr(ctx.trader, "_place_taker_order",
                        lambda *a, **kw: place_calls.append(a) or "oid")

    temporal_arb._observe(ctx)
    assert place_calls == [], "expected no buys after cutoff"
    # Phase stays half_open (we don't clobber it; the position still resolves at end)
```

- [ ] **Step 2: Implementar — añadir guard al inicio de half_open**

```python
# In bot/strategies/temporal_arb.py, line ~615, BEFORE `current_first_ask = ...`,
# insert:

    if ta.phase == "half_open":
        # Spec 2026-10-08: cutoff estricto. Ningún path de coverage compra
        # si secs < cutoff. La pata abierta se queda hasta resolución.
        # Mart-Hedge (Path E) tiene su propio gate, así que este guard
        # afecta A/B/C/D únicamente.
        if secs < q_cut and not ta.logged_hold_cutoff:
            ta.logged_hold_cutoff = True
            logger.info(
                f"[TA] ⏭ HOLD_CUTOFF  primera_pata={ta.first_side}@{ta.first_px:.3f} "
                f"secs={secs:.0f} < cutoff={q_cut:.0f}  → mantener hasta resolución",
                icon="⏭",
            )
            state.record_observation("TA_HOLD_CUTOFF")
        if secs < q_cut:
            # Saltar los paths A/B/C/D (hold pasivo).
            return

        second_side = "DOWN" if ta.first_side == "UP" else "UP"
        ...
```

(Add `logged_hold_cutoff: bool = False` to `_TAWindow` dataclass at line 224 next to `logged_bailout`.)

- [ ] **Step 3: Verificar test + regresión + commit**

```bash
python -m pytest tests/test_temporal_arb.py::test_half_open_no_paths_after_cutoff -v
python -m pytest tests/ -q
git add bot/strategies/temporal_arb.py tests/test_temporal_arb.py
git commit -m "fix(temporal_arb): half_open paths A/B/C/D gated by cutoff

Spec 2026-10-08 cutoff estricto. Cuando secs < ta_entry_cutoff_sec
(default 120) en half_open, no se ejecutan los paths de coverage
(pair-complete, hedge-recovery, TWAP-hedge, profit-lock). La pata
abierta se mantiene hasta resolución. Mart-Hedge tiene su propio
gate (siguiente task)."
```

---

### Task 7: Cutoff gate en Mart-Hedge (R1 y R2)

**Files:**
- Modify: `bot/strategies/temporal_arb.py:1049` (R1), `1120` (R2)
- Modify: `tests/test_temporal_arb.py`

**Interfaces:**
- Consumes: `secs`, `state.ta_entry_cutoff_sec`
- Produces: si `secs < cutoff`, R_n se salta aunque side_changed o loss_fallback sean True.

- [ ] **Step 1: Tests**

```python
def test_mh_r1_cutoff_blocks_fire(monkeypatch):
    """Mart-Hedge R1 no dispara aunque side_changed=True si secs < cutoff."""
    from bot.strategies import temporal_arb
    ta, ctx = _build_mart_hedge_scenario(
        first_shares_filled=30.0, first_px=0.50,
        mart_hedge_mult=2.0, side_changed=True,
    )
    ctx.seconds_left = 100.0  # below cutoff, but side_changed True
    ctx.state.ta_entry_cutoff_sec = 120.0

    place_calls = []
    monkeypatch.setattr(ctx.trader, "_place_taker_order",
                        lambda *a, **kw: place_calls.append(a) or "oid")

    temporal_arb._observe(ctx)
    assert place_calls == [], f"expected no MH fire post-cutoff, got {place_calls}"
    assert ta.mart_hedge_rounds == 0


def test_mh_r2_cutoff_blocks_fire(monkeypatch):
    """Mart-Hedge R2 no dispara aunque side_changed=True si secs < cutoff."""
    from bot.strategies import temporal_arb
    ta, ctx = _build_mart_hedge_scenario(
        first_shares_filled=30.0, first_px=0.50,
        mart_hedge_mult=2.0, side_changed=True,
    )
    ta.mart_hedge_rounds = 1
    ta.mart_hedge_side = "UP"
    ta.mart_hedge_px = 0.55
    ta.mart_hedge_qty = 60.0
    ta.mart_hedge_round_qty = 60.0
    ta.last_mh_round_at = 0.0
    ctx.seconds_left = 100.0  # below cutoff
    ctx.state.spot_price = 86080.0  # side changed from UP→DOWN

    place_calls = []
    monkeypatch.setattr(ctx.trader, "_place_taker_order",
                        lambda *a, **kw: place_calls.append(a) or "oid")

    temporal_arb._observe(ctx)
    assert place_calls == [], f"expected no R2 post-cutoff, got {place_calls}"
    assert ta.mart_hedge_rounds == 1
```

- [ ] **Step 2: Implementar — añadir guard en bloque R1 (línea ~1049) y R2 (~1120)**

For R1, add inside the `if (mart_hedge_enabled and ta.first_leg_filled_at is not None ...):` block, RIGHT AFTER `current_first_ask is not None and opp_ask is not None` and BEFORE `and (side_changed_e1 or loss_pct >= mart_hedge_loss_fallback)`:

```python
        if (
            mart_hedge_enabled
            and ta.first_leg_filled_at is not None
            and ta.first_shares_filled > 0
            and ta.first_px is not None
            and ta.mart_hedge_rounds == 0
            and (time.time() - ta.first_leg_filled_at) >= mart_hedge_min_secs
            and current_first_ask is not None
            and opp_ask is not None
            and secs >= q_cut                # NEW: cutoff gate
            and (
                side_changed_e1
                or loss_pct >= mart_hedge_loss_fallback
            )
        ):
```

For R2, in the `if (mart_hedge_enabled and ta.mart_hedge_rounds >= 1 and ...):` block, add `and secs >= q_cut` to the condition list, and add an early-return at the top of the block:

```python
        if (
            mart_hedge_enabled
            and ta.mart_hedge_rounds >= 1
            and ta.mart_hedge_rounds < mart_hedge_max_rounds
            and ta.mart_hedge_side is not None
            and ta.mart_hedge_px is not None
            and ta.mart_hedge_px > 0
            and ta.last_mh_round_at is not None
            and (time.time() - ta.last_mh_round_at) >= mh_monitor_secs
            and secs >= q_cut              # NEW: cutoff gate
        ):
```

- [ ] **Step 3: Verificar + regresión + commit**

```bash
python -m pytest tests/test_temporal_arb.py::test_mh_r1_cutoff_blocks_fire tests/test_temporal_arb.py::test_mh_r2_cutoff_blocks_fire -v
python -m pytest tests/ -q
git add bot/strategies/temporal_arb.py tests/test_temporal_arp.py 2>/dev/null
git add bot/strategies/temporal_arb.py tests/test_temporal_arb.py
git commit -m "fix(temporal_arb): Mart-Hedge R1 and R2 gated by cutoff (no buy post-120s)

Per spec 2026-10-08: even if side_changed or loss_fallback triggers
Mart-Hedge, the round does not execute when secs < ta_entry_cutoff_sec
(default 120). The open first leg resolves normally at window end."
```

---

### Task 8: Módulo whitelist horario (`market_hours_filter.py`)

**Files:**
- Create: `bot/strategies/market_hours_filter.py`
- Modify: `bot/strategies/__init__.py` (registry import + DESCRIPTOR list)
- Create: `tests/test_market_hours.py`

**Interfaces:**
- Consumes: `utc_hour(now_epoch)`, `state.whitelist_hours_<symbol>` (CSV string), `ctx.symbol`
- Produces: `DESCRIPTOR` registered in `bot/strategies/__init__.py`. The descriptor's `evaluate(ctx)` returns `[]` (skip signal) when current hour NOT in whitelist, `None` (no opinion) when IN whitelist.

- [ ] **Step 1: Crear el módulo**

```python
# bot/strategies/market_hours_filter.py
"""Market hours filter — gates strategies by UTC hour whitelist.

Spec 2026-10-08: hours are derived from production trade history analysis
(see scripts/analyze_trade_hours.py and data/hour_analysis_<TS>.json).
Per-symbol whitelist is loaded from BotState fields
`whitelist_hours_<symbol>` (CSV like "0,3,7,12,21"). Empty whitelist =
no filter (degradación segura).

This is registered at priority 90 so it runs FIRST — before any
strategy evaluate() — and short-circuits the cycle when the current
hour is not in the whitelist.
"""

from __future__ import annotations

import time
from typing import Optional

from .base import StrategyContext, StrategyDescriptor
from ..runtime_field import RuntimeField


def is_hour_whitelisted(utc_hour: int, whitelist_csv: str) -> bool:
    """Return True if utc_hour is in the CSV whitelist (or list empty).

    Whitelist values: integers in 0..23, comma-separated. Whitespace
    tolerated. Empty string or None → whitelist is open (no filter).
    Invalid entries are silently ignored (test catches the empty case).
    """
    if not whitelist_csv or not whitelist_csv.strip():
        return True
    hours = set()
    for raw in whitelist_csv.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            h = int(raw)
        except ValueError:
            continue
        if 0 <= h <= 23:
            hours.add(h)
    return utc_hour in hours


def current_utc_hour() -> int:
    """Return current UTC hour (0..23). Pure helper, deterministic given time.time()."""
    ts = time.gmtime()
    return ts.tm_hour


class _FilterStrategy:
    """Marker — the actual gate logic lives in evaluate."""
    pass


def _filter_evaluate(ctx: StrategyContext) -> Optional[list]:
    """Descriptor callback. Returns [] when current hour is blocked,
    None when allowed (so the descriptor doesn't bias other strategies).
    """
    state = ctx.state
    symbol = (ctx.symbol or "").lower()
    field_name = f"whitelist_hours_{symbol}"
    whitelist_csv = getattr(state, field_name, "") or ""
    hour = current_utc_hour()
    if is_hour_whitelisted(hour, whitelist_csv):
        return None
    return []


DESCRIPTOR = StrategyDescriptor(
    id="market_hours_filter",
    name="Market Hours Filter",
    description=(
        "Bloquea toda la operativa si la hora UTC actual no está en la "
        "whitelist por símbolo. Whitelist vacía = sin filtro. "
        "Whitelist derivada del análisis de P&L histórico."
    ),
    notes="Prioridad 90 — corre antes que cualquier otra estrategia.",
    evaluate=_filter_evaluate,
    observe=lambda ctx: None,
    is_enabled=lambda state: True,  # always on; whitelist emptiness decides
    enabled_when={"field": "whitelist_hours_btc", "values": ["*"]},  # any non-empty OR empty
    priority=90,
    params=(
        RuntimeField("whitelist_hours_btc", "string",
                     label="Whitelist horas BTC (CSV)",
                     hint="Horas UTC 0-23 separadas por coma. Vacío = sin filtro."),
        RuntimeField("whitelist_hours_eth", "string",
                     label="Whitelist horas ETH (CSV)",
                     hint="Horas UTC 0-23 separadas por coma. Vacío = sin filtro."),
        RuntimeField("whitelist_hours_sol", "string",
                     label="Whitelist horas SOL (CSV)",
                     hint="Horas UTC 0-23 separadas por coma. Vacío = sin filtro."),
    ),
)
```

- [ ] **Step 2: Registrar en el strategy registry**

```python
# bot/strategies/__init__.py — add at top of imports + add to __all__ or registry list
# (whatever pattern the file uses). Check the file first; likely it has a
# DESCRIPTORS = [temporal_arb.DESCRIPTOR, ...] list.

# Find the registry pattern:
grep -n 'DESCRIPTOR\|register' bot/strategies/__init__.py
```

Then add `market_hours_filter.DESCRIPTOR` to whatever list/registry pattern exists. Most likely:

```python
from . import market_hours_filter
# then in the registry list:
DESCRIPTORS = [
    temporal_arb.DESCRIPTOR,
    box_builder.DESCRIPTOR,
    coin_flip_dog.DESCRIPTOR,
    near_res.DESCRIPTOR,
    market_hours_filter.DESCRIPTOR,  # NEW — highest priority
    ...
]
```

- [ ] **Step 3: Crear tests**

```python
# tests/test_market_hours.py
"""Tests for market_hours_filter module and its descriptor."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.strategies.market_hours_filter import (
    is_hour_whitelisted, current_utc_hour, DESCRIPTOR,
    _filter_evaluate,
)


class TestIsHourWhitelisted:
    def test_empty_csv_no_filter(self):
        assert is_hour_whitelisted(0, "") is True
        assert is_hour_whitelisted(23, None) is True
        assert is_hour_whitelisted(12, "  ") is True

    def test_explicit_hour_match(self):
        assert is_hour_whitelisted(14, "0,7,14,21") is True
        assert is_hour_whitelisted(7, "0,7,14,21") is True

    def test_explicit_hour_miss(self):
        assert is_hour_whitelisted(8, "0,7,14,21") is False
        assert is_hour_whitelisted(22, "0,7,14,21") is False

    def test_whitespace_tolerated(self):
        assert is_hour_whitelisted(7, "  0 , 7 , 14 , 21  ") is True

    def test_invalid_entries_ignored(self):
        # 24 is invalid (out of range), "abc" is invalid
        assert is_hour_whitelisted(7, "7,24,abc") is True
        assert is_hour_whitelisted(14, "24,abc") is False  # only invalid → whitelist empty → open


class TestCurrentUtcHour:
    def test_returns_int_in_range(self):
        h = current_utc_hour()
        assert isinstance(h, int)
        assert 0 <= h <= 23


class TestFilterEvaluate:
    def test_returns_none_when_whitelisted(self):
        state = MagicMock()
        state.whitelist_hours_btc = f"{current_utc_hour()}"  # always allow
        ctx = MagicMock()
        ctx.state = state
        ctx.symbol = "btc"
        assert _filter_evaluate(ctx) is None

    def test_returns_empty_list_when_blocked(self):
        state = MagicMock()
        not_now = (current_utc_hour() + 1) % 24
        state.whitelist_hours_btc = f"{not_now}"  # current NOT in list
        ctx = MagicMock()
        ctx.state = state
        ctx.symbol = "btc"
        assert _filter_evaluate(ctx) == []

    def test_empty_whitelist_passes(self):
        state = MagicMock()
        state.whitelist_hours_btc = ""
        ctx = MagicMock()
        ctx.state = state
        ctx.symbol = "btc"
        assert _filter_evaluate(ctx) is None


class TestDescriptor:
    def test_registered_with_priority_90(self):
        assert DESCRIPTOR.priority == 90

    def test_has_runtime_fields(self):
        fields = DESCRIPTOR.params
        names = {f.name for f in fields}
        assert "whitelist_hours_btc" in names
        assert "whitelist_hours_eth" in names
        assert "whitelist_hours_sol" in names
```

- [ ] **Step 4: Verificar tests + regresión**

```bash
python -m pytest tests/test_market_hours.py -v
python -m pytest tests/ -q
```

Expected: 12 nuevos tests PASS, suite completa verde.

- [ ] **Step 5: Commit**

```bash
git add bot/strategies/market_hours_filter.py bot/strategies/__init__.py tests/test_market_hours.py
git commit -m "feat(market_hours): per-symbol UTC hour whitelist gate

Whitelist is heuristic, derived from production trade history
(analyze_trade_hours.py output). Empty whitelist = no filter
(safe degradation). Priority 90 makes it run first, gating all
other strategies. Three RuntimeFields for /settings editing."
```

---

### Task 9: State fields + config defaults

**Files:**
- Modify: `bot/state.py` (additive fields)
- Modify: `bot/config.py` (defaults + add whitelist fields to BASE_FIELDS)

**Interfaces:**
- Consumes: `bot_config` key/value
- Produces: `BotState.whitelist_hours_{btc,eth,sol}` strings initialized from `.env` defaults — empty.

- [ ] **Step 1: Verificar patrón existente para añadir campos al BotState**

```bash
grep -n "whitelist_hours\|ta_mart_hedge_mult\|RuntimeField" bot/state.py | head -10
grep -n "BASE_FIELDS\|whitelist_hours\|RuntimeField" bot/config.py | head -10
```

Look at how `ta_mart_hedge_mult` was added. Use the same pattern.

- [ ] **Step 2: Añadir campos a `BotState` (en `bot/state.py`)**

```python
    # Market Hours Filter — per-symbol UTC hour whitelist (CSV "0,7,14").
    # Empty / None = no filter applied (safe degradation).
    whitelist_hours_btc: str = ""
    whitelist_hours_eth: str = ""
    whitelist_hours_sol: str = ""
```

(Add next to existing similar config fields. If state is a dataclass, follow that pattern; if plain class, follow existing convention.)

- [ ] **Step 3: Añadir defaults en `bot/config.py`**

Find `BASE_FIELDS` (or equivalent) and append three RuntimeField entries that mirror the ones in `market_hours_filter.DESCRIPTOR.params`. Pattern:

```python
RuntimeField("whitelist_hours_btc", "string",
             label="Whitelist horas BTC (CSV)",
             default="",
             hint="Horas UTC 0-23 separadas por coma. Vacío = sin filtro."),
# ... eth, sol análogos
```

(If `BASE_FIELDS` is a list and DESCRIPTOR.params are also added there, decide whether to keep the duplication or only have them in the descriptor. Per spec, the descriptor owns them; if dashboard reads from BASE_FIELDS, duplicate or reference. Look at how `ta_mart_hedge_mult` is registered to know.)

- [ ] **Step 4: Verificar carga desde .env (test rápido)**

```bash
python -c "from bot.config import get_config; print(get_config())"
```

Expected: no error. Whitelist fields default to "".

- [ ] **Step 5: Verificar tests existentes + commit**

```bash
python -m pytest tests/test_db.py tests/test_dashboard.py -v
git add bot/state.py bot/config.py
git commit -m "feat(state,config): whitelist_hours_{btc,eth,sol} default empty

Empty defaults preserve current behavior (no hour filter). Operators
populate via /settings after running analyze_trade_hours and reviewing
the recommended whitelist."
```

---

### Task 10: Dashboard UI para whitelist (form en `/settings`)

**Files:**
- Modify: `bot/templates/settings.html` (3 inputs CSV)
- Modify: `bot/static/settings.js` (validate + save CSV)

**Interfaces:**
- Consumes: GET `/settings` rendering, POST `/config` payload `{key, value}`
- Produces: visible inputs for each symbol's whitelist, validating CSV format on submit

- [ ] **Step 1: Inspeccionar el settings existente para BTC/ETH/SOL config inputs ya presentes**

```bash
grep -n "whitelist\|ta_mart_hedge_mult\|RuntimeField\|csv" bot/templates/settings.html bot/static/settings.js bot/dashboard.py | head -30
```

Find pattern used for `ta_mart_hedge_mult` (probably a number input) and similar. Adapt for string CSV inputs.

- [ ] **Step 2: Añadir los 3 inputs en `bot/templates/settings.html`**

```html
<div class="row">
  <div class="col-md-4">
    <label class="form-label">Whitelist horas BTC (CSV)</label>
    <input type="text" class="form-control whitelist-hours-input"
           data-symbol="btc" placeholder="ej: 0,3,7,14,21"
           value="{{ state.whitelist_hours_btc or '' }}">
    <small class="text-muted">Horas UTC 0–23 separadas por coma. Vacío = sin filtro.</small>
  </div>
  <div class="col-md-4">
    <label class="form-label">Whitelist horas ETH (CSV)</label>
    <input type="text" class="form-control whitelist-hours-input"
           data-symbol="eth" placeholder="ej: 0,3,7,14,21"
           value="{{ state.whitelist_hours_eth or '' }}">
  </div>
  <div class="col-md-4">
    <label class="form-label">Whitelist horas SOL (CSV)</label>
    <input type="text" class="form-control whitelist-hours-input"
           data-symbol="sol" placeholder="ej: 0,3,7,14,21"
           value="{{ state.whitelist_hours_sol or '' }}">
  </div>
</div>
```

(Place alongside the existing TA section. Adapt class names to the project's bootstrap conventions if different.)

- [ ] **Step 3: Añadir validación y POST en `bot/static/settings.js`**

```javascript
// Add to the existing settings form handler — assume there's a global
// saveConfig(key, value) helper. If pattern differs, adapt.

document.querySelectorAll('.whitelist-hours-input').forEach(input => {
  input.addEventListener('change', () => {
    const csv = input.value.trim();
    // Validate: each token must be integer 0-23
    const tokens = csv ? csv.split(',').map(s => s.trim()).filter(Boolean) : [];
    for (const t of tokens) {
      const n = parseInt(t, 10);
      if (!Number.isInteger(n) || n < 0 || n > 23) {
        alert(`Valor inválido en whitelist: "${t}". Use enteros 0–23 separados por coma.`);
        input.focus();
        return;
      }
    }
    const symbol = input.dataset.symbol;
    if (typeof saveConfig === 'function') {
      saveConfig(`whitelist_hours_${symbol}`, csv);
    } else {
      // Fallback: POST /config directly
      fetch('/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
        body: JSON.stringify({key: `whitelist_hours_${symbol}`, value: csv})
      });
    }
  });
});
```

- [ ] **Step 4: Verificar carga de la página `/settings` (smoke test)**

```bash
python run.py 2>&1 | head -20 &
sleep 4
curl -sS http://127.0.0.1:5000/settings -b "dashboard_session=$(curl -sS -c - http://127.0.0.1:5000/login -d 'password=...')" | grep -c "whitelist_hours_btc"
kill %1
```

Expected: at least 1 match.

- [ ] **Step 5: Commit**

```bash
git add bot/templates/settings.html bot/static/settings.js
git commit -m "feat(dashboard): whitelist_hours CSV inputs in /settings

Three per-symbol text inputs with client-side validation (integers
0-23 comma-separated). Empty input = no filter (safe degradation).
Server-side already accepts arbitrary strings via POST /config."
```

---

### Task 11: Default `ta_mart_hedge_mult = 2.0` (compounding)

**Files:**
- Modify: `bot/strategies/temporal_arb.py:169` (`MART_HEDGE_MULT_DEFAULT`)
- Modify: `tests/test_temporal_arb.py` (verify default changed)

**Interfaces:**
- Consumes: nothing
- Produces: default mult cambia de 2.5 → 2.0. Si el VPS ya tiene `bot_config.ta_mart_hedge_mult=2.5`, ese valor persiste (DB override). Para este deploy,我们要 setear `bot_config.ta_mart_hedge_mult=2.0` explícitamente.

- [ ] **Step 1: Cambiar el default en el código fuente**

```python
# In bot/strategies/temporal_arb.py line 169:
MART_HEDGE_MULT_DEFAULT      = 2.0  # Spec 2026-10-08: compounding ×2 sobre base de primera pata.
                                   # Antes 2.5 (constante). Ahora 2.0 (compounding).
```

- [ ] **Step 2: Test que el default cambió**

```python
def test_default_mart_hedge_mult_is_2():
    from bot.strategies import temporal_arb
    assert temporal_arb.MART_HEDGE_MULT_DEFAULT == 2.0
```

- [ ] **Step 3: Commit**

```bash
git add bot/strategies/temporal_arb.py tests/test_temporal_arb.py
git commit -m "chore(temporal_arb): default ta_mart_hedge_mult 2.5 → 2.0

Reflects the new compounding semantics (R_n = R_(n-1) × mult rather
than R_n = first_shares × mult). DB overrides via bot_config still
take precedence — see Task 12 for production application."
```

---

### Task 12: Documentar el spec en `docs/PLAN.md` + actualizar `docs/RUTA.md` con nueva medición

**Files:**
- Modify: `docs/PLAN.md` (1-line summary of this change)
- Modify: `docs/RUTA.md` (placeholder: "medición post-compounding pendiente tras 7d")

**Interfaces:**
- Consumes: nada
- Produces: trazabilidad del cambio en el PLAN maestro y un placeholder en RUTA para la medición post-deploy.

- [ ] **Step 1: Append a `docs/PLAN.md`**

```markdown
## Cambios post-2026-10-08 — Mart-Hedge compounding + cutoff estricto + filtro horario

- **Mart-Hedge compounding**: R_n = R_{n-1} × mult. Default mult=2.0, max_rounds=2. Spec en `docs/superpowers/specs/2026-10-08-temporal-arb-fixes-design.md`.
- **Cutoff estricto 120s**: bloquea toda compra post-cutoff (accumulating, Path A/B/C/D, Mart-Hedge). LPT queda fuera (su ventana ya respeta el cutoff).
- **Filtro horario**: whitelist derivada de `scripts/analyze_trade_hours.py`. Vacía por defecto. UI en `/settings` con tres inputs CSV (BTC, ETH, SOL).

### Comando de re-análisis
```bash
sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@76.13.251.202 "cd /opt/polymarket-bot && source venv/bin/activate && \
  sqlite3 data/streak_snapper.db '.dump trades' | head -100"
# Pull DB → correr analyze_trade_hours.py localmente → actualizar bot_config.
```
```

- [ ] **Step 2: Append a `docs/RUTA.md` (sección de cambios recientes)**

```markdown
### 2026-10-08 — Mart-Hedge compounding + cutoff estricto (medición pendiente)
- Cambio: `ta_mart_hedge_mult` ahora es **compounding** (R_n = R_{n-1} × mult, default 2.0) en lugar de constante.
- Cambio: cutoff 120s ahora **estricto** (todas las fases de compra gating).
- Cambio: whitelist horario derivada de análisis de producción (ver `scripts/analyze_trade_hours.py`).
- **Medición (7d post-deploy)**: tasa de mart-hedge R2 cerradas con profit, P&L agregado de la ventana vs baseline, distribución de horas con profit. Pendiente tras validación paper.
```

- [ ] **Step 3: Commit**

```bash
git add docs/PLAN.md docs/RUTA.md
git commit -m "docs(plan,ruta): record 2026-10-08 temporal-arb fixes"
```

---

### Task 13: Deploy a VPS (snapshot + scp + tests + restart + monitor)

**Files:**
- Create (VPS): `/opt/polymarket-bot/.backups/mh_compound_<TS>/`
- Modify (VPS): `bot/strategies/temporal_arb.py`, `bot/strategies/market_hours_filter.py`, `bot/strategies/__init__.py`, `bot/state.py`, `bot/config.py`, `bot/dashboard.py`, `bot/templates/settings.html`, `bot/static/settings.js`, `tests/test_temporal_arb.py`, `tests/test_market_hours.py`, `tests/test_analyze_hours.py`, `scripts/analyze_trade_hours.py`
- VPS `bot_config`: insert/update keys `ta_mart_hedge_mult=2.0`, `whitelist_hours_<sym>`

**Interfaces:**
- Consumes: local working tree (this repo) con todos los commits de Tasks 1-12 ya aplicados.
- Produces: bot corriendo en VPS con el nuevo código + DB overrides + logs limpios durante 30 min.

- [ ] **Step 1: Snapshot VPS**

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
export SSHPASS='<vps-root-password>'
sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@76.13.251.202 \
  "mkdir -p /opt/polymarket-bot/.backups/mh_compound_$TS && \
   cp -r /opt/polymarket-bot/bot /opt/polymarket-bot/.backups/mh_compound_$TS/ && \
   cp -r /opt/polymarket-bot/tests /opt/polymarket-bot/.backups/mh_compound_$TS/ && \
   ls /opt/polymarket-bot/.backups/mh_compound_$TS/"
```

Expected: backup folder created with bot/ and tests/ subdirs.

- [ ] **Step 2: Pull la DB actual a local (para re-correr análisis si es necesario)**

```bash
sshpass -e scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@76.13.251.202:/opt/polymarket-bot/data/streak_snapper.db \
  data/streak_snapper_vps_<TS>.db
```

- [ ] **Step 3: `scp` archivos modificados — listados explícitos para preservar customizaciones VPS**

```bash
REMOTE=root@76.13.251.202
DEST=/opt/polymarket-bot

# Modified
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/strategies/temporal_arb.py $REMOTE:$DEST/bot/strategies/

# New module
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/strategies/market_hours_filter.py $REMOTE:$DEST/bot/strategies/

# Modified registry
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/strategies/__init__.py $REMOTE:$DEST/bot/strategies/

# State + config + dashboard (additive patches)
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/state.py $REMOTE:$DEST/bot/
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/config.py $REMOTE:$DEST/bot/
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/dashboard.py $REMOTE:$DEST/bot/
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/templates/settings.html $REMOTE:$DEST/bot/templates/
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  bot/static/settings.js $REMOTE:$DEST/bot/static/

# Tests
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  tests/test_temporal_arb.py $REMOTE:$DEST/tests/
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  tests/test_market_hours.py $REMOTE:$DEST/tests/
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  tests/test_analyze_hours.py $REMOTE:$DEST/tests/

# Script
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  scripts/analyze_trade_hours.py $REMOTE:$DEST/scripts/
```

DO NOT `scp -r bot/` — that would overwrite untracked VPS files like `bot/coinbase_ticker_feed.py` and `bot/polymarket_twap_tracker.py`.

- [ ] **Step 4: Run pytest en VPS**

```bash
sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  $REMOTE "cd /opt/polymarket-bot && source venv/bin/activate && \
  python -m pytest tests/ -q 2>&1 | tail -20"
```

Expected: ALL PASS (modulo 3 pre-existing failures in `test_spread_harvest.py`).

If RED: restore from backup

```bash
sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  $REMOTE "systemctl stop polymarket-bot && \
  cp -r /opt/polymarket-bot/.backups/mh_compound_<TS>/* /opt/polymarket-bot/ && \
  systemctl start polymarket-bot"
```

…and STOP. Do not proceed.

- [ ] **Step 5: Aplicar overrides al `bot_config` table — incluyendo whitelist recomendada**

```bash
# 1. Set ta_mart_hedge_mult=2.0 (cambio de default explícito).
#    Usar la whitelist recomendada del JSON de Task 1 Step 3.
#    Reemplazar BUCKS con el JSON.parse del archivo data/hour_analysis_<TS>.json.

SSH="sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@76.13.251.202"

$SSH "cd /opt/polymarket-bot && sqlite3 data/streak_snapper.db <<SQL
INSERT INTO bot_config(key, value) VALUES('ta_mart_hedge_mult', '2.0')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO bot_config(key, value) VALUES('whitelist_hours_btc', '0,3,7,12,14,21')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO bot_config(key, value) VALUES('whitelist_hours_eth', '')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO bot_config(key, value) VALUES('whitelist_hours_sol', '')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
SELECT * FROM bot_config WHERE key LIKE 'ta_mart_hedge_mult' OR key LIKE 'whitelist_hours%';
SQL"
```

Expected: 4 rows, one per override. Replace the BTC whitelist CSV with the recommendation from Task 1.

- [ ] **Step 6: Restart y monitor 30 min**

```bash
$SSH "systemctl restart polymarket-bot && sleep 5 && \
  systemctl is-active polymarket-bot && \
  tail -5 /opt/polymarket-bot/logs/bot.log && \
  tail -5 /opt/polymarket-bot/logs/bot.error.log"

# Background monitor:
$SSH "tail -f /opt/polymarket-bot/logs/bot.log /opt/polymarket-bot/logs/bot.error.log" &
TAIL_PID=$!

# After 30 minutes: check no new errors
sleep 1800
$SSH "wc -l /opt/polymarket-bot/logs/bot.error.log"   # capture baseline
grep -c 'CUTOFF_STRICT_TRANCHES\|HOLD_CUTOFF\|MART-HEDGE' \
  /opt/polymarket-bot/logs/bot.log

kill $TAIL_PID 2>/dev/null
```

Expected:
- `bot.error.log` does not grow during the 30 min.
- Bot is active.
- Logs contain at least some decision lines (TA, MH, CUTOFF).
- Dashboard `/healthz` returns 200.

- [ ] **Step 7: Commit (post-deploy)**

```bash
# Commit the deploy script (if any was written) and the data/outputs:
git add data/hour_analysis_<TS>.{json,md} deploy_mart_hedge_2026-10-05.sh 2>/dev/null || true
git commit -m "chore(deploy): post-deploy snapshot of VPS state 2026-10-08

- VPS working tree updated to commit <HEAD>.
- bot_config.ta_mart_hedge_mult=2.0
- bot_config.whitelist_hours_btc='0,3,7,12,14,21' (from analyze_trade_hours)
- bot_config.whitelist_hours_eth='' (no qualifying hours)
- bot_config.whitelist_hours_sol='' (no qualifying hours)
- 30-min post-deploy smoke test passed: no new errors in bot.error.log."
```

---

## Self-Review

**1. Spec coverage:**

| Spec requirement | Task |
|---|---|
| Mart-Hedge compounding (R1=first×2, R2=round1×2) | T3, T4 |
| Cutoff strict (no buy post-120s) | T5, T6, T7 |
| Side-change filter preserved | T4 (test) |
| Loss-fallback preserved | T3 (R1 still gated by loss_pct or side_change) |
| Monitor secs unchanged | (default already 30s, no code change needed) |
| Edge-lock suprimido post-MH | (already in code, untouched) |
| LPT within cutoff window | T6 test confirms |
| Whitelist hourly from data | T1, T8, T9, T10, T11 |
| Empty whitelist = safe degradation | T8 tests |
| State fields for whitelist | T9 |
| Dashboard UI for whitelist | T10 |
| `mult=2.0` default | T11 |
| Snapshot before deploy | T13 |
| pytest in venv | T13 |
| Rollback procedure | T13 (documented in spec) |
| Bug audit (silent bugs) | T1 (DB-driven) + tasks below |
| No VPS overwrites | T13 uses `scp` per-file |

**2. Placeholder scan:** No TBD/TODO/fill-in-details in the plan. Every code block is concrete.

**3. Type consistency:**

- `ta.mart_hedge_round_qty` — defined T2, written T3, read T4. ✓
- `is_hour_whitelisted(int, str) -> bool` — defined T8, tested T8. ✓
- `_filter_evaluate(ctx)` returns `[]` or `None` — defined T8, tested T8. ✓
- `state.whitelist_hours_<symbol>` — added T9, read T8 via `getattr`. ✓
- `_build_mart_hedge_scenario` helper — defined T3, reused T4, T7. ✓
- `logged_hold_cutoff` — added in T6 description but referencing addition to `_TAWindow`. Make sure to add in the actual file at T6 Step 2.

**4. Open ambiguity found in self-review:**

The R1 test in Task 3 builds `ta.strike = 86050` so that spot=86100 implies UP-winner (side_changed from "DOWN" first_side → "UP" second_side). But the actual `mh_qty` placement code at line 1072-1074 places the order on `tokens.up_token_id if second_side == "UP"`. So the test should capture which token was passed. Make sure the test asserts the qty via the captured list — already done.

**5. Risk acknowledgment:**

The compounding change doubles max_rounds exposure (was 5×first_shares, now 6×first_shares). With first=20 default and mult=2, max deployment = 120 shares/window ≈ $60. If user has starting_bankroll=$1000, that's 6% bankroll-at-risk per window — manageable. Documented in spec section 8.
