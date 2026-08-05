"""Forma 1 — Fade (anti-racha).

The signal logic still lives in `bot/strategy_streak.py`; this module is the
wrapper that makes it a registry member. Fase 8 measured this as the only form
with a positive expectation (+3.74%/trade, 53.8% over n=1150), which is why it
holds the higher `priority` and wins the direction tie-break against Trend.
"""

from __future__ import annotations

from ..runtime_field import RuntimeField
from .base import StrategyContext, StrategyDescriptor


def _evaluate(ctx: StrategyContext) -> list:
    if ctx.streak is None:
        return []
    signal = ctx.streak.get_fade_signal()
    return [signal] if signal else []


DESCRIPTOR = StrategyDescriptor(
    id="ss_fade",
    name="Forma 1 — Fade (anti-racha)",
    description=(
        "Tras N ventanas seguidas en la misma dirección, compra el lado "
        "contrario y aguanta hasta la resolución."
    ),
    notes=(
        "La única de las dos con expectativa positiva medida: +3,74% por "
        "operación (53,8% sobre 1.150 señales, docs/RUTA.md Fase 8). Aun así "
        "t=+1,32, por debajo de significancia."
    ),
    evaluate=_evaluate,
    # `ss_mode` stays the switch for the two original forms: it is persisted in
    # bot_config, documented in .env.example and covered by tests. Fase B
    # strategies bring their own `ss_<id>_enabled` boolean instead.
    is_enabled=lambda state: getattr(state, "ss_mode", "fade") in ("fade", "both"),
    priority=100,
    params=(
        RuntimeField(
            "ss_fade_base_shares", "float", minimum=1, maximum=100_000,
            label="Shares base", step=0.5,
            hint="Tamaño de la entrada antes de aplicar el modo de sizing",
        ),
        RuntimeField(
            "ss_fade_limit_cap", "float", minimum=0.10, maximum=0.99,
            label="Precio máximo", step=0.01,
            hint="0,52 — la señal vale 0,538, pagar más regala la diferencia",
        ),
        RuntimeField(
            "ss_fade_streak_min", "int", minimum=2, maximum=20,
            label="Racha mínima", step=1,
            hint="Ventanas seguidas en la misma dirección antes de operar",
        ),
    ),
)
