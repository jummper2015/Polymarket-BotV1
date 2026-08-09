"""Fase B — Spread-Harvest Maker, **en modo solo-observación**.

The thesis (`Revisar Estrategias/spread_harvest_maker/`): when the 5-minute book
goes wide — both asks summing to $1.10 or more — and the window is a verified
coin flip, a resting bid on the underdog side at 0.40–0.48 buys a ~50/50 shot at
a discount. It never crosses the spread; it collects it.

**It does not trade here, and that is the point of this stage.**

Two reasons, and neither is "not finished yet":

1. A resting bid cannot be simulated in paper. Whether someone sells into it is
   the entire question, and inventing a fill model would be exactly the defect
   `docs/RUTA.md` Fase A.1 removed — paper booking fills that would never have
   happened.
2. The opportunity rate is unmeasured. The strategy's own RESEARCH.md rests on
   17 wide-book windows from a single week of June and says so: "wide books may
   be a thin-week artifact". Building a maker order lifecycle before knowing
   whether the gate opens twice a day or twice a month is speculative.

So this stage measures. Every few seconds it evaluates both gates and records
what it saw; `/state` then answers, from live data, whether the gate opens often
enough to justify the execution work.

The gates (unchanged from the reference):
  · coin flip  — coa = |mark − strike| / ATR4 ≤ 0.40
  · wide book  — best_ask_up + best_ask_down ≥ 1.10
  · the quote would be the underdog side's best bid + $0.01, banded 0.40–0.48
"""

from __future__ import annotations

import threading
from typing import Optional

from ..runtime_field import RuntimeField
from .base import StrategyContext, StrategyDescriptor

# `bot.logger`, `bot.binance_api` and `bot.polymarket_price` are imported inside
# the functions that use them, not here. `bot.state` imports this package to
# build its strategy ids, and `bot.logger` imports `bot.state` — so a module
# level import of any of them closes the loop and nothing starts. The same
# reason `ss_fade` declares only `runtime_field` and `base`.

# Thresholds are the reference's, kept as constants rather than runtime fields:
# they are the definition of the experiment, and a knob that moves them mid-run
# would make the sample it produces uninterpretable.
COA_MAX = 0.40          # verified coin flip
ASK_SUM_MIN = 1.10      # the market makers have stepped away
QUOTE_FLOOR = 0.40      # cheap fills were toxic: 32–35% win over 184 of them
QUOTE_CAP = 0.48        # the mid-price shelf ceiling
TIME_BAND = (30.0, 180.0)   # quotable only with 30–180 s left (T-120 → T-30)


class _WindowObs:
    """What one window looked like, accumulated across its ticks."""

    __slots__ = ("window_ts", "ticks", "min_coa", "max_ask_sum",
                 "quotable_ticks", "first_quotable_left", "quote_price", "no_data")

    def __init__(self, window_ts: int) -> None:
        self.window_ts = window_ts
        self.ticks = 0
        self.min_coa: Optional[float] = None
        self.max_ask_sum: Optional[float] = None
        self.quotable_ticks = 0
        self.first_quotable_left: Optional[float] = None
        self.quote_price: Optional[float] = None
        self.no_data = 0


# One live window per symbol. The trader runs one thread per symbol, so the lock
# only guards against the dashboard reading mid-update.
_WINDOWS: dict[str, _WindowObs] = {}
_lock = threading.Lock()


def quote_price_for(dog_bid: Optional[float], dog_ask: Optional[float]) -> Optional[float]:
    """Where the resting bid would go, or None if there is no room for one.

    Best bid plus a tick, banded to the 0.40–0.48 shelf and kept strictly below
    the ask — the quote exists to be the bid, never to cross it. Returns None
    when the band and the book leave no price that satisfies both.
    """
    if dog_bid is None or dog_bid <= 0:
        return None
    price = round(dog_bid + 0.01, 4)
    price = max(QUOTE_FLOOR, min(QUOTE_CAP, price))
    if dog_ask is not None and dog_ask > 0 and price >= dog_ask:
        return None
    return price


def evaluate_gates(
    mark: Optional[float],
    strike: Optional[float],
    atr4: Optional[float],
    ask_up: Optional[float],
    ask_down: Optional[float],
    seconds_left: float,
) -> dict:
    """Both gates plus the quote, as one pure verdict. No network, no state."""
    verdict: dict = {
        "coa": None, "ask_sum": None, "quote": None,
        "coin_flip": False, "wide_book": False, "in_band": False,
        "quotable": False, "reason": "",
    }

    if mark is None or strike is None or atr4 is None or atr4 <= 0:
        verdict["reason"] = "SH_NO_DATA"
        return verdict

    coa = abs(mark - strike) / atr4
    verdict["coa"] = round(coa, 4)
    verdict["coin_flip"] = coa <= COA_MAX

    if ask_up is None or ask_down is None or ask_up <= 0 or ask_down <= 0:
        verdict["reason"] = "SH_NO_BOOK"
        return verdict

    ask_sum = ask_up + ask_down
    verdict["ask_sum"] = round(ask_sum, 4)
    verdict["wide_book"] = ask_sum >= ASK_SUM_MIN

    low, high = TIME_BAND
    verdict["in_band"] = low <= seconds_left <= high

    if not verdict["coin_flip"]:
        verdict["reason"] = "SH_SKIP_COA"
    elif not verdict["wide_book"]:
        verdict["reason"] = "SH_SKIP_SPREAD"
    elif not verdict["in_band"]:
        verdict["reason"] = "SH_OUT_OF_BAND"

    # The underdog is the cheaper side to buy, i.e. the one with the lower ask.
    if ask_up <= ask_down:
        dog_bid_key, dog_ask = "UP", ask_up
    else:
        dog_bid_key, dog_ask = "DOWN", ask_down
    verdict["dog"] = dog_bid_key
    verdict["dog_ask"] = dog_ask

    verdict["quotable"] = (
        verdict["coin_flip"] and verdict["wide_book"] and verdict["in_band"]
    )
    return verdict


def _flush(obs: "_WindowObs", state) -> None:
    """Publish one finished window's counters. Called outside the lock."""
    from .. import logger

    if obs.ticks == 0:
        return

    state.record_observation("SH_WINDOWS")

    if obs.quotable_ticks:
        state.record_observation("SH_QUOTABLE")
        quote = f"${obs.quote_price:.2f}" if obs.quote_price else "sin hueco"
        logger.info(
            f"[SH] ventana {obs.window_ts} COTIZABLE — "
            f"coa={obs.min_coa:.3f} ask_sum={obs.max_ask_sum:.3f} "
            f"puja≈{quote} ({obs.quotable_ticks} ticks)",
            icon="📖",
        )
    elif obs.no_data >= obs.ticks:
        state.record_observation("SH_NO_DATA")
    elif obs.min_coa is not None and obs.min_coa > COA_MAX:
        state.record_observation("SH_SKIP_COA")
    else:
        state.record_observation("SH_SKIP_SPREAD")


def _observe(ctx: StrategyContext) -> None:
    """One tick. Reads the book from memory; the strike and ATR from the network."""
    from ..binance_api import get_atr4
    from ..polymarket_price import get_strike_and_mark

    tokens = ctx.tokens
    if tokens is None:
        return
    window_ts = int(getattr(tokens, "window_ts", 0) or 0)

    with _lock:
        current = _WINDOWS.get(ctx.symbol)
        finished = None
        if current is None or current.window_ts != window_ts:
            finished = current
            current = _WINDOWS[ctx.symbol] = _WindowObs(window_ts)

    # Outside the lock: publishing takes the state's own lock and logs.
    if finished is not None:
        _flush(finished, ctx.state)

    strike, mark = get_strike_and_mark(window_ts, ctx.symbol)
    atr4 = get_atr4(ctx.symbol)
    ask_up, ask_down = ctx.state.get_asks()

    verdict = evaluate_gates(mark, strike, atr4, ask_up, ask_down, ctx.seconds_left)

    with _lock:
        current.ticks += 1
        if verdict["reason"] == "SH_NO_DATA":
            current.no_data += 1
            return
        if verdict["coa"] is not None:
            current.min_coa = (
                verdict["coa"] if current.min_coa is None
                else min(current.min_coa, verdict["coa"])
            )
        if verdict["ask_sum"] is not None:
            current.max_ask_sum = (
                verdict["ask_sum"] if current.max_ask_sum is None
                else max(current.max_ask_sum, verdict["ask_sum"])
            )
        if verdict["quotable"]:
            current.quotable_ticks += 1
            if current.first_quotable_left is None:
                current.first_quotable_left = ctx.seconds_left
            bid = (ctx.state.last_up_bid if verdict.get("dog") == "UP"
                   else ctx.state.last_down_bid)
            current.quote_price = quote_price_for(bid, verdict.get("dog_ask"))


DESCRIPTOR = StrategyDescriptor(
    id="spread_harvest",
    name="Spread-Harvest Maker (solo observación)",
    description=(
        "Mide cuántas ventanas serían cotizables: moneda al aire verificada "
        "(coa ≤ 0,40) y libro ancho (suma de asks ≥ 1,10). No opera."
    ),
    notes=(
        "No coloca órdenes a propósito. Una puja en reposo no se puede simular "
        "en paper —si alguien vende contra ella es justo la pregunta abierta— y "
        "su propio RESEARCH.md admite que las 17 ventanas de libro ancho salen "
        "de una sola semana de junio. Esta etapa mide si la puerta se abre; la "
        "ejecución sólo se construye si los números la justifican."
    ),
    evaluate=lambda ctx: [],     # never trades in this stage
    observe=_observe,
    is_enabled=lambda state: bool(getattr(state, "sh_observe_enabled", False)),
    enabled_when={"field": "sh_observe_enabled", "values": [True]},
    priority=0,                  # produces no signals, so it never wins a tie-break
    params=(
        RuntimeField(
            "sh_observe_enabled", "bool",
            label="Medir oportunidad",
            hint="Cuenta ventanas cotizables sin operar. No coloca órdenes.",
        ),
    ),
)
