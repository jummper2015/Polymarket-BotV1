"""Temporal Arbitrage strategy — Fase B.

Unlike Box Builder (simultaneous maker bids), Temporal Arb waits for a
directional price extreme on one side, buys it cheaply as a **taker**, then
waits for the opposite side to become cheap too when BTC reverts.

Target pair cost: ≤ ta_complete_cap (default 0.82 vs Box Builder's 0.94).
Because the legs are bought at two different market states the locked profit
is often 15–25¢ instead of 6¢ — but there is directional exposure between
the two fills.

State machine (per window per symbol):

  IDLE
    └─ ask[side] ≤ ta_cheap_threshold  →  BUY taker  →  HALF_OPEN

  HALF_OPEN  (first leg recorded as open trade, waiting for second)
    ├─ second_ask ≤ ta_complete_cap − first_px  →  BUY taker  →  COMPLETE
    └─ secs ≤ ta_bailout_sec  →  CLOSED  (first leg holds until resolution)

  COMPLETE   hold passively; both legs resolve via normal resolution path
  CLOSED     nothing more to do this window (skip or bailout)

Bailout behaviour (user choice): the first leg stays in the DB as an open
trade and is settled normally at window end.  If direction was right → win,
otherwise → loss.  No forced cut is attempted.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from .base import StrategyContext, StrategyDescriptor
from ..runtime_field import RuntimeField

# ── defaults ──────────────────────────────────────────────────────────────────
CHEAP_THRESHOLD_DEFAULT = 0.35    # buy first leg when ask ≤ this
COMPLETE_CAP_DEFAULT    = 0.82    # max total pair cost to accept
SHARES_PER_LEG_DEFAULT  = 5.0
ENTRY_CUTOFF_SEC_DEFAULT = 150.0  # don't start a new pair this late in window
BAILOUT_SEC_DEFAULT      = 60.0   # stop waiting for 2nd leg when ≤ this secs left
CANCEL_ALL_SEC_DEFAULT   = 10.0   # T-10 (no resting orders to cancel, kept for symmetry)

# ── per-window state ──────────────────────────────────────────────────────────

@dataclass
class _TAWindow:
    window_ts:       Optional[int]   = None
    phase:           str             = "idle"   # idle | half_open | complete | closed
    first_side:      Optional[str]   = None     # "UP" | "DOWN"
    first_tok:       Optional[str]   = None
    first_px:        Optional[float] = None
    logged_bailout:  bool            = False
    logged_complete: bool            = False


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

def find_cheap_side(
    ask_up: Optional[float],
    ask_dn: Optional[float],
    threshold: float,
) -> tuple[Optional[str], Optional[float]]:
    """Return (side, ask) of the cheapest side at or below threshold.

    If both qualify, pick the cheaper one first — maximises room left in the
    complete cap for the second leg.  Returns (None, None) if nothing qualifies.
    """
    up_ok = ask_up is not None and ask_up <= threshold
    dn_ok = ask_dn is not None and ask_dn <= threshold

    if up_ok and dn_ok:
        return ("UP", ask_up) if ask_up <= ask_dn else ("DOWN", ask_dn)
    if up_ok:
        return "UP", ask_up
    if dn_ok:
        return "DOWN", ask_dn
    return None, None


def second_leg_worthwhile(first_px: float, second_ask: float, cap: float) -> bool:
    """True when buying the second leg keeps total pair cost ≤ cap."""
    return round(first_px + second_ask, 4) <= cap


# ── observe (state machine tick) ─────────────────────────────────────────────

def _observe(ctx: StrategyContext) -> None:
    """Temporal-Arb tick, called every OBSERVE_TICK_SECONDS during the window."""
    from .. import logger

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
    threshold = float(getattr(state, "ta_cheap_threshold",  CHEAP_THRESHOLD_DEFAULT))
    cap       = float(getattr(state, "ta_complete_cap",     COMPLETE_CAP_DEFAULT))
    shares    = float(getattr(state, "ta_shares_per_leg",   SHARES_PER_LEG_DEFAULT))
    q_cut     = float(getattr(state, "ta_entry_cutoff_sec", ENTRY_CUTOFF_SEC_DEFAULT))
    bail_sec  = float(getattr(state, "ta_bailout_sec",      BAILOUT_SEC_DEFAULT))
    c_all     = float(getattr(state, "ta_cancel_all_sec",   CANCEL_ALL_SEC_DEFAULT))

    # ── terminal states ───────────────────────────────────────────────────────
    if ta.phase in ("complete", "closed"):
        return

    # ── T-10: nothing to cancel (all taker orders fill or fail immediately) ──
    if secs <= c_all:
        return

    ask_up, ask_dn = state.get_asks()

    # ── IDLE: look for a cheap first leg ─────────────────────────────────────
    if ta.phase == "idle":
        if secs < q_cut:
            ta.phase = "closed"
            state.record_skip("TA_SKIP_LATE")
            logger.info(
                f"[TA] SKIP_LATE — ventana ya en curso ({300 - secs:.0f}s)", icon="⏭"
            )
            return

        if ask_up is None or ask_dn is None:
            return  # book not ready yet

        side, px = find_cheap_side(ask_up, ask_dn, threshold)
        if side is None:
            return  # nothing cheap enough this tick

        tok = tokens.up_token_id if side == "UP" else tokens.down_token_id

        logger.info(
            f"[TA] 🎯 primera pata  {side} @ {px:.3f}"
            f"  threshold={threshold:.2f}  left={secs:.0f}s",
            icon="🎯",
        )

        oid = trader._place_taker_order(tok, "BUY", px, shares)
        if not oid:
            logger.warn("[TA] orden taker rechazada para primera pata", icon="⚠")
            return

        ta.first_side = side
        ta.first_tok  = tok
        ta.first_px   = px
        ta.phase      = "half_open"

        # Taker orders fill immediately (or fail).  Record the position now.
        trader._record_box_fill(
            tokens, side, tok, px, shares, strategy="temporal_arb"
        )
        state.record_observation("TA_FIRST_LEG")
        return

    # ── HALF_OPEN: wait for a cheap second leg ────────────────────────────────
    if ta.phase == "half_open":
        second_side = "DOWN" if ta.first_side == "UP" else "UP"
        second_ask  = ask_dn if second_side == "DOWN" else ask_up

        if (
            second_ask is not None
            and ta.first_px is not None
            and second_leg_worthwhile(ta.first_px, second_ask, cap)
        ):
            tok2 = tokens.up_token_id if second_side == "UP" else tokens.down_token_id
            oid2 = trader._place_taker_order(tok2, "BUY", second_ask, shares)
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
                    tokens, second_side, tok2, second_ask, shares,
                    strategy="temporal_arb",
                )
                ta.logged_complete = True
                ta.phase = "complete"
                state.record_observation("TA_COMPLETE")
                return

        # Bailout: time's up — first leg stays open and resolves normally
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


# ── descriptor ────────────────────────────────────────────────────────────────

DESCRIPTOR = StrategyDescriptor(
    id="temporal_arb",
    name="Temporal Arb",
    description=(
        "Compra cada pata cuando está en su precio extremo (taker). "
        "Par objetivo ≤ 0.82¢ vs dos estados de mercado distintos."
    ),
    notes=(
        "Diferencia vs Box Builder: órdenes taker (inmediatas, no maker pasivo). "
        "Entre patas existe exposición direccional. "
        "Bailout = primera pata se resuelve normalmente como trade win/loss."
    ),
    evaluate=lambda ctx: [],
    observe=_observe,
    is_enabled=lambda state: bool(getattr(state, "ta_enabled", False)),
    enabled_when={"field": "ta_enabled", "values": [True]},
    priority=80,
    params=(
        RuntimeField("ta_enabled", "bool", label="Temporal Arb activo"),
        RuntimeField(
            "ta_cheap_threshold", "float",
            label="Threshold pata barata",
            minimum=0.10, maximum=0.45, step=0.01,
            hint="Compra el lado cuando ask ≤ este valor (default 0.35)",
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
        ),
        RuntimeField(
            "ta_entry_cutoff_sec", "float",
            label="Cutoff entrada (s)",
            minimum=60.0, maximum=240.0, step=10.0,
            hint="No inicia par si la ventana lleva más de (300-cutoff) segundos",
        ),
        RuntimeField(
            "ta_bailout_sec", "float",
            label="Bailout segunda pata (s restantes)",
            minimum=20.0, maximum=120.0, step=5.0,
            hint="Deja de esperar la segunda pata cuando quedan ≤ este tiempo",
        ),
    ),
)
