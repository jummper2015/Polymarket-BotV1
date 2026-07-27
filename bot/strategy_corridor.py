"""
Corridor Collector Strategy — BTC 15m + 5m cross-window pair trade.

Adapted from Moon Dev's Corridor Collector v1.0 for the existing bot infrastructure.
No pandas, termcolor, dotenv, CSV logging, or external market discovery.

THE STRUCTURE
  A 15-min window [T, T+900] contains the 5-min window [T+600, T+900] as its
  final third — BOTH resolve off the SAME close P15.

  Buy 15m-LEADER + 5m-OPPOSITE:
      P15 beyond P10   (leader runs)  → $1  (15m leg pays)
      P0 < P15 < P10   (CORRIDOR)     → $2  (BOTH legs pay — the payday)
      P15 beyond P0    (full reversal) → $1  (5m leg saves)

  Floor = $1 guaranteed. Upside = $2 if corridor hit.
  Fair value = 1 + P(corridor). Zone (5-30 bps, ATR ≥ 1×) → P ≈ 41% → fair $1.41.

ENTRY WINDOW: T15+600 → T15+690 (first 90 s of the final 5-min window)
  ✅ Zone gate   : lead 5–30 bps AND lead/ATR14 ≥ 1.0
  ✅ Price gate  : ask15 + ask5 ≤ fair_sum − EDGE (default 0.08)
  ✅ Sanity caps : ask5 ≤ 0.55,  ask15 ≤ 0.93
  ✅ Taker GTC both legs back-to-back (equal shares)
  ✅ Hold BOTH to resolution — the $1 floor IS the stop
  ✅ Kill switch : trailing-30 corridor rate < 20% → pause entries
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

from . import logger
from .state import Trade


# ── P(corridor) table (52-week 1-min BTCUSDT candles) ────────────────────────
#   (lead_bps_lo, lead_bps_hi, p_corridor)
P_CORRIDOR_BINS: List[Tuple[float, float, float]] = [
    (0.0,  2.0,  0.072),
    (2.0,  5.0,  0.219),
    (5.0,  10.0, 0.326),
    (10.0, 15.0, 0.405),
    (15.0, 20.0, 0.440),
    (20.0, 30.0, 0.464),
    (30.0, 50.0, 0.497),
]

# ── timing constants ──────────────────────────────────────────────────────────
WIN15        = 900   # 15-min window seconds
ACTION_START = 600   # action window opens at T15 + 600 s
ACTION_END   = 690   # action window closes at T15 + 690 s


@dataclass
class EntrySignal:
    """A fully-validated corridor entry signal."""
    s15: str           # "UP" or "DOWN" — 15m leader side
    s5: str            # "DOWN" or "UP" — 5m opposite side
    ask15: float       # ask price of the 15m leader token
    ask5: float        # ask price of the 5m opposite token
    lead_bps: float    # BTC move P0→P10 in basis points
    lead_atr: float    # lead_abs / ATR14
    p_corridor: float  # P(corridor) from the table
    fair_sum: float    # 1 + p_corridor


# ── Binance 1-min price helpers ───────────────────────────────────────────────

def get_btc_bar_open(ts: int, max_retries: int = 3) -> Optional[float]:
    """Open price of the 1-min BTCUSDT bar starting exactly at unix `ts`."""
    for attempt in range(max_retries):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "limit": 1,
                    "startTime": ts * 1000,
                    "endTime": (ts + 59) * 1000,
                },
                timeout=10,
            )
            if r.status_code == 200 and r.json():
                return float(r.json()[0][1])  # index 1 = open price
        except Exception as exc:
            if attempt < max_retries - 1:
                time.sleep(2.0)
            else:
                logger.warn(f"[CORR] Binance bar_open({ts}) error: {exc}")
    return None


def get_atr14(ts: int, period: int = 14, max_retries: int = 3) -> Optional[float]:
    """ATR14 (USD) from `period` completed 1-min bars ending at unix `ts`."""
    for attempt in range(max_retries):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "limit": period + 1,
                    "startTime": (ts - (period + 1) * 60) * 1000,
                    "endTime": ts * 1000,
                },
                timeout=10,
            )
            if r.status_code != 200 or not r.json():
                raise ValueError(f"status {r.status_code}")
            k = r.json()
            if len(k) < period + 1:
                raise ValueError(f"only {len(k)} bars returned")
            trs: List[float] = []
            for i in range(1, len(k)):
                hi = float(k[i][2])
                lo = float(k[i][3])
                pc = float(k[i - 1][4])
                trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
            return sum(trs[-period:]) / period
        except Exception as exc:
            if attempt < max_retries - 1:
                time.sleep(2.0)
            else:
                logger.warn(f"[CORR] Binance atr14({ts}) error: {exc}")
    return None


def p_corridor_lookup(lead_bps: float) -> float:
    """Return P(corridor) from the 52-week table."""
    for lo, hi, p in P_CORRIDOR_BINS:
        if lo <= lead_bps < hi:
            return p
    return P_CORRIDOR_BINS[-1][2]


# ── Main strategy class ───────────────────────────────────────────────────────

class CorridorCollectorStrategy:
    """Two-sided cross-timeframe pair trade: BTC 15m leader + 5m opposite.

    Call ``evaluate_entry(...)`` once per evaluation tick in the action window.
    Call ``execute_pair(...)`` when the signal is not None.
    """

    def __init__(self, cfg, state) -> None:
        """
        Args:
            cfg:   bot.Config instance (clob_host, chain_id, private_key, …)
            state: BotState for btc15 (has cc_* config fields + trade registry)
        """
        self.cfg   = cfg
        self.state = state
        self._client = None
        if cfg.is_real and cfg.has_credentials:
            self._client = self._build_client()

    # ── public API ────────────────────────────────────────────────────────────

    def evaluate_entry(
        self,
        T15: int,
        P0: float,
        P10: float,
        atr14: float,
        up_ask_15m: float,
        dn_ask_15m: float,
        up_ask_5m: float,
        dn_ask_5m: float,
    ) -> Optional[EntrySignal]:
        """Run zone + price gates. Returns EntrySignal if all pass, else None."""
        cfg = self.state

        # Kill switch check
        if getattr(cfg, "cc_paused", False):
            logger.warn("[CORR] KILL SWITCH activo — entradas en pausa")
            return None

        # Leader determination (P10 vs P0)
        lead_abs = abs(P10 - P0)
        lead_bps = (lead_abs / P0) * 10_000 if P0 > 0 else 0.0
        lead_atr = lead_abs / atr14 if atr14 > 0 else 0.0
        s15 = "UP"   if P10 >= P0 else "DOWN"
        s5  = "DOWN" if s15 == "UP" else "UP"

        ask15 = up_ask_15m if s15 == "UP" else dn_ask_15m
        ask5  = dn_ask_5m  if s5  == "DOWN" else up_ask_5m

        p_corr   = p_corridor_lookup(lead_bps)
        fair_sum = 1.0 + p_corr
        live_sum = ask15 + ask5

        logger.info(
            f"[CORR] 15m leads {s15}  {lead_bps:.1f}bps ({lead_atr:.1f}×ATR) | "
            f"corridor={p_corr*100:.0f}% fair=${fair_sum:.3f} live=${live_sum:.3f}",
            icon="🌙",
        )

        # Gate 1: zone (lead 5–30 bps AND lead/ATR ≥ 1.0)
        zone_bps = cfg.cc_zone_lead_min <= lead_bps <= cfg.cc_zone_lead_max
        zone_atr = lead_atr >= cfg.cc_zone_min_atr
        if not zone_bps or not zone_atr:
            reason = "ZONE_BPS" if not zone_bps else "ZONE_ATR"
            logger.warn(
                f"[CORR] SKIP ({reason})  lead={lead_bps:.1f}bps "
                f"lead/ATR={lead_atr:.2f}  "
                f"zone=[{cfg.cc_zone_lead_min},{cfg.cc_zone_lead_max}]bps "
                f"atr_min={cfg.cc_zone_min_atr}"
            )
            return None

        # Gate 2: sanity caps
        if ask5 > cfg.cc_ask5_cap:
            logger.warn(f"[CORR] SKIP (ASK5_CAP)  ask5={ask5:.4f} > cap={cfg.cc_ask5_cap}")
            return None
        if ask15 > cfg.cc_ask15_cap:
            logger.warn(f"[CORR] SKIP (ASK15_CAP)  ask15={ask15:.4f} > cap={cfg.cc_ask15_cap}")
            return None

        # Gate 3: price edge
        if live_sum > fair_sum - cfg.cc_edge:
            logger.warn(
                f"[CORR] SKIP (SUM_RICH)  live={live_sum:.4f} > "
                f"fair-edge={fair_sum - cfg.cc_edge:.4f}"
            )
            return None

        logger.ok(
            f"[CORR] ✅ GATES PASSED  "
            f"15m-{s15}@{ask15:.4f} + 5m-{s5}@{ask5:.4f} = ${live_sum:.4f} "
            f"vs fair ${fair_sum:.4f} (edge ${fair_sum - live_sum:.4f})",
            icon="🌙",
        )
        return EntrySignal(
            s15=s15, s5=s5,
            ask15=ask15, ask5=ask5,
            lead_bps=lead_bps, lead_atr=lead_atr,
            p_corridor=p_corr, fair_sum=fair_sum,
        )

    def execute_pair(
        self,
        tokens15,
        tokens5,
        signal: EntrySignal,
        T15: int,
        window_slug_15m: str,
    ) -> Tuple[bool, Optional[float], Optional[float]]:
        """Place both legs (taker GTC). Returns (hedged, fill15, fill5).

        Paper mode: simulates fills at ask.
        Real mode: marketable GTC BUY on both legs, one retry if orphaned.
        """
        shares   = max(1, int(self.state.cc_shares))
        tok15_id = tokens15.up_token_id   if signal.s15 == "UP"   else tokens15.down_token_id
        tok5_id  = tokens5.up_token_id    if signal.s5  == "UP"   else tokens5.down_token_id

        est_cost = shares * (signal.ask15 + signal.ask5)
        logger.ok(
            f"[CORR] 🔥 CORRIDOR COLLECT  "
            f"15m-{signal.s15}@{signal.ask15:.4f} + 5m-{signal.s5}@{signal.ask5:.4f}  "
            f"{shares} sh/leg ≈ ${est_cost:.2f} total",
            icon="🌙",
        )

        is_paper = self.state.mode != "real"
        fill15   = signal.ask15
        fill5    = signal.ask5
        hedged   = False

        if is_paper:
            hedged = True
            logger.info(f"[CORR] PAPER: par simulado ≈ ${est_cost:.2f}", icon="📄")
        else:
            if self._client is None:
                logger.err("[CORR] real mode: CLOB client no inicializado — abortando")
                return False, None, None

            f15 = self._taker_buy(tok15_id, signal.ask15, shares)
            f5  = self._taker_buy(tok5_id,  signal.ask5,  shares)

            # One retry for orphaned legs (NEVER at a worse price)
            if f15 and not f5:
                logger.warn("[CORR] pierna 5m no llenada — reintentando...")
                f5 = self._taker_buy(tok5_id, signal.ask5, shares)
            elif f5 and not f15:
                logger.warn("[CORR] pierna 15m no llenada — reintentando...")
                f15 = self._taker_buy(tok15_id, signal.ask15, shares)

            hedged = bool(f15 and f5)

            if not hedged:
                logger.err(
                    f"[CORR] 🚨 PAR INCOMPLETO  f15={f15} f5={f5}  "
                    "aplanando la pierna solitaria al bid"
                )
                if f15 and not f5:
                    self._taker_sell(tok15_id, signal.ask15 - 0.02, shares)
                elif f5 and not f15:
                    self._taker_sell(tok5_id,  signal.ask5  - 0.02, shares)
                return False, fill15, fill5

        # Register both legs in btc15 state
        self._register_trades(tokens15, tokens5, signal, fill15, fill5, T15, window_slug_15m)
        return True, fill15, fill5

    # ── trade registration ────────────────────────────────────────────────────

    def _register_trades(
        self,
        tokens15, tokens5,
        signal: EntrySignal,
        fill15: float, fill5: float,
        T15: int, window_slug_15m: str,
    ) -> None:
        shares   = max(1, int(self.state.cc_shares))
        tok15_id = tokens15.up_token_id if signal.s15 == "UP" else tokens15.down_token_id
        tok5_id  = tokens5.up_token_id  if signal.s5  == "UP"  else tokens5.down_token_id
        now      = time.time()

        # 15m leader leg (initial)
        trade15 = Trade(
            id=0,
            window_slug=window_slug_15m,
            window_ts=T15,
            side=signal.s15,
            token_id=tok15_id,
            price=fill15,
            shares=float(shares),
            cost=round(fill15 * shares, 4),
            mode=self.state.mode,
            opened_at=now,
            note=(
                f"corridor 15m líder | "
                f"lead={signal.lead_bps:.1f}bps ATR={signal.lead_atr:.2f}× "
                f"P(corr)={signal.p_corridor*100:.0f}%"
            ),
            is_hedge=False,
            strategy="corridor",
        )
        self.state.add_trade(trade15)

        # 5m opposite leg (paired, stored as is_hedge so it doesn't double-count windows_traded)
        trade5 = Trade(
            id=0,
            window_slug=window_slug_15m,
            window_ts=T15,
            side=signal.s5,
            token_id=tok5_id,
            price=fill5,
            shares=float(shares),
            cost=round(fill5 * shares, 4),
            mode=self.state.mode,
            opened_at=now,
            note=(
                f"corridor 5m opuesto | "
                f"fair=${signal.fair_sum:.4f} live=${fill15+fill5:.4f} "
                f"edge=${signal.fair_sum - fill15 - fill5:.4f}"
            ),
            is_hedge=True,
            strategy="corridor",
        )
        self.state.add_trade(trade5)

        total_cost = (fill15 + fill5) * shares
        logger.ok(
            f"[CORR] par registrado  costo=${total_cost:.2f}  "
            f"suelo=${float(shares):.0f}  upside_corr=${shares * 2:.0f}  "
            f"P={signal.p_corridor*100:.0f}%",
            icon="🌙",
        )

    # ── CLOB V2 order helpers ─────────────────────────────────────────────────

    def _taker_buy(self, token_id: str, price: float, shares: int) -> bool:
        """Marketable GTC BUY. Returns True if order was accepted."""
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, Side as ClobSide
            resp = self._client.create_and_post_order(
                OrderArgs(
                    token_id=str(token_id),
                    price=float(price),
                    size=float(shares),
                    side=ClobSide.BUY,
                ),
                order_type=OrderType.GTC,
                post_only=False,
            )
            ok = bool(resp)
            if ok:
                logger.ok(f"[CORR] BUY  {token_id[:14]}…  ${price:.4f}×{shares}", icon="🌙")
            else:
                logger.warn(f"[CORR] BUY falló: {resp}")
            return ok
        except Exception as exc:
            logger.err(f"[CORR] BUY error: {exc}")
            return False

    def _taker_sell(self, token_id: str, price: float, shares: int) -> None:
        """Marketable GTC SELL — flatten orphan at bid."""
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, Side as ClobSide
            self._client.create_and_post_order(
                OrderArgs(
                    token_id=str(token_id),
                    price=max(0.01, float(price)),
                    size=float(shares),
                    side=ClobSide.SELL,
                ),
                order_type=OrderType.GTC,
                post_only=False,
            )
            logger.warn(f"[CORR] SELL (aplanar)  {token_id[:14]}…  ${price:.4f}×{shares}")
        except Exception as exc:
            logger.err(f"[CORR] SELL error: {exc}")

    def _build_client(self):
        try:
            from py_clob_client_v2 import ClobClient
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
            logger.ok("[CORR] CLOB V2 autenticado", icon="🔑")
            return client
        except Exception as exc:
            logger.err(f"[CORR] CLOB V2 auth error: {exc}")
            return None
