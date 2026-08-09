"""Fase B — Coin-Flip Dog (CFD).

Entry thesis (from flip_harvester_IDEA.md and live observation of two traders,
docs/RUTA.md):

When BTC has moved enough during a 5-minute window to cheapen one side to
0.22–0.47, and the window is still close to a coin flip
(coa = |cushion| / ATR4 ≤ CFD_MAX_COA), there is a structural discount: the
cheap side (underdog) is more likely to win than its price implies.

Evidence:
- flip_harvester_IDEA.md: n=10,180 (52 weeks), 42.4% win @ avg entry 0.37
  = +14.6% ROI holding to resolution.
- Live traders: zhengying9999 enters at 0.35–0.46 and exits at 0.96;
  hurrican1 enters at 0.155–0.17 and holds to resolution. Both profitable
  over dozens of observed trades. Analysis: docs/RUTA.md.
- Entry band 0.40–0.50 in the Moon Dev "july_17th" fleet: 60.2% win, +30% EV
  over n=1,372 taker fills (mid_price_continuation/RESEARCH.md Exhibit 1).

Signal gates (evaluated every OBSERVE_TICK_SECONDS during the window):
  1. seconds_left in [CFD_ENTRY_MIN_LEFT, CFD_ENTRY_MAX_LEFT] — 30–90 s by
     default. Entering too early means BTC hasn't finished moving yet; too
     late means no time for reversal.
  2. coa = |mark − strike| / ATR4 ≤ CFD_MAX_COA (default 0.20). The cushion
     is small relative to recent volatility → the window is near a coin flip
     regardless of the current leading probability.
  3. Underdog ask in [CFD_MIN_ASK, CFD_MAX_ASK] (default 0.22–0.47). The
     band that is historically +EV; below 0.22 the discount is correctly
     priced (see flip_harvester_IDEA.md Source 4); above 0.47 the edge
     disappears.

One entry per window. Taker GTC. Hold to resolution (no exit engine yet —
see docs/RUTA.md Fase B.1 for the zhengying9999-style 0.96-exit road-map).
"""

from __future__ import annotations

from typing import Optional

from ..runtime_field import RuntimeField
from .base import StrategyContext, StrategyDescriptor

# Imports of binance_api and polymarket_price are inside the function body.
# bot.state imports this package (to collect strategy ids), and bot.logger
# imports bot.state — closing the loop at module level makes nothing start.


# ── thresholds ────────────────────────────────────────────────────────────────

COA_MAX_DEFAULT      = 0.20   # near coin-flip gate
ASK_MIN_DEFAULT      = 0.22   # below this the dog is correctly-priced trash
ASK_MAX_DEFAULT      = 0.47   # above this the edge disappears
ENTRY_MIN_LEFT       = 30.0   # seconds before close: never enter < 30 s
ENTRY_MAX_LEFT       = 90.0   # seconds before close: signal only in last 90 s
MIN_SHARES           = 5.0    # Polymarket dust floor


# ── gates (pure functions for testing) ───────────────────────────────────────

def compute_coa(
    mark: float, strike: float, atr4: float
) -> Optional[float]:
    """Cushion-over-ATR. None when ATR4 is zero or unavailable."""
    if not atr4 or atr4 <= 0:
        return None
    return abs(mark - strike) / atr4


def find_underdog(
    ask_up: Optional[float], ask_down: Optional[float]
) -> Optional[tuple[str, float]]:
    """Return (direction, ask) for the cheaper side, or None if book is absent.

    The underdog is the side the market considers less likely to win, expressed
    as the lower ask. An ask of zero or None is treated as absent — a missing
    quote is not a discount.
    """
    if not ask_up or ask_up <= 0 or not ask_down or ask_down <= 0:
        return None
    if ask_up <= ask_down:
        return ("UP", ask_up)
    return ("DOWN", ask_down)


def check_gates(
    coa: Optional[float],
    dog_ask: Optional[float],
    seconds_left: float,
    max_coa: float,
    min_ask: float,
    max_ask: float,
    entry_min_left: float,
    entry_max_left: float,
) -> tuple[bool, str]:
    """All three gates in one call.  Returns (passes, reason_if_skipped)."""
    if not (entry_min_left <= seconds_left <= entry_max_left):
        return False, "CFD_OUT_OF_TIME"
    if coa is None:
        return False, "CFD_NO_DATA"
    if coa > max_coa:
        return False, "CFD_COA_TOO_HIGH"
    if dog_ask is None:
        return False, "CFD_NO_BOOK"
    if dog_ask < min_ask:
        return False, "CFD_ASK_TOO_LOW"
    if dog_ask > max_ask:
        return False, "CFD_ASK_TOO_HIGH"
    return True, ""


def size_shares(base_bet: float, ask: float) -> float:
    """Flat sizing: approximately `base_bet` dollars at the underdog ask."""
    if ask <= 0:
        return MIN_SHARES
    raw = round(base_bet / ask)
    return max(MIN_SHARES, float(raw))


# ── signal evaluation (called by evaluate_late) ───────────────────────────────

def _evaluate_late(ctx: StrategyContext) -> list:
    """One tick. Returns a StreakSignal if all gates pass, else []."""
    from ..binance_api import get_atr4
    from ..polymarket_price import get_strike_and_mark
    from ..strategy_streak import StreakSignal
    from .. import logger

    state   = ctx.state
    symbol  = ctx.symbol

    max_coa       = float(getattr(state, "cfd_max_coa",       COA_MAX_DEFAULT))
    min_ask       = float(getattr(state, "cfd_min_ask",       ASK_MIN_DEFAULT))
    max_ask       = float(getattr(state, "cfd_max_ask",       ASK_MAX_DEFAULT))
    entry_min     = float(getattr(state, "cfd_entry_min_left", ENTRY_MIN_LEFT))
    entry_max     = float(getattr(state, "cfd_entry_max_left", ENTRY_MAX_LEFT))
    base_bet      = float(getattr(state, "cfd_base_bet",       5.0))

    tokens = ctx.tokens
    if tokens is None:
        return []

    window_ts = int(getattr(tokens, "window_ts", 0) or 0)
    strike, mark = get_strike_and_mark(window_ts, symbol)
    atr4         = get_atr4(symbol)
    ask_up, ask_down = state.get_asks()

    coa         = compute_coa(mark, strike, atr4) if (mark and strike) else None
    dog_result  = find_underdog(ask_up, ask_down)
    dog_dir, dog_ask = dog_result if dog_result else (None, None)

    passes, reason = check_gates(
        coa, dog_ask, ctx.seconds_left,
        max_coa, min_ask, max_ask, entry_min, entry_max,
    )

    if not passes:
        # Only log the skip when we're inside the entry time band — outside it
        # is every tick for 4 minutes and would drown the log.
        if reason not in ("CFD_OUT_OF_TIME",):
            state.record_skip(reason)
            logger.info(
                f"[CFD] SKIP {reason}  "
                f"coa={f'{coa:.3f}' if coa is not None else 'N/A'}  "
                f"dog_ask={dog_ask}  left={ctx.seconds_left:.0f}s",
                icon="⏭",
            )
        return []

    shares   = size_shares(base_bet, dog_ask)
    cap      = round(min(max_ask, dog_ask), 4)  # never pay above the band
    signal_r = (
        f"CFD coa={coa:.3f} dog={dog_dir}@{dog_ask:.3f} "
        f"left={ctx.seconds_left:.0f}s"
    )

    logger.ok(
        f"[CFD] 🐶 señal  {dog_dir} @ {cap:.3f}  "
        f"×{shares:.0f} shares  coa={coa:.3f}  "
        f"left={ctx.seconds_left:.0f}s",
        icon="🐕",
    )

    return [StreakSignal(
        strategy="coin_flip_dog",
        direction=dog_dir,
        limit_cap=cap,
        shares=shares,
        multiplier=1.0,   # flat — no martingale
        loss_streak=0,
        signal_reason=signal_r,
    )]


# ── descriptor ────────────────────────────────────────────────────────────────

DESCRIPTOR = StrategyDescriptor(
    id="coin_flip_dog",
    name="Coin-Flip Dog",
    description=(
        "Compra el lado barato (0,22–0,47) cuando la ventana está cerca de "
        "un volado (coa ≤ 0,20) con 30–90 s restantes."
    ),
    notes=(
        "Tesis validada en flip_harvester_IDEA.md (n=10.180, 42,4% win, "
        "+14,6% ROI) y observada en dos traders de Polymarket con rentabilidad "
        "medida. Entra tarde en la ventana para dejar que el precio se desarrolle; "
        "aguanta hasta resolución. Sin motor de salida anticipada (road-map: "
        "vender a ≥ 0,90 como hace zhengying9999)."
    ),
    evaluate=lambda ctx: [],     # nothing to do at window open
    evaluate_late=_evaluate_late,
    is_enabled=lambda state: bool(getattr(state, "cfd_enabled", False)),
    enabled_when={"field": "cfd_enabled", "values": [True]},
    priority=80,   # between fade (100) and trend (50)
    params=(
        RuntimeField(
            "cfd_enabled", "bool",
            label="Activar CFD",
            hint="Compra el underdog al final de la ventana cuando la señal pasa los tres filtros.",
        ),
        RuntimeField(
            "cfd_base_bet", "float", minimum=1.0, maximum=1000.0,
            label="Apuesta base ($)", step=0.5,
            hint="Importe base; las shares = round(apuesta / ask_underdog).",
        ),
        RuntimeField(
            "cfd_min_ask", "float", minimum=0.01, maximum=0.50,
            label="Ask mínimo", step=0.01,
            hint="0,22 — por debajo el underdog está correctamente descontado.",
        ),
        RuntimeField(
            "cfd_max_ask", "float", minimum=0.20, maximum=0.60,
            label="Ask máximo (cap)", step=0.01,
            hint="0,47 — por encima desaparece la ventaja medida.",
        ),
        RuntimeField(
            "cfd_max_coa", "float", minimum=0.05, maximum=1.0,
            label="COA máximo", step=0.01,
            hint="0,20 — cushion/ATR4, mide si la ventana es near-coin-flip.",
        ),
        RuntimeField(
            "cfd_entry_min_left", "float", minimum=5.0, maximum=120.0,
            label="Segundos mínimos", step=5.0,
            hint="30 — no entrar con menos de N segundos: tiempo para la reversión.",
        ),
        RuntimeField(
            "cfd_entry_max_left", "float", minimum=10.0, maximum=270.0,
            label="Segundos máximos", step=5.0,
            hint="90 — no entrar antes de T-90: señal no desarrollada aún.",
        ),
    ),
)
