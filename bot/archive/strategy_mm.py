"""Box Builder Market Making Strategy.

Two-sided post-only maker: quotes bids on BOTH UP and DOWN in the FIRST HALF
of each 5-minute window with bid_UP + bid_DOWN ≤ mm_bid_sum_cap (default 0.94).

When both legs fill, the box is complete — it redeems for exactly $1.00/pair at
resolution regardless of which side wins. The spread (1.00 - box_cost) is
locked profit.

Evidence (Moon Dev's real logs — nothing invented):
  ✅ Early quoting fills: 57% fill rate at T-240
  ❌ Late deep bids never fill: armed 35x at T-35 → ZERO fills
  ❌ Chasing the book → 249 post-only "crosses book" rejects → quotes stay STATIC

Phases (T = seconds remaining in the 5-minute window):
  ARM        T > cutoff  : two-sided post-only bids, reprice ≤every 20s if >2c behind
  COMPLETE   T > 90      : one leg filled → raise maker bid / lift taker to complete box
  BAILOUT    T ≤ 90      : stranded leg → cut at best bid
  CANCEL     T ≤ 10      : cancel ALL resting orders before window rollover

Box payoff (both legs fill at bid_UP=p1, bid_DOWN=p2):
  cost = p1 + p2 ≤ 0.94  →  locked PnL = shares × (1.00 − cost) ≥ 0.06/share

Arm gate: ask_UP + ask_DOWN ≥ mm_arm_spread_sum (default 1.03)
  If the combined ask sum is < 1.03 the market is already too tight — skip.

Completion cap (after 1st fill at p1):
  Maker: raise other bid to min(best_bid, COMPLETE_MAKER_CAP − p1) [≥ 3c locked]
  Taker: lift the ask if ≤ COMPLETE_TAKER_CAP − p1               [≥ 1c locked]
"""
from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from . import logger
from .state import Trade


# ── type alias ────────────────────────────────────────────────────────────────
Book = Dict[str, Optional[float]]   # {"best_bid": float|None, "best_ask": float|None}


class BoxBuilderStrategy:
    """Two-sided post-only maker with completion ladder and T-90 cut bailout."""

    # ── timing / price constants (from the real Box Builder logs) ─────────────
    REPRICE_INTERVAL   = 20.0   # seconds between reprices (chasing = 249 rejects)
    REPRICE_BEHIND     = 0.02   # only reprice if > 2c behind best bid
    COMPLETE_MAKER_CAP = 0.97   # after 1st fill at p1: other bid ≤ 0.97 − p1
    COMPLETE_TAKER_CAP = 0.99   # lift ask if ≤ 0.99 − p1 (guaranteed ≥ 1c box)
    BAILOUT_SEC        = 90     # stranded-leg bailout at T-90
    CANCEL_SEC         = 10     # cancel all resting orders at T-10
    POLL_SEC           = 3.0    # main loop poll interval (seconds)
    PRICE_TICK         = 0.01   # 1c ticks

    def __init__(self, cfg, state) -> None:
        self.cfg   = cfg
        self.state = state
        self._client = None
        if cfg.is_real and cfg.has_credentials:
            self._client = self._build_client()

    # ── public entry point ────────────────────────────────────────────────────

    def run_for_window(self, tokens) -> None:
        """Run the full Box Builder cycle for one 5-minute window."""
        from . import logger as _logger
        _logger.set_context(self.state)

        window_ends = tokens.window_ts + 300
        sym = tokens.slug

        if self.state.count_mm_trades_for_window(sym) > 0:
            logger.warn(f"BOX  {sym}  ya tiene trades MM — omitiendo")
            return

        # ── per-leg mutable state (mirroring BoxBuilderBot.legs) ──────────────
        legs: Dict[str, dict] = {
            "UP":   _new_leg(tokens.up_token_id),
            "DOWN": _new_leg(tokens.down_token_id),
        }

        armed          : Optional[bool] = None   # None=undecided, True/False after check
        skip_reason    : str            = ""
        cancelled_all  : bool           = False
        bailout_done   : bool           = False
        last_reprice   : float          = 0.0
        compl_last_raise: float         = 0.0

        logger.info(
            f"BOX  {sym}  iniciando  "
            f"arm_gate={self.state.mm_arm_spread_sum:.2f}  "
            f"cap={self.state.mm_bid_sum_cap:.2f}  "
            f"shares={self.state.mm_shares_per_leg}  "
            f"cutoff=T-{self.state.mm_quote_cutoff_sec}s",
            icon="📦",
        )

        # ── main window loop ──────────────────────────────────────────────────
        while True:
            now            = time.time()
            time_remaining = window_ends - now

            if time_remaining <= 0:
                break

            books = self._get_books()
            self._check_fills(legs, books)

            filled   = [s for s in ("UP", "DOWN") if legs[s]["fill_px"] is not None]
            n_filled = len(filled)
            both     = n_filled == 2

            # ── T-10: cancel everything, never carry into rollover ─────────────
            if time_remaining <= self.CANCEL_SEC:
                if not cancelled_all:
                    cancelled_all = True
                    self._cancel_all_resting(legs)
                time.sleep(1.0)
                continue

            # ── arming decision (one shot at window open) ─────────────────────
            if armed is None:
                up_book = books.get("UP")
                dn_book = books.get("DOWN")
                if not up_book or not dn_book:
                    logger.transient(
                        f"BOX  {sym}  ⏳ esperando libro de precios…  T-{int(time_remaining)}s"
                    )
                    time.sleep(self.POLL_SEC)
                    continue
                if time_remaining < self.state.mm_quote_cutoff_sec:
                    armed, skip_reason = False, "SKIP_PAST_CUTOFF"
                    logger.warn(
                        f"BOX  {sym}  libro llegó tarde (T-{int(time_remaining)}s < cutoff), omitiendo"
                    )
                    time.sleep(self.POLL_SEC)
                    continue
                armed, skip_reason, last_reprice = self._try_arm(books, legs)
                time.sleep(self.POLL_SEC)
                continue

            if not armed:
                logger.transient(
                    f"BOX  {sym}  ⏭ omitido ({skip_reason})  T-{int(time_remaining)}s"
                )
                time.sleep(self.POLL_SEC)
                continue

            # ── box complete: hold and wait for resolution 🔒 ─────────────────
            if both:
                cost = legs["UP"]["fill_px"] + legs["DOWN"]["fill_px"]
                sh   = legs["UP"]["fill_shares"] or self.state.mm_shares_per_leg
                logger.transient(
                    f"BOX  {sym}  📦 COMPLETO  cost={cost:.2f}  "
                    f"locked≥${(1.0 - cost) * sh:.2f}  T-{int(time_remaining)}s"
                )
                time.sleep(self.POLL_SEC)
                continue

            # ── one leg filled: completion ladder or T-90 bailout ─────────────
            if n_filled == 1:
                if time_remaining <= self.BAILOUT_SEC and not bailout_done:
                    bailout_done = True
                    self._run_bailout(books, legs, filled[0])
                elif not bailout_done:
                    compl_last_raise = self._work_completion(
                        books, legs, filled[0], compl_last_raise
                    )
                time.sleep(self.POLL_SEC)
                continue

            # ── no fills yet: static two-sided quotes, first half only ─────────
            if time_remaining >= self.state.mm_quote_cutoff_sec:
                last_reprice = self._maybe_reprice(books, legs, last_reprice)
                up_px = legs["UP"]["order_px"]
                dn_px = legs["DOWN"]["order_px"]
                logger.transient(
                    f"BOX  {sym}  🪑 UP={up_px if up_px else '--'}  "
                    f"DN={dn_px if dn_px else '--'}  "
                    f"cap={self.state.mm_bid_sum_cap:.2f}  T-{int(time_remaining)}s"
                )
            else:
                logger.transient(
                    f"BOX  {sym}  ⏳ past T-{self.state.mm_quote_cutoff_sec}s, "
                    f"cotizaciones congeladas, esperando fills…  T-{int(time_remaining)}s"
                )
            time.sleep(self.POLL_SEC)

        # ── window closed: final cancel + register trades ─────────────────────
        if not cancelled_all:
            self._cancel_all_resting(legs)
        self._register_trades(tokens, legs)

    # ── book snapshot from live WebSocket prices ──────────────────────────────

    def _get_books(self) -> Dict[str, Book]:
        """Return best bid/ask per side from the WebSocket price state (no HTTP)."""
        with self.state._lock:
            up_bid  = self.state.last_up_bid
            up_ask  = self.state.last_up_ask
            dn_bid  = self.state.last_down_bid
            dn_ask  = self.state.last_down_ask
        books: Dict[str, Book] = {}
        if up_bid and up_ask and up_bid > 0 and up_ask > 0:
            books["UP"]   = {"best_bid": up_bid, "best_ask": up_ask}
        if dn_bid and dn_ask and dn_bid > 0 and dn_ask > 0:
            books["DOWN"] = {"best_bid": dn_bid, "best_ask": dn_ask}
        return books

    # ── fill detection ────────────────────────────────────────────────────────

    def _check_fills(self, legs: dict, books: Dict[str, Book]) -> None:
        """Detect fills. Paper: ask crosses our bid. Real: client.get_order."""
        is_real = self.state.mode == "real"
        shares  = self.state.mm_shares_per_leg
        for side in ("UP", "DOWN"):
            leg = legs[side]
            if leg["fill_px"] is not None or leg["order_id"] is None:
                continue
            if not is_real:
                book = books.get(side)
                if book and book["best_ask"] is not None and leg["order_px"] is not None:
                    if book["best_ask"] <= leg["order_px"] + 1e-9:
                        self._mark_filled(leg, side, leg["order_px"], shares)
            else:
                matched = self._get_order_matched(leg["order_id"])
                if matched and matched > 0:
                    self._mark_filled(leg, side, leg["order_px"], matched)

    def _mark_filled(self, leg: dict, side: str, px: float, shares: float) -> None:
        leg["fill_px"]     = round(float(px), 2)
        leg["fill_shares"] = float(shares)
        leg["order_id"]    = None
        leg["order_px"]    = None
        left = self.COMPLETE_TAKER_CAP - leg["fill_px"]
        logger.ok(
            f"BOX  ✅ {side} leg filled @ {leg['fill_px']:.2f}  "
            f"(otro lado target ≤ {left:.2f})",
            icon="🧱",
        )

    def _get_order_matched(self, order_id: str) -> Optional[float]:
        if not order_id or self._client is None:
            return None
        try:
            order = self._client.get_order(order_id)
            if not order:
                return None
            if isinstance(order, dict):
                return float(order.get("size_matched", 0) or 0)
            return float(getattr(order, "size_matched", 0) or 0)
        except Exception as exc:
            logger.warn(f"BOX  get_order failed: {exc}")
            return None

    # ── bid math ──────────────────────────────────────────────────────────────

    def _cap_bids(self, bid_up: float, bid_dn: float) -> Tuple[float, float]:
        """Symmetrically back off bids in 1c ticks until sum ≤ mm_bid_sum_cap."""
        cap = self.state.mm_bid_sum_cap
        bu, bd = round(bid_up, 2), round(bid_dn, 2)
        while round(bu + bd, 2) > cap and (bu > self.PRICE_TICK or bd > self.PRICE_TICK):
            if bu >= bd:
                bu = round(bu - self.PRICE_TICK, 2)
            else:
                bd = round(bd - self.PRICE_TICK, 2)
        return max(bu, self.PRICE_TICK), max(bd, self.PRICE_TICK)

    # ── arming ────────────────────────────────────────────────────────────────

    def _try_arm(
        self, books: Dict[str, Book], legs: dict
    ) -> Tuple[bool, str, float]:
        """One-shot arming decision. Returns (armed, skip_reason, last_reprice)."""
        up_book = books["UP"]
        dn_book = books["DOWN"]
        arm_gate = self.state.mm_arm_spread_sum
        shares   = self.state.mm_shares_per_leg

        spread_sum = round(up_book["best_ask"] + dn_book["best_ask"], 2)

        if spread_sum < arm_gate:
            logger.warn(
                f"BOX  SKIP — spread_sum {spread_sum:.2f} < {arm_gate:.2f}  "
                f"(libro demasiado ajustado para hacer box)"
            )
            return False, "SKIP_NARROW", 0.0

        bid_up, bid_dn = self._cap_bids(up_book["best_bid"], dn_book["best_bid"])

        # ── bid-below-ask guard ───────────────────────────────────────────────
        # Post-only orders are rejected the instant bid ≥ best_ask.
        # Clamp each bid to strictly one tick below its respective ask so we
        # never trigger a cross-book reject on either leg.
        bid_up = max(self.PRICE_TICK, min(bid_up, round(up_book["best_ask"] - self.PRICE_TICK, 2)))
        bid_dn = max(self.PRICE_TICK, min(bid_dn, round(dn_book["best_ask"] - self.PRICE_TICK, 2)))

        # After clamping, re-check sum cap (clamping can only reduce, so it
        # stays ≤ cap — but verify defensively).
        if round(bid_up + bid_dn, 2) > self.state.mm_bid_sum_cap:
            bid_up, bid_dn = self._cap_bids(bid_up, bid_dn)

        logger.info(
            f"BOX  ARMAR  UP bid={bid_up:.2f}  DN bid={bid_dn:.2f}  "
            f"sum={bid_up + bid_dn:.2f}  spread_sum={spread_sum:.2f}  shares={shares}",
            icon="📦",
        )

        ok_up = self._place_leg_bid(legs["UP"],  bid_up, shares)
        ok_dn = self._place_leg_bid(legs["DOWN"], bid_dn, shares)

        # ── BOTH legs must succeed — partial arm is worse than no arm ─────────
        # If only one order landed, the bot holds a naked position with no
        # complementary hedge; cancel the placed leg and abort this window.
        if not ok_up or not ok_dn:
            if ok_up:
                self._cancel_leg(legs["UP"])
                logger.warn("BOX  DOWN leg falló — cancelando UP y abortando arm")
            elif ok_dn:
                self._cancel_leg(legs["DOWN"])
                logger.warn("BOX  UP leg falló — cancelando DOWN y abortando arm")
            else:
                logger.warn("BOX  ambas piernas fallaron — abortando arm")
            return False, "SKIP_ORDER_FAIL", 0.0

        logger.ok(
            f"BOX  ambas piernas colocadas ✓  UP@{bid_up:.2f}  DN@{bid_dn:.2f}  "
            f"box_cost={bid_up + bid_dn:.2f}  locked≥${round(1.0 - bid_up - bid_dn, 2):.2f}/share",
            icon="📦",
        )
        return True, "", time.time()

    # ── static reprice ────────────────────────────────────────────────────────

    def _maybe_reprice(
        self, books: Dict[str, Book], legs: dict, last_reprice: float
    ) -> float:
        """Reprice ≤ once per REPRICE_INTERVAL, only if > 2c behind best bid."""
        now = time.time()
        if now - last_reprice < self.REPRICE_INTERVAL:
            return last_reprice

        cap    = self.state.mm_bid_sum_cap
        shares = self.state.mm_shares_per_leg
        repriced = False

        for side in ("UP", "DOWN"):
            leg   = legs[side]
            other = legs["DOWN" if side == "UP" else "UP"]
            book  = books.get(side)

            if leg["fill_px"] is not None or leg["order_px"] is None or not book:
                continue

            behind = (book["best_bid"] or 0) - leg["order_px"]
            if behind <= self.REPRICE_BEHIND:
                continue

            other_px = other["order_px"] if other["order_px"] is not None else 0.0
            new_px   = round(min(book["best_bid"], cap - other_px), 2)
            if new_px <= leg["order_px"]:
                continue   # cap won't let us move up — stay static

            logger.info(
                f"BOX  REPRICE {side}  "
                f"{leg['order_px']:.2f} → {new_px:.2f}  "
                f"(era {behind:.2f} detrás del best bid)"
            )
            self._cancel_leg(leg)
            self._place_leg_bid(leg, new_px, shares)
            repriced = True

        return now if repriced else last_reprice

    # ── completion ladder ─────────────────────────────────────────────────────

    def _work_completion(
        self,
        books: Dict[str, Book],
        legs: dict,
        filled_side: str,
        last_raise: float,
    ) -> float:
        """One leg filled — work the other to complete the box.

        Taker path (checked every poll):
          If best_ask ≤ COMPLETE_TAKER_CAP − p1, LIFT IT via marketable GTC.
          (Never use FAK/FOK — they 400 for amounts < $1 on Polymarket.)

        Maker path (rate-limited like every other quote):
          Raise the bid to min(best_bid, COMPLETE_MAKER_CAP − p1).
        """
        other_side = "DOWN" if filled_side == "UP" else "UP"
        p1     = legs[filled_side]["fill_px"]
        shares = legs[filled_side]["fill_shares"] or self.state.mm_shares_per_leg
        leg    = legs[other_side]
        book   = books.get(other_side)
        if not book:
            return last_raise

        taker_cap = round(self.COMPLETE_TAKER_CAP - p1, 2)

        # taker path — checked every poll
        if book["best_ask"] is not None and book["best_ask"] <= taker_cap:
            logger.ok(
                f"BOX  LIFT {other_side}  ask={book['best_ask']:.2f} ≤ "
                f"taker_cap={taker_cap:.2f}  → COMPLETANDO box",
                icon="🏋️",
            )
            self._cancel_leg(leg)
            self._place_taker_order(leg["token_id"], "BUY", book["best_ask"], shares)
            if self.state.mode != "real":
                self._mark_filled(leg, other_side, book["best_ask"], shares)
            return last_raise

        # maker raise path — rate-limited
        now = time.time()
        if now - last_raise < self.REPRICE_INTERVAL and leg["order_px"] is not None:
            return last_raise

        maker_cap = round(self.COMPLETE_MAKER_CAP - p1, 2)
        best_bid  = book["best_bid"] or maker_cap
        target    = round(min(best_bid, maker_cap), 2)
        if leg["order_px"] is not None and target <= leg["order_px"]:
            return last_raise   # only ever RAISE toward completion

        logger.info(
            f"BOX  COMPLETION LADDER  {other_side} bid → {target:.2f}  "
            f"(maker_cap={maker_cap:.2f})",
            icon="🪜",
        )
        self._cancel_leg(leg)
        self._place_leg_bid(leg, target, shares)
        return now

    # ── T-90 bailout ──────────────────────────────────────────────────────────

    def _run_bailout(self, books: Dict[str, Book], legs: dict, filled_side: str) -> None:
        """Stranded leg at T-90: cancel the other side's resting bid, cut at best bid.

        No COA / external API used — a plain cut at the current bid is the safe
        default without Binance/Coinbase data.
        """
        other_side = "DOWN" if filled_side == "UP" else "UP"
        leg        = legs[filled_side]
        oleg       = legs[other_side]
        shares     = leg["fill_shares"] or self.state.mm_shares_per_leg

        # pull the unfilled leg's resting bid first
        if oleg["order_id"] is not None:
            self._cancel_leg(oleg)

        book = books.get(filled_side)
        if not book or not book.get("best_bid"):
            logger.warn(f"BOX  BAILOUT  sin libro para {filled_side} — manteniendo")
            return

        cut_px = book["best_bid"]
        logger.warn(
            f"BOX  BAILOUT T-90  {filled_side} varado @ {leg['fill_px']:.2f}  "
            f"→ CORTANDO al bid {cut_px:.2f}",
            icon="⚠️",
        )
        self._place_taker_order(leg["token_id"], "SELL", cut_px, shares)
        leg["cut_px"] = cut_px

    # ── order helpers ─────────────────────────────────────────────────────────

    def _place_leg_bid(self, leg: dict, price: float, shares: float) -> bool:
        """Post-only GTC bid. Retries 1c lower on cross-book reject (up to 3×)."""
        is_real = self.state.mode == "real"
        if not is_real:
            leg["order_id"] = "paper"
            leg["order_px"] = round(price, 2)
            logger.info(
                f"BOX  PAPER bid {leg['token_id'][:12]}…  "
                f"@ {price:.2f} ×{shares}"
            )
            return True

        if self._client is None:
            logger.err("BOX  modo real: cliente CLOB no inicializado")
            return False

        try:
            from py_clob_client_v2 import OrderArgs, OrderType, Side

            px = round(price, 2)
            for _attempt in range(3):
                if px < self.PRICE_TICK:
                    return False
                order_args = OrderArgs(
                    token_id=str(leg["token_id"]),
                    price=float(px),
                    size=float(shares),
                    side=Side.BUY,
                )
                try:
                    resp = self._client.create_and_post_order(
                        order_args, order_type=OrderType.GTC, post_only=True
                    )
                except Exception as exc:
                    err = str(exc).lower()
                    if "post-only" in err and "cross" in err:
                        px = round(px - self.PRICE_TICK, 2)
                        logger.warn(f"BOX  crosses book, re-precio 1c menor → {px:.2f}")
                        continue
                    logger.err(f"BOX  bid falló: {exc}")
                    return False

                if resp and isinstance(resp, dict) and resp.get("orderID"):
                    leg["order_id"] = resp["orderID"]
                    leg["order_px"] = px
                    logger.ok(
                        f"BOX  bid {leg['token_id'][:12]}…  "
                        f"@ {px:.2f} ×{shares}  id={resp['orderID'][:8]}…"
                    )
                    return True
                # response-body cross-book signal
                if resp and "cross" in str(resp).lower():
                    px = round(px - self.PRICE_TICK, 2)
                    logger.warn(f"BOX  resp indica crosses, re-precio → {px:.2f}")
                    continue
                logger.warn(f"BOX  bid rechazado: {str(resp)[:80]}")
                return False
        except Exception as exc:
            logger.err(f"BOX  _place_leg_bid error: {exc}")

        return False

    def _place_taker_order(
        self, token_id: str, side: str, price: float, shares: float
    ) -> None:
        """Marketable GTC order (post_only=False) for completion and bailout.

        Uses GTC, NOT FAK/FOK — Polymarket 400s marketable FAK/FOK when the
        crossable amount is < $1 (confirmed in real logs).
        """
        is_real = self.state.mode == "real"
        if not is_real:
            logger.info(f"BOX  PAPER taker {side}  @ {price:.2f} ×{shares}")
            return
        if self._client is None:
            logger.err("BOX  modo real: cliente CLOB no inicializado")
            return
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, Side as SideEnum

            sdk_side   = SideEnum.BUY if side.upper() == "BUY" else SideEnum.SELL
            order_args = OrderArgs(
                token_id=str(token_id),
                price=float(price),
                size=float(shares),
                side=sdk_side,
            )
            resp = self._client.create_and_post_order(
                order_args, order_type=OrderType.GTC, post_only=False
            )
            logger.ok(f"BOX  taker {side}  @ {price:.2f}  resp={str(resp)[:60]}")
        except Exception as exc:
            logger.err(f"BOX  taker {side} falló: {exc}")

    def _cancel_leg(self, leg: dict) -> None:
        """Cancel the resting order on a single leg."""
        if leg["order_id"] is None:
            return
        if self.state.mode == "real" and self._client is not None:
            self._cancel_token_orders(leg["token_id"])
        leg["order_id"] = None
        leg["order_px"] = None

    def _cancel_all_resting(self, legs: dict) -> None:
        """Cancel all resting orders on both legs (called at T-10 and window end)."""
        for side in ("UP", "DOWN"):
            leg = legs[side]
            if leg["order_id"] is not None and leg["fill_px"] is None:
                self._cancel_leg(leg)
        logger.info("BOX  todas las órdenes canceladas antes del rollover", icon="🚫")

    def _cancel_token_orders(self, token_id: str) -> None:
        """Cancel ALL resting orders on a token via cancel_market_orders."""
        try:
            from py_clob_client_v2.clob_types import OrderMarketCancelParams

            self._client.cancel_market_orders(
                OrderMarketCancelParams(asset_id=str(token_id))
            )
        except Exception as exc:
            logger.warn(f"BOX  cancel falló: {exc}")

    # ── trade registration ────────────────────────────────────────────────────

    def _register_trades(self, tokens, legs: dict) -> None:
        """Register filled legs as MM trades in state.trades at window end."""
        filled = [s for s in ("UP", "DOWN") if legs[s]["fill_px"] is not None]

        for side in filled:
            leg    = legs[side]
            shares = leg["fill_shares"] or self.state.mm_shares_per_leg
            cost   = round(shares * leg["fill_px"], 4)
            note   = f"box @ {leg['fill_px']:.2f}"
            if leg.get("cut_px") is not None:
                note += f"  cut@{leg['cut_px']:.2f}"

            trade = Trade(
                id=0,
                window_slug=tokens.slug,
                window_ts=tokens.window_ts,
                side=side,
                token_id=leg["token_id"],
                price=leg["fill_px"],
                shares=shares,
                cost=cost,
                mode=self.state.mode,
                opened_at=time.time(),
                order_id=None,
                note=note,
                is_hedge=False,
                strategy="mm",
            )
            self.state.add_trade(trade)

        if len(filled) == 2:
            cost   = legs["UP"]["fill_px"] + legs["DOWN"]["fill_px"]
            sh     = legs["UP"]["fill_shares"] or self.state.mm_shares_per_leg
            locked = round((1.0 - cost) * sh, 2)
            logger.ok(
                f"BOX  COMPLETO  cost/par={cost:.2f}  "
                f"locked_pnl≈${locked:+.2f}/par  "
                f"→ redime $1.00 en resolución sin importar el ganador",
                icon="📦",
            )
        elif len(filled) == 1:
            logger.warn(f"BOX  VARADO  solo {filled[0]} leg llenado — ver nota en trades")
        else:
            logger.info("BOX  sin fills este ciclo")

    # ── CLOB V2 client ────────────────────────────────────────────────────────

    def _build_client(self):
        try:
            from py_clob_client_v2 import ClobClient
        except Exception as exc:
            logger.err(f"BOX: py_clob_client_v2 no disponible: {exc}")
            return None
        try:
            seed = ClobClient(
                host=self.cfg.clob_host,
                chain_id=self.cfg.chain_id,
                key=self.cfg.private_key,
            )
            creds  = seed.create_or_derive_api_key()
            client = ClobClient(
                host=self.cfg.clob_host,
                chain_id=self.cfg.chain_id,
                key=self.cfg.private_key,
                creds=creds,
            )
            logger.ok("BOX  cliente CLOB V2 autenticado (pUSD)", icon="🔑")
            return client
        except Exception as exc:
            logger.err(f"BOX  autenticación CLOB V2 falló: {exc}")
            return None


# ── helpers ───────────────────────────────────────────────────────────────────

# ── backward-compat alias so trader.py import remains unchanged ───────────────
MarketMakerStrategy = BoxBuilderStrategy


def _new_leg(token_id: str) -> dict:
    """Create a fresh per-leg state dict."""
    return {
        "token_id"   : token_id,
        "order_id"   : None,    # active resting order id (or "paper")
        "order_px"   : None,    # price at which we quoted
        "fill_px"    : None,    # price at which we were filled (None = not filled)
        "fill_shares": 0.0,     # shares actually filled
        "cut_px"     : None,    # bailout cut price (if applicable)
    }
