"""Fase B — Box Builder (BB).

Both-sided maker on BTC 5-minute Up/Down markets.

Thesis (from box_builder/readme.md and Moon Dev's live logs):
  Quote post-only bids on BOTH sides in the FIRST HALF of each window,
  combined price ≤ BB_BID_SUM_CAP (default 0.94). When both legs fill the
  pair redeems for exactly $1.00 at resolution — direction-neutral, ≥ 6 c/pair
  locked. Adverse selection that kills directional makers *builds* the box.

Evidence (from box_builder/readme.md):
- fable maker: 57% fill rate at T-240 (early window).
- Late deep bids: ZERO fills in 35 windows — quoting too late is dead.
- Directional maker fills: ~9 c adverse selection (losers avg 0.647 vs 0.726
  winners). A completed box is direction-neutral so the adversely-selected
  fill becomes the profit source.
- Live trader 0x3c58ef...776b: $9,003 all-time P&L on 94,009 predictions.

State machine (inside `observe`, called every OBSERVE_TICK_SECONDS ≈ 4 s):

  START (armed=None)
    ├─ book absent or secs < QUOTE_CUTOFF_SEC      → armed=False (SKIP)
    ├─ ask_UP + ask_DOWN < ARM_MIN_SPREAD           → armed=False (SKIP_NARROW)
    └─ place post-only bid on each leg              → armed=True

  armed=False  → return, nothing to do this window

  armed=True, 0 fills
    ├─ secs ≥ QUOTE_CUTOFF_SEC → maybe_reprice (at most every REPRICE_INTERVAL)
    └─ secs < QUOTE_CUTOFF_SEC → quotes frozen, wait

  armed=True, 1 fill (p1 = fill price of filled leg)
    ├─ secs > BAILOUT_SEC:
    │    if other_ask ≤ COMPLETE_TAKER_CAP - p1  → lift (taker) → BOX_COMPLETE
    │    else                                     → raise other maker bid
    └─ secs ≤ BAILOUT_SEC (T-90 bailout):
         COA ≥ MIN_COA_HOLD and favors filled side → HOLD naked leg
         else                                       → CUT at best bid

  armed=True, 2 fills → BOX_COMPLETE, hold passively

  secs ≤ CANCEL_ALL_SEC (T-10) → cancel_all_resting
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..runtime_field import RuntimeField
from .base import StrategyContext, StrategyDescriptor

# ── defaults ──────────────────────────────────────────────────────────────────

ARM_MIN_SPREAD_DEFAULT    = 1.03   # ask_UP + ask_DOWN must be ≥ this to arm
BID_SUM_CAP_DEFAULT       = 0.94   # max combined bid — guarantees ≥ 6 c/pair
COMPLETE_TAKER_CAP_DEFAULT= 0.99   # lift if other ask ≤ 0.99 − p1
COMPLETE_MAKER_CAP_DEFAULT= 0.97   # raise other bid to ≤ 0.97 − p1
QUOTE_CUTOFF_SEC_DEFAULT  = 150.0  # no new two-sided quotes after T-150
BAILOUT_SEC_DEFAULT       = 90.0   # stranded-leg COA decision at T-90
CANCEL_ALL_SEC_DEFAULT    = 10.0   # cancel all resting at T-10
SHARES_PER_LEG_DEFAULT    = 5.0    # exchange floor; same count both legs
REPRICE_INTERVAL_DEFAULT  = 20.0   # reprice at most every 20 s
REPRICE_BEHIND_DEFAULT    = 0.02   # only if > 2 c behind best bid
MIN_COA_HOLD_DEFAULT      = 1.0    # hold stranded leg only if coa ≥ 1.0
PRICE_TICK                = 0.01


# ── pure functions (testable without CLOB) ────────────────────────────────────

def cap_bids(bid_up: float, bid_dn: float, cap: float) -> tuple[float, float]:
    """Reduce the larger bid until bid_up + bid_dn ≤ cap (1-cent steps)."""
    bu, bd = round(bid_up, 2), round(bid_dn, 2)
    while round(bu + bd, 2) > cap:
        if bu < PRICE_TICK and bd < PRICE_TICK:
            break
        if bu >= bd:
            bu = round(max(PRICE_TICK, bu - PRICE_TICK), 2)
        else:
            bd = round(max(PRICE_TICK, bd - PRICE_TICK), 2)
    return bu, bd


def initial_bids(
    ask_up: float, ask_dn: float,
    bid_up: Optional[float], bid_dn: Optional[float],
    bid_sum_cap: float,
) -> tuple[float, float]:
    """Starting bid prices: best_bid − 1 c, capped at bid_sum_cap combined."""
    bu = round((bid_up if bid_up and bid_up > 0 else ask_up - PRICE_TICK) - PRICE_TICK, 2)
    bd = round((bid_dn if bid_dn and bid_dn > 0 else ask_dn - PRICE_TICK) - PRICE_TICK, 2)
    bu = max(PRICE_TICK, bu)
    bd = max(PRICE_TICK, bd)
    return cap_bids(bu, bd, bid_sum_cap)


def completion_prices(
    p1: float,
    other_ask: Optional[float],
    other_bid: Optional[float],
    taker_cap: float,
    maker_cap: float,
) -> tuple[bool, float]:
    """(use_taker, price) for the completion leg after first fill at p1.

    Returns (True, ask) when the ask is cheap enough to lift immediately,
    (False, maker_px) otherwise.
    """
    if other_ask is not None and other_ask <= round(taker_cap - p1, 2):
        return True, other_ask
    target = round(maker_cap - p1, 2)
    safe   = round(min(
        target,
        (other_bid if other_bid and other_bid > 0 else target),
    ), 2)
    return False, max(PRICE_TICK, safe)


# ── per-window state ──────────────────────────────────────────────────────────

@dataclass
class _Leg:
    token_id:   Optional[str]   = None
    order_id:   Optional[str]   = None
    order_px:   Optional[float] = None
    fill_px:    Optional[float] = None
    fill_shares: float          = 0.0


@dataclass
class BoxWindow:
    """All mutable state for one 5-min window on one symbol."""
    window_ts:          Optional[int]  = None
    armed:              Optional[bool] = None   # None / True / False
    skip_reason:        str            = ""
    legs:               dict           = field(default_factory=lambda: {
        "UP": _Leg(), "DOWN": _Leg(),
    })
    bailout_done:       bool  = False
    cancelled_all:      bool  = False
    complete_attempted: bool  = False
    last_reprice_ts:    float = 0.0
    logged_complete:    bool  = False


# Global: one BoxWindow per symbol
_WINDOWS: dict[str, BoxWindow] = {}


def _get_window(symbol: str, window_ts: int) -> BoxWindow:
    box = _WINDOWS.get(symbol)
    if box is None or box.window_ts != window_ts:
        _WINDOWS[symbol] = box = BoxWindow(window_ts=window_ts)
    return box


# ── state machine helpers ─────────────────────────────────────────────────────

def _check_fills(
    box: BoxWindow,
    tokens,
    trader,
    state,
    logger,
) -> None:
    """Poll the CLOB for fills on any unfilled leg."""
    for side in ("UP", "DOWN"):
        leg = box.legs[side]
        if leg.fill_px is not None:
            continue  # already filled
        if leg.order_id is None:
            continue  # never placed

        tok = leg.token_id or (
            tokens.up_token_id if side == "UP" else tokens.down_token_id
        )
        size = trader._get_position_size(tok)
        if size and size > 0:
            leg.fill_px    = leg.order_px  # best approximation before GET /order
            leg.fill_shares = size
            logger.ok(
                f"[BB] ✅ {side} LLENADA @ {leg.order_px:.2f} ×{size:.0f}",
                icon="📦",
            )
            # Record the fill as an open trade in the DB so it resolves normally
            trader._record_box_fill(tokens, side, tok, leg.order_px or 0.0, size)


def _cancel_leg(leg: _Leg, trader) -> None:
    """Cancel the resting order for a leg, if any."""
    if leg.order_id and leg.fill_px is None and leg.token_id:
        trader._cancel_token_orders(leg.token_id)
        leg.order_id = None


def _observe(ctx: StrategyContext) -> None:
    """Box-builder tick. Called every OBSERVE_TICK_SECONDS during the window."""
    from .. import logger

    state   = ctx.state
    symbol  = ctx.symbol
    tokens  = ctx.tokens
    trader  = ctx.trader
    secs    = ctx.seconds_left

    if tokens is None or trader is None:
        return

    window_ts = int(getattr(tokens, "window_ts", 0) or 0)
    box       = _get_window(symbol, window_ts)

    # ── config ────────────────────────────────────────────────────────────────
    arm_min  = float(getattr(state, "bb_arm_min_spread",    ARM_MIN_SPREAD_DEFAULT))
    bid_cap  = float(getattr(state, "bb_bid_sum_cap",       BID_SUM_CAP_DEFAULT))
    t_cap    = float(getattr(state, "bb_complete_taker_cap",COMPLETE_TAKER_CAP_DEFAULT))
    m_cap    = float(getattr(state, "bb_complete_maker_cap",COMPLETE_MAKER_CAP_DEFAULT))
    q_cut    = float(getattr(state, "bb_quote_cutoff_sec",  QUOTE_CUTOFF_SEC_DEFAULT))
    bail_sec = float(getattr(state, "bb_bailout_sec",       BAILOUT_SEC_DEFAULT))
    c_all    = float(getattr(state, "bb_cancel_all_sec",    CANCEL_ALL_SEC_DEFAULT))
    shares   = float(getattr(state, "bb_shares_per_leg",    SHARES_PER_LEG_DEFAULT))
    r_iv     = float(getattr(state, "bb_reprice_interval",  REPRICE_INTERVAL_DEFAULT))
    r_beh    = float(getattr(state, "bb_reprice_behind",    REPRICE_BEHIND_DEFAULT))
    coa_hold = float(getattr(state, "bb_min_coa_hold",      MIN_COA_HOLD_DEFAULT))

    ask_up, ask_dn = state.get_asks()
    bid_up, bid_dn = state.get_bids()

    # ── T-10: cancel everything ───────────────────────────────────────────────
    if secs <= c_all:
        if not box.cancelled_all:
            box.cancelled_all = True
            for side in ("UP", "DOWN"):
                _cancel_leg(box.legs[side], trader)
            logger.info("[BB] T-10 cancelando órdenes restantes", icon="🚫")
        return

    # ── Check fills on live orders ────────────────────────────────────────────
    if box.armed:
        _check_fills(box, tokens, trader, state, logger)

    filled_sides = [s for s in ("UP", "DOWN") if box.legs[s].fill_px is not None]
    n_filled     = len(filled_sides)

    # ── BOX COMPLETE ──────────────────────────────────────────────────────────
    if n_filled == 2:
        if not box.logged_complete:
            box.logged_complete = True
            p_up = box.legs["UP"].fill_px
            p_dn = box.legs["DOWN"].fill_px
            locked = round(1.0 - (p_up + p_dn), 4) if p_up and p_dn else 0.0
            logger.ok(
                f"[BB] 📦 BOX COMPLETO  up={p_up:.3f} dn={p_dn:.3f} "
                f"costo={round((p_up or 0)+(p_dn or 0),3):.3f} "
                f"locked={locked:+.4f}/share",
                icon="🎯",
            )
            state.record_observation("BB_COMPLETE")
        return

    # ── UNDECIDED → try to arm ────────────────────────────────────────────────
    if box.armed is None:
        if secs < q_cut:
            # Missed the quoting window — window already too advanced
            box.armed      = False
            box.skip_reason = "BB_SKIP_LATE"
            state.record_skip("BB_SKIP_LATE")
            return
        if ask_up is None or ask_dn is None:
            return  # no book yet; wait for next tick
        spread_sum = round(ask_up + ask_dn, 2)
        if spread_sum < arm_min:
            box.armed       = False
            box.skip_reason = "BB_SKIP_NARROW"
            state.record_skip("BB_SKIP_NARROW")
            logger.info(
                f"[BB] SKIP_NARROW spread_sum={spread_sum:.2f} "
                f"< {arm_min:.2f}  ({secs:.0f}s left)",
                icon="⏭",
            )
            return

        # Place both post-only maker bids
        b_up, b_dn = initial_bids(ask_up, ask_dn, bid_up, bid_dn, bid_cap)
        up_id = trader._place_maker_bid(tokens.up_token_id,   b_up, shares)
        dn_id = trader._place_maker_bid(tokens.down_token_id, b_dn, shares)

        if not up_id and not dn_id:
            box.armed       = False
            box.skip_reason = "BB_SKIP_ORDER_FAIL"
            state.record_skip("BB_SKIP_ORDER_FAIL")
            return

        box.armed = True
        if up_id:
            box.legs["UP"] = _Leg(
                token_id=tokens.up_token_id,
                order_id=up_id, order_px=b_up,
            )
        if dn_id:
            box.legs["DOWN"] = _Leg(
                token_id=tokens.down_token_id,
                order_id=dn_id, order_px=b_dn,
            )
        logger.ok(
            f"[BB] 🏹 armada  bid_up={b_up:.2f} bid_dn={b_dn:.2f} "
            f"sum={b_up+b_dn:.2f}  spread={spread_sum:.2f}  "
            f"{secs:.0f}s left",
            icon="📦",
        )
        state.record_observation("BB_ARMED")
        return

    # ── SKIPPED ───────────────────────────────────────────────────────────────
    if not box.armed:
        return

    # ── 0 fills: maybe reprice ────────────────────────────────────────────────
    if n_filled == 0 and secs >= q_cut:
        now = time.time()
        if now - box.last_reprice_ts >= r_iv:
            box.last_reprice_ts = now
            for side, b_cur, b_mkt in (
                ("UP",   bid_up,  bid_up),
                ("DOWN", bid_dn, bid_dn),
            ):
                leg = box.legs[side]
                if leg.order_id is None or leg.order_px is None:
                    continue
                b_mkt = b_mkt or 0.0
                if b_mkt - leg.order_px > r_beh:
                    # More than 2c behind: reprice
                    tok  = leg.token_id or (
                        tokens.up_token_id if side == "UP" else tokens.down_token_id
                    )
                    trader._cancel_token_orders(tok)
                    # Re-check cap still holds with new price
                    other = "DOWN" if side == "UP" else "UP"
                    o_px  = box.legs[other].order_px or 0.0
                    new_px = round(min(b_mkt - PRICE_TICK, bid_cap - o_px), 2)
                    new_px = max(PRICE_TICK, new_px)
                    new_id = trader._place_maker_bid(tok, new_px, shares)
                    if new_id:
                        leg.order_id = new_id
                        leg.order_px = new_px
                        logger.info(
                            f"[BB] reprecio {side} {leg.order_px:.2f}→{new_px:.2f}",
                            icon="🔄",
                        )

    # ── 1 fill: complete or bailout ───────────────────────────────────────────
    elif n_filled == 1:
        filled_side = filled_sides[0]
        other_side  = "DOWN" if filled_side == "UP" else "UP"
        p1          = box.legs[filled_side].fill_px
        other_leg   = box.legs[other_side]
        o_ask       = ask_up if other_side == "UP" else ask_dn
        o_bid       = bid_up if other_side == "UP" else bid_dn
        o_tok       = (tokens.up_token_id if other_side == "UP" else tokens.down_token_id)
        o_tok       = o_tok or other_leg.token_id

        if secs <= bail_sec and not box.bailout_done:
            box.bailout_done = True
            _run_bailout(
                box, tokens, trader, state, logger,
                filled_side, p1, shares, coa_hold, symbol,
            )

        elif secs > bail_sec and not box.complete_attempted:
            box.complete_attempted = True
            use_taker, price = completion_prices(p1, o_ask, o_bid, t_cap, m_cap)
            if use_taker:
                # Cancel resting maker and lift the ask immediately
                _cancel_leg(other_leg, trader)
                oid = trader._place_taker_order(o_tok, "BUY", price, shares)
                if oid:
                    other_leg.order_id = oid
                    other_leg.order_px = price
                    logger.ok(
                        f"[BB] 🎯 lift {other_side} @ {price:.2f} "
                        f"(box locked ≥{round(1.0-p1-price, 3):+.3f})",
                        icon="🎯",
                    )
            else:
                # Raise maker bid toward maker_cap - p1
                _cancel_leg(other_leg, trader)
                oid = trader._place_maker_bid(o_tok, price, shares)
                if oid:
                    other_leg.order_id = oid
                    other_leg.order_px = price
                    other_leg.token_id = o_tok
                    logger.info(
                        f"[BB] completion bid {other_side} subido a {price:.2f}",
                        icon="⬆",
                    )


def _run_bailout(
    box: BoxWindow, tokens, trader, state, logger,
    filled_side: str, p1: float, shares: float,
    coa_hold: float, symbol: str,
) -> None:
    """T-90 bailout: hold the naked leg only if COA strongly favors it."""
    from ..binance_api import get_atr4
    from ..polymarket_price import get_strike_and_mark

    other_side = "DOWN" if filled_side == "UP" else "UP"
    other_leg  = box.legs[other_side]

    # Compute live COA
    window_ts = box.window_ts or 0
    coa: Optional[float] = None
    try:
        strike, mark = get_strike_and_mark(window_ts, symbol)
        atr4 = get_atr4(symbol)
        if strike and mark and atr4 and atr4 > 0:
            coa = abs(mark - strike) / atr4
    except Exception:
        pass

    # Hold only if COA is large enough AND favors the filled side
    # (filled side is winning ↔ coin doesn't favor the other side)
    hold = (
        coa is not None and coa >= coa_hold
        and _coa_favors(filled_side, mark, strike)
    )

    if hold:
        _cancel_leg(other_leg, trader)
        state.record_observation("BB_STRANDED_HELD")
        logger.info(
            f"[BB] T-90 HOLD naked {filled_side}  coa={coa:.3f}",
            icon="🏦",
        )
    else:
        # Cut the filled leg at best bid
        f_leg  = box.legs[filled_side]
        f_tok  = f_leg.token_id or (
            tokens.up_token_id if filled_side == "UP" else tokens.down_token_id
        )
        f_bid  = state.get_bids()
        f_bid  = (f_bid[0] if filled_side == "UP" else f_bid[1]) or p1
        trader._place_taker_order(f_tok, "SELL", f_bid, shares)
        _cancel_leg(other_leg, trader)
        state.record_observation("BB_STRANDED_CUT")
        logger.warn(
            f"[BB] T-90 CUT {filled_side} @ {f_bid:.2f}  "
            f"coa={coa if coa is not None else 'N/A'}",
        )


def _coa_favors(side: str, mark: float, strike: float) -> bool:
    """True if BTC has moved in the direction of `side`."""
    if mark is None or strike is None:
        return False
    if side == "UP":
        return mark > strike
    return mark < strike


# ── descriptor ────────────────────────────────────────────────────────────────

DESCRIPTOR = StrategyDescriptor(
    id="box_builder",
    name="Box Builder",
    description=(
        "Maker de dos lados: cotiza bids en UP y DOWN en la primera mitad de "
        "la ventana (suma ≤ 0,94). Si ambas patas se llenan el par redime a "
        "$1,00 sin riesgo direccional (≥ 6 c/par garantizados)."
    ),
    notes=(
        "Estrategia estructuralmente positiva: colecta el spread en lugar de "
        "predecir la dirección. Evidencia: trader 0x3c58ef…776b, $9k P&L con "
        "94k predicciones. Requiere órdenes maker (post_only) + cancelación. "
        "Publicada en Revisar Estrategias/box_builder/box_builder.py."
    ),
    evaluate=lambda ctx: [],
    observe=_observe,
    is_enabled=lambda state: bool(getattr(state, "bb_enabled", False)),
    enabled_when={"field": "bb_enabled", "values": [True]},
    priority=90,   # between ss_fade (100) and coin_flip_dog (80)
    params=(
        RuntimeField(
            "bb_enabled", "bool",
            label="Activar Box Builder",
            hint="Coloca bids en ambos lados; si ambos se llenan el par redime a $1.",
        ),
        RuntimeField(
            "bb_shares_per_leg", "float", minimum=5.0, maximum=500.0,
            label="Shares por pata", step=5.0,
            hint="Mínimo 5 (suelo de Polymarket). Mismo número en UP y DOWN.",
        ),
        RuntimeField(
            "bb_bid_sum_cap", "float", minimum=0.80, maximum=0.98,
            label="Cap suma bids", step=0.01,
            hint="0,94 → ≥ 6 c/par garantizados. Nunca subir por encima de 0,98.",
        ),
        RuntimeField(
            "bb_arm_min_spread", "float", minimum=1.00, maximum=1.20,
            label="Spread mínimo para armar", step=0.01,
            hint="1,03: ask_UP + ask_DOWN debe ser ≥ esto al abrir la ventana.",
        ),
        RuntimeField(
            "bb_complete_taker_cap", "float", minimum=0.90, maximum=1.00,
            label="Cap taker de completado", step=0.01,
            hint="0,99: levanta el otro ask si es ≤ 0,99 − p1 (≥ 1 c garantizado).",
        ),
        RuntimeField(
            "bb_complete_maker_cap", "float", minimum=0.88, maximum=0.99,
            label="Cap maker de completado", step=0.01,
            hint="0,97: sube bid del otro lado a ≤ 0,97 − p1 (≥ 3 c garantizado).",
        ),
        RuntimeField(
            "bb_quote_cutoff_sec", "float", minimum=60.0, maximum=240.0,
            label="Corte de cita (s)", step=10.0,
            hint="150 s: no hay nuevas citas de dos lados después de T-150.",
        ),
        RuntimeField(
            "bb_bailout_sec", "float", minimum=30.0, maximum=120.0,
            label="Bailout (s)", step=5.0,
            hint="90 s: decisión COA si una pata está descubierta a T-90.",
        ),
        RuntimeField(
            "bb_min_coa_hold", "float", minimum=0.5, maximum=3.0,
            label="COA mínimo para hold", step=0.1,
            hint="1,0: solo mantener la pata descubierta si COA ≥ 1,0 y favorece el lado.",
        ),
    ),
)
