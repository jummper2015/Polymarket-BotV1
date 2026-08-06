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


5 de Agosto 2026
Plan — Filtros de régimen, multi-activo y registro de estrategias

 Contexto

 El bot opera hoy un solo activo (BTC) con dos formas, ss_fade y ss_trend.
 La medición sobre 10.119 ventanas etiquetadas por Gamma y 3.734 con cotización
 previa a la apertura (documentado en docs/RUTA.md Fase 8) dice:

 - ss_trend pierde −4,22% por operación (−7,08% al excluir ventanas con
 racha, t=−2,61). No es neutral: está del lado perdedor de un efecto real.
 - ss_fade gana +3,74% por operación (53,8% de acierto a 0,519), pero el
 cap actual de 0,60 lo destruye: los 5 trades reales entraron a 0,558 de
 media, por encima del valor justo de 0,538.
 - El desempate de streak_trader.py:207-214 descarta Fade y conserva Trend
 — justo al revés de lo que dicen los datos (98 de 1.152 Fade descartados).
 - La martingala apuesta ~100× lo que justifica el edge (Kelly pide 4,03% del
 bankroll; el backtest llega a $983 en una ventana).

 Sobre la idea de "operar solo en horas de poca volatilidad": la versión ingenua
 empeora las cosas — el cuartil de menor volatilidad da −0,46%, porque en un
 mercado plano una racha de 4 ventanas es ruido. Lo que sí mide bien es
 volatilidad media (+6,44%), rango estrecho de 2h (+6,06%) y sobre todo
 franja horaria: US 13-21h UTC da +9,22% y es positivo en los 4 tramos de
 8,8 días (+12,8%, +4,8%, +8,1%, +11,4%), mientras Europa 08-13h da −5,0%.

 Todo esto sigue por debajo de significancia (t=+1,93 en el mejor caso, y probé
 ~20 filtros). Por eso el objetivo de esta fase no es "ganar más" sino dejar de
 perder por defectos medidos y montar la infraestructura para decidir con datos:
 multi-activo triplica la muestra y baja el tiempo de validación de ~78 días a
 ~26.

 Decisiones tomadas

 - ss_trend se apaga por defecto (sigue en el código y en la config).
 - Las dos estrategias de liquidaciones se construyen sobre un feed gratuito:
 verifiqué que Bybit v5 allLiquidation.<SYM> funciona (ack OK, control
 958 msg, 1 liquidación real capturada en 120 s). Binance !forceOrder@arr
 está bloqueado en este entorno pero probablemente funcione en el VPS — se
 añade como fuente secundaria opcional.
 - Entrega por fases. Este plan cubre la Fase A en detalle y deja la
 Fase B esbozada.

 Fase A — Base

 A1. Arreglos medidos de la estrategia vigente

 En bot/config.py (load_config + RUNTIME_FIELDS) y .env.example:

 - SS_FADE_LIMIT_CAP: 0.60 → 0.52
 - SS_MODE: both → fade
 - Nuevo SS_SIZING: flat | kelly | martingale, por defecto flat.
 martingale sigue disponible pero deja de ser el camino por defecto.
 - Nuevo SS_KELLY_FRACTION (0.05–1.0, def. 0.25) para el modo kelly.

 En bot/streak_trader.py, invertir el desempate: hoy conserva ss_trend
 cuando las señales chocan; debe conservar ss_fade. El comentario que justifica
 la regla actual queda obsoleto y hay que reescribirlo citando la medición.

 En bot/strategy_streak.py, el sizing sale de on_win/on_loss hacia una
 función size_for(strategy, price, state) que respeta SS_SIZING.

 A2. Filtros de régimen configurables

 Módulo nuevo bot/regime.py con funciones puras y testeables que reciben la
 ventana y el historial de velas y devuelven (permitido: bool, razón: str):

 - hours_filter — lista de franjas UTC permitidas (SS_TRADING_HOURS, formato
 "13-21,21-24", vacío = todas)
 - volatility_filter — banda de ATR de 1 h entre percentiles
 (SS_VOL_MIN_PCT / SS_VOL_MAX_PCT, def. 25/75, que es la banda que midió
 +6,44%)
 - range_filter — rango de 2 h por debajo de un percentil (SS_RANGE_MAX_PCT)

 Los percentiles se calculan sobre una ventana móvil de las últimas N velas, no
 sobre constantes fijas, para que no caduquen al cambiar el régimen de
 volatilidad. Todos por defecto desactivados salvo el horario, que arranca
 apagado también: la evidencia es sugerente, no concluyente, y el dashboard debe
 poder medir con y sin filtro.

 Cada rechazo se registra con su razón para que el motivo del skip sea auditable
 (mismo patrón que los SKIP_* de las estrategias de Revisar Estrategias/).

 A3. Multi-activo BTC / ETH / SOL

 Verificado en vivo: los tres tienen mercado de 5 m con spread de 1 centavo
 (profundidad BTC 376/1059, ETH 110/496, SOL 40/165 shares) y también existen los
 de 15 m, que hacen falta para corridor. XRP y DOGE existen pero con spreads de
 3–6 centavos: se descartan.

 - bot/binance_api.py: todas las funciones pasan a aceptar symbol
 (BTCUSDT/ETHUSDT/SOLUSDT). Hoy está fijo en BTCUSDT.
 - bot/market.py: ya acepta symbol en los slugs de 5 m; hay que
 parametrizar slug_for_15m_timestamp, que está fijo en btc.
 - bot/state.py: STATES pasa a ser un diccionario real
 {symbol: BotState}. STATE se mantiene como alias del de BTC para no
 romper dashboard.py y los tests durante la transición.
 - bot/db.py: añadir columna symbol a TradeModel (el helper
 _add_missing_columns() ya existe para esto) y cambiar la unicidad de
 MartingaleStateModel de strategy a (strategy, symbol). Las filas
 existentes se rellenan con symbol='btc'.
 - Un hilo trader por activo, con SS_SYMBOLS (def. btc) para elegir cuáles.
 Arrancar con BTC solo y añadir ETH/SOL cuando la base esté verde.

 A4. Registro de estrategias

 Hoy añadir un parámetro obliga a tocar cinco sitios (config.py, state.py,
 dashboard.py, settings.html, settings.js), y _aggregate_db_stats tiene
 los nombres ("ss_fade", "ss_trend") escritos a mano. Eso no escala a seis
 estrategias × tres activos.

 Paquete nuevo bot/strategies/ con un descriptor por estrategia:

 id, nombre, descripción, activos soportados,
 params: tuple[RuntimeField, ...],   # reutiliza el tipo que ya existe en config.py
 enabled_field: str,                 # el toggle de activación
 evaluate(ctx) -> list[Signal]

 ss_fade y ss_trend se migran a este formato como primeros miembros — sin
 cambiar su lógica, solo su envoltorio. RUNTIME_FIELDS pasa a construirse
 concatenando los campos base con los de cada estrategia registrada, de modo
 que la validación, la persistencia en bot_config y el reset ya existentes
 funcionen sin tocarlos.

 bot/templates/settings.html deja de tener un bloque escrito a mano por
 estrategia y pasa a renderizar el registro: por cada estrategia, una tarjeta con
 su toggle y sus campos, generada desde el JSON que ya sirve /state.

 A5. Métricas por estrategia en el dashboard

 - bot/dashboard.py: _aggregate_db_stats agrupa por (strategy, symbol) y
 deriva las claves del registro en vez de la tupla fija.
 - bot/trade_queries.py: metric_series ya es genérico sobre
 t.strategy (usa setdefault), así que solo hay que añadir el corte por
 symbol y exponer symbol como filtro en build_query, junto a los que ya
 soporta.
 - El dashboard gana un selector de activo y una fila de tarjetas por estrategia
 con trades, V/D, win rate, P&L, ROI y drawdown, más el desglose de skips por
 razón — que es lo que permite comparar "con filtro" contra "sin filtro".

 Fase B — Estrategias nuevas (esbozo)

 Sobre la base anterior, una por una y en paper:

 1. spread_harvest_maker y box_builder — no predicen dirección,
 cobran el spread. Son las que tienen razón estructural para ser positivas.
 Necesitan órdenes maker (post_only) y gestión de cancelación, que el bot
 hoy no tiene: es el trabajo real de esta fase.
 2. mid_price_continuation — solo necesita spot + libro, ya disponibles.
 3. corridor — necesita operar 5 m y 15 m a la vez; los mercados existen.
 4. Liquidaciones — primero un bot/liquidation_feed.py (Bybit WS, con
 Binance !forceOrder@arr opcional) y su grabador, siguiendo el patrón de
 chainlink_recorder.py. Las liquidaciones no se sirven históricamente: sin
 semanas de tape grabado no hay forma de validar liq_cascade_chaser ni
 small_liq_continuation, así que el grabador va antes que las estrategias.

 Verificación


     Verificación

     - python -m pytest tests/ -q — la suite (298 tests) debe seguir en verde;
     añadir tests para bot/regime.py (funciones puras, sin red), para el sizing
     flat/kelly, para el desempate invertido y para la migración de symbol.
     - DATABASE_URL="sqlite:////tmp/verify.db" PORT=5055 python run.py en paper,
     comprobando en el log que el desempate conserva Fade, que el cap es 0,52 y que
     los skips por régimen aparecen con su razón.
     - sqlite3 /tmp/verify.db "select strategy, symbol, entry_price, status from trades;"
     para confirmar que la columna nueva se rellena.
     - curl -s localhost:5055/state | python -m json.tool — verificar que
     strategy_stats sale del registro y trae el corte por activo.
     - Re-medir con python -m scripts.regime_filter tras acumular operaciones
     nuevas, que es el script que decide si los filtros se quedan.

     Lo que este plan NO promete

     Ninguno de los filtros llega a significancia estadística con 35 días de datos.
     La Fase A elimina pérdidas medidas (cap, desempate, martingala, ss_trend) y
     construye el instrumento para decidir; no convierte el bot en rentable. La
     única cifra con respaldo sólido es que lo que hay hoy pierde dinero de forma
     evitable.