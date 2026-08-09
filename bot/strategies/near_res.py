"""Near-Resolution Capture strategy — Fase B.

Enters in the final seconds of a window when one side is nearly certain to
win (ask ≥ nrc_min_ask) and collects 1–3¢ on settlement to $1.00.

⚠  RISK WARNING ⚠
Win rate is very high but the loss distribution is extremely asymmetric:
  - Normal outcome:  collect +1–3¢ per trade
  - Tail outcome:    BTC crosses back at the last second → lose ~97¢ per share

Risk controls built in:
  - nrc_shares hard cap (default 5) limits loss per event to ≈ $5 × 0.97
  - Time gate: only T-5..T-20s — far enough from expiry to cancel if needed
  - Book premium gate: reject if up + down asks > nrc_max_book_sum (avoid $1+ pairs)
  - One entry per window (enforced by `late_fired` in _wait_out_window)

Signal path: evaluate_late → _execute_signal (same as Coin-Flip Dog).
"""

from __future__ import annotations

from typing import Optional

from .base import StrategyContext, StrategyDescriptor
from ..runtime_field import RuntimeField

# ── defaults ──────────────────────────────────────────────────────────────────
MIN_ASK_DEFAULT        = 0.970    # frontrunner ask must be ≥ this
MAX_ASK_DEFAULT        = 0.995    # but < $1 (nothing to capture at $1.00)
MIN_ENTRY_LEFT_DEFAULT = 5.0      # don't enter if < 5 s remaining
MAX_ENTRY_LEFT_DEFAULT = 20.0     # entry window opens at T-20s
SHARES_DEFAULT         = 5.0      # hard cap — the main tail-risk limiter
MAX_BOOK_SUM_DEFAULT   = 1.01     # reject if ask_up + ask_dn > this (premium book)


# ── pure helpers ──────────────────────────────────────────────────────────────

def find_frontrunner(
    ask_up: Optional[float],
    ask_dn: Optional[float],
    min_ask: float,
    max_ask: float,
) -> tuple[Optional[str], Optional[float]]:
    """Return (direction, ask) of the side that's nearly certain to win.

    The frontrunner is the side whose ask is in [min_ask, max_ask].
    If both qualify simultaneously (pathological data), returns (None, None)
    to force a skip rather than picking randomly.
    """
    up_ok = ask_up is not None and min_ask <= ask_up <= max_ask
    dn_ok = ask_dn is not None and min_ask <= ask_dn <= max_ask

    if up_ok and dn_ok:
        # Both sides near $1 — stale data or market error; skip safely
        return None, None
    if up_ok:
        return "UP", ask_up
    if dn_ok:
        return "DOWN", ask_dn
    return None, None


def check_nrc_gates(
    ask_up: Optional[float],
    ask_dn: Optional[float],
    seconds_left: float,
    min_ask: float,
    max_ask: float,
    min_entry_left: float,
    max_entry_left: float,
    max_book_sum: float,
) -> tuple[bool, str, Optional[str], Optional[float]]:
    """Run all entry gates.

    Returns (passes, reason, direction, frontrunner_ask).
    On failure: (False, reason_code, None, None).
    """
    # Gate 1: time window
    if not (min_entry_left <= seconds_left <= max_entry_left):
        return False, "NRC_OUT_OF_TIME", None, None

    # Gate 2: find one clearly-winning side
    direction, fr_ask = find_frontrunner(ask_up, ask_dn, min_ask, max_ask)
    if direction is None:
        return False, "NRC_NO_FRONTRUNNER", None, None

    # Gate 3: book premium check — ensure we're not paying > $1 for the pair
    loser_ask = ask_dn if direction == "UP" else ask_up
    if loser_ask is not None and fr_ask is not None:
        if fr_ask + loser_ask > max_book_sum:
            return False, "NRC_PREMIUM_BOOK", None, None

    return True, "NRC_OK", direction, fr_ask


# ── evaluate_late ─────────────────────────────────────────────────────────────

def _evaluate_late(ctx: StrategyContext) -> list:
    """Near-Resolution Capture: enter when a side is nearly certain to win."""
    from ..strategy_streak import StreakSignal
    from .. import logger

    state  = ctx.state
    tokens = ctx.tokens

    if tokens is None:
        return []

    # Read config — getattr with defaults means a missing attr never crashes
    min_ask      = float(getattr(state, "nrc_min_ask",        MIN_ASK_DEFAULT))
    max_ask      = float(getattr(state, "nrc_max_ask",        MAX_ASK_DEFAULT))
    min_left     = float(getattr(state, "nrc_min_entry_left", MIN_ENTRY_LEFT_DEFAULT))
    max_left     = float(getattr(state, "nrc_max_entry_left", MAX_ENTRY_LEFT_DEFAULT))
    shares       = float(getattr(state, "nrc_shares",         SHARES_DEFAULT))
    max_book_sum = float(getattr(state, "nrc_max_book_sum",   MAX_BOOK_SUM_DEFAULT))

    ask_up, ask_dn = state.get_asks()

    passes, reason, direction, fr_ask = check_nrc_gates(
        ask_up, ask_dn,
        ctx.seconds_left,
        min_ask, max_ask,
        min_left, max_left,
        max_book_sum,
    )

    if not passes:
        # NRC_OUT_OF_TIME fires on every tick for ~4 minutes — don't log it.
        # Other skips are rare and meaningful.
        if reason != "NRC_OUT_OF_TIME":
            state.record_skip(reason)
            logger.info(
                f"[NRC] SKIP {reason}"
                f"  up={ask_up}  dn={ask_dn}  left={ctx.seconds_left:.1f}s",
                icon="⏭",
            )
        return []

    cap = round(min(max_ask, fr_ask), 4)

    logger.ok(
        f"[NRC] 🏁 señal  {direction} @ {cap:.4f}"
        f"  ×{shares:.0f} shares  left={ctx.seconds_left:.1f}s",
        icon="🏁",
    )

    return [StreakSignal(
        strategy="near_res",
        direction=direction,
        limit_cap=cap,
        shares=shares,
        multiplier=1.0,
        loss_streak=0,
        signal_reason=f"NRC ask={fr_ask:.3f} left={ctx.seconds_left:.1f}s",
    )]


# ── descriptor ────────────────────────────────────────────────────────────────

DESCRIPTOR = StrategyDescriptor(
    id="near_res",
    name="Near-Res Capture",
    description=(
        "Compra el lado casi-ganador a T-5..T-20s y cobra 1–3¢ al resolver a $1. "
        "Win rate muy alto, riesgo de cola asimétrico."
    ),
    notes=(
        "⚠ RIESGO DE COLA: una reversión de último segundo puede perder ≈ $1/share. "
        "Mantener nrc_shares bajo (default 5). "
        "Requiere feed de resolución con latencia < 5s para ser seguro."
    ),
    evaluate=lambda ctx: [],
    evaluate_late=_evaluate_late,
    is_enabled=lambda state: bool(getattr(state, "nrc_enabled", False)),
    enabled_when={"field": "nrc_enabled", "values": [True]},
    priority=60,
    params=(
        RuntimeField("nrc_enabled", "bool", label="Near-Res Capture activo"),
        RuntimeField(
            "nrc_min_ask", "float",
            label="Ask mínimo frontrunner",
            minimum=0.95, maximum=0.99, step=0.005,
            hint="El lado ganador debe valer ≥ este precio (default 0.970)",
        ),
        RuntimeField(
            "nrc_max_ask", "float",
            label="Ask máximo frontrunner",
            minimum=0.96, maximum=0.999, step=0.001,
            hint="No comprar si ask ≥ este (ya no queda margen, default 0.995)",
        ),
        RuntimeField(
            "nrc_min_entry_left", "float",
            label="Mín segundos restantes",
            minimum=2.0, maximum=15.0, step=1.0,
            hint="No entrar si quedan < estos segundos (evita cancelaciones tardías)",
        ),
        RuntimeField(
            "nrc_max_entry_left", "float",
            label="Máx segundos restantes",
            minimum=10.0, maximum=45.0, step=1.0,
            hint="Ventana de entrada se abre aquí (default T-20s)",
        ),
        RuntimeField(
            "nrc_shares", "float",
            label="Shares (hard cap riesgo de cola)",
            minimum=5.0, maximum=50.0, step=1.0,
            hint="Limita la pérdida máxima por ventana. No aumentar sin análisis.",
        ),
        RuntimeField(
            "nrc_max_book_sum", "float",
            label="Max suma asks (premium)",
            minimum=1.00, maximum=1.05, step=0.005,
            hint="Rechaza si ask_up + ask_dn > este valor (libro a prima)",
        ),
    ),
)
