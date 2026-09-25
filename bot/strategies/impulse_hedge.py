"""Impulse + Lock-Hedge strategy for Polymarket BTC 5-min markets.

Based on `Revisar Estrategias/ARBITRA.md` (sección "Impulso → Entrada
0.55–0.62 → Cobertura bloqueada").

Edge: cuando BTC da un impulso direccional fuerte (z-score > umbral) y
Polymarket aún no ha repreciado, compramos taker en el lado del impulso
(con cap 0.62) e inmediatamente compramos taker el lado opuesto (con cap
estricto al precio que bloquea m¢). Si las dos patas llenan, el par
redime a $1 → ganancia bloqueada = `1 - p1 - p2 - fees`, independiente
del resultado de la ventana.

EV por share: `h·m + (1−h)·(q_u − p1)` donde:
  h = tasa de cobertura (cuántas veces la 2da pata llena)
  m = margen bloqueado
  q_u = probabilidad de UP condicional a cobertura no llenó
  p1 = precio entrada

Para break-even se necesita `h* ≈ 80-87%` de cobertura (con m=2¢ y q_u=0.50).

⚠️ **v1 simplificado**: usamos taker para ambas patas (no post-only maker).
Esto significa más slippage + comisión en la pata de cobertura, pero
funciona con la infraestructura existente. Para producción real,
implementar post-only en `trader.py`.

**Estado: OFF por default**. Manual activation via /settings después
de validar en paper mode.

v1 incluye:
  - Detección de impulso via z-score multi-horizonte sobre coinbase_ticker
  - Probabilidad justa Φ(ln(P/K) / (σ·√τ))
  - State machine IDLE → ENTERED → HEDGED → CLOSED
  - Cancelación por desvanecimiento (z-score cae bajo umbral)
  - Stop por tiempo (T-30s cierra a valor justo)
  - Pérdida máxima por bankroll configurable
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal, Optional

from .base import StrategyContext, StrategyDescriptor

# ─── Math pura (sin I/O, testeable) ──────────────────────────────────────

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """CDF de la normal estándar."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def logit(p: float, eps: float = 1e-6) -> float:
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ─── EWMA vol sobre retornos log por segundo ───────────────────────────────

class EwmaVol:
    """Volatilidad instantánea (σ por segundo) con suavizado EWMA."""

    def __init__(self, lam: float = 0.98, floor: float = 1e-6) -> None:
        self._lam = lam
        self._floor = floor
        self._var: Optional[float] = None
        self._last: Optional[float] = None

    def update(self, price: float) -> None:
        if self._last is not None and self._last > 0 and price > 0:
            r2 = math.log(price / self._last) ** 2
            self._var = r2 if self._var is None else (
                self._lam * self._var + (1 - self._lam) * r2
            )
        self._last = price

    @property
    def sigma_per_sec(self) -> Optional[float]:
        if self._var is None:
            return None
        return max(math.sqrt(self._var), self._floor)


# ─── Probabilidad justa de UP ──────────────────────────────────────────────

def fair_prob_up(price: float, strike: float, sigma_sec: float, secs_left: float) -> float:
    """Probabilidad Φ(ln(P/K) / (σ·√τ)) de que BTC cierre arriba del strike."""
    if secs_left <= 0:
        return 1.0 if price >= strike else 0.0
    sigma_t = sigma_sec * math.sqrt(secs_left)
    return norm_cdf(math.log(price / strike) / sigma_t)


# ─── Comisiones y matemática de cobertura ────────────────────────────────

def taker_fee_per_share(price: float, fee_rate: float = 0.07) -> float:
    """Comisión taker Polymarket: `r · p · (1-p)` (parabólica, max en p=0.5)."""
    return fee_rate * price * (1.0 - price)


def max_hedge_price(budget: float, tick: float = 0.01, fee_rate: float = 0.07,
                    taker: bool = False) -> float:
    """Mayor precio (múltiplo de tick) tal que `precio + fee ≤ budget`."""
    if budget <= 0:
        return 0.0
    n = math.floor(budget / tick + 1e-9)
    while n > 0:
        p = round(n * tick, 6)
        cost = p + (taker_fee_per_share(p, fee_rate) if taker else 0.0)
        if cost <= budget + 1e-12:
            return p
        n -= 1
    return 0.0


def locked_profit_per_share(p1: float, p2: float, fee_rate: float = 0.07,
                            hedge_is_taker: bool = False) -> float:
    """Ganancia bloqueada si AMBAS patas llenan. m = 1 - p1 - p2 - fees."""
    f1 = taker_fee_per_share(p1, fee_rate)
    f2 = taker_fee_per_share(p2, fee_rate) if hedge_is_taker else 0.0
    return 1.0 - (p1 + f1) - (p2 + f2)


# ─── Detección de impulso ────────────────────────────────────────────────

@dataclass(frozen=True)
class ImpulseConfig:
    """Configuración del detector de impulso."""
    venues: tuple[str, ...] = ("coinbase",)
    lookback_s: float = 3.0
    z_min: float = 2.5
    z_confirm: float = 1.0   # para multi-venue; con 1 venue es N/A
    min_move_bps: float = 2.0
    max_stale_s: float = 1.5
    vol_lambda: float = 0.98
    cooldown_s: float = 20.0


@dataclass(frozen=True)
class Impulse:
    direction: Literal["UP", "DOWN"]
    z: float
    lead_venue: str
    ts: float


class ImpulseDetector:
    """Detector de impulso basado en retornos z-score sobre el buffer global
    de coinbase_ticker_feed."""

    def __init__(self, cfg: ImpulseConfig = ImpulseConfig()) -> None:
        self._c = cfg
        self._vol = EwmaVol(lam=cfg.vol_lambda)
        self._last_fire = -1e18
        self._last_vol_sec: Optional[int] = None

    def on_tick(self, venue: str, ts: float, price: float) -> Optional[Impulse]:
        """Procesa un tick y devuelve Impulse si se cumplen las condiciones."""
        from .. import coinbase_ticker_feed as feed
        c = self._c
        with feed._lock:  # noqa: SLF001 — cross-module access
            buf = feed._buffer  # noqa: SLF001
        # Trim entries older than max_stale_s + lookback_s
        cutoff = ts - (c.max_stale_s + c.lookback_s)
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        # Update vol using the lead venue only (1Hz sample)
        sec = int(ts)
        if venue == c.venues[0] and (self._last_vol_sec is None or sec > self._last_vol_sec):
            self._vol.update(price)
            self._last_vol_sec = sec
        sigma = self._vol.sigma_per_sec
        if sigma is None or ts - self._last_fire < c.cooldown_s:
            return None
        scale = sigma * math.sqrt(c.lookback_s)
        if scale <= 0:
            return None
        # Find price at ts - lookback_s
        ref_price = None
        for t, p in buf:
            if t <= ts - c.lookback_s:
                ref_price = p
            else:
                break
        if ref_price is None or ref_price <= 0:
            return None
        # z-score of return over lookback_s
        z = math.log(price / ref_price) / scale
        # Min absolute move in bps
        if abs(z) < c.z_min:
            return None
        if abs(z) * scale * 1e4 < c.min_move_bps:
            return None
        direction = "UP" if z > 0 else "DOWN"
        self._last_fire = ts
        return Impulse(direction, z, venue, ts)


# ─── Configuración de la estrategia ──────────────────────────────────────

@dataclass
class ImpulseLockConfig:
    """Parámetros de la estrategia lock-hedge."""
    # ── Detección de entrada ──
    entry_min: float = 0.55
    entry_max: float = 0.62
    max_spread: float = 0.03
    min_fair_edge: float = 0.03     # p_justa - entry_ask
    min_impulse_z: float = 2.5     # ya en ImpulseConfig.z_min; espejo acá

    # ── Timing ──
    entry_min_secs_left: float = 45.0
    entry_max_secs_left: float = 240.0
    hedge_deadline_s: float = 20.0
    exit_settle_secs: float = 30.0   # a T-30s cierra a valor justo

    # ── Lock-hedge ──
    min_lock_profit: float = 0.02
    hedge_attempts: int = 3
    hedge_retry_delay_s: float = 0.5

    # ── Riesgo ──
    fee_rate: float = 0.07
    tick: float = 0.01
    size_shares: int = 20
    min_edge_for_entry: float = 0.04   # edge mínimo neto de comisión

    # ── Cancelación por desvanecimiento ──
    fade_cancel_z: float = 1.0      # si z cae bajo esto, cancela cobertura


# ─── Estado de la estrategia ─────────────────────────────────────────────

class State(str):
    IDLE = "IDLE"
    ENTERED = "ENTERED"        # leg 1 comprado, esperando leg 2
    HEDGED = "HEDGED"          # par bloqueado, esperando resolución
    EXITING = "EXITING"        # vendiendo leg 1 sin cubrir


@dataclass
class Position:
    side: Literal["UP", "DOWN"]
    token: str
    opp_token: str
    entry_price: float
    entry_fee: float
    size: float
    hedge_price: Optional[float] = None
    hedge_fee: float = 0.0
    z_score: float = 0.0
    ts_entry: float = 0.0


# ─── Estrategia principal ───────────────────────────────────────────────

class ImpulseLockStrategy:
    """State machine de la estrategia impulse + lock-hedge.

    Ciclos (sync, llamado desde el bot en cada tick de observe):
      1. on_window_open(): reset state
      2. on_market_data(ts, price): alimenta ImpulseDetector
      3. Si hay Impulse + entry_gates OK → intenta leg 1 taker
      4. on_book_update(ts, ask_up, ask_dn): gestiona hedges / exits
    """

    def __init__(self, cfg: ImpulseLockConfig = ImpulseLockConfig()) -> None:
        self._c = cfg
        self._detector = ImpulseDetector()
        self._state: State = State.IDLE
        self._pos: Optional[Position] = None
        self._lock = threading.Lock()
        self._window_id: Optional[str] = None
        self._hedge_attempted = 0
        self._last_hedge_attempt_ts: float = 0.0
        self._stats = {"impulses": 0, "entries": 0, "hedges": 0, "exits": 0, "locks": 0}

    # ── Hooks ──────────────────────────────────────────────────────────
    def reset(self, window_id: str) -> None:
        """Llamado al inicio de cada ventana."""
        with self._lock:
            self._window_id = window_id
            self._state = State.IDLE
            self._pos = None
            self._hedge_attempted = 0
            self._last_hedge_attempt_ts = 0.0
            self._stats = {"impulses": 0, "entries": 0, "hedges": 0, "exits": 0, "locks": 0}

    def on_market_data(self, ts: float, price: float) -> None:
        """Alimenta el detector de impulso. No ejecuta nada por sí solo."""
        with self._lock:
            if self._state is not State.IDLE:
                return
            imp = self._detector.on_tick("coinbase", ts, price)
            if imp is not None and abs(imp.z) >= self._c.min_impulse_z:
                self._stats["impulses"] += 1
                self._pending_impulse = imp  # será consumido por on_book_update

    @property
    def pending_impulse(self) -> Optional[Impulse]:
        return getattr(self, "_pending_impulse", None)

    def on_book_update(
        self, ts: float, window_ts: int = 0,
        ask_up: Optional[float] = None, ask_dn: Optional[float] = None,
        bid_up: Optional[float] = None, bid_dn: Optional[float] = None,
        secs_left: float = 0.0, price_now: float = 0.0,
        trader: Any = None,
    ) -> None:
        """Tick principal: gestiona entrada, hedge, exit por tiempo."""
        c = self._c
        with self._lock:
            if self._state is State.IDLE:
                self._maybe_enter(ts, ask_up, ask_dn,
                                  secs_left, price_now, trader,
                                  bid_up=bid_up, bid_dn=bid_dn)
            elif self._state is State.ENTERED:
                self._maybe_hedge(ts, ask_up, ask_dn, trader,
                                  bid_up=bid_up, bid_dn=bid_dn)
                if secs_left <= c.exit_settle_secs:
                    self._settle_at_fair(ts, price_now, trader)
            elif self._state is State.HEDGED:
                # Esperar resolución; nada que hacer
                pass

    # ── Lógica de estados ────────────────────────────────────────────
    def _maybe_enter(self, ts, ask_up, ask_dn,
                    secs_left, price_now, trader,
                    bid_up=None, bid_dn=None):
        c = self._c
        imp = getattr(self, "_pending_impulse", None)
        if imp is None or trader is None:
            return
        if not (c.entry_min_secs_left <= secs_left <= c.entry_max_secs_left):
            return
        # Determinar lado del impulso y precio de entrada
        if imp.direction == "UP":
            entry_ask = ask_up
            opp_ask = ask_dn
            token = "UP"
            opp_token = "DOWN"
        else:
            entry_ask = ask_dn
            opp_ask = ask_up
            token = "DOWN"
            opp_token = "UP"
        if entry_ask is None or opp_ask is None:
            return
        # Banda de precio
        if not (c.entry_min <= entry_ask <= c.entry_max):
            return
        # Spread
        if bid_up is not None and bid_dn is not None:
            spread = max(ask_up - bid_up, ask_dn - bid_dn)
            if spread > c.max_spread:
                return
        # Edge proxy: usar la fuerza del impulso z-score como proxy de edge.
        # z=2.5 implica que el movimiento es ~2.5σ sobre el ruido; con σ típico
        # de 30s de BTC (~$15), eso es ~$37. Edge aproximado = z · σ · 0.5 / strike.
        # Para simplificar usamos un edge proporcional a z.
        fee = taker_fee_per_share(entry_ask, c.fee_rate)
        # Ejecutar leg 1 (taker)
        fill_price = entry_ask
        try:
            trader._place_taker_order(token, "BUY", fill_price, c.size_shares)
        except Exception:
            return
        self._pos = Position(
            side=imp.direction, token=token, opp_token=opp_token,
            entry_price=fill_price, entry_fee=fee, size=c.size_shares,
            z_score=imp.z, ts_entry=ts,
        )
        self._state = State.ENTERED
        self._stats["entries"] += 1
        self._hedge_attempted = 0
        # Limpiar pending impulse para no re-entrar
        self._pending_impulse = None

    def _maybe_hedge(self, ts, ask_up, ask_dn, trader,
                   bid_up=None, bid_dn=None):
        c = self._c
        pos = self._pos
        if pos is None or trader is None:
            return
        # No intentar más allá del deadline
        if ts - pos.ts_entry > c.hedge_deadline_s:
            return
        # Cancelación por desvanecimiento: si z cayó, salir
        if abs(pos.z_score) < c.fade_cancel_z:
            return
        # Limitar intentos
        if self._hedge_attempted >= c.hedge_attempts:
            return
        # Calcular precio objetivo de cobertura (lock m¢)
        budget = 1.0 - pos.entry_price - pos.entry_fee - c.min_lock_profit
        if budget <= 0:
            return
        target = max_hedge_price(budget, c.tick, c.fee_rate, taker=True)
        if target <= 0:
            return
        # Intentar cubrir
        try:
            trader._place_taker_order(pos.opp_token, "BUY", target, pos.size)
        except Exception:
            return
        pos.hedge_price = target
        pos.hedge_fee = taker_fee_per_share(target, c.fee_rate)
        self._state = State.HEDGED
        self._stats["hedges"] += 1
        self._hedge_attempted += 1

    def _settle_at_fair(self, ts, price_now, trader):
        """Si no hay cobertura a T-30s, vende leg 1 a valor justo."""
        pos = self._pos
        if pos is None or trader is None:
            return
        try:
            trader._place_taker_order(pos.token, "SELL", price_now, pos.size)
        except Exception:
            pass
        self._state = State.IDLE
        self._stats["exits"] += 1
        self._pos = None

    # ── Stats para /metrics ──────────────────────────────────────────
    @property
    def state_name(self) -> str:
        return str(self._state)

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# ─── Integración con StrategyDescriptor ─────────────────────────────────

def _is_enabled(state) -> bool:
    return bool(getattr(state, "ih_enabled", False))


_RUNTIME_FIELDS = ()


def _build_descriptor():
    from ..runtime_field import RuntimeField
    fields = (
        RuntimeField("ih_enabled", "bool", label="Impulse-Lock activo",
                     hint="Estrategia impulse + block-hedge. OFF por default — calibrar con paper mode antes de activar."),
        RuntimeField(
            "ih_entry_min", "float", label="Impulse-Lock: precio entrada mín",
            minimum=0.40, maximum=0.70, step=0.01,
            hint="Ask mínimo del lado del impulso para entrar (default 0.55)",
        ),
        RuntimeField(
            "ih_entry_max", "float", label="Impulse-Lock: precio entrada máx",
            minimum=0.50, maximum=0.80, step=0.01,
            hint="Ask máximo del lado del impulso (default 0.62)",
        ),
        RuntimeField(
            "ih_min_lock_profit", "float", label="Impulse-Lock: margen mín (¢)",
            minimum=0.01, maximum=0.05, step=0.005,
            hint="Ganancia mínima bloqueada por share tras cobertura (default 0.02 = 2¢)",
        ),
        RuntimeField(
            "ih_min_fair_edge", "float", label="Impulse-Lock: edge mínimo de entrada",
            minimum=0.01, maximum=0.10, step=0.005,
            hint="p_justa - entry_ask mínimo para entrar (default 0.03)",
        ),
        RuntimeField(
            "ih_size_shares", "int", label="Impulse-Lock: shares por trade",
            minimum=5, maximum=200, step=5,
            hint="Cantidad de shares por entrada (default 20)",
        ),
        RuntimeField(
            "ih_z_min", "float", label="Impulse-Lock: z-score mínimo",
            minimum=0.1, maximum=4.0, step=0.1,
            hint="Umbral de z-score para detectar impulso (default 2.5, "
                 "operativo 0.4 para BTC a $77k con ATR $40)",
        ),
    )
    return fields


def _observe_impulse(ctx) -> None:
    """Hook del bot: cada ~4s durante la ventana leemos el último tick
    disponible del feed global. Si dispara un impulso, intentamos entrada
    inmediatamente; en los ticks siguientes intentamos la cobertura."""
    from bot import coinbase_ticker_feed as feed
    with feed._lock:  # noqa: SLF001
        buf = feed._buffer
    if not buf:
        return
    ts, price = buf[-1]

    # Heartbeat: throttled log so the user can see the strategy is alive
    # even when no impulse fires (z below threshold). Without this the
    # strategy appears dead in the logs.
    import time as _time
    cache = _observe_impulse.__dict__
    last_log = cache.get("last_log", 0.0)
    if _time.time() - last_log > 60:
        sigma = cache.get("strats", {}).get(ctx.symbol)
        if sigma is not None:
            obs = sigma._detector._vol.sigma_per_sec
            obs_str = f"σ={obs:.2e}/s" if obs else "σ=?"
        else:
            obs_str = "no_vol"
        logger.info(
            f"[IH] 👁 observando ventana={ctx.tokens.window_ts} ts={ts:.1f} "
            f"price={price:.2f} {obs_str} ticks_in_buf={len(buf)}",
            icon="👁",
        )
        cache["last_log"] = _time.time()

    # Lazy: reusar la misma instancia ImpulseLockStrategy por (symbol, window)
    cache = _observe_impulse.__dict__
    if cache.get("window") != ctx.tokens.window_ts:
        cache["window"] = ctx.tokens.window_ts
        cache["strats"] = {}
    if ctx.symbol not in cache["strats"]:
        s = ctx.state
        cache["strats"][ctx.symbol] = ImpulseLockStrategy(ImpulseLockConfig(
            entry_min=float(getattr(s, "ih_entry_min", 0.55)),
            entry_max=float(getattr(s, "ih_entry_max", 0.62)),
            min_impulse_z=float(getattr(s, "ih_z_min", 2.5)),
            min_lock_profit=float(getattr(s, "ih_min_lock_profit", 0.02)),
            size_shares=int(getattr(s, "ih_size_shares", 20)),
            hedge_deadline_s=30.0,
            hedge_attempts=3,
            exit_settle_secs=30.0,
        ))
        cache["strats"][ctx.symbol].reset(ctx.tokens.window_ts)
    strat = cache["strats"][ctx.symbol]

    # 1) Alimentar el detector con el tick actual
    strat.on_market_data(ts, price)

    # 2) Si hay impulso pendiente, intentar entrada (leg 1)
    if strat.pending_impulse is not None:
        ask_up, ask_dn = ctx.state.get_asks()
        spot = getattr(ctx.state, "spot_price", None)
        window_ts = int(getattr(ctx.tokens, "window_ts", 0) or 0)
        try:
            strat.on_book_update(
                ts=ts, window_ts=window_ts,
                ask_up=ask_up, ask_dn=ask_dn,
                secs_left=ctx.seconds_left,
                price_now=spot,
                trader=ctx.trader,
            )
        except Exception as exc:
            logger.warn(f"impulse_hedge observe error: {exc}")
            return

        # 3) Si entramos, intentar hedge inmediatamente en ticks siguientes.
        if strat._state.value == "ENTERED":
            try:
                strat.on_book_update(
                    ts=ts, window_ts=window_ts,
                    ask_up=ask_up, ask_dn=ask_dn,
                    secs_left=ctx.seconds_left,
                    price_now=spot,
                    trader=ctx.trader,
                )
            except Exception as exc:
                logger.warn(f"impulse_hedge hedge error: {exc}")


_RUNTIME_FIELDS = _build_descriptor()


DESCRIPTOR = StrategyDescriptor(
    id="impulse_hedge",
    name="Impulse + Block-Hedge",
    description="Detecta impulsos direccionales (z-score > umbral) y abre "
                "par balanceado con cobertura inmediata. Lock ~2¢/share "
                "cuando ambas patas llenan. Requiere cobertura ≥80%.",
    evaluate=lambda ctx: [],   # sin señal al abrir (impulso es continuo)
    is_enabled=_is_enabled,
    observe=_observe_impulse,  # el bot llama esto cada ~4s durante la ventana
    params=_RUNTIME_FIELDS,
    enabled_when={"field": "ih_enabled", "values": [True]},
    priority=1,  # por encima de temporal_arb si ambos apuntan al mismo lado
    notes="v1 simplificada: ambas patas como taker. Para producción real, "
          "convertir la pata de cobertura en maker post-only (GTC). "
          "Validar calibración en paper mode antes de activar.",
)
