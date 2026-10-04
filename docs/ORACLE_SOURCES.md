# 🔮 ORACLE_SOURCES — Fuentes de precio del bot

> Adaptado de la skill `prediction-market-oracle-research` de ECC para
> ME$IRVE. Documenta los **tres oracles** que el bot usa para decidir el
> strike en mercados up/down 5-min de BTC en Polymarket.

---

## 1. La decisión que la señal informa

El bot ME$IRVE opera mercados **up/down 5-min de BTC en Polymarket**. Cada
ventana tiene un strike price implícito (el precio de BTC al cierre de la
vela) y dos tokens (UP / DOWN) que se liquidan a $1 según si BTC cerró por
encima o por debajo.

Para decidir **comprar UP o DOWN**, el bot compara:
- `current_btc` (precio spot actual) vs `strike` (precio al cierre esperado)
- El signo y magnitud del gap determina la dirección (Path A en
  `bot/strategies/temporal_arb.py`)
- La magnitud absoluta filtra señales débiles (`min_itm_pct`)

**Por qué importa la fuente del strike**:
- Polymarket usa Chainlink `btc-usd-twap-60s` (TWAP de los últimos 60s)
- Si el bot usa un strike distinto al de Polymarket, **entrará en
  ventanas que ya están Perdedes** según el exchange — o se perderá
  ventanas que ya están Ganadas
- Un gap sistemático de >$5 entre la fuente local y la oficial degrada
 显著mente el edge

---

## 2. Las tres fuentes (en orden de prioridad)

### Tier 1: TWAP-60s local (PRIMARIO)

**Implementación**: `bot/polymarket_twap_tracker.py`

- Mantiene un buffer rolling de **90 segundos** de ticks BTC-USD
- Tick source: WebSocket Coinbase (`bot/coinbase_ticker_feed.py`)
- Calcula TWAP de los últimos 60 segundos
- Gap medido vs oficial (Chainlink) en VPS: **<$5** sistemáticamente
- Latencia: <1 segundo (datos reales, no agregados)

**Por qué es primario**:
- Más fresco que cualquier candle agregado (5m)
- Cerrado al método oficial (TWAP-60s)
- No depende de APIs externas con rate limits

**Limitaciones**:
- Si Coinbase WebSocket cae, el buffer se vacía → fallback a Tier 2
- Si Coinbase tiene un gap de precio (flash crash, exchange halt), el TWAP
  arrastra el sesgo hasta que se reemplaza por datos limpios

**Configuración** (en `bot/polymarket_twap_tracker.py`):
- `lookback_seconds = 60` (default)
- `buffer_seconds = 90` (mantiene margen sobre lookback)
- `min_required_ticks = 30` (descarta ventana si hay <30 ticks en 60s)

### Tier 2: Coinbase candle OPEN (FALLBACK 1)

**Implementación**: `bot/coinbase_api.py` + `get_strike()` en
`bot/strategies/temporal_arb.py`

- Cuando el TWAP-60s local no tiene datos suficientes, usa el **precio OPEN
  de la vela 5-min más reciente** de Coinbase
- Latencia: ~5s tras el cierre de la vela
- Es **idéntico al valor que Polymarket usa** como fallback oficial cuando
  Chainlink está caído

**Por qué funciona**:
- Coinbase tiene profundidad de mercado en BTC-USD (>$1B daily volume)
- Vela 5-min cierra cada 5 minutos exactos (alineado con ventanas de
  Polymarket)
- Es una fuente agregada, no tick-by-tick → menos susceptible a flash
  crashes de milisegundos

**Limitaciones**:
- Solo hay un valor por minuto-de-cierre de vela → no se actualiza durante
  la ventana
- Si la vela cierra con un wick extremo (dentro-vela spike), el bot lo
  acepta como strike

### Tier 3: Chainlink oficial de Polymarket (FALLBACK 2)

**Implementación**: implícita — el bot espera que el feed de Polymarket
publicar el strike vía la API Gamma/CLOB

- Es la fuente **oficial** que Polymarket usa para liquidar
- Latencia: ~3 minutos tras cierre de vela
- Es lo que Polymarket decide — el bot no puede discrepar

**Por qué es fallback, no primario**:
- Demasiado lento para señales near-tmin (T-5 a T-90)
- El bot ya habría tomado la decisión antes de que el feed oficial
  publique

**Cuándo se usa**:
- Solo si Tier 1 y Tier 2 fallan simultáneamente
- En la práctica, casi nunca en operación normal

---

## 3. Métricas de calidad de señal

Por cada oracle, evaluamos:

| Métrica | Tier 1 TWAP-60s | Tier 2 Candle OPEN | Tier 3 Chainlink |
|---|---|---|---|
| **Latencia vs strike real** | <1s | ~5s | ~3min |
| **Gap medio vs oficial** | <$5 | $0 (es lo que Polymarket usa) | $0 (es el oficial) |
| **Spread implícito** | Tick-by-tick → spread cero | Vela agregada → suaviza ruido | TWAP-60s → mismo método |
| **Edad del dato** | <1s | hasta 300s | hasta 180s |
| **Disponibilidad histórica** | Solo desde deploy (no histórico) | Toda la historia de Coinbase | Toda la historia de Chainlink |
| **Susceptibilidad a manipulación** | Media (puede haber flash crashes) | Baja (vela cierra) | Baja (red descentralizada) |
| **Dependencia de conectividad** | WebSocket Coinbase | REST API Coinbase | API Gamma/Polymarket |

---

## 4. Patrones de integración

El bot actual implementa el patrón **"cascada con threshold"**:

```python
# Pseudocódigo simplificado de get_strike() en temporal_arb.py
def get_strike(current_ts):
    # Tier 1: TWAP-60s local
    twap = twap_tracker.get_rolling_twap(current_ts, lookback_seconds=60)
    if twap is not None and twap.min_ticks >= 30:
        return twap.value, source="twap_60s_local"

    # Tier 2: Coinbase candle OPEN
    candle = coinbase_api.get_latest_5m_candle_open()
    if candle is not None:
        return candle.open, source="coinbase_candle_open"

    # Tier 3: esperar Chainlink
    return None, source="waiting_for_chainlink"
```

**Otros patrones posibles** (no implementados, futuro):
- **Alerting input**: si Tier 1 gap >$10 vs Tier 2, notificar al operador
- **Confidence interval**: usar desviación estándar del TWAP como
  incertidumbre
- **Multi-oracle vote**: requerir que Tier 1 y Tier 2 concuerden dentro de
  $X antes de operar

---

## 6. Pendiente: tracking post-commit

Estado actual:
- [ ] **Logging de fuente por trade** — qué oracle dictaminó cada strike
- [ ] **Métricas de gap** — loggear gap Tier-1 vs Tier-2 vs Tier-3 en cada
  decisión (rechazada o ejecutada)
- [ ] **Alerting** si Tier 1 gap >$10 sostenido por >5 minutos

Esto se implementaría en `bot/polymarket_twap_tracker.py` +
`bot/strategies/temporal_arb.py:get_strike()`. No requiere cambio de schema,
solo logging adicional.

---

## 7. Comparación contra fuentes no-mercado

El bot NO compara contra fuentes no-mercado (no usa noticias, filings,
KPIs internos). Es 100% price-driven.

**Riesgo**: si Polymarket tiene un strike claramente divergente del precio
real de BTC en exchanges externos (por liquidez thin en el mercado de
Polymarket, por ejemplo), el bot no lo detecta.

**Mitigación**: el `twap_60s` local usa Coinbase como referencia — si hay
divergencia sistemática, se notaría en `bot/polymarket_price.py:log_gap()`.

---

## 8. Recomendación de uso

| Condición | Tier a usar |
|---|---|
| Operación normal, BTC liquid, Coinbase WS conectado | Tier 1 (TWAP-60s local) |
| Coinbase WS caído pero API REST funcional | Tier 2 (Candle OPEN) |
| Coinbase caído completamente | Tier 3 (Chainlink oficial) — esperar |
| BTC en flash crash o halt | **PAUSAR BOT** — no tomar señales hasta que se estabilice |

---

## 9. Output Contract

Resultado de evaluar cada nueva fuente de oracle:

1. **decision context** — ¿qué señal informa? (en este caso: strike de
   ventana up/down)
2. **market sources** — ¿qué exchange/feed?
3. **signal quality** — tabla arriba
4. **comparison sources** — ¿hay fuentes no-mercado? (no en este caso)
5. **integration recommendation** — ¿primario/fallback/descartado?
6. **caveats** — flash crashes, gaps, halt, etc.

---

## 10. Referencias

- `bot/polymarket_twap_tracker.py` — implementación Tier 1
- `bot/coinbase_ticker_feed.py` — WebSocket Coinbase
- `bot/coinbase_api.py` — REST API + candles Tier 2
- `bot/strategies/temporal_arb.py:get_strike()` — lógica de cascada
- `bot/polymarket_price.py` — gap tracking vs oficial