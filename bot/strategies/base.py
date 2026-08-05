"""What a strategy has to declare to be part of the bot.

Before this existed, adding a parameter meant editing five files (`config.py`,
`state.py`, `dashboard.py`, `settings.html`, `settings.js`) and remembering to
add the strategy's id to a hand-written tuple in `_aggregate_db_stats`. That
does not survive six strategies across three assets, which is what
`docs/PLAN.md` Fase B asks for.

A descriptor is data, not a base class to inherit from: strategies differ too
much in *how* they produce signals (a candle read, a resting quote, a
liquidation tape) for a common superclass to say anything useful. What they do
share is the paperwork — an id, parameters, which assets they can trade, and a
way to ask "any signal for this window?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..runtime_field import RuntimeField


@dataclass
class StrategyContext:
    """Everything a strategy is allowed to look at for one window.

    `streak` is the `StreakSnapperStrategy` instance, which the two migrated
    forms still own their signal logic in. Fase B strategies won't need it —
    the field is here so migrating them doesn't change this signature.
    """

    state: Any                  # BotState for this symbol
    symbol: str
    tokens: Any = None          # MarketTokens for the current window
    streak: Any = None          # StreakSnapperStrategy, for ss_fade / ss_trend


@dataclass(frozen=True)
class StrategyDescriptor:
    id: str                                  # matches `trades.strategy`
    name: str                                # shown in /settings and the dashboard
    description: str                         # one line, Spanish, shown in the UI
    evaluate: Callable[[StrategyContext], list]
    is_enabled: Callable[[Any], bool]        # takes BotState
    params: tuple[RuntimeField, ...] = ()
    # Declarative mirror of `is_enabled`, for the settings page: it has to grey
    # out a card the moment the user changes the toggle, before saving, and it
    # can't evaluate a Python lambda. Shape: {"field": name, "values": [...]}.
    #
    # Two sources of truth for one fact is a smell, so `test_strategies.py`
    # asserts they agree for every declared value of the field.
    enabled_when: dict | None = None
    # Which assets this strategy can trade. Empty = every supported symbol.
    # `corridor` will need this: it requires a 15-minute market to exist too.
    symbols: tuple[str, ...] = ()
    # Who wins when two strategies point at opposite sides of the same window.
    # Higher wins. Buying both sides is a guaranteed wash (the pair costs
    # exactly what it pays), so somebody has to be dropped.
    priority: int = 0
    notes: str = ""                          # longer UI copy, optional

    def supports(self, symbol: str) -> bool:
        return not self.symbols or symbol in self.symbols

    def enabled_for(self, state: Any) -> bool:
        try:
            return bool(self.is_enabled(state))
        except Exception:
            # A descriptor that can't answer is off. A strategy silently
            # trading because its toggle raised would be the worse failure.
            return False

    def to_json(self, state: Any = None) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "notes": self.notes,
            "symbols": list(self.symbols),
            "priority": self.priority,
            "enabled": self.enabled_for(state) if state is not None else None,
            "enabled_when": self.enabled_when,
            "params": [p.to_json() for p in self.params],
        }
