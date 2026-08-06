"""Filtered/paginated trade queries and the metric series behind the charts.

Kept out of dashboard.py so the route handlers stay thin and this stays testable
without a Flask request context.

Everything filters and paginates in SQL. The trades table grows by ~288 rows a
day per strategy, so loading it into Python to slice it would get slower every
week the bot runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_

from .db import TradeModel, db


DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 200

# Only these can be sorted on — the value goes into an ORDER BY, so it must come
# from a fixed set rather than straight from the query string.
SORTABLE = {
    "id": TradeModel.id,
    "window_ts": TradeModel.window_ts,
    "pnl": TradeModel.pnl,
    "cost": TradeModel.cost,
    "multiplier": TradeModel.multiplier,
    "opened_at": TradeModel.opened_at,
    "resolved_at": TradeModel.resolved_at,
}


def _parse_ts(value: Optional[str]) -> Optional[int]:
    """Accept either a unix timestamp or an ISO date (YYYY-MM-DD)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def build_query(filters: dict):
    """Apply filters to the trades table. Unknown/blank values are ignored."""
    q = TradeModel.query

    for field, column in (
        ("strategy", TradeModel.strategy),
        ("symbol", TradeModel.symbol),
        ("status", TradeModel.status),
        ("mode", TradeModel.mode),
        ("direction", TradeModel.direction),
        ("resolution_source", TradeModel.resolution_source),
    ):
        value = (filters.get(field) or "").strip()
        if value and value != "all":
            q = q.filter(column == value)

    ts_from = _parse_ts(filters.get("from"))
    if ts_from is not None:
        q = q.filter(TradeModel.window_ts >= ts_from)

    ts_to = _parse_ts(filters.get("to"))
    if ts_to is not None:
        # Inclusive of the whole end day when given as a date.
        q = q.filter(TradeModel.window_ts <= ts_to + 86_399)

    # Free-text box: match the slug or the note, plus the id when it's a number.
    text = (filters.get("q") or "").strip()
    if text:
        like = f"%{text}%"
        clauses = [TradeModel.window_slug.ilike(like), TradeModel.note.ilike(like)]
        if text.isdigit():
            clauses.append(TradeModel.id == int(text))
        q = q.filter(or_(*clauses))

    return q


def paginate_trades(filters: dict, page: int = 1, per_page: int = DEFAULT_PER_PAGE,
                    sort: str = "id", order: str = "desc") -> dict:
    """One page of trades plus the totals the pager needs."""
    per_page = max(1, min(int(per_page or DEFAULT_PER_PAGE), MAX_PER_PAGE))
    page = max(1, int(page or 1))

    q = build_query(filters)
    total = q.count()

    column = SORTABLE.get(sort, TradeModel.id)
    q = q.order_by(column.asc() if order == "asc" else column.desc())

    rows = q.limit(per_page).offset((page - 1) * per_page).all()
    pages = max(1, (total + per_page - 1) // per_page)

    return {
        "items": [t.to_dict() for t in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


def iter_trades_for_export(filters: dict, limit: int = 50_000):
    """Rows for CSV export, oldest first so the file reads chronologically."""
    return (
        build_query(filters)
        .order_by(TradeModel.id.asc())
        .limit(limit)
        .all()
    )


def _drawdown(points: list[dict]) -> tuple[list[dict], float, float]:
    """Running drawdown from the previous peak of an equity curve.

    Returns (series, max_drawdown_abs, max_drawdown_pct). Drawdown is reported
    as a positive magnitude — 0 means "at a new high".

    The percentage is relative to the running peak, so it's undefined until the
    peak is positive; before that only the absolute figure is meaningful.
    """
    series: list[dict] = []
    peak = float("-inf")
    max_abs = 0.0
    max_pct = 0.0

    for point in points:
        equity = point["equity"]
        peak = max(peak, equity)
        dd_abs = peak - equity
        dd_pct = (dd_abs / peak) if peak > 0 else 0.0
        max_abs = max(max_abs, dd_abs)
        max_pct = max(max_pct, dd_pct)
        series.append({
            "t": point["t"],
            "drawdown": round(dd_abs, 4),
            "drawdown_pct": round(dd_pct, 6),
        })

    return series, round(max_abs, 4), round(max_pct, 6)


def metric_series(starting_bankroll: float, limit: int = 5_000,
                  rolling_window: int = 50, symbol: str | None = None) -> dict:
    """Equity, drawdown and win-rate series for the charts.

    Built from resolved trades in resolution order. Open trades are excluded:
    they have no P&L yet, so including them would flatten the curve with points
    that carry no information.

    `symbol` restricts the curve to one market. Without it the equity line mixes
    every market into one series, which is the portfolio view — useful, but not
    what you read to judge whether a strategy works on a given asset.

    No new table needed — TradeModel already stores pnl, cost, strategy, symbol
    and resolved_at.
    """
    query = TradeModel.query.filter(TradeModel.status.in_(("won", "lost")))
    if symbol:
        query = query.filter(TradeModel.symbol == symbol)
    rows = (
        query
        .order_by(TradeModel.resolved_at.asc(), TradeModel.id.asc())
        .limit(limit)
        .all()
    )

    equity_all: list[dict] = []
    per_strategy: dict[str, list[dict]] = {}
    win_rate: list[dict] = []
    recent: list[int] = []

    running = float(starting_bankroll)
    running_by_strategy: dict[str, float] = {}
    wins = 0

    for i, t in enumerate(rows, start=1):
        pnl = float(t.pnl or 0.0)
        running += pnl
        stamp = int(t.resolved_at.timestamp()) if t.resolved_at else t.window_ts

        equity_all.append({"t": stamp, "equity": round(running, 4), "id": t.id})

        bucket = per_strategy.setdefault(t.strategy, [])
        base = running_by_strategy.get(t.strategy, 0.0) + pnl
        running_by_strategy[t.strategy] = base
        bucket.append({"t": stamp, "equity": round(base, 4), "id": t.id})

        if t.won:
            wins += 1
        recent.append(1 if t.won else 0)
        if len(recent) > rolling_window:
            recent.pop(0)

        win_rate.append({
            "t": stamp,
            "cumulative": round(wins / i, 6),
            "rolling": round(sum(recent) / len(recent), 6),
        })

    dd_series, dd_max, dd_max_pct = _drawdown(equity_all)

    strategy_dd = {}
    for name, points in per_strategy.items():
        _, s_abs, _s_pct = _drawdown(points)
        # Absolute only. Per-strategy curves track cumulative P&L from zero, not
        # equity from a bankroll, so a percentage against that running peak is
        # meaningless — an early $2 peak followed by a $5.40 dip reads as 270%.
        strategy_dd[name] = {"max_drawdown": s_abs, "max_drawdown_pct": None}

    return {
        "equity": equity_all,
        "equity_by_strategy": per_strategy,
        "drawdown": dd_series,
        "max_drawdown": dd_max,
        "max_drawdown_pct": dd_max_pct,
        "strategy_drawdown": strategy_dd,
        "win_rate": win_rate,
        "rolling_window": rolling_window,
        "resolved_trades": len(rows),
        "truncated": len(rows) >= limit,
    }
