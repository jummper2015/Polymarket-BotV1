"""Forma 2 — Trend (tendencia 4h). Apagada por defecto.

Wrapper only; the signal and the 4h cycle live in `bot/strategy_streak.py`.

Kept in the registry rather than deleted because the cycle machinery is real
and reusable, and because a strategy measured at −4.22%/trade is worth keeping
visible in the dashboard as the control group. `SS_MODE` defaults to `fade`, so
`is_enabled` returns False unless someone opts in.
"""

from __future__ import annotations

from ..runtime_field import RuntimeField
from .base import StrategyContext, StrategyDescriptor


def _evaluate(ctx: StrategyContext) -> list:
    if ctx.streak is None:
        return []
    signal = ctx.streak.get_trend_signal()
    return [signal] if signal else []


DESCRIPTOR = StrategyDescriptor(
    id="ss_trend",
    name="Forma 2 — Trend (tendencia 4h)",
    description=(
        "Fija un lado con la vela de 4h ya cerrada y lo opera durante las 4h "
        "siguientes; si el bloque acaba sin recuperar, el ciclo se prorroga."
    ),
    notes=(
        "Medida en −4,22% por operación (48,2% sobre 1.725 señales, "
        "docs/RUTA.md Fase 8): apuesta a continuación y el efecto real es la "
        "reversión. Apagada por defecto; pierde el desempate contra Fade."
    ),
    evaluate=_evaluate,
    is_enabled=lambda state: getattr(state, "ss_mode", "fade") in ("trend", "both"),
    priority=50,
    params=(
        RuntimeField(
            "ss_trend_base_shares", "float", minimum=1, maximum=100_000,
            label="Shares base", step=0.5,
            hint="Tamaño de la entrada antes de aplicar el modo de sizing",
        ),
        RuntimeField(
            "ss_trend_limit_cap", "float", minimum=0.10, maximum=0.99,
            label="Precio máximo", step=0.01,
            hint="El cap fija el suelo del factor martingala: 1/(1−cap)",
        ),
        RuntimeField(
            "ss_trend_min_strength", "float", minimum=0.0, maximum=0.10,
            label="Tendencia mínima (%)", step=0.1, scale=100.0,
            hint="Movimiento de la vela 4h cerrada para operar su lado",
        ),
    ),
)
