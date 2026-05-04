"""Market Making with Deterministic Strategy.

Places BUY orders on BOTH sides (UP and DOWN) at the current market price
(capped at mm_max_price) when mm_last_seconds remain in the window.

Edge: near expiry, ~85% directional certainty pushes one side toward $1 and
the other toward $0. Placing limit buys on BOTH sides simultaneously gives
a fill on the high-probability side at a favorable price. The low-probability
side's order will either not fill (price too far from limit) or result in a
small loss offset by the winner's profit.
"""
from __future__ import annotations

import time
from typing import Optional

from . import logger
from .state import Trade


class MarketMakerStrategy:
    def __init__(self, cfg, state) -> None:
        self.cfg = cfg
        self.state = state
        self._client = None
        if cfg.is_real and cfg.has_credentials:
            self._client = self._build_client()

    # ----- public interface -----

    def run_for_window(self, tokens) -> None:
        """Block until the MM strategy for this window completes."""
        from . import logger as _logger
        _logger.set_context(self.state)  # route logs in this thread to the correct market
        window_ends = tokens.window_ts + 300
        mm_seconds = self.state.mm_last_seconds
        entry_time = window_ends - mm_seconds

        while True:
            now = time.time()
            if now >= window_ends:
                return
            if now >= entry_time:
                break
            remaining = entry_time - now
            logger.transient(
                f"MM  {tokens.slug}  ⏳ esperando ventana MM  "
                f"entry_in={int(remaining)}s  ttl={int(window_ends - now)}s"
            )
            time.sleep(min(remaining, 1.0))

        if self.state.count_mm_trades_for_window(tokens.slug) > 0:
            logger.warn(f"MM  {tokens.slug}  ya tiene trades — omitiendo")
            return

        mm_max_price = self.state.mm_max_price
        mm_shares    = self.state.mm_shares

        logger.info(
            f"MM  {tokens.slug}  ejecutando ambos lados  "
            f"max_price={mm_max_price:.2f}  shares={mm_shares:.2f}  "
            f"ttl={int(window_ends - time.time())}s",
            icon="🏦",
        )

        # Read both prices atomically once before iterating sides
        up_mid, down_mid = self.state.get_prices()
        up_ask, down_ask = self.state.get_asks()

        for side, token_id in [
            ("UP",   tokens.up_token_id),
            ("DOWN", tokens.down_token_id),
        ]:
            current_mid  = up_mid  if side == "UP" else down_mid
            current_ask  = up_ask  if side == "UP" else down_ask
            current_price = current_mid  # mid used for filter/display

            if current_price is None:
                logger.warn(f"MM  {tokens.slug}  {side} sin precio — omitiendo lado")
                continue

            if current_price < 0.01:
                logger.warn(
                    f"MM  {tokens.slug}  {side} precio demasiado bajo "
                    f"({current_price:.4f}) — omitiendo lado"
                )
                continue

            if current_price > mm_max_price:
                logger.warn(
                    f"MM  {tokens.slug}  {side} precio {current_price:.4f} "
                    f"> máximo {mm_max_price:.2f} — omitiendo lado",
                    icon="🚫",
                )
                continue

            # Use ask for actual execution cost; fall back to mid if unavailable
            exec_price = round(current_ask or current_price, 4)
            shares = mm_shares
            cost = round(shares * exec_price, 4)

            logger.ok(
                f"MM BUY  side={side}  mid={current_price:.4f}  ask={exec_price:.4f}  "
                f"shares={shares:.2f}  cost=${cost:.4f}",
                icon="🏦",
            )

            order_id: Optional[str] = None
            note = ""
            is_real_now = self.state.mode == "real"

            if is_real_now and self._client is not None:
                try:
                    order_id, note = self._place_real_order(token_id, shares, exec_price)
                except Exception as exc:
                    logger.err(f"MM orden falló {side}: {exc}")
                    note = f"MM orden falló: {exc}"
            elif is_real_now and self._client is None:
                logger.err("MM modo real: cliente CLOB no inicializado — reiniciar bot")
                return
            else:
                note = f"MM paper @ {exec_price:.4f}"

            trade = Trade(
                id=0,
                window_slug=tokens.slug,
                window_ts=tokens.window_ts,
                side=side,
                token_id=token_id,
                price=exec_price,
                shares=shares,
                cost=cost,
                mode=self.state.mode,
                opened_at=time.time(),
                order_id=order_id,
                note=note,
                is_hedge=False,
                strategy="mm",
            )
            self.state.add_trade(trade)

        self.state.set_status("mm_placed", f"MM {tokens.slug} — ambos lados colocados")

    # ----- real-order plumbing -----

    def _build_client(self):
        try:
            from py_clob_client.client import ClobClient
        except Exception as exc:
            logger.err(f"MM: py_clob_client no disponible: {exc}")
            return None
        try:
            client = ClobClient(
                self.cfg.clob_host,
                key=self.cfg.private_key,
                chain_id=self.cfg.chain_id,
                signature_type=self.cfg.signature_type,
                funder=self.cfg.proxy_wallet,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            logger.ok("MM: cliente CLOB autenticado", icon="🔑")
            return client
        except Exception as exc:
            logger.err(f"MM: autenticación CLOB falló: {exc}")
            return None

    def _place_real_order(self, token_id: str, shares: float, price: float):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        order = self._client.create_order(
            OrderArgs(price=price, size=shares, side=BUY, token_id=token_id)
        )
        resp = self._client.post_order(order, OrderType.GTC)
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        return str(order_id) if order_id else None, "MM real order placed"
