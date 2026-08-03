# 🏗️ PLAN — Streak Snapper v2

> **Fecha:** Agosto 2026
> **Versión:** 2.0
> **Basado en:** [RESUMEN_STREAK_SNAPPER.md](../Revisar%20Estrategias/RESUMEN_STREAK_SNAPPER.md)

---

## 🎯 Objetivo

Un bot de trading en Polymarket que opera ventanas de 5 minutos de BTC (UP/DOWN) usando **dos formas de operar** distintas, ambas con progresión **Martingale ×1.5**:

| Forma | Nombre | Filosofía | Señal | Límite |
|---|---|---|---|---|
| **1** | Fade (Anti-racha) | Contrarian | 4 ventanas 5m misma dirección | ≤ $0.60 |
| **2** | Trend (Tendencia 4h) | Trend-following | Vela 4h close > open | ≤ $0.52 |

Ambas comparten:
- Inversión base: **$5.00**
- Martingale: **×1.5** por pérdida, reset al ganar
- Hold hasta resolución del evento
- Datos BTC vía **Binance API** (klines públicos)
- Ejecución vía **Polymarket CLOB V2**
- Persistencia en **PostgreSQL** (SQLAlchemy)

---

## 🧱 Arquitectura

```
StreakSnapperTrader (thread principal)
  │
  ├── binance_api.py     ← Datos BTC (klines 5m + 4h)
  ├── strategy_streak.py ← Señales + Martingale
  ├── market.py          ← Descubrimiento de tokens (Gamma API)
  ├── price_feed.py      ← Precios bid/ask en vivo (WebSocket)
  ├── streak_trader.py   ← Ejecución CLOB V2 + resolución
  └── db.py              ← Persistencia PostgreSQL
```

### Flujo por ventana de 5 minutos

```
1. ESPERAR → Siguiente ventana btc-updown-5m-{timestamp}
2. RESOLVER → Trades pendientes (Binance; confirmación Gamma posterior)
3. DATOS   → Binance API: klines 5m + vela 4h
4. SEÑAL   → Fade (si racha ≥ 4) / Trend (dirección 4h)
   ↳ Si Fade y Trend apuntan a lados opuestos, se omite la ventana
5. ENTRAR  → LIMIT BUY a min(cap, best_ask), GTC
6. GUARDAR → Persistir trade en PostgreSQL
7. HOLD    → Esperar fin de ventana
8. LIQUIDAR → Resolver ESTA ventana antes de abrir la siguiente
9. REPETIR
```

### Resolución de operaciones

Gamma tarda **~3 minutos** en publicar `outcomePrices` tras el cierre de una
ventana — más que la propia ventana. Esperarlo dejaría la Martingala siempre un
ciclo por detrás, así que la liquidación es en dos etapas:

| Etapa | Fuente | Cuándo | Para qué |
|---|---|---|---|
| 1 | Binance (vela 5m) | Al instante del cierre | Avanza/resetea la Martingala a tiempo para la ventana siguiente |
| 2 | Gamma `outcomePrices` | ~200 s después | Confirma o corrige el P&L registrado |

**Importante:** Polymarket **no** resuelve con Binance. Según la descripción del
mercado, la fuente es el stream **Chainlink BTC/USD**, y la regla es
`precio_final >= precio_inicial → Up` (el empate resuelve *Up*).

Los dos feeds coinciden en la dirección de cualquier ventana normal — la ventana
mediana de 5m se mueve un **0.031%** — pero pueden discrepar cuando la ventana
queda casi plana. Caso observado en producción: la ventana `1785677100` se movió
$0.09 sobre $63.082 (0.00014%); Binance dijo DOWN y Chainlink dijo UP.

Por eso `get_window_direction` **no decide** si el movimiento relativo es
`<= NEAR_FLAT_THRESHOLD` (**0.005%**) y difiere a Gamma. Se difiere en torno a
**11 de cada 100 ventanas**.

> El umbral se recalibró el 3-ago-2026 de `1e-5` a **`5e-5`** sobre 3.016
> ventanas etiquetadas con Gamma. Con el valor anterior el bot liquidaba mal el
> **4,68%** de sus trades; ahora el **2,03%**. El precio es diferir el 11% de las
> ventanas en vez del 3,3%, y la medición muestra que sale a cuenta: pese a
> diferir más, la Martingala se sobre-dimensiona un 31% menos.
> Reproducible con `python -m bot.threshold_study`.
> **[CHAINLINK_TWAP.md §11.1.b y §12.1](CHAINLINK_TWAP.md)**.

Cada trade guarda en `resolution_source` quién lo resolvió (`binance` o
`gamma`); una corrección posterior de Gamma se registra en el log con nivel de
error. Si esas correcciones se vuelven frecuentes, subir el umbral.

---

## 🎛️ Selector de modo

`ss_mode` = `"fade"` | `"trend"` | `"both"`

- **fade**: Solo Forma 1 (anti-racha)
- **trend**: Solo Forma 2 (tendencia 4h)
- **both**: Ambas en paralelo (estados Martingale independientes)

En modo `both`, si las dos formas apuntan a **lados opuestos** de la misma
ventana no se opera. Comprar ambos lados cuesta exactamente lo que paga el par
($0.54 + $0.46 = $1.00 por share), así que el resultado es neutro garantizado y
además inyecta una victoria y una derrota falsas en las Martingalas.

---

## 🗄️ Base de datos

**PostgreSQL** vía SQLAlchemy + Flask-SQLAlchemy. Misma `DATABASE_URL` que el frontend TypeScript.

Tablas:
- `trades` — Historial completo de entradas
- `martingale_state` — Multiplicador actual por estrategia
- `bot_config` — Key-value store para config runtime *(declarada, aún sin uso:
  los cambios hechos en `/settings` no sobreviven a un reinicio)*

Las columnas añadidas después de la primera versión se aplican solas al arrancar
(`_add_missing_columns` en `db.py`) — `create_all()` solo crea tablas, nunca
columnas nuevas en tablas que ya existen.

Si no hay `DATABASE_URL`, fallback automático a **SQLite** (`data/streak_snapper.db`).

---

## 🚀 Arranque

```bash
# Instalar dependencias
uv sync
# o
pip install -r requirements.txt

# Ejecutar
python run.py
```

### Variables de entorno (.env)

| Variable | Default | Descripción |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` o `real` |
| `SS_MODE` | `both` | `fade`, `trend` o `both` |
| `SS_FADE_BASE_BET` | `5.0` | Inversión base Forma 1 |
| `SS_FADE_LIMIT_CAP` | `0.60` | Precio máximo Forma 1 |
| `SS_FADE_STREAK_MIN` | `4` | Racha mínima Forma 1 |
| `SS_TREND_BASE_BET` | `5.0` | Inversión base Forma 2 |
| `SS_TREND_LIMIT_CAP` | `0.52` | Precio máximo Forma 2 |
| `SS_MARTINGALE_MULT` | `1.5` | Factor multiplicador |
| `PRIVATE_KEY` | — | Clave privada wallet Polygon |
| `PROXY_WALLET` | — | Dirección proxy Polymarket |
| `DATABASE_URL` | — | URL PostgreSQL (opcional) |
| `PORT` | `5000` | Puerto del dashboard |
| `STARTING_BANKROLL` | `1000.0` | Bankroll inicial |

---

## ⚠️ Riesgos

1. **Edge fino** — El Streak Snapper original operaba con ~54.3% win rate. Sin el filtro ATR (3x stretch), la Forma 1 pierde ese edge. Monitorizar win rate de cerca.
2. **Martingale** — ×1.5 compuesto crece rápido. 6 pérdidas seguidas = ×11.4 la inversión base ($5 → $57). Tener bankroll suficiente.
3. **Slippage** — Entrar al ASK real, no al mid. El código ya usa `best_ask` del WebSocket.
4. **CLOB V2** — `post_only=False` con GTC. FAK/FOK devuelven 400 en montos < $1.
5. **Discrepancia Binance ↔ Chainlink** — 🟡 La Martingala avanza con el resultado
   de Binance; Polymarket resuelve con Chainlink. El P&L del trade se corrige
   cuando llega Gamma, pero el multiplicador ya se usó en ventanas posteriores y
   no se puede reconstruir; el log lo marca como error.
   **Medido: ocurre en el 2,03% de los trades** tras recalibrar el umbral
   (antes 4,68%). Ningún umbral basado en Binance llega a cero: la solución
   definitiva sería liquidar contra el propio stream de Chainlink, que cuesta
   $150/mes. Ver [CHAINLINK_TWAP.md §11.2.d](CHAINLINK_TWAP.md).
6. **Sizing desfasado al diferir** — Cuando Binance no puede llamar la ventana, se
   difiere a Gamma, que tarda ~200 s: el trade no se resuelve hasta el tick
   siguiente, así que **la entrada inmediata se dimensiona con un resultado de
   retraso**. Si la ventana diferida fue *ganada*, la siguiente entra sin resetear
   el Martingale y arriesga de más. Afecta al **11%** de las ventanas con el
   umbral actual. Es el precio —medido y favorable— de reducir el riesgo 5.
   Ver [CHAINLINK_TWAP.md §11.1.b](CHAINLINK_TWAP.md).
