"""Temporal Arbitrage strategy — Fase B.

Edge: Polymarket's book is slow to reprice when BTC moves through the window's
opening price.  The **leading side** (the side BTC has already moved toward)
keeps trading at 40–55 cents even though its real probability is closer to 60%.
We buy that mispriced leader as a taker; if BTC then reverses and the other side
also gets cheap we complete a covered pair (redeems at $1).  If the reversion
never comes, the first leg settles normally as a directional win/loss.

Signal: impulse from BTC spot vs the window's own opening price (the
Polymarket "strike").  Below ta_min_itm_pct the window is a coin flip — no
edge, no entry.  Above that threshold, only the leader side is a buy candidate,
and only when its ask is in [ta_min_ask, ta_max_ask].

Order slicing (iceberg / TWAP):
When ta_shares_per_leg > ta_order_slice, the first leg is bought in tranches of
ta_order_slice shares per observe tick (every ~4 s) instead of all at once.
This avoids walking the book on thin markets.  The phase stays "accumulating"
until the full target is reached, then transitions to "half_open" as normal.
The average entry price stored in first_px is the VWAP of all tranches.

Late Pair Taker (LPT):
If no entry fired before ta_entry_cutoff_sec but ask_up + ask_dn ≤
ta_lpt_cap at any point from T-ta_lpt_max_left to T-ta_lpt_min_left, buy
BOTH sides simultaneously as takers.  No directional signal needed — the
locked profit is 1.0 − (ask_up + ask_dn) regardless of outcome.

Hedge Recovery:
When phase=half_open and the first leg's current ask has dropped more than
ta_hedge_drop_pct below the entry price (i.e. the position is losing), AND
the opposite side's ask is cheap enough that entry + hedge ≤ ta_hedge_max_sum,
buy the opposite side to lock in whatever value remains and reduce the loss
(or reach breakeven).  Only fires once per window.

State machine (per window per symbol):

  IDLE
    ├─ secs ∈ [ta_lpt_min_left, ta_lpt_max_left] AND ask_up+ask_dn ≤ ta_lpt_cap
    │    → BUY both sides taker → LPT_COMPLETE   (late pair, no directional signal)
    └─ fetch strike, |itm_pct| >= ta_min_itm_pct AND secs >= ta_entry_cutoff_sec
       AND leader_ask ∈ [ta_min_ask, ta_max_ask]
         → BUY first tranche → ACCUMULATING  (or HALF_OPEN if slice ≥ target)

  ACCUMULATING  (buying leader in tranches, price still in band)
    ├─ ask exits [ta_min_ask, ta_max_ask]  →  stop accumulating → HALF_OPEN
    ├─ target reached                      →  HALF_OPEN
    └─ secs ≤ ta_bailout_sec              →  HALF_OPEN  (hold what we have)

  HALF_OPEN  (leader leg open, waiting for BTC to reverse + cheap second leg)
    ├─ second_ask ≤ ta_complete_cap − first_px     →  BUY taker  →  COMPLETE
    ├─ current_ask dropped ≥ ta_hedge_drop_pct AND
    │  first_px + hedge_ask ≤ ta_hedge_max_sum     →  BUY hedge taker  →  HEDGED
    └─ secs ≤ ta_bailout_sec  →  CLOSED  (first leg resolves normally)

  HEDGED     both sides open; one wins, minimising net loss / reaching breakeven
  LPT_COMPLETE / COMPLETE  hold passively; legs resolve via normal resolution path
  CLOSED     nothing more to do this window

Bailout: first leg stays in DB as an open trade and is settled at window end.
If the directional call was right → win; otherwise → loss.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from .base import StrategyContext, StrategyDescriptor
from ..runtime_field import RuntimeField

# ── defaults ──────────────────────────────────────────────────────────────────
MIN_ITM_PCT_DEFAULT      = 0.05   # % BTC must move through the strike before entry
ENTRY_MIN_ASK_DEFAULT    = 0.40   # leader ask floor (below = market already caught up)
ENTRY_MAX_ASK_DEFAULT    = 0.55   # leader ask ceiling (above = no misprice to exploit)
COMPLETE_CAP_DEFAULT     = 0.82   # max total pair cost to accept for second leg
SHARES_PER_LEG_DEFAULT   = 5.0
ORDER_SLICE_DEFAULT      = 5.0    # shares per taker order; ≤ ta_shares_per_leg
ENTRY_CUTOFF_SEC_DEFAULT = 150.0  # don't enter this late in the window
BAILOUT_SEC_DEFAULT      = 60.0   # stop waiting for 2nd leg when ≤ this secs left
CANCEL_ALL_SEC_DEFAULT   = 10.0   # T-10 (no resting orders; kept for symmetry)

# Late Pair Taker defaults
LPT_ENABLED_DEFAULT      = True
LPT_CAP_DEFAULT          = 0.90   # buy both sides if ask_up + ask_dn ≤ this
LPT_MIN_LEFT_DEFAULT     = 20.0   # earliest T-N seconds to fire LPT
LPT_MAX_LEFT_DEFAULT     = 148.0  # latest T-N seconds (just below entry_cutoff)

# Hedge Recovery defaults
HEDGE_ENABLED_DEFAULT    = True
HEDGE_DROP_PCT_DEFAULT   = 0.40   # fire if current_ask ≤ entry_px * (1 - drop_pct)
HEDGE_MAX_SUM_DEFAULT    = 0.92   # max entry_px + hedge_ask to still be worthwhile

# ── per-window state ──────────────────────────────────────────────────────────

@dataclass
class _TAWindow:
    window_ts:           Optional[int]   = None
    phase:               str             = "idle"   # idle | accumulating | half_open | hedged | complete | lpt_complete | closed
    first_side:          Optional[str]   = None     # "UP" | "DOWN"
    first_tok:           Optional[str]   = None
    first_px:            Optional[float] = None     # VWAP of all accumulated tranches
    # Slicing: track progress toward the full first-leg target.
    first_shares_target: float           = 0.0      # ta_shares_per_leg, locked at first buy
    first_shares_filled: float           = 0.0      # shares bought so far
    first_cost_sum:      float           = 0.0      # sum(price * shares) for VWAP
    # Strike cached per window — one Binance call per 5-min window.
    strike:              Optional[float] = None
    logged_bailout:      bool            = False
    logged_complete:     bool            = False
    logged_flat:         bool            = False    # "mercado plano" already logged this window
    hedge_fired:         bool            = False    # Hedge Recovery already executed this window
    lpt_fired:           bool            = False    # Late Pair Taker already executed this window


_WINDOWS: dict[str, _TAWindow] = {}
_LOCK = threading.Lock()


def _get_window(symbol: str, window_ts: int) -> _TAWindow:
    """Return (or create) the TAWindow for this symbol/window; auto-resets."""
    with _LOCK:
        win = _WINDOWS.get(symbol)
        if win is None or win.window_ts != window_ts:
            _WINDOWS[symbol] = win = _TAWindow(window_ts=window_ts)
        return win


# ── pure helpers (importable by tests without network) ───────────────────────

def find_leader_side(
    spot: Optional[float],
    strike: Optional[float],
    ask_up: Optional[float],
    ask_dn: Optional[float],
    min_itm_pct: float,
    min_ask: float,
    max_ask: float,
) -> tuple[Optional[str], Optional[float], float]:
    """Identify the mispriced leading side based on BTC impulse vs strike.

    Returns (side, ask, itm_pct):
      - side  = "UP" | "DOWN" | None (no signal)
      - ask   = the leader's current ask, or None
      - itm_pct = signed % move through the strike (positive = BTC above strike)

    The leader is the side BTC has already moved toward:
      - BTC > strike → "UP" is the leader
      - BTC < strike → "DOWN" is the leader
    We only enter if the leader's ask is in [min_ask, max_ask]: below that band
    the market has already fully repriced (no edge left); above it the market
    already overpriced the leader (also no edge, and wrong direction of misprice).
    """
    if spot is None or strike is None or strike <= 0:
        return None, None, 0.0

    itm_pct = (spot - strike) / strike * 100.0

    if abs(itm_pct) < min_itm_pct:
        return None, None, itm_pct  # coin-flip territory — no directional signal

    if itm_pct > 0:
        # BTC above strike → UP is winning; buy UP when the market hasn't caught up yet
        leader_side = "UP"
        leader_ask  = ask_up
    else:
        # BTC below strike → DOWN is winning
        leader_side = "DOWN"
        leader_ask  = ask_dn

    if leader_ask is None:
        return None, None, itm_pct

    if not (min_ask <= leader_ask <= max_ask):
        return None, None, itm_pct  # outside the mispriced band

    return leader_side, leader_ask, itm_pct


def second_leg_worthwhile(first_px: float, second_ask: float, cap: float) -> bool:
    """True when buying the second leg keeps total pair cost ≤ cap."""
    return round(first_px + second_ask, 4) <= cap


def lpt_pair_worthwhile(ask_up: float, ask_dn: float, cap: float) -> bool:
    """True when buying both sides simultaneously locks a profit (sum ≤ cap < 1.0)."""
    return round(ask_up + ask_dn, 4) <= cap


def hedge_worthwhile(
    entry_px: float,
    current_ask: float,
    hedge_ask: float,
    drop_pct: float,
    max_sum: float,
) -> bool:
    """True when a hedge buy on the opposite side is justified.

    Two conditions must both hold:
    1. The first leg has dropped enough to trigger: current_ask ≤ entry_px * (1 - drop_pct)
       e.g. entry=0.45, drop_pct=0.40 → triggers when current_ask ≤ 0.27
    2. entry_px + hedge_ask ≤ max_sum — so we're not overpaying for the hedge.
       The net outcome: one side pays $1, total cost is entry_px + hedge_ask,
       locked = 1.0 − (entry_px + hedge_ask). If max_sum < 1 this is always positive.
    """
    drop_threshold = round(entry_px * (1.0 - drop_pct), 4)
    if current_ask is None or current_ask > drop_threshold:
        return False
    return round(entry_px + hedge_ask, 4) <= max_sum


# ── observe (state machine tick) ─────────────────────────────────────────────

def _observe(ctx: StrategyContext) -> None:
    """Temporal-Arb tick, called every OBSERVE_TICK_SECONDS during the window."""
    from .. import logger
    from ..binance_api import get_current_window_open

    state  = ctx.state
    symbol = ctx.symbol
    tokens = ctx.tokens
    trader = ctx.trader
    secs   = ctx.seconds_left

    if tokens is None or trader is None:
        return

    window_ts = int(getattr(tokens, "window_ts", 0) or 0)
    ta = _get_window(symbol, window_ts)

    # ── config ────────────────────────────────────────────────────────────────
    min_itm  = float(getattr(state, "ta_min_itm_pct",      MIN_ITM_PCT_DEFAULT))
    min_ask  = float(getattr(state, "ta_min_ask",          ENTRY_MIN_ASK_DEFAULT))
    max_ask  = float(getattr(state, "ta_max_ask",          ENTRY_MAX_ASK_DEFAULT))
    cap      = float(getattr(state, "ta_complete_cap",     COMPLETE_CAP_DEFAULT))
    shares   = float(getattr(state, "ta_shares_per_leg",   SHARES_PER_LEG_DEFAULT))
    raw_slice = float(getattr(state, "ta_order_slice", ORDER_SLICE_DEFAULT))
    slice_sz  = max(1.0, min(raw_slice, shares))
    q_cut    = float(getattr(state, "ta_entry_cutoff_sec", ENTRY_CUTOFF_SEC_DEFAULT))
    bail_sec = float(getattr(state, "ta_bailout_sec",      BAILOUT_SEC_DEFAULT))
    c_all    = float(getattr(state, "ta_cancel_all_sec",   CANCEL_ALL_SEC_DEFAULT))
    # Late Pair Taker config
    lpt_enabled  = bool(getattr(state, "ta_lpt_enabled",   LPT_ENABLED_DEFAULT))
    lpt_cap      = float(getattr(state, "ta_lpt_cap",      LPT_CAP_DEFAULT))
    lpt_min_left = float(getattr(state, "ta_lpt_min_left", LPT_MIN_LEFT_DEFAULT))
    lpt_max_left = float(getattr(state, "ta_lpt_max_left", LPT_MAX_LEFT_DEFAULT))
    # Hedge Recovery config
    hedge_enabled  = bool(getattr(state, "ta_hedge_enabled",  HEDGE_ENABLED_DEFAULT))
    hedge_drop_pct = float(getattr(state, "ta_hedge_drop_pct", HEDGE_DROP_PCT_DEFAULT))
    hedge_max_sum  = float(getattr(state, "ta_hedge_max_sum",  HEDGE_MAX_SUM_DEFAULT))

    # ── terminal states ───────────────────────────────────────────────────────
    if ta.phase in ("complete", "lpt_complete", "hedged", "closed"):
        return

    if secs <= c_all:
        return

    ask_up, ask_dn = state.get_asks()

    # ── ACCUMULATING: keep buying tranches until first-leg target is filled ───
    # Price leaving the band stops accumulation — we hold what we have and
    # transition to half_open to wait for the pair.
    if ta.phase == "accumulating":
        leader_ask = ask_up if ta.first_side == "UP" else ask_dn
        remaining  = round(ta.first_shares_target - ta.first_shares_filled, 4)

        stop_reason = None
        if secs <= bail_sec:
            stop_reason = "bailout"
        elif remaining <= 0:
            stop_reason = "target_reached"
        elif leader_ask is None or not (min_ask <= leader_ask <= max_ask):
            stop_reason = "ask_out_of_band"

        if stop_reason:
            # Finalise VWAP and move to half_open regardless of reason.
            if ta.first_shares_filled > 0:
                ta.first_px = round(ta.first_cost_sum / ta.first_shares_filled, 4)
            logger.info(
                f"[TA] acumulación finalizada ({stop_reason})  "
                f"filled={ta.first_shares_filled:.0f}/{ta.first_shares_target:.0f}  "
                f"vwap={ta.first_px:.4f}",
                icon="📊",
            )
            ta.phase = "half_open"
            return

        # Buy one more tranche.
        tranche = min(slice_sz, remaining)
        oid = trader._place_taker_order(ta.first_tok, "BUY", leader_ask, tranche)
        if oid:
            ta.first_shares_filled = round(ta.first_shares_filled + tranche, 4)
            ta.first_cost_sum      = round(ta.first_cost_sum + leader_ask * tranche, 4)
            ta.first_px            = round(ta.first_cost_sum / ta.first_shares_filled, 4)
            trader._record_box_fill(
                tokens, ta.first_side, ta.first_tok, leader_ask, tranche,
                strategy="temporal_arb",
            )
            logger.info(
                f"[TA] 📥 tranche {ta.first_side} +{tranche:.0f}  "
                f"@ {leader_ask:.3f}  "
                f"total={ta.first_shares_filled:.0f}/{ta.first_shares_target:.0f}  "
                f"vwap={ta.first_px:.4f}",
                icon="📥",
            )
            state.record_observation("TA_TRANCHE")
        return

    # ── HALF_OPEN: pair completion + Hedge Recovery ───────────────────────────
    # Checked before IDLE so a reversion on the same tick isn't missed.
    if ta.phase == "half_open":
        second_side = "DOWN" if ta.first_side == "UP" else "UP"
        second_ask  = ask_dn if second_side == "DOWN" else ask_up

        # Path A: normal pair completion — second leg is cheap enough
        if (
            second_ask is not None
            and ta.first_px is not None
            and second_leg_worthwhile(ta.first_px, second_ask, cap)
        ):
            tok2 = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
            leg2_shares = ta.first_shares_filled if ta.first_shares_filled > 0 else shares
            oid2 = trader._place_taker_order(tok2, "BUY", second_ask, leg2_shares)
            if oid2:
                locked = round(1.0 - (ta.first_px + second_ask), 4)
                logger.ok(
                    f"[TA] 📦 PAR COMPLETO  "
                    f"{ta.first_side}={ta.first_px:.3f} + {second_side}={second_ask:.3f}"
                    f"  costo={round(ta.first_px + second_ask, 3):.3f}"
                    f"  locked={locked:+.4f}/share",
                    icon="📦",
                )
                trader._record_box_fill(
                    tokens, second_side, tok2, second_ask, leg2_shares,
                    strategy="temporal_arb",
                )
                ta.logged_complete = True
                ta.phase = "complete"
                state.record_observation("TA_COMPLETE")
                return

        # Path B: Hedge Recovery — first leg is losing hard, buy opposite side
        # to reduce net loss or reach breakeven.
        # Example: bought DOWN@0.45, now DOWN@0.18 (lost 60% of value).
        # Buying UP@0.40 → total cost=0.85 → at expiry one side pays $1.00,
        # net P&L = 1.0 - 0.85 = +$0.15 instead of -$0.45 (or vice versa).
        if hedge_enabled and not ta.hedge_fired and ta.first_px is not None:
            # current ask of the FIRST leg (the losing side)
            current_first_ask = ask_up if ta.first_side == "UP" else ask_dn
            # ask of the HEDGE side (opposite)
            hedge_ask = ask_dn if second_side == "DOWN" else ask_up

            if (
                current_first_ask is not None
                and hedge_ask is not None
                and hedge_worthwhile(
                    ta.first_px, current_first_ask, hedge_ask,
                    hedge_drop_pct, hedge_max_sum,
                )
            ):
                tok_hedge = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
                hedge_shares = ta.first_shares_filled if ta.first_shares_filled > 0 else shares
                oid_h = trader._place_taker_order(tok_hedge, "BUY", hedge_ask, hedge_shares)
                if oid_h:
                    net_cost  = round(ta.first_px + hedge_ask, 4)
                    net_locked = round(1.0 - net_cost, 4)
                    logger.ok(
                        f"[TA] 🛡 HEDGE RECOVERY  "
                        f"primera={ta.first_side}@{ta.first_px:.3f}"
                        f" (ahora {current_first_ask:.3f})  "
                        f"hedge={second_side}@{hedge_ask:.3f}  "
                        f"costo_total={net_cost:.3f}  "
                        f"breakeven_locked={net_locked:+.4f}/share",
                        icon="🛡",
                    )
                    trader._record_box_fill(
                        tokens, second_side, tok_hedge, hedge_ask, hedge_shares,
                        strategy="temporal_arb",
                    )
                    ta.hedge_fired = True
                    ta.phase = "hedged"
                    state.record_observation("TA_HEDGE")
                    return

        # Bailout: time ran out — first leg resolves normally (win or loss)
        if secs <= bail_sec and not ta.logged_bailout:
            ta.logged_bailout = True
            ta.phase = "closed"
            logger.info(
                f"[TA] ⏭ BAILOUT  primera_pata={ta.first_side}@{ta.first_px:.3f}"
                f"  segunda no disponible (left={secs:.0f}s)"
                f"  → primera pata se resuelve normalmente",
                icon="⏭",
            )
            state.record_skip("TA_BAILOUT")
        return

    # ── IDLE: look for the mispriced leader ───────────────────────────────────
    if ta.phase == "idle":
        # Gate 0: Late Pair Taker (LPT) — checked FIRST, before the normal cutoff.
        # When normal entry already closed (secs < q_cut) but the pair is cheap
        # enough to lock a guaranteed profit, buy both sides simultaneously.
        # No directional signal needed — the profit is 1.0 − (ask_up + ask_dn).
        if (
            lpt_enabled
            and not ta.lpt_fired
            and ask_up is not None
            and ask_dn is not None
            and lpt_min_left <= secs <= lpt_max_left
            and lpt_pair_worthwhile(ask_up, ask_dn, lpt_cap)
        ):
            tok_up = tokens.up_token_id
            tok_dn = tokens.down_token_id
            oid_up = trader._place_taker_order(tok_up, "BUY", ask_up, shares)
            oid_dn = trader._place_taker_order(tok_dn, "BUY", ask_dn, shares)
            if oid_up and oid_dn:
                pair_sum = round(ask_up + ask_dn, 4)
                locked   = round(1.0 - pair_sum, 4)
                logger.ok(
                    f"[TA] ⚡ LATE PAIR  UP={ask_up:.3f} + DOWN={ask_dn:.3f}"
                    f"  suma={pair_sum:.3f}  locked={locked:+.4f}/share"
                    f"  left={secs:.0f}s",
                    icon="⚡",
                )
                trader._record_box_fill(
                    tokens, "UP", tok_up, ask_up, shares, strategy="temporal_arb"
                )
                trader._record_box_fill(
                    tokens, "DOWN", tok_dn, ask_dn, shares, strategy="temporal_arb"
                )
                ta.lpt_fired = True
                ta.phase = "lpt_complete"
                state.record_observation("TA_LPT")
                return
            # If only one side filled, log and continue (don't enter half-covered)
            logger.warn(
                f"[TA] LPT orden parcial — UP={'ok' if oid_up else 'fail'} "
                f"DOWN={'ok' if oid_dn else 'fail'} — skipping",
                icon="⚠",
            )
            ta.lpt_fired = True   # don't retry; avoid partial fills piling up
            ta.phase = "closed"
            return

        # Gate 1: too late for normal directional entry
        if secs < q_cut:
            ta.phase = "closed"
            state.record_skip("TA_SKIP_LATE")
            # Distinguish two cases: either the bot started into an already-running
            # window (strike never fetched), or the window ran normally but BTC
            # never crossed the min-itm threshold before the cutoff.
            if ta.strike is None:
                reason = f"ventana ya en curso al arrancar ({300 - secs:.0f}s transcurridos)"
            else:
                reason = (
                    f"sin señal antes del cutoff ({300 - secs:.0f}s transcurridos)"
                    f"  strike={ta.strike:,.2f}"
                )
            logger.info(f"[TA] SKIP_LATE — {reason}", icon="⏭")
            return

        # Gate 2: fetch the window's opening price (the "strike") once per window.
        if ta.strike is None:
            open_px = get_current_window_open(symbol, window_ts)
            if open_px is None:
                return
            ta.strike = open_px

        # Gate 3: check BTC spot vs strike to identify the leader
        spot = getattr(state, "spot_price", None)
        if ask_up is None or ask_dn is None or spot is None:
            return

        side, px, itm_pct = find_leader_side(
            spot, ta.strike, ask_up, ask_dn, min_itm, min_ask, max_ask
        )

        if side is None:
            if abs(itm_pct) >= min_itm:
                leader_ask = ask_up if itm_pct > 0 else ask_dn
                if leader_ask is not None and leader_ask > max_ask:
                    logger.info(
                        f"[TA] SKIP_ASK_HIGH  itm={itm_pct:+.3f}%  "
                        f"leader_ask={leader_ask:.3f} > {max_ask:.2f}  (mercado ya repriced)",
                        icon="⏭",
                    )
                elif leader_ask is not None and leader_ask < min_ask:
                    logger.info(
                        f"[TA] SKIP_ASK_LOW  itm={itm_pct:+.3f}%  "
                        f"leader_ask={leader_ask:.3f} < {min_ask:.2f}",
                        icon="⏭",
                    )
            else:
                # BTC has not moved far enough from the strike — log once per window
                # so the operator can confirm TA is alive even in flat markets.
                if not ta.logged_flat:
                    ta.logged_flat = True
                    pair_sum = round((ask_up or 0) + (ask_dn or 0), 3)
                    logger.info(
                        f"[TA] mercado plano  itm={itm_pct:+.4f}% < {min_itm:.2f}%  "
                        f"strike={ta.strike:,.2f}  spot={spot:,.2f}  "
                        f"sum={pair_sum:.3f}  left={secs:.0f}s",
                        icon="⏭",
                    )
            return

        tok = tokens.up_token_id if side == "UP" else tokens.down_token_id

        logger.info(
            f"[TA] 🎯 líder mispriced  {side} @ {px:.3f}"
            f"  itm={itm_pct:+.3f}%  strike={ta.strike:,.2f}  spot={spot:,.2f}"
            f"  slice={slice_sz:.0f}/{shares:.0f}  left={secs:.0f}s",
            icon="🎯",
        )

        oid = trader._place_taker_order(tok, "BUY", px, slice_sz)
        if not oid:
            logger.warn("[TA] orden taker rechazada para primera pata", icon="⚠")
            return

        ta.first_side           = side
        ta.first_tok            = tok
        ta.first_shares_target  = shares
        ta.first_shares_filled  = slice_sz
        ta.first_cost_sum       = round(px * slice_sz, 4)
        ta.first_px             = px

        trader._record_box_fill(
            tokens, side, tok, px, slice_sz, strategy="temporal_arb"
        )
        state.record_observation("TA_FIRST_LEG")

        if slice_sz >= shares:
            ta.phase = "half_open"
        else:
            ta.phase = "accumulating"


# ── descriptor ────────────────────────────────────────────────────────────────

DESCRIPTOR = StrategyDescriptor(
    id="temporal_arb",
    name="Temporal Arb",
    description=(
        "Compra el lado líder cuando BTC ya atravesó el strike pero Polymarket "
        "todavía lo cotiza barato (ask 0.40–0.55). Si BTC revierte, completa el "
        "par para cubrir. Late Pair Taker: si la suma UP+DOWN ≤ ta_lpt_cap en "
        "T-20..T-150s, compra ambos lados a la vez. Hedge Recovery: si la primera "
        "pata cae ≥ ta_hedge_drop_pct, compra el lado contrario para limitar la pérdida."
    ),
    notes=(
        "Señal: |itm_pct| = |spot − strike| / strike. "
        "Solo entra el lado que BTC ya favoreció, en la banda de misprice. "
        "Segunda pata si reversion + par ≤ ta_complete_cap. "
        "LPT: entrada tardía sin señal, si par garantiza profit. "
        "Hedge: mitiga pérdida cuando primera pata bajó mucho."
    ),
    evaluate=lambda ctx: [],
    observe=_observe,
    is_enabled=lambda state: bool(getattr(state, "ta_enabled", False)),
    enabled_when={"field": "ta_enabled", "values": [True]},
    priority=80,
    params=(
        RuntimeField("ta_enabled", "bool", label="Temporal Arb activo"),
        RuntimeField(
            "ta_min_itm_pct", "float",
            label="Min ITM % (impulso)",
            minimum=0.01, maximum=0.50, step=0.01,
            hint="BTC debe haber movido al menos este % a través del strike (default 0.05)",
        ),
        RuntimeField(
            "ta_min_ask", "float",
            label="Ask mínimo líder",
            minimum=0.30, maximum=0.55, step=0.01,
            hint="Por debajo el mercado ya repriced — sin edge (default 0.40)",
        ),
        RuntimeField(
            "ta_max_ask", "float",
            label="Ask máximo líder",
            minimum=0.40, maximum=0.70, step=0.01,
            hint="Por encima el libro sobreprecio al líder — no entrar (default 0.55)",
        ),
        RuntimeField(
            "ta_complete_cap", "float",
            label="Cap par completo",
            minimum=0.60, maximum=0.94, step=0.01,
            hint="No completa el par si primera+segunda > este cap (default 0.82)",
        ),
        RuntimeField(
            "ta_shares_per_leg", "float",
            label="Shares por pata",
            minimum=5.0, maximum=100.0, step=1.0,
            hint="Total de shares a acumular por pata (se compra en tranches de ta_order_slice)",
        ),
        RuntimeField(
            "ta_order_slice", "float",
            label="Shares por orden (tranche)",
            minimum=5.0, maximum=100.0, step=5.0,
            hint="Máximo shares por orden taker. Si < ta_shares_per_leg, compra en tranches cada 4s para reducir impacto en el libro (default 5)",
        ),
        RuntimeField(
            "ta_entry_cutoff_sec", "float",
            label="Cutoff entrada (s restantes)",
            minimum=60.0, maximum=240.0, step=10.0,
            hint="No inicia par si quedan menos de este tiempo (default 150s)",
        ),
        RuntimeField(
            "ta_bailout_sec", "float",
            label="Bailout segunda pata (s restantes)",
            minimum=20.0, maximum=120.0, step=5.0,
            hint="Deja de esperar la segunda pata cuando quedan ≤ este tiempo (default 60s)",
        ),
        # ── Late Pair Taker ───────────────────────────────────────────────────
        RuntimeField("ta_lpt_enabled", "bool", label="Late Pair Taker activo",
                     hint="Compra ambos lados si suma ≤ ta_lpt_cap cuando ya pasó el cutoff normal"),
        RuntimeField(
            "ta_lpt_cap", "float",
            label="LPT cap suma par",
            minimum=0.70, maximum=0.98, step=0.01,
            hint="Entra LPT si ask_UP + ask_DN ≤ este valor (default 0.90 → profit ≥ 10¢)",
        ),
        RuntimeField(
            "ta_lpt_min_left", "float",
            label="LPT min segundos restantes",
            minimum=10.0, maximum=60.0, step=5.0,
            hint="LPT no entra si quedan menos de este tiempo (default 20s)",
        ),
        RuntimeField(
            "ta_lpt_max_left", "float",
            label="LPT max segundos restantes",
            minimum=60.0, maximum=200.0, step=10.0,
            hint="LPT solo activa por debajo del cutoff normal (default 148s)",
        ),
        # ── Hedge Recovery ────────────────────────────────────────────────────
        RuntimeField("ta_hedge_enabled", "bool", label="Hedge Recovery activo",
                     hint="Compra lado contrario si la primera pata baja mucho para limitar pérdida"),
        RuntimeField(
            "ta_hedge_drop_pct", "float",
            label="Hedge drop mínimo",
            minimum=0.10, maximum=0.80, step=0.05,
            hint="Activa hedge si el ask cayó ≥ este % del precio de entrada (default 0.40 = 40%)",
        ),
        RuntimeField(
            "ta_hedge_max_sum", "float",
            label="Hedge max suma",
            minimum=0.60, maximum=0.99, step=0.01,
            hint="Solo hace hedge si entrada + hedge_ask ≤ este valor (default 0.92)",
        ),
    ),
)
