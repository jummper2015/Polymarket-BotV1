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
import time
from dataclasses import dataclass
from typing import Optional

from .base import StrategyContext, StrategyDescriptor
from ..runtime_field import RuntimeField

# ── defaults ──────────────────────────────────────────────────────────────────
# Lowered from 0.05 → 0.025 on 2026-09-17 after the strike source moved to a
# local TWAP-60s (gap vs Polymarket official dropped from $20-30 to $0-5).
# Bot can now safely take thinner edges without false positives from stale
# strikes. Production .env still overrides (was 0.02, can stay or drop further).
MIN_ITM_PCT_DEFAULT      = 0.025  # % BTC must move through the strike before entry
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

# TWAP-aware hedge defaults (added 2026-09-17)
# Fires earlier than the price-drop hedge (Path B): when the Polymarket
# resolution oracle's TWAP-60s strongly opposes our position during the
# last 60s of the window. Earlier signal → better hedge price.
#
# Default: DISABLED. Activate via /settings once the local TWAP-60s source
# has been validated in production. Tests must explicitly enable.
TWAP_HEDGE_ENABLED_DEFAULT = False
TWAP_HEDGE_MARGIN_DEFAULT  = 50.0  # $|margin|$ in dollars for the TWAP projection
TWAP_HEDGE_MAX_SUM_DEFAULT = 0.92  # same cost ceiling as Path B

# Hold-Winner constants (added 2026-09-25)
# Si ask >= HOLD_WINNER_THRESHOLD y se mantiene >= HOLD_WINNER_DWELL_SECS,
# pasamos a HOLD (no más acciones hasta resolución). Si cae antes,
# inmediatamente buscamos edge (profit-lock).
HOLD_WINNER_THRESHOLD   = 0.90
HOLD_WINNER_DWELL_SECS  = 60

# Extended profit-lock window (added 2026-09-25)
# Beyond profit_lock_min_secs we keep trying to lock (max until
# profit_lock_max_secs). Catches the user's Case 2: edge becomes available
# after the 30s initial grace but the bot keeps looking.
PROFIT_LOCK_MAX_SECS_DEFAULT = 60

# TWAP-based entry signal defaults (added 2026-09-21)
# Replaces the raw spot price as the reference for itm_pct and the ATR
# impulse filter. The rolling TWAP (60s of Coinbase ticks) smooths out
# intra-spread noise and reduces false-direction flips while keeping the
# signal responsive. Default ON; disable for backtesting or A/B comparison.
USE_TWAP_SIGNAL_DEFAULT     = True
TWAP_LOOKBACK_SEC_DEFAULT    = 60    # window for the rolling TWAP computation

# Profit-Lock completion defaults (added 2026-09-21)
# When the first leg has gone ≥ta_profit_lock_min_secs in profit
# (current_ask > first_px), force-close the pair if the pair cost is
# ≤ ta_complete_cap. Reuses Path A's cap so behavior is consistent with
# the normal completion logic. Default OFF.
PROFIT_LOCK_ENABLED_DEFAULT  = False
PROFIT_LOCK_MIN_SECS_DEFAULT = 30
PROFIT_LOCK_CAP_DEFAULT      = 1.00

# Martingale hedge defaults (added 2026-09-21)
# When the first leg has gone ≥ta_mart_hedge_min_secs in loss
# (current_ask < first_px), buy the opposite side with qty =
# first_shares × mult × (1 + loss_pct). Each round is recorded
# (`mart_hedge_rounds`); the strategy stops once `ta_mart_hedge_max_rounds`
# is reached, after which only the regular stop-loss / hedge-recovery
# paths apply. Cap on pair cost = `ta_hedge_max_sum` (same as Path B).
# Default OFF — high risk in trending markets.
MART_HEDGE_ENABLED_DEFAULT   = False
MART_HEDGE_MIN_SECS_DEFAULT  = 30
MART_HEDGE_MULT_DEFAULT      = 2.0
MART_HEDGE_MAX_ROUNDS_DEFAULT = 3

# Stop-loss defaults
STOP_LOSS_ENABLED_DEFAULT    = True
STOP_LOSS_TIME_SEC_DEFAULT   = 60.0   # wait at least this long before considering stop-loss
STOP_LOSS_THRESHOLD_DEFAULT  = 0.25   # 25% loss triggers stop
CATASTROPHIC_LOSS_PCT_DEFAULT = 0.50  # 50% loss triggers immediate stop, no time gate
TRAILING_STOP_ENABLED_DEFAULT = True
TRAILING_STOP_PCT_DEFAULT    = 0.30   # 30% drawdown from peak triggers trailing stop

# Technical indicators defaults
USE_ATR_DEFAULT              = True
MIN_NORMALIZED_IMPULSE_DEFAULT = 0.8   # impulse must be ≥ 80% of ATR(14)
USE_RSI_DEFAULT              = True
RSI_OVERBOUGHT_DEFAULT       = 75.0    # don't buy UP if RSI > this
RSI_OVERSOLD_DEFAULT         = 25.0    # don't buy DOWN if RSI < this
USE_VOLUME_DEFAULT           = False   # volume filter (future enhancement)
MIN_VOLUME_RATIO_DEFAULT     = 1.5     # current volume must be ≥ 1.5x average

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
    # Stop-loss tracking
    entry_timestamp:     Optional[float] = None     # time.time() when first leg entered
    first_leg_peak_ask:  Optional[float] = None     # highest ask seen for first leg (trailing stop)
    stop_loss_fired:     bool            = False    # stop-loss already executed this window
    # ──── Hold Winner tracking ───────────────────────────────────────────
    logged_hold_winner:  bool            = False
    reached_winner_zone: bool            = False  # alguna vez llegó al threshold
    winner_zone_entered_at: Optional[float] = None  # ts cuando crossed threshold
    absolute_peak:       Optional[float] = None
    logged_winner_reversal: bool         = False
    # ──── Profit-Lock + Mart-Hedge tracking ─────────────────────────────
    first_leg_filled_at: Optional[float] = None
    hedge_fill_attempts: int             = 0  # cuántas veces se intentó la cobertura
    profit_lock_fired:   bool            = False  # any profit-lock executed this window
    mart_hedge_rounds:    int             = 0
    mart_hedge_fired:     bool            = False



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
    twap: Optional[float] = None,
) -> tuple[Optional[str], Optional[float], float]:
    """Identify the mispriced leading side based on BTC impulse vs strike.

    Returns (side, ask, itm_pct):
      - side  = "UP" | "DOWN" | None (no signal)
      - ask   = the leader's current ask, or None
      - itm_pct = signed % move through the strike (positive = BTC above strike)

    `twap` is a smoothed price (e.g., 60s rolling average of Coinbase ticks)
    used as the reference when provided; `spot` is the live tick used as
    fallback when TWAP isn't ready. The smoother TWAP signal produces fewer
    false-direction flips than raw spot in a 5-min window — meaningful
    when the entry threshold (`min_itm_pct`) is small.

    The leader is the side BTC has already moved toward:
      - ref > strike → "UP" is the leader
      - ref < strike → "DOWN" is the leader
    We only enter if the leader's ask is in [min_ask, max_ask]: below that band
    the market has already fully repriced (no edge left); above it the market
    already overpriced the leader (also no edge, and wrong direction of misprice).
    """
    ref = twap if twap is not None else spot
    if ref is None or strike is None or strike <= 0:
        return None, None, 0.0

    itm_pct = (ref - strike) / strike * 100.0

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


def _execute_stop_loss(
    ctx: StrategyContext,
    ta: _TAWindow,
    current_ask: float,
    reason: str,
) -> bool:
    """Execute stop-loss by attempting to sell or hedging the losing position.

    Args:
        ctx: Strategy context
        ta: Temporal arb window state
        current_ask: Current ask price of the first leg
        reason: Human-readable reason for stop (e.g., "TIME_THRESHOLD", "TRAILING_STOP")

    Returns:
        True if stop-loss was executed successfully, False otherwise
    """
    from .. import logger

    state  = ctx.state
    tokens = ctx.tokens
    trader = ctx.trader

    if tokens is None or trader is None:
        return False

    # Calculate loss percentage
    loss_pct = (ta.first_px - current_ask) / ta.first_px if ta.first_px else 0.0
    loss_dollars = (ta.first_px - current_ask) * ta.first_shares_filled

    # Strategy 1: Try to sell the losing position if there's a bid
    # (In most cases there won't be enough liquidity, so we'll hedge instead)
    bid_up, bid_dn = state.get_bids() if hasattr(state, 'get_bids') else (None, None)
    first_bid = bid_up if ta.first_side == "UP" else bid_dn

    # Strategy 2: Hedge immediately (more reliable)
    # Buy the opposite side to lock in remaining value
    second_side = "DOWN" if ta.first_side == "UP" else "UP"
    second_ask  = ctx.state.get_asks()[1] if ta.first_side == "UP" else ctx.state.get_asks()[0]

    if second_ask is None:
        logger.warn(
            f"[M$] ⚠ STOP_LOSS failed — no ask available for hedge side {second_side}",
            icon="⚠",
        )
        return False

    # Execute hedge to minimize loss
    tok_hedge = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
    hedge_shares = ta.first_shares_filled if ta.first_shares_filled > 0 else 5.0

    oid = trader._place_taker_order(tok_hedge, "BUY", second_ask, hedge_shares)
    if not oid:
        logger.warn(
            f"[M$] ⚠ STOP_LOSS hedge order failed for {second_side}",
            icon="⚠",
        )
        return False

    # Calculate net outcome
    net_cost = round(ta.first_px + second_ask, 4)
    net_locked = round(1.0 - net_cost, 4)

    logger.ok(
        f"[M$] 🛑 STOP-LOSS EXECUTED ({reason})  "
        f"primera={ta.first_side}@{ta.first_px:.3f} (ahora {current_ask:.3f})  "
        f"pérdida={loss_pct:+.1%} (${loss_dollars:+.2f})  "
        f"hedge={second_side}@{second_ask:.3f}  "
        f"costo_total={net_cost:.3f}  "
        f"resultado_neto={net_locked:+.4f}/share",
        icon="🛑",
    )

    trader._record_box_fill(
        tokens, second_side, tok_hedge, second_ask, hedge_shares,
        strategy="temporal_arb",
    )

    ta.stop_loss_fired = True
    ta.phase = "hedged"  # Mark as hedged (same as hedge recovery)
    state.record_observation(f"TA_STOP_LOSS_{reason}")

    return True


# ── observe (state machine tick) ─────────────────────────────────────────────

def _observe(ctx: StrategyContext) -> None:
    """Temporal-Arb tick, called every OBSERVE_TICK_SECONDS during the window."""
    from .. import logger
    from ..polymarket_price import get_strike

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
    # TWAP-aware hedge config (added 2026-09-17)
    twap_hedge_enabled = bool(getattr(state, "ta_twap_hedge_enabled", TWAP_HEDGE_ENABLED_DEFAULT))
    twap_hedge_margin  = float(getattr(state, "ta_twap_hedge_margin", TWAP_HEDGE_MARGIN_DEFAULT))
    twap_hedge_max_sum = float(getattr(state, "ta_twap_hedge_max_sum", TWAP_HEDGE_MAX_SUM_DEFAULT))
    # Stop-loss config
    stop_loss_enabled    = bool(getattr(state, "ta_stop_loss_enabled",    STOP_LOSS_ENABLED_DEFAULT))
    stop_loss_time_sec   = float(getattr(state, "ta_stop_loss_time_sec",  STOP_LOSS_TIME_SEC_DEFAULT))
    stop_loss_threshold  = float(getattr(state, "ta_stop_loss_threshold", STOP_LOSS_THRESHOLD_DEFAULT))
    catastrophic_loss_pct = float(getattr(state, "ta_catastrophic_loss_pct", CATASTROPHIC_LOSS_PCT_DEFAULT))
    trailing_stop_enabled = bool(getattr(state, "ta_trailing_stop_enabled", TRAILING_STOP_ENABLED_DEFAULT))
    trailing_stop_pct    = float(getattr(state, "ta_trailing_stop_pct",   TRAILING_STOP_PCT_DEFAULT))
    # Technical indicators config
    use_atr              = bool(getattr(state, "ta_use_atr",              USE_ATR_DEFAULT))
    min_norm_impulse     = float(getattr(state, "ta_min_normalized_impulse", MIN_NORMALIZED_IMPULSE_DEFAULT))
    use_rsi              = bool(getattr(state, "ta_use_rsi",              USE_RSI_DEFAULT))
    rsi_overbought       = float(getattr(state, "ta_rsi_overbought",      RSI_OVERBOUGHT_DEFAULT))
    rsi_oversold         = float(getattr(state, "ta_rsi_oversold",        RSI_OVERSOLD_DEFAULT))
    use_volume           = bool(getattr(state, "ta_use_volume",           USE_VOLUME_DEFAULT))
    min_volume_ratio     = float(getattr(state, "ta_min_volume_ratio",    MIN_VOLUME_RATIO_DEFAULT))
    # TWAP-based entry signal (2026-09-21)
    use_twap_signal        = bool(getattr(state, "ta_use_twap_signal", USE_TWAP_SIGNAL_DEFAULT))
    twap_lookback_sec      = int(getattr(state, "ta_twap_lookback_sec", TWAP_LOOKBACK_SEC_DEFAULT))
    # Profit-Lock completion (2026-09-21)
    profit_lock_enabled   = bool(getattr(state, "ta_profit_lock_enabled", PROFIT_LOCK_ENABLED_DEFAULT))
    profit_lock_min_secs  = int(getattr(state, "ta_profit_lock_min_secs", PROFIT_LOCK_MIN_SECS_DEFAULT))
    profit_lock_cap       = float(getattr(state, "ta_profit_lock_cap", PROFIT_LOCK_CAP_DEFAULT))
    # Martingale hedge (2026-09-21)
    mart_hedge_enabled    = bool(getattr(state, "ta_mart_hedge_enabled", MART_HEDGE_ENABLED_DEFAULT))
    mart_hedge_min_secs   = int(getattr(state, "ta_mart_hedge_min_secs", MART_HEDGE_MIN_SECS_DEFAULT))
    mart_hedge_mult       = float(getattr(state, "ta_mart_hedge_mult", MART_HEDGE_MULT_DEFAULT))
    mart_hedge_max_rounds = int(getattr(state, "ta_mart_hedge_max_rounds", MART_HEDGE_MAX_ROUNDS_DEFAULT))
    # Hold-Winner + extended profit-lock (2026-09-25)
    profit_lock_max_secs  = float(getattr(state, "ta_profit_lock_max_secs", PROFIT_LOCK_MAX_SECS_DEFAULT))

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
            ta.first_leg_filled_at = time.time()  # mark time the first leg completed
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

        # Get current ask of the first leg for stop-loss tracking
        current_first_ask = ask_up if ta.first_side == "UP" else ask_dn

        # Live BTC spot price — used by the TWAP-aware hedge (Path C, last 60s).
        # Defined here (not inside the IDLE block) so it's in scope throughout
        # the function. May be None if the price feed hasn't reported yet.
        spot = getattr(state, "spot_price", None)

        # ══════════════════════════════════════════════════════════════════════════
        # HOLD WINNER (added 2026-09-25):
        # Si el ask del primer leg llega a ≥0.90 y se mantiene por ≥60s
        # consecutivos, entramos en HOLD (no buscamos más edge — el otro lado
        # ya no va a llenar). Si antes de los 60s cae por debajo, salimos
        # del intento y buscamos edge vía profit-lock.
        # ══════════════════════════════════════════════════════════════════════════

        # Track del peak absoluto (máximo histórico)
        if ta.absolute_peak is None:
            ta.absolute_peak = ta.first_px if ta.first_px else 0.0
        if current_first_ask is not None and current_first_ask > ta.absolute_peak:
            ta.absolute_peak = current_first_ask

        # Track tiempo en winner zone
        if current_first_ask is not None and current_first_ask >= HOLD_WINNER_THRESHOLD:
            if ta.winner_zone_entered_at is None:
                ta.winner_zone_entered_at = time.time()
            ta.reached_winner_zone = True
        else:
            ta.winner_zone_entered_at = None

        # Si llevamos ≥60s en zona de ganador, pasar a HOLD
        in_winner_zone_long = (
            ta.reached_winner_zone
            and ta.winner_zone_entered_at is not None
            and (ts - ta.winner_zone_entered_at) >= HOLD_WINNER_DWELL_SECS
        )
        if in_winner_zone_long:
            if not ta.logged_hold_winner:
                ta.logged_hold_winner = True
                logger.ok(
                    f"[TA] 🏆 HOLD WINNER  {ta.first_side}@{ta.first_px:.3f} "
                    f"ahora {current_first_ask:.3f} (≥{HOLD_WINNER_THRESHOLD} "
                    f"por ≥{HOLD_WINNER_DWELL_SECS}s)  "
                    f"→ mantener hasta resolución",
                    icon="🏆"
                )
                state.record_observation("TA_HOLD_WINNER")
            return  # HOLD: no buscar edge

        # Si estamos en winner zone pero aún no 60s, esperar (no buscar edge)
        # pero dejar correr la lógica de profit-lock por si el precio oscila.
        # La condición es: winner_zone_entered_at existe pero dwell < 60s.
        currently_in_zone = (
            ta.reached_winner_zone
            and ta.winner_zone_entered_at is not None
        )
        in_grace_period = currently_in_zone and not in_winner_zone_long

        # Reversión desde winner zone: solo relevante cuando ya estábamos en HOLD
        # y el precio cayó por debajo del threshold.
        if (
            ta.reached_winner_zone
            and not in_grace_period
            and not in_winner_zone_long
            and current_first_ask is not None
            and current_first_ask < HOLD_WINNER_THRESHOLD
            and not ta.logged_winner_reversal
            and not ta.logged_complete
        ):
            # Salió de winner zone sin haber llegado a HOLD
            # (porque cayó antes de los 60s). Buscar edge directamente.
            if second_ask is not None and ta.first_px is not None:
                pair_sum = round(ta.first_px + second_ask, 4)

                if pair_sum <= cap:  # Cap configurado (0.88 por defecto)
                    ta.logged_winner_reversal = True
                    tok2 = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
                    leg2_shares = ta.first_shares_filled if ta.first_shares_filled > 0 else shares
                    oid2 = trader._place_taker_order(tok2, "BUY", second_ask, leg2_shares)
                    
                    if oid2:
                        locked = round(1.0 - pair_sum, 4)
                        logger.ok(
                            f"[TA] 💰 EDGE LOCKED (reversión desde winner zone)  "
                            f"peak={ta.absolute_peak:.3f} → ahora={current_first_ask:.3f}  "
                            f"{ta.first_side}={ta.first_px:.3f} + {second_side}={second_ask:.3f}  "
                            f"costo={pair_sum:.3f}  locked={locked:+.4f}/share",
                            icon="💰"
                        )
                        trader._record_box_fill(
                            tokens, second_side, tok2, second_ask, leg2_shares,
                            strategy="temporal_arb"
                        )
                        ta.logged_complete = True
                        ta.phase = "complete"
                        state.record_observation("TA_EDGE_LOCKED_REVERSAL")
                        return


        # ── Stop-loss logic: three independent triggers, evaluated every tick ──
        # Outer guard only checks that we have a position and a current ask.
        # Each path has its own enabled flag and condition so a momentary
        # ask=None or a slow time_since_entry doesn't suppress the others:
        #   - CATASTROPHIC:   loss ≥ catastrophic_loss_pct (default 50%) — fires
        #                     immediately, no time gate. Caps the worst case.
        #   - TIME_THRESHOLD: held ≥ stop_loss_time_sec AND loss ≥ threshold.
        #                     Original behavior — let the position breathe.
        #   - TRAILING_STOP:  drawdown from peak ≥ trailing_stop_pct. No time
        #                     gate; can fire on the first tick after entry if
        #                     the price never rallied.
        # Peak tracking happens inside the same guard so we never compare
        # against a stale peak that was set before a price feed gap.
        if (
            not ta.stop_loss_fired
            and ta.first_px is not None
            and current_first_ask is not None
            and ta.entry_timestamp is not None
        ):
            # Always update peak when we have a fresh ask — used by trailing.
            if trailing_stop_enabled:
                if ta.first_leg_peak_ask is None:
                    ta.first_leg_peak_ask = current_first_ask
                else:
                    ta.first_leg_peak_ask = max(ta.first_leg_peak_ask, current_first_ask)

            time_since_entry = time.time() - ta.entry_timestamp
            loss_pct = (ta.first_px - current_first_ask) / ta.first_px
            drawdown = (
                (ta.first_leg_peak_ask - current_first_ask) / ta.first_leg_peak_ask
                if ta.first_leg_peak_ask else 0.0
            )

            # Path C: catastrophic loss — fire first, no time gate
            if (
                stop_loss_enabled
                and loss_pct >= catastrophic_loss_pct
                and _execute_stop_loss(ctx, ta, current_first_ask, "CATASTROPHIC_LOSS")
            ):
                return

            # Path 1: time-based stop — sustained loss after a minimum hold
            if (
                stop_loss_enabled
                and time_since_entry >= stop_loss_time_sec
                and loss_pct >= stop_loss_threshold
                and _execute_stop_loss(ctx, ta, current_first_ask, "TIME_THRESHOLD")
            ):
                return

            # Path 2: trailing stop — drawdown from peak, no time gate
            if (
                trailing_stop_enabled
                and drawdown >= trailing_stop_pct
                and _execute_stop_loss(ctx, ta, current_first_ask, "TRAILING_STOP")
            ):
                return

            # Diagnostic — log every ~20s so we can see what the eval sees
            # without spamming the log every 4s tick. Throttled per-window.
            last_log = getattr(ta, "_last_stop_eval_log", 0.0)
            if time.time() - last_log >= 20.0:
                ta._last_stop_eval_log = time.time()
                logger.info(
                    f"[TA] 🩺 STOP_EVAL  "
                    f"t+{time_since_entry:.1f}s  "
                    f"loss={loss_pct:+.1%}  "
                    f"drawdown={drawdown:+.1%}  "
                    f"px={ta.first_px:.3f}→{current_first_ask:.3f}  "
                    f"peak={ta.first_leg_peak_ask:.3f}",
                    icon="🩺",
                )

        # Path D: Profit-Lock completion (added 2026-09-21, extended 2026-09-25).
        # Caso 2 del usuario: tras `profit_lock_min_secs` (30s) en ganancia, si
        # el edge está disponible (par < $1) cerramos. Si en los siguientes
        # `profit_lock_max_secs` (60s) sigue sin completar pero el precio
        # sigue a favor, seguimos intentando en cada tick. No usamos flag
        # `profit_lock_fired` para permitir múltiples intentos dentro de la
        # ventana — cada intento es independiente.
        if (
            profit_lock_enabled
            and ta.first_leg_filled_at is not None
            and ta.first_shares_filled > 0
            and ta.first_px is not None
            and current_first_ask is not None
            and current_first_ask > ta.first_px  # in profit
            and (time.time() - ta.first_leg_filled_at) >= profit_lock_min_secs
            and (time.time() - ta.first_leg_filled_at) < profit_lock_max_secs
        ):
            opp_ask = ask_dn if second_side == "DOWN" else ask_up
            if opp_ask is not None and second_leg_worthwhile(ta.first_px, opp_ask, profit_lock_cap):
                tok_second = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
                second_shares = ta.first_shares_filled
                oid_pl = trader._place_taker_order(tok_second, "BUY", opp_ask, second_shares)
                if oid_pl:
                    pair_sum = round(ta.first_px + opp_ask, 4)
                    locked = round(1.0 - pair_sum, 4)
                    pct = (current_first_ask - ta.first_px) / ta.first_px * 100
                    logger.ok(
                        f"[TA] 💰 PROFIT-LOCK  "
                        f"primera={ta.first_side}@{ta.first_px:.3f}"
                        f" (ahora {current_first_ask:.3f}, +{pct:.1f}%)  "
                        f"segunda={second_side}@{opp_ask:.3f}  "
                        f"suma={pair_sum:.3f}  locked={locked:+.4f}/share",
                        icon="💰",
                    )
                    trader._record_box_fill(
                        tokens, second_side, tok_second, opp_ask, second_shares,
                        strategy="temporal_arb",
                    )
                    ta.hedge_fill_attempts += 1
                    ta.profit_lock_fired = True  # informational: al menos 1 lock esta ventana
                    ta.phase = "complete"
                    state.record_observation("TA_PROFIT_LOCK")
                    return

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

        # Path C: TWAP-aware early hedge — fires only in the last 60s when
        # the official TWAP-60s (Polymarket resolution oracle) strongly
        # opposes our position. Earlier signal than Path B because it uses
        # the resolution oracle's projected winner, not the spot price.
        if (
            twap_hedge_enabled
            and not ta.hedge_fired
            and secs < 60.0
            and ta.first_px is not None
            and ta.first_side is not None
            and ta.strike is not None
        ):
            from .. import polymarket_twap_tracker as twap_tracker
            twap_state = twap_tracker.get_state(
                window_ts=int(window_ts),
                current_ts=int(time.time()),
                strike=ta.strike,
                current_btc=spot,
            )
            # Only fire when TWAP projects the opposite side as winner with
            # enough margin AND the hedge price is acceptable.
            if (
                twap_state.projected_winner is not None
                and twap_state.projected_winner != ta.first_side
                and twap_state.margin is not None
                and abs(twap_state.margin) >= twap_hedge_margin
            ):
                hedge_ask = ask_dn if second_side == "DOWN" else ask_up
                if (
                    hedge_ask is not None
                    and round(ta.first_px + hedge_ask, 4) <= twap_hedge_max_sum
                ):
                    tok_hedge = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
                    hedge_shares = ta.first_shares_filled if ta.first_shares_filled > 0 else shares
                    oid_th = trader._place_taker_order(tok_hedge, "BUY", hedge_ask, hedge_shares)
                    if oid_th:
                        net_cost = round(ta.first_px + hedge_ask, 4)
                        net_locked = round(1.0 - net_cost, 4)
                        logger.ok(
                            f"[TA] 🧭 TWAP EARLY HEDGE  "
                            f"primera={ta.first_side}@{ta.first_px:.3f}  "
                            f"proy_TWAP_winner={twap_state.projected_winner}  "
                            f"margin=${twap_state.margin:+.2f}  "
                            f"hedge={second_side}@{hedge_ask:.3f}  "
                            f"costo_total={net_cost:.3f}  "
                            f"breakeven_locked={net_locked:+.4f}/share  "
                            f"left={secs:.0f}s",
                            icon="🧭",
                        )
                        trader._record_box_fill(
                            tokens, second_side, tok_hedge, hedge_ask, hedge_shares,
                            strategy="temporal_arb",
                        )
                        ta.hedge_fired = True
                        ta.phase = "hedged"
                        state.record_observation("TA_TWAP_HEDGE")
                        return

        # Path E: Martingale hedge (added 2026-09-21, refined 2026-09-25).
        # Caso 3 del usuario: el impulso resulta falso, el precio va en contra.
        # Compramos el lado opuesto con qty = first_shares × (2 + loss_pct) para
        # duplicar la posición inicial más un extra proporcional a la pérdida
        # de la primera pata. Si el precio revierte y vuelve a favor, repite
        # el paso (martingale). Cap a `mart_hedge_max_rounds` rondas.
        if (
            mart_hedge_enabled
            and not ta.mart_hedge_fired
            and ta.first_leg_filled_at is not None
            and ta.first_shares_filled > 0
            and ta.first_px is not None
            and current_first_ask is not None
            and current_first_ask < ta.first_px  # in loss
            and (time.time() - ta.first_leg_filled_at) >= mart_hedge_min_secs
            and ta.mart_hedge_rounds < mart_hedge_max_rounds
        ):
            opp_ask = ask_dn if second_side == "DOWN" else ask_up
            if opp_ask is not None and round(ta.first_px + opp_ask, 4) <= hedge_max_sum:
                loss_pct = (ta.first_px - current_first_ask) / ta.first_px  # positive fraction
                # qty = 2x initial + loss% of initial → qty = initial × (2 + loss_pct)
                mart_qty = round(
                    ta.first_shares_filled * (2.0 + loss_pct), 4
                )
                tok_second = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
                oid_mh = trader._place_taker_order(tok_second, "BUY", opp_ask, mart_qty)
                if oid_mh:
                    cost = round(ta.first_px + opp_ask, 4)
                    ta.mart_hedge_rounds += 1
                    ta.mart_hedge_fired = True
                    logger.ok(
                        f"[TA] 🛡 MART-HEDGE round={ta.mart_hedge_rounds}/{mart_hedge_max_rounds}  "
                        f"primera={ta.first_side}@{ta.first_px:.3f}"
                        f" (ahora {current_first_ask:.3f}, -{loss_pct*100:.1f}%)  "
                        f"hedge={second_side}@{opp_ask:.3f} × {mart_qty:.0f}sh  "
                        f"suma={cost:.3f}  (qty=initial×(2+{loss_pct:.2f}))",
                        icon="🛡",
                    )
                    trader._record_box_fill(
                        tokens, second_side, tok_second, opp_ask, mart_qty,
                        strategy="temporal_arb",
                    )
                    state.record_observation(f"TA_MART_HEDGE_R{ta.mart_hedge_rounds}")
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
        if secs < 30:
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
            open_px = get_strike(window_ts, symbol)
            if open_px is None:
                return
            ta.strike = open_px
            logger.info(f"[TA] ✅ Strike obtenido: ${ta.strike:,.2f}", icon="⚡")

        # Gate 3: check BTC spot vs strike to identify the leader.
        # When the TWAP signal is enabled (default) we feed find_leader_side
        # the rolling TWAP of the last N seconds — a smoothed price that
        # produces fewer false-direction flips than raw spot in a 5-min
        # window. Falls back to spot when the feed isn't ready or hasn't
        # accumulated enough ticks.
        spot = getattr(state, "spot_price", None)
        if ask_up is None or ask_dn is None or spot is None:
            return

        twap_ref = None
        if use_twap_signal:
            try:
                from .. import polymarket_twap_tracker as _twap_tracker
                import time as _time
                twap_ref = _twap_tracker.get_rolling_twap(
                    int(_time.time()), lookback_seconds=twap_lookback_sec
                )
            except Exception:
                twap_ref = None  # fall back to spot silently

        side, px, itm_pct = find_leader_side(
            spot, ta.strike, ask_up, ask_dn, min_itm, min_ask, max_ask,
            twap=twap_ref,
        )

        # ── Technical indicator filters (applied BEFORE entry) ────────────────────
        # Only check if we have a valid side candidate
        if side is not None:
            from ..indicators import get_atr, get_rsi, get_volume_ratio

            # Filter 1: ATR normalized impulse (TWAP-based when available,
            # so the filter measures smoothed deviation rather than tick noise).
            if use_atr:
                atr = get_atr(symbol, 14)
                if atr and atr > 0 and ta.strike:
                    ref_price = twap_ref if twap_ref is not None else spot
                    impulse_dollars = abs(ref_price - ta.strike) if ref_price else 0
                    norm_impulse = impulse_dollars / atr
                    if norm_impulse < min_norm_impulse:
                        logger.info(
                            f"[M$] ⏭ SKIP_WEAK_IMPULSE  "
                            f"impulse=${impulse_dollars:.2f}  atr=${atr:.2f}  "
                            f"norm={norm_impulse:.2f}ATR < {min_norm_impulse:.2f}ATR"
                            f"{'  [TWAP]' if twap_ref is not None else ''}",
                            icon="⏭",
                        )
                        state.record_skip("TA_SKIP_WEAK_IMPULSE")
                        return

            # Filter 2: RSI overbought/oversold
            if use_rsi:
                rsi = get_rsi(symbol, 14)
                if rsi is not None:
                    if side == "UP" and rsi > rsi_overbought:
                        logger.info(
                            f"[M$] ⏭ SKIP_RSI_OVERBOUGHT  "
                            f"side=UP  rsi={rsi:.1f} > {rsi_overbought:.1f}  "
                            f"(posible corrección bajista)",
                            icon="⏭",
                        )
                        state.record_skip("TA_SKIP_RSI_OVERBOUGHT")
                        return
                    elif side == "DOWN" and rsi < rsi_oversold:
                        logger.info(
                            f"[M$] ⏭ SKIP_RSI_OVERSOLD  "
                            f"side=DOWN  rsi={rsi:.1f} < {rsi_oversold:.1f}  "
                            f"(posible rebote alcista)",
                            icon="⏭",
                        )
                        state.record_skip("TA_SKIP_RSI_OVERSOLD")
                        return

            # Filter 3: Volume ratio (optional, disabled by default)
            if use_volume:
                vol_ratio = get_volume_ratio(symbol, 10)
                if vol_ratio is not None and vol_ratio < min_volume_ratio:
                    logger.info(
                        f"[M$] ⏭ SKIP_LOW_VOLUME  "
                        f"vol_ratio={vol_ratio:.2f}x < {min_volume_ratio:.2f}x  "
                        f"(impulso sin volumen)",
                        icon="⏭",
                    )
                    state.record_skip("TA_SKIP_LOW_VOLUME")
                    return

        # Log cada 20 segundos para ver evaluación
        if int(secs) % 20 < 4:
            strike_str = f"{ta.strike:.2f}" if ta.strike else "0.00"
            spot_str = f"{spot:.2f}" if spot else "0.00"
            ask_up_str = f"{ask_up:.3f}" if ask_up is not None else "None"
            ask_dn_str = f"{ask_dn:.3f}" if ask_dn is not None else "None"
            logger.info(
                f"[TA] 🔍 eval  "
                f"strike=${strike_str}  "
                f"spot=${spot_str}  "
                f"itm={itm_pct:+.3f}% (min={min_itm:.3f}%)  "
                f"ask_up={ask_up_str}  "
                f"ask_dn={ask_dn_str}  "
                f"side={side or 'NONE'}  "
                f"left={secs:.0f}s",
                icon="🔍"
            )

        if side is None:
            if abs(itm_pct) >= min_itm:
                leader_ask = ask_up if itm_pct > 0 else ask_dn
                if leader_ask is not None and leader_ask > max_ask:
                    logger.info(
                        f"[TA] ⏭ SKIP_ASK_HIGH  itm={itm_pct:+.3f}%  "
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
        ta.entry_timestamp      = time.time()  # Track entry time for stop-loss
        ta.first_leg_peak_ask   = px           # Initialize peak at entry price

        trader._record_box_fill(
            tokens, side, tok, px, slice_sz, strategy="temporal_arb"
        )
        state.record_observation("TA_FIRST_LEG")

        if slice_sz >= shares:
            ta.first_leg_filled_at = time.time()  # single tranche → immediately half_open
            ta.phase = "half_open"
        else:
            ta.phase = "accumulating"
            # first_leg_filled_at is set when accumulating transitions to half_open
            # (see above)


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
        # ── TWAP-aware hedge (added 2026-09-17) ─────────────────────────────────
        RuntimeField("ta_twap_hedge_enabled", "bool", label="TWAP early hedge activo",
                     hint="En los últimos 60s, hedgea si la TWAP-60s oficial se opone fuertemente a la posición"),
        RuntimeField(
            "ta_twap_hedge_margin", "float",
            label="TWAP hedge margen mínimo ($)",
            minimum=0.0, maximum=500.0, step=10.0,
            hint="Activa hedge si $|margin|$ ≥ este valor (default 50 USD). margin = current_btc - required_avg.",
        ),
        RuntimeField(
            "ta_twap_hedge_max_sum", "float",
            label="TWAP hedge max suma",
            minimum=0.60, maximum=0.99, step=0.01,
            hint="Solo hace TWAP hedge si entrada + hedge_ask ≤ este valor (default 0.92)",
        ),
        # ── Stop-loss ──────────────────────────────────────────────────────────
        RuntimeField("ta_stop_loss_enabled", "bool", label="Stop-loss activo",
                     hint="Corta pérdidas cuando la posición cae por debajo del umbral configurado"),
        RuntimeField(
            "ta_stop_loss_time_sec", "float",
            label="Stop-loss tiempo mínimo (s)",
            minimum=30.0, maximum=180.0, step=10.0,
            hint="Esperar al menos este tiempo antes de evaluar stop-loss (default 60s)",
        ),
        RuntimeField(
            "ta_catastrophic_loss_pct", "float",
            label="Stop-loss catastrófico (sin gate de tiempo)",
            minimum=0.30, maximum=0.80, step=0.05,
            hint="Ejecutar stop INMEDIATO si pérdida ≥ este % (default 0.50 = 50%). Sin gate de tiempo.",
        ),
        RuntimeField(
            "ta_stop_loss_threshold", "float",
            label="Stop-loss umbral pérdida",
            minimum=0.15, maximum=0.50, step=0.05,
            hint="Ejecutar stop si pérdida ≥ este % del precio de entrada (default 0.25 = 25%)",
        ),
        RuntimeField("ta_trailing_stop_enabled", "bool", label="Trailing stop activo",
                     hint="Corta cuando el precio cae X% desde su máximo alcanzado post-entrada"),
        RuntimeField(
            "ta_trailing_stop_pct", "float",
            label="Trailing stop drawdown",
            minimum=0.20, maximum=0.50, step=0.05,
            hint="Ejecutar trailing stop si cae ≥ este % desde el peak (default 0.30 = 30%)",
        ),
        # ── Technical Indicators ───────────────────────────────────────────────
        RuntimeField("ta_use_atr", "bool", label="Usar filtro ATR",
                     hint="Normalizar impulso por ATR — rechaza señales débiles vs volatilidad"),
        RuntimeField(
            "ta_min_normalized_impulse", "float",
            label="Impulso mínimo (ATR)",
            minimum=0.3, maximum=2.0, step=0.1,
            hint="Impulso debe ser ≥ este múltiplo del ATR(14) — default 0.8 = 80%",
        ),
        RuntimeField("ta_use_rsi", "bool", label="Usar filtro RSI",
                     hint="Evitar comprar UP en sobrecompra o DOWN en sobreventa"),
        RuntimeField(
            "ta_rsi_overbought", "float",
            label="RSI sobrecompra",
            minimum=60.0, maximum=85.0, step=5.0,
            hint="No comprar UP si RSI > este valor (default 75)",
        ),
        RuntimeField(
            "ta_rsi_oversold", "float",
            label="RSI sobreventa",
            minimum=15.0, maximum=40.0, step=5.0,
            hint="No comprar DOWN si RSI < este valor (default 25)",
        ),
        RuntimeField("ta_use_volume", "bool", label="Usar filtro volumen",
                     hint="Confirmar impulso con volumen elevado (experimental)"),
        RuntimeField(
            "ta_min_volume_ratio", "float",
            label="Ratio volumen mínimo",
            minimum=1.0, maximum=3.0, step=0.1,
            hint="Volumen actual debe ser ≥ X veces el promedio (default 1.5)",
        ),
        # ── TWAP-based entry signal (added 2026-09-21) ────────────────────────────
        RuntimeField("ta_use_twap_signal", "bool", label="Usar TWAP rolling para entry",
                     hint="Reemplaza spot por TWAP de 60s como referencia para itm_pct y filtro ATR"),
        RuntimeField(
            "ta_twap_lookback_sec", "int",
            label="TWAP lookback (s)",
            minimum=10, maximum=120, step=5,
            hint="Ventana en segundos para el TWAP rolling (default 60)",
        ),
        # ── Profit-Lock completion (added 2026-09-21) ────────────────────────────
        RuntimeField("ta_profit_lock_enabled", "bool", label="Profit-Lock activo",
                     hint="Tras N segundos con primera pata en ganancia, fuerza cerrar par si suma ≤ cap"),
        RuntimeField(
            "ta_profit_lock_min_secs", "int",
            label="Profit-Lock espera (s)",
            minimum=10, maximum=120, step=5,
            hint="Segundos desde que la primera pata quedó half_open antes de forzar (default 30)",
        ),
        RuntimeField(
            "ta_profit_lock_cap", "float",
            label="Profit-Lock cap",
            minimum=0.80, maximum=1.00, step=0.01,
            hint="Suma máxima permitida para profit-lock (default 1.00, más permisivo que Path A)",
        ),
        # ── Martingale hedge (added 2026-09-21) ──────────────────────────────────
        RuntimeField("ta_mart_hedge_enabled", "bool", label="Mart-Hedge activo",
                     hint="Si primera pata en pérdidas tras Ns, compra lado opuesto con qty × mult × (1+loss_pct). PELIGROSO en tendencia."),
        RuntimeField(
            "ta_mart_hedge_min_secs", "int",
            label="Mart-Hedge espera (s)",
            minimum=10, maximum=120, step=5,
            hint="Segundos desde half_open antes de evaluar mart-hedge (default 30)",
        ),
        RuntimeField(
            "ta_mart_hedge_mult", "float",
            label="Mart-Hedge multiplicador",
            minimum=1.5, maximum=5.0, step=0.5,
            hint="Multiplicador de qty: nueva_qty = first_shares × mult × (1 + loss_pct) (default 2.0)",
        ),
        RuntimeField(
            "ta_mart_hedge_max_rounds", "int",
            label="Mart-Hedge rondas máx",
            minimum=1, maximum=5, step=1,
            hint="Cap de rondas consecutivas (default 3). Después, aplican stops normales.",
        ),
    )
)
