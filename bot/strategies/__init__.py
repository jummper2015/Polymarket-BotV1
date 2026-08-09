"""The strategy registry.

Import order matters exactly once: `bot.config` builds `RUNTIME_FIELDS` from
`params()` below, so nothing in this package may import `bot.config`. Field
declarations come from `bot.runtime_field`, which imports nothing of ours.

Adding a strategy is one file plus one line in `_REGISTERED`. Its parameters
then get validation, `POST /config` handling, persistence in `bot_config`,
`/settings` rendering and per-strategy KPIs without touching anything else.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..runtime_field import RuntimeField
from .base import StrategyContext, StrategyDescriptor
from . import box_builder, coin_flip_dog, temporal_arb, near_res
# ss_fade y ss_trend desactivadas:
#   - ss_fade:  medida +3.74%/op (Fase 8) pero se descontinúa en esta fase.
#   - ss_trend: medida −4.22%/op (Fase 8, t=−2.61). Sin edge.
#   - spread_harvest: solo observación, nunca operó.
# Los módulos se conservan en bot/strategies/ como referencia histórica.

# Declaration order is the display order. Execution order is by descending
# priority, so the tie-break doesn't depend on how this list is sorted.
_REGISTERED: tuple[StrategyDescriptor, ...] = (
    box_builder.DESCRIPTOR,
    coin_flip_dog.DESCRIPTOR,
    temporal_arb.DESCRIPTOR,
    near_res.DESCRIPTOR,
)

REGISTRY: dict[str, StrategyDescriptor] = {d.id: d for d in _REGISTERED}

if len(REGISTRY) != len(_REGISTERED):
    raise RuntimeError("dos estrategias comparten id — los KPIs se mezclarían")


def all_descriptors() -> tuple[StrategyDescriptor, ...]:
    return _REGISTERED


def ids() -> tuple[str, ...]:
    """Every registered id, in display order. `trades.strategy` uses these."""
    return tuple(d.id for d in _REGISTERED)


def get(strategy_id: str) -> StrategyDescriptor | None:
    return REGISTRY.get(strategy_id)


def params() -> tuple[RuntimeField, ...]:
    """Every strategy parameter, for `config.RUNTIME_FIELDS`."""
    return tuple(p for d in _REGISTERED for p in d.params)


def enabled_for(state: Any, symbol: str | None = None) -> list[StrategyDescriptor]:
    """Strategies that are switched on and can trade `symbol`.

    Highest priority first, so a caller that resolves conflicts by taking the
    first entry gets the same answer as one that compares priorities.
    """
    out = [
        d for d in _REGISTERED
        if d.enabled_for(state) and (symbol is None or d.supports(symbol))
    ]
    return sorted(out, key=lambda d: -d.priority)


def resolve_conflicts(signals: Iterable) -> tuple[list, list]:
    """Split signals into (kept, dropped) when they disagree on direction.

    Two strategies buying opposite sides of the same window is a guaranteed
    wash — the pair costs exactly what it pays out — and it also feeds one fake
    win and one fake loss into the per-strategy stats. So the highest-priority
    strategy present wins and everything pointing elsewhere is dropped.

    Signals from the *same* strategy are never split against each other, and
    agreeing strategies all survive: only a genuine disagreement costs anyone
    their entry.
    """
    signals = list(signals)
    if len({s.direction for s in signals}) <= 1:
        return signals, []

    def rank(sig) -> int:
        desc = REGISTRY.get(sig.strategy)
        return desc.priority if desc else -1

    winner = max(signals, key=rank)
    kept = [s for s in signals if s.direction == winner.direction]
    dropped = [s for s in signals if s.direction != winner.direction]
    return kept, dropped


def to_json(state: Any = None) -> list[dict]:
    """The registry as the dashboard sees it (see `/state`)."""
    return [d.to_json(state) for d in _REGISTERED]


__all__ = [
    "REGISTRY",
    "StrategyContext",
    "StrategyDescriptor",
    "all_descriptors",
    "enabled_for",
    "get",
    "ids",
    "params",
    "resolve_conflicts",
    "to_json",
]
