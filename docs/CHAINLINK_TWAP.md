# 🔗 CHAINLINK TWAP + BINANCE — Análisis e implementación

> **Fecha del análisis:** 3 de agosto de 2026
> **Fuente:** https://docs.polymarket.com/market-data/chainlink-twap
> **Estado:** analizado, medido e **implementado** — ver [§12](#12--estado-a-día-de-hoy-3-ago-2026)

> ## ⚠️ Cómo leer este documento
>
> Está en dos capas y **la segunda corrige a la primera**:
>
> - **§1–§10** — análisis original. Conserva el razonamiento, pero **§5 y §8
>   fueron refutados por medición posterior.** No actuar según ellos.
> - **§11–§12** — revisión medida e implementación. **Es la capa vigente.**
>
> Si una cifra de §1–§10 contradice a §11–§12, gana §11–§12.

---

## 📋 Resumen ejecutivo

| Hallazgo | Resultado |
|---|---|
| Topics TWAP en el relay de Polymarket | ❌ **No activos** el 3-ago — lanzan el 4-ago-2026 |
| Fuente de resolución real de `btc-updown-5m` | Chainlink BTC/USD **stream normal**, *no* el TWAP |
| Ventaja de Binance sobre el feed de Polymarket | **~0.31 s** (no 2.4 s) |
| Historial de Chainlink para backtesting | ✅ **Sí existe** — Candlestick API ([§11.2.c](#112c--corrección-a-6--sí-existe-histórico-de-chainlink)) |
| Etiquetas históricas para backtesting | ✅ Gamma, **100% de cobertura** medida sobre 3.000 ventanas |
| Acuerdo Binance ↔ Chainlink al liquidar | **97.97%** tras recalibrar — antes 95.32% ([§12.1](#121--near_flat_threshold-aplicado--1e-5--5e-5)) |
| Credenciales de Chainlink Data Streams | Self-serve, **$150/mes**, sin tier gratuito ([§11.2.b](#112b--investigado--self-serve-150mes-y-existe-histórico)) |

La conclusión de más impacto no es sobre TWAP: **el bot liquidaba mal ~1 de cada
21 trades**, lo que corrompe el multiplicador Martingale. Corregido subiendo
`NEAR_FLAT_THRESHOLD` a `5e-5` — ahora ~1 de cada 49.

⚠️ **El valor que proponía §5 (`1.5e-4`) habría empeorado el bot.** Medía solo la
mitad del problema. Ver [§11.1.b](#111b--medición-resuelta--el-umbral-óptimo-es-5e-5-y-15e-4-es-peor-que-no-hacer-nada).

---

## 1. Qué son realmente los feeds TWAP

Chainlink Data Streams publica dos feeds TWAP por par: ventanas de **30 s** y
**60 s**. Son *lookback windows*, no cadencias de publicación — la documentación
avisa explícitamente de no inferir la ventana a partir de la frecuencia de
actualización, y de fijar un umbral de frescura con fallback.

Dos rutas de acceso:

| Ruta | Credenciales | Endpoint |
|---|---|---|
| Chainlink directo | API key + user secret (firmado) | `wss://ws.dataengine.chain.link` |
| **Relay de Polymarket (RTDS)** | **ninguna** | `wss://ws-live-data.polymarket.com` |

Para este bot la ruta correcta es **RTDS**: sin credenciales, sin firma, sin
sincronización de reloj (Chainlink directo exige el reloj del servidor dentro de
±5 s), y es la que Polymarket recomienda para producción.

### Topics RTDS

| Ventana | Topic crudo | Spec del SDK Python |
|---|---|---|
| 30 s | `crypto_prices_twap_thirty` | `CryptoPricesChainlinkTwapSpec(window_seconds=30, ...)` |
| 60 s | `crypto_prices_twap_sixty` | `CryptoPricesChainlinkTwapSpec(window_seconds=60, ...)` |

Payload:

```json
{ "topic": "crypto_prices_twap_thirty", "type": "update", "timestamp": 1785178800123,
  "payload": { "symbol": "btc/usd", "value": 65000.5,
    "full_accuracy_value": "65000500000000000000000",
    "timestamp": 1785178800000, "window_s": 30 } }
```

- `full_accuracy_value` es el entero **E18** exacto → dividir por `10^18` con
  `Decimal`, nunca con `float`. `value` es solo para mostrar.
- `payload.timestamp` = momento de observación de Chainlink.
  El `timestamp` externo = momento en que el publisher lo entregó al relay.
  **La diferencia entre ambos es la métrica de frescura.**
- Símbolos en minúsculas con barra: `btc/usd`. Ojo: el topic no-TWAP
  (`crypto_prices`) usa otro formato, `btcusdt`.
- `filters` exige JSON compacto exacto, sin espacios: `{"symbol":"btc/usd"}`.
  Para varios símbolos: omitir `filters` y filtrar por `payload.symbol`.
- Heartbeat: enviar el frame de texto `PING` cada 5 s.

---

## 2. ⚠️ Corrección 1 — los topics TWAP no están activos todavía

Probado hoy (3-ago-2026) contra `wss://ws-live-data.polymarket.com`:

```
sub -> crypto_prices_twap_thirty   → 500 {"message":"leger AddSubscriptions error:
                                      ERROR #22001 value too long for type character(16)"}
sub -> crypto_prices_twap_sixty    → 500 (idem)
sub -> crypto_prices               → ✅ 237 mensajes en 40 s
```

Los topics TWAP se rechazan con un error interno del servidor (una columna de
16 caracteres en el registro de suscripciones no admite un topic de 25). El relay
funciona; los topics TWAP aún no están provisionados. Coincide con la fecha de
lanzamiento del propio documento: **4 de agosto de 2026**, con soak testing hasta
ese día.

**Consecuencias para el diseño:**

1. Todo el código TWAP va detrás de un flag (`CL_TWAP_ENABLED`, default `false`).
2. El fallo de suscripción **no puede tumbar el bot** — se registra y se sigue.
3. La documentación avisa de que una suscripción rechazada antes del lanzamiento
   *puede no reintentar sobre un socket abierto*: al reconectar hay que **abrir
   socket nuevo**, no reenviar la suscripción sobre el mismo.
4. Los feeds pueden cambiar antes del día 4 — no fijar el esquema a fuego.

---

## 3. ⚠️ Corrección 2 — el TWAP no es la fuente de resolución

La premisa del plan era «Chainlink → resolución de eventos». Es correcta, pero
**no con el feed TWAP**. El propio mercado lo dice:

```
resolutionSource: https://data.chain.link/streams/btc-usd

"The resolution source for this market is information from Chainlink,
 specifically the BTC/USD data stream available at
 https://data.chain.link/streams/btc-usd."
"Please note that this market is about the price according to Chainlink data
 stream BTC/USD, not according to other sources or spot markets."
```

`btc-usd` es el stream **benchmark normal**. Los feeds TWAP son *feed IDs
distintos* (`BTC / USD - TWAP: 30s`), un producto nuevo. Sustituir uno por otro
introduce un error sistemático: un TWAP de 30 s es una media móvil que **retrasa
al spot ~15 s de media**; el de 60 s, ~30 s.

**Reparto de responsabilidades corregido:**

| Fuente | Rol | Por qué |
|---|---|---|
| **Binance WS** | Señal de entrada, momentum | Latencia mínima, tick completo |
| **Chainlink TWAP (RTDS)** | **Confirmación / filtro anti-manipulación** | Resistente a wicks y spoofing |
| Chainlink `btc-usd` stream | Precio de liquidación *de iure* | Es lo que dice el mercado |
| **Gamma `outcomePrices`** | Verdad de liquidación *de facto* | Único acceso sin credenciales |

Ese retraso estructural del TWAP no es un defecto: es **la señal**. Cerca del
cierre de ventana conocemos el spot mientras el TWAP todavía arrastra la media.
La divergencia `spot − twap30` es un predictor direccional utilizable, y es un
efecto de **~15 s**, dos órdenes de magnitud mayor que cualquier carrera de red.

---

## 4. ⚠️ Corrección 3 — la ventaja de Binance es ~0.31 s, no 2.4 s

Medido con dos WebSockets en paralelo durante 70 s
(`btcusdt@ticker` vs RTDS `crypto_prices` filtrado a `btcusdt`):

| Métrica | Binance WS | RTDS `crypto_prices` |
|---|---|---|
| Muestras | 69 | 70 |
| Espaciado entre mensajes | 1.00 s | 0.995 s |
| Lag evento → llegada (mediana) | **0.119 s** | **0.446 s** |

**Binance adelanta a RTDS 0.312 s (mediana, n=69).**

El motivo: el topic `crypto_prices` de RTDS **ya es Binance** — el símbolo es
`btcusdt`, remuestreado a 1 Hz. No es una fuente independiente; es Binance con
~330 ms de sobrecoste de relay.

Dos implicaciones:

- No hay 2.4 s de ventaja que explotar. Perseguir esos milisegundos no compensa.
- Como el bot ya opera en ventanas de 5 min y decide en el boundary, **300 ms son
  irrelevantes**. La ventaja real está en el retraso del TWAP (§3) y en la
  calidad de la señal, no en la latencia.
- El `@ticker` de Binance también va a 1 Hz. Para momentum de verdad hay que usar
  `@trade` o `@bookTicker`, que entregan cada tick.

---

## 5. ~~🔴 Corrección crítica: `NEAR_FLAT_THRESHOLD`~~ ⛔ SUPERADA

> ## ⛔ Esta sección está refutada — no aplicar su recomendación
>
> Su conclusión (`1.5e-4`) es **peor que el valor que pretendía corregir**: mide
> solo el error de liquidación e ignora el coste de diferir, así que su óptimo
> aparente es «lo más alto posible». Con las dos mitades medidas, el óptimo es
> **`5e-5`** — ya aplicado.
>
> Sus cifras además vienen de 545 ventanas; sobre 3.016, `1e-5` no da 2,84% de
> error sino **3,64%**.
>
> **Vigente: [§11.1.b](#111b--medición-resuelta--el-umbral-óptimo-es-5e-5-y-15e-4-es-peor-que-no-hacer-nada) y [§12.1](#121--near_flat_threshold-aplicado--1e-5--5e-5).**
> Se conserva por el razonamiento sobre la asimetría entre los dos errores, que
> sigue siendo correcto.

Este es el hallazgo con más impacto económico y **no depende de TWAP**.

`bot/binance_api.py:83` usa `NEAR_FLAT_THRESHOLD = 1e-5` para decidir cuándo
Binance es demasiado ajustado como para liquidar y hay que diferir a Gamma. El
comentario del código estima ~3% de ventanas diferidas. Se midió el acierto real
comparando la dirección de la vela de Binance contra el `outcomePrices` ya
resuelto de Gamma en **545 ventanas consecutivas**:

```
N=545   discrepancias=25 (4.59%)
movimiento mediano=0.0315%
discrepancia con mayor movimiento=0.01426%   ← 14× el umbral actual
```

Barrido del umbral:

| Umbral | Ventanas diferidas | Liquidadas | Mal liquidadas | **Tasa de error** |
|---|---|---|---|---|
| 0 | 0.4% | 543 | 25 | 4.60% |
| **1e-5 (actual)** | **3.1%** | **528** | **15** | **2.84%** |
| 2e-5 | 4.2% | 522 | 13 | 2.49% |
| 5e-5 | 9.4% | 494 | 6 | 1.21% |
| 1e-4 | 17.8% | 448 | 2 | 0.45% |
| **1.5e-4 (propuesto)** | **25.7%** | **405** | **0** | **0.00%** |
| 2e-4 | 34.7% | 356 | 0 | 0.00% |

**El umbral actual solo captura 3 de las 8 discrepancias más ajustadas.** El bot
liquida mal el **2.84%** de sus trades: ~1 de cada 35.

### Por qué importa tanto

La asimetría entre los dos errores es enorme:

- **Diferir** cuesta *una ventana de sizing desactualizado*. Gamma confirma a los
  ~200 s, `_confirm_binance_resolutions()` lo arregla, y el efecto se autocura.
- **Liquidar mal** mueve el Martingale **en la dirección equivocada**. Con ×1.5,
  un fallo propaga un sizing incorrecto durante muchas ventanas. El propio código
  lo admite en `streak_trader.py:552`:

  > *"The martingale already moved on the Binance result. We can't reconstruct the
  > multiplier it should have had (later windows have already used the wrong one)"*

Diferir es baratísimo. Liquidar mal es caro. El umbral está calibrado al revés.

### Acción

```python
# bot/binance_api.py
NEAR_FLAT_THRESHOLD = 1.5e-4   # 0.015% — antes 1e-5
```

Medido sobre 545 ventanas (3-ago-2026): elimina las 25 discrepancias a coste de
diferir el 25.7% de las ventanas a Gamma. Reconfirmar el barrido cada varias
semanas; el umbral depende de la volatilidad del régimen.

> **Nota:** ningún umbral basado en Binance llega al 100% garantizado. La
> solución definitiva es liquidar contra el propio Chainlink (§7.3).

---

## 6. 📉 Backtesting — ~~no hay historial de Chainlink~~ ⚠️ PARCIALMENTE SUPERADA

> ## ⚠️ La premisa central de esta sección es falsa
>
> **Sí existe historial de Chainlink**: la Candlestick API sirve OHLC histórico
> (`/api/v1/history/rows`). Lo que sigue es cierto para el *stream de reports*,
> pero no para Data Streams en su conjunto. Ver
> [§11.2.c](#112c--corrección-a-6--sí-existe-histórico-de-chainlink).
>
> La conclusión práctica **no cambia**: el backtest se etiqueta con Gamma, que
> es gratis, tiene 100% de cobertura medida y es la verdad de liquidación —
> mientras que la Candlestick API cuesta $150/mes y arrastra tres salvedades sin
> verificar.

La documentación es explícita:

> *"Subscriptions start with the next update. There is no snapshot, history, or
> replay after a disconnect."*

`getLatestReport()` es lo único previo al stream, y solo da el valor actual. **No
se puede backtestear contra precios históricos de Chainlink.** No existe el
endpoint.

⚠️ No sustituir por los agregadores *on-chain* de Chainlink (`getRoundData` en
Polygon): son un producto distinto (basado en rondas, disparado por desviación) y
**no coincidirán con la liquidación de Data Streams**. Introduciría un sesgo
silencioso en todo el backtest.

### La solución: Gamma da las etiquetas, no hacen falta los precios

Para backtestear no se necesita el *precio* de Chainlink — se necesita **quién
ganó**. Y eso Gamma lo da resuelto, que es exactamente la verdad de liquidación.
Profundidad medida:

| Antigüedad | `outcomePrices` |
|---|---|
| 1.000 ventanas (3.5 días) | ✅ resuelto |
| 4.000 ventanas (13.9 días) | ✅ resuelto |
| 8.000 ventanas (27.8 días) | ✅ resuelto |
| **20.000 ventanas (69.4 días)** | ✅ **resuelto** |
| 50.000 ventanas (173.6 días) | ❌ vacío |

**~20.000 ventanas etiquetadas, ~70 días de historial.**

Detalles de acceso medidos:

- Solo funciona `GET /events?slug=btc-updown-5m-<ts>`, **una ventana por
  petición**. `/markets?slug=` devuelve vacío y `series_slug` se ignora (devuelve
  mercados sin relación). No hay endpoint de lote → paralelizar con
  `ThreadPoolExecutor` (8–10 workers va bien) y cachear en disco.
- `urllib` sin `User-Agent` recibe **403**. Usar `requests`.
- Campos útiles: `outcomePrices` (`["1","0"]` = UP), `umaResolutionStatus`,
  `closedTime`.

### Arquitectura del backtest corregida

```
FEATURES (señales)          ←  klines de Binance          [histórico ilimitado]
ETIQUETAS (quién ganó)      ←  Gamma outcomePrices        [~70 días, 20k ventanas]
FILTRO TWAP (a futuro)      ←  tape grabado por nosotros  [desde 4-ago-2026]
```

`bot/backtest.py` hoy deriva el resultado de la dirección de la vela de Binance
(`backtest.py:82`) — la misma aproximación con **4.59% de error** medido en §5.
Sobre 2.000 ventanas eso son ~92 resultados mal etiquetados, y con Martingale el
error no se promedia: se compone. **Las cifras actuales del backtest no son
fiables** hasta migrar las etiquetas a Gamma.

### Grabar el tape desde ya

Como no hay historial y nunca lo habrá, **el tape que no grabemos hoy se pierde
para siempre**. Arrancar el grabador el 4-ago-2026 es lo que permite backtestear
estrategias basadas en TWAP dentro de unas semanas.

---

## 7. 🛠️ Plan de implementación

### 7.1 `bot/chainlink_feed.py` (nuevo)

Cliente RTDS, mismo patrón que `price_feed.py` (thread daemon,
`websocket-client`, auto-reconexión) para no añadir dependencias.

```python
RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC_30 = "crypto_prices_twap_thirty"
TOPIC_60 = "crypto_prices_twap_sixty"
```

Requisitos:

- Heartbeat: frame de texto `PING` cada 5 s en su propio thread.
- Precisión: `Decimal(full_accuracy_value) / Decimal(10**18)`. Nunca `float`.
- Frescura: descartar si `now - payload.timestamp > CL_TWAP_STALE_SECONDS`
  (default 15 s). La doc no publica cadencia → el umbral es obligatorio.
- **Reconexión = socket nuevo** (§2.3), no reenviar suscripción.
- Suscribir 30 s y 60 s por separado (dos suscripciones, un socket).
- Mantener el mapa `topic → (símbolo, ventana)` localmente: los reportes de
  Chainlink no llevan etiqueta de símbolo ni de ventana.
- Sin credenciales: `PRIVATE_KEY` no entra aquí.

### 7.2 Señal de divergencia en `strategy_streak.py`

Filtro **opcional y aditivo**: solo puede *vetar*, nunca originar una entrada. Si
el TWAP no está disponible → no veta (fail-open), el bot opera como hoy.

```
div = (spot_binance - twap_30) / twap_30
```

- `|div|` por encima de `CL_DIVERGENCE_MAX` → wick o manipulación reciente:
  saltar la ventana. Aquí es donde el TWAP paga: filtra los movimientos que el
  precio de liquidación va a ignorar.
- `sign(div)` contra la dirección de la señal → confirmación direccional, por el
  retraso estructural de ~15 s (§3).
- `twap_30` vs `twap_60`: pendiente del momentum suavizado, inmune a wicks.

**Calibrar antes de activar.** Umbrales sin medir sobre datos reales no valen; y
los datos no existen hasta que el grabador (§7.4) acumule semanas. Se implementa
con default desactivado.

### 7.3 Liquidación — orden de preferencia

Sustituir la cadena actual `binance → gamma` por:

```
1. Chainlink TWAP grabado   (si CL_TWAP_ENABLED y el tape cubre la ventana)
2. Binance                  (con el umbral corregido de §5)
3. Gamma outcomePrices      (verdad oficial, ~200 s de retraso)
```

Añadir `"chainlink"` a `TradeModel.resolution_source` (hoy
`"binance" | "gamma"`, `VARCHAR(8)` — cabe) vía `_LATE_COLUMNS`, y mantener
`_confirm_binance_resolutions()` como auditoría contra Gamma para las tres
fuentes.

⚠️ Con el matiz de §3: el TWAP **no** es el precio de liquidación. Un veredicto
derivado del TWAP es una *estimación mejor que Binance*, no la verdad. Gamma sigue
siendo la autoridad final y `_confirm_binance_resolutions()` no se toca.

### 7.4 Grabador de tape (§6)

Tabla nueva, escritura desde `chainlink_feed.py`:

```python
class ChainlinkTickModel(db.Model):
    __tablename__ = "chainlink_ticks"
    id          = Column(Integer, primary_key=True)
    symbol      = Column(String(16), nullable=False, index=True)  # "btc/usd"
    window_s    = Column(Integer, nullable=False)                 # 30 | 60
    value_e18   = Column(String(40), nullable=False)   # entero exacto, como str
    observed_at = Column(Integer, nullable=False, index=True)      # payload.timestamp
    received_at = Column(Integer, nullable=False)                  # timestamp externo
```

- `value_e18` como `String`, no `Float`: `Float` destruye la precisión E18 que la
  doc insiste en preservar.
- Guardar los dos timestamps: su diferencia es el histórico de frescura y sirve
  para calibrar `CL_TWAP_STALE_SECONDS` con datos en vez de a ojo.
- 1 tick/s × 2 ventanas ≈ 172.800 filas/día. Prever purga o agregación a 5 min
  antes de dejarlo corriendo semanas en el VPS.
- Índice en `observed_at` — el backtest consulta por rango temporal.

### 7.5 Backtest: migrar etiquetas a Gamma

En `bot/backtest.py`:

- Nuevo módulo `bot/gamma_history.py`: descarga paralela de `outcomePrices` por
  slug, con caché en disco (`data/gamma_outcomes.json`) para no repetir 20.000
  peticiones en cada corrida.
- Flag `--labels {binance,gamma}`, **default `gamma`**.
- Binance sigue dando las *features* (klines para racha y tendencia 4h); Gamma da
  las *etiquetas*. Nunca mezclar: usar Binance para ambas es la fuga de 4.59%.
- Reportar en el CSV cuántas ventanas se etiquetaron con cada fuente y cuántas
  discreparon — es la validación continua del umbral de §5.

### 7.6 Configuración

Nuevos campos en `Config` + `RUNTIME_FIELDS` (`config.py`), todos apagados por
defecto para que el arranque del día 4 no cambie el comportamiento:

| Campo | Tipo | Default | Rango | Descripción |
|---|---|---|---|---|
| `cl_twap_enabled` | bool | `false` | — | Activa la suscripción RTDS |
| `cl_twap_window` | choice | `30` | `30`/`60` | Ventana para la señal |
| `cl_twap_stale_seconds` | float | `15.0` | 1–120 | Umbral de frescura |
| `cl_divergence_max` | float | `0.0` | 0–0.05 | Veto por divergencia (`0` = off) |
| `cl_record_ticks` | bool | `false` | — | Grabador de tape (§7.4) |
| `cl_settle_from_twap` | bool | `false` | — | TWAP como fuente 1 (§7.3) |

Variables de entorno correspondientes: `CL_TWAP_ENABLED`, `CL_TWAP_WINDOW`,
`CL_TWAP_STALE_SECONDS`, `CL_DIVERGENCE_MAX`, `CL_RECORD_TICKS`,
`CL_SETTLE_FROM_TWAP`.

`cl_twap_window` es `choice` y no `int`: solo existen 30 y 60.

### 7.7 Dashboard

- Tile de estado del feed Chainlink: conectado / frescura en segundos / último
  valor. Con `CL_TWAP_ENABLED=false`, mostrar «desactivado», no «error».
- `spot_binance` vs `twap_30` vs `twap_60` y la divergencia en vivo — es lo que
  permite calibrar `CL_DIVERGENCE_MAX` observando en vez de adivinando.
- Columna «fuente de resolución» en el historial de trades (ya en la DB, sin UI).

### 7.8 Dependencias

**Ninguna nueva.** `websocket-client` (ya presente) cubre RTDS. El SDK oficial
`polymarket-client` está en PyPI (`0.3.0b2`, `requires_python >=3.11`) pero es
beta, solo async, y añadiría un cliente asíncrono a una arquitectura de threads.
Los topics crudos ya están verificados contra el servidor (§2) — no aporta.

---

## 8. ~~✅ Orden de ejecución recomendado~~ ✅ EJECUTADO

> Plan original, conservado como registro. **Estado real en
> [§12.2](#122-qué-se-implementó-y-qué-falta-del-7-original).** La tarea #1 se
> ejecutó con un valor distinto del que aquí figura, por el motivo de §11.1.b.

| # | Tarea | Impacto estimado | Estado real |
|---|---|---|---|
| 1 | ~~`NEAR_FLAT_THRESHOLD` → 1.5e-4~~ (§5) | 🔴 Alto | ✅ Hecho, pero **con `5e-5`** — `1.5e-4` era peor (§11.1.b) |
| 2 | **Etiquetas de backtest desde Gamma** (§7.5) | 🔴 Alto | ✅ Hecho — `--labels gamma` por defecto |
| 3 | `bot/chainlink_feed.py` + grabador (§7.1, §7.4) | 🟡 Medio | ✅ Hecho, apagado por defecto |
| 4 | Dashboard: estado del feed (§7.7) | 🟢 Bajo | ✅ Hecho — badge con edad y divergencia |
| 5 | Señal de divergencia (§7.2) | 🟡 Medio | ⚪ Cálculo listo; **veto sin cablear** — falta calibrar |
| 6 | TWAP como fuente de liquidación (§7.3) | 🟢 Bajo | ⚪ Descartado por ahora — §3 lo desaconseja |

**1 y 2 no dependen de Chainlink ni de la fecha del día 4.** Son los dos cambios
que más valor aportan y se pueden hacer hoy.

---

## 9. 🔍 Reproducir las mediciones

Todo lo numérico de este documento es medido, no estimado. Para reproducirlo:

| § | Medición | Cómo |
|---|---|---|
| 2 | Topics TWAP rechazados | Suscribir a `crypto_prices_twap_thirty` en RTDS → 500 |
| 3 | Fuente de resolución | `GET /events?slug=btc-updown-5m-<ts>` → `resolutionSource` |
| 4 | Latencia Binance vs RTDS | 2 WS en paralelo, comparar `arrival − event_ts` |
| 5 | Barrido del umbral | Vela de Binance vs `outcomePrices` de Gamma, 545 ventanas |
| 6 | Profundidad de Gamma | Sondear `btc-updown-5m-<base − 300·N>` con N creciente |

Conviene rehacer el barrido de §5 periódicamente: el umbral óptimo depende del
régimen de volatilidad, y 545 ventanas son ~2 días.

---

## 10. 📌 Riesgos abiertos

- **Los feeds pueden cambiar antes del 4-ago-2026** (soak testing en curso).
  Verificar el esquema del payload contra el servidor real antes de fiarse.
- **Sin cadencia publicada.** No hay SLA de frecuencia; el umbral de frescura y
  el fallback no son opcionales.
- **La metodología TWAP no está publicada** — Chainlink no documenta los límites
  de muestreo, la ponderación ni el redondeo. No intentar recalcular el TWAP
  localmente ni asumir que es una media aritmética simple.
- **`decodeReport()` no verifica las firmas del DON.** Irrelevante en la ruta
  RTDS, pero si algún día se migra a Chainlink directo hay que verificarlas antes
  de usar el valor para liquidar.
- **El umbral de §5 es empírico, no una garantía.** Cero discrepancias en 545
  ventanas no es cero discrepancias siempre. Gamma sigue siendo la autoridad.

---

## 11. 🧩 Huecos del análisis — lo que faltaba por documentar

> **Revisión:** 3-ago-2026, posterior al análisis original.
> Verificado contra el servidor y contra el código, no estimado.

### 11.0 Re-verificación de §2 — los topics siguen cerrados

Reprobado hoy contra `wss://ws-live-data.polymarket.com`:

```
crypto_prices_twap_thirty → 500  ERROR #22001 value too long for type character(16)
crypto_prices_twap_sixty  → 500  (idem)
```

Sin cambios respecto al análisis original. El lanzamiento del **4-ago-2026**
sigue siendo la fecha operativa; nada que implementar contra el feed hoy.

### 11.1 🔴 El coste de diferir no estaba cuantificado — y no es gratis

§5 despacha el diferimiento como *«cuesta una ventana de sizing desactualizado»*
y *«el efecto se autocura»*. Es cierto en dirección, pero faltaba el número, y el
número cambia la decisión.

**Qué pasa realmente cuando se difiere** (trazado sobre el código):

`_resolve_pending_trades()` corre al **inicio del tick de la ventana N+1**, es
decir ~5 s después del cierre de N (`streak_trader.py:402`,
`RESOLVE_GRACE_SECONDS = 5`). Si Binance difiere, se consulta Gamma en ese mismo
instante — pero Gamma tarda ~200 s en publicar. Devuelve `None`, el trade queda
`open` y se reintenta en el tick **N+2**, donde ya han pasado ~305 s y sí resuelve.

Como el settle va **antes** de la entrada dentro del tick, la secuencia es:

| Tick | Resuelve | Entra con multiplicador que incluye |
|---|---|---|
| N+1 | nada | resultados hasta **N−1** ← *le falta N* |
| N+2 | N (vía Gamma) | resultados hasta **N** |

**Cada ventana diferida hace que exactamente la entrada siguiente se dimensione
con un resultado de retraso.** En rachas de diferimientos el desfase **no se
acumula**: se mantiene constante en 1. Eso es lo bueno de la noticia.

Lo malo es la frecuencia. Medido sobre las últimas **1.000 ventanas de 5 min** de
Binance (~3,5 días), incluyendo el agrupamiento, que §5 no miraba:

| Umbral | Ventanas diferidas | Rachas | Racha más larga | Rachas ≥ 2 |
|---|---|---|---|---|
| **1e-5 (actual)** | **3.30%** | 32 | 2 | 1 |
| 2e-5 | 5.50% | 50 | 2 | 5 |
| 5e-5 | 10.90% | 96 | 3 | 11 |
| 1e-4 | 20.50% | 141 | 7 | 35 |
| **1.5e-4 (propuesto §5)** | **28.60%** | 171 | **8** | 59 |
| 2e-4 | 37.20% | 198 | 12 | 83 |

El salto a 1.5e-4 no cambia «3 de cada 100» por «26 de cada 100 con un coste
menor»: cambia el régimen. Pasa de **1 entrada mal dimensionada de cada 30** a
**más de 1 de cada 4**, con tramos de hasta 8 ventanas consecutivas operando
desfasadas.

**La dirección del error de sizing importa y §5 no lo distinguía:**

- Se difiere una ventana **perdida** → la siguiente entra **sin escalar** (apuesta
  de menos). Recuperación más lenta. Conservador; benigno.
- Se difiere una ventana **ganada** → la siguiente entra **sin resetear**, con el
  multiplicador alto acumulado. **Arriesga de más**, que es el modo de fallo que
  el Martingale ×1.5 vuelve peligroso.

Ese segundo caso es el que no está evaluado en ninguna parte del documento.

**Conclusión revisada:** la dirección de §5 se sostiene — liquidar mal sigue
siendo peor que diferir, porque propaga un multiplicador erróneo hasta la
siguiente victoria mientras que diferir cuesta una entrada acotada. Pero **el
salto directo a 1.5e-4 no está justificado por los datos presentados**. `5e-5`
recorta el error de liquidación de 2.84% → 1.21% (−57%) por solo 10.9% de
diferimientos y rachas máximas de 3. Es el punto de la curva con mejor relación,
y es lo que debería probarse primero.

### 11.1.b ✅ Medición resuelta — el umbral óptimo es `5e-5`, y `1.5e-4` es peor que no hacer nada

Ejecutado con **`python -m bot.threshold_study`** (nuevo, §11.7) sobre **3.016
ventanas consecutivas (10,4 días)** etiquetadas al **100% con Gamma**, simulando
el bot completo: las entradas se dimensionan con el multiplicador que el bot
*tendría*, el P&L se liquida con Gamma, y el desfase por diferimiento se modela
aplicando el resultado una entrada tarde.

**Primer hallazgo: el P&L final no sirve para elegir umbral.** Con Martingale
×1.5 alcanzando ×57,7, unas pocas rachas deciden el resultado y el ranking sale
no monótono — el umbral `0`, el peor de todos con 4,52% de error, «gana» al
oráculo por $3,59. Eso es ruido, no señal. Medirlo por P&L es lo que habría
llevado a una conclusión falsa.

La métrica correcta es determinista: **cuánto se desvía cada entrada del tamaño
que tendría con liquidación perfecta.** Las señales dependen solo de las velas de
Binance y la tendencia 4h, nunca del Martingale, así que el conjunto de entradas
es idéntico en todos los escenarios y solo cambia el tamaño. Sin componente de
suerte:

| Umbral | Err. liquidación | Diferidas | Entradas mal dimensionadas | **Sobre-apostado** | Infra-apostado |
|---|---|---|---|---|---|
| 0 | 4.52% | 0.4% | 7.0% | 837 | 623 |
| **1e-5 (actual)** | **3.64%** | **2.1%** | **6.6%** | **770** | **543** |
| 2e-5 | 2.84% | 3.7% | 6.4% | 704 | 568 |
| **5e-5 ⭐** | **1.43%** | **9.1%** | **7.9%** | **533** | **641** |
| 1e-4 | 0.97% | 17.2% | 12.8% | 725 | 1102 |
| **1.5e-4 (propuesto §5)** | **0.40%** | **24.1%** | **17.4%** | **951** | **1462** |
| 2e-4 | 0.05% | 32.3% | 22.5% | 1373 | 1766 |
| 3e-4 | 0.00% | 44.2% | 31.1% | 2164 | 2328 |

La sobre-apuesta —arriesgar más de lo que corresponde, que es el modo de fallo
que vacía cuentas con Martingale— dibuja una **U nítida con mínimo en `5e-5`**:

```
837 → 770 → 704 → 533 → 725 → 951 → 1373 → 2164
                   ⭐
```

**Verificado sobre una segunda muestra** de 1.400 ventanas: el mínimo sigue en
`5e-5` (383 → 332 → 287 → **235** → 327 → 384 → 534 → 920). No es un artefacto
de la muestra.

**Conclusiones, dos de ellas contrarias al análisis original:**

1. ⭐ **`5e-5` es el óptimo medido.** Frente al `1e-5` actual: recorta el error de
   liquidación un **61%** (3,64% → 1,43%) *y* la sobre-apuesta un **31%**
   (770 → 533). Mejora en las dos métricas a la vez, no hay compromiso.
2. 🔴 **`1.5e-4` (la recomendación de §5) es peor que el umbral actual.** Elimina
   casi todo el error de liquidación, pero a cambio sobre-apuesta **un 24% más**
   que hoy (951 vs 770) y triplica las entradas mal dimensionadas (17,4% vs 6,6%).
   Introduce más daño del que quita. **No debe aplicarse.**
3. El error real de `1e-5` es **3,64%**, no el 2,84% de §5 — esa cifra venía de
   una muestra de 545 ventanas. Sobre 3.016, `2,84%` es lo que corresponde a `2e-5`.

> **Por qué §5 se equivocó:** midió únicamente el error de liquidación, que baja
> de forma monótona al subir el umbral, así que el óptimo aparente es «lo más
> alto posible». El coste del diferimiento no estaba en la ecuación. Al meterlo,
> la función deja de ser monótona y el óptimo se mueve al centro.

### 11.7 `bot/threshold_study.py` — la medición es reproducible

El estudio anterior no era repetible sin rehacerlo a mano; §9 pedía rehacer el
barrido «cada varias semanas» sin dar con qué. Ahora:

```bash
python -m bot.threshold_study                  # 3000 ventanas
python -m bot.threshold_study --windows 5000
```

Se apoya en `bot/gamma_history.py` (nuevo), que cachea las etiquetas en
`data/gamma_outcomes.json`, así que la segunda ejecución no repite descargas.
Reporta ambas métricas y **avisa explícitamente de que la columna de P&L no es
comparable entre umbrales**, para que nadie repita el error de §5.

Conviene rehacerlo cada varias semanas: el óptimo depende del régimen de
volatilidad.

### 11.2 La solución definitiva sí existe, y no es TWAP

§5 cierra con *«ningún umbral basado en Binance llega al 100%; la solución
definitiva es liquidar contra el propio Chainlink (§7.3)»*, y §7.3 propone hacerlo
con el TWAP grabado. Pero §3 ya demostró que **el TWAP no es el precio de
liquidación**. Las dos secciones se contradicen: liquidar con TWAP sustituye un
proxy sesgado (Binance) por otro proxy sesgado (una media que arrastra ~15 s).

Lo que elimina el problema de raíz es el stream **`btc-usd` normal**, que es
literalmente la fuente de resolución (§3). Y ahí falta un dato que el documento no
recoge:

- Ese stream **no está disponible en RTDS sin credenciales** — el topic
  `crypto_prices` de RTDS es Binance remuestreado (§4), no Chainlink.
- Obtenerlo exige la **ruta Chainlink directa**: API key + user secret, peticiones
  firmadas y reloj del servidor dentro de ±5 s (§1).

### 11.2.b ✅ Investigado — self-serve, **$150/mes**, y existe histórico

Consultado en la documentación oficial de Chainlink (3-ago-2026):

| Pregunta | Respuesta |
|---|---|
| ¿Self-serve? | ✅ **Sí** — portal en `app.chain.link`, sin pasar por comercial |
| ¿Coste? | 💰 **desde $150/mes por feed**, o bundles con descuento |
| ¿Tier gratuito? | ❌ **No** — *«All Data Streams subscriptions are paid. There is no free account tier»* |
| Facturación | Stripe, ciclos de 30 días, **sin prorrateo**; cancelación revisada a mano |
| Credenciales | Usuario + **HMAC secret** (servicio principal) + **API key** (Candlestick API) |
| Vista de credenciales | **Una sola vez** — no se almacenan en el portal; perderlas obliga a rotar |
| SDKs | Go, Rust, TypeScript — **no hay SDK de Python** (habría que firmar HMAC-SHA256 a mano) |

Nada de esto está gestionado por comercial: se puede contratar hoy con tarjeta.

### 11.2.c 🔴 Corrección a §6 — **sí existe histórico de Chainlink**

§6 afirma en negrita que *«no se puede backtestear contra precios históricos de
Chainlink; no existe el endpoint»*. Esa conclusión salía de la documentación de
Data Streams (*«no snapshot, history, or replay»*), que es cierta **para el
stream de reports**. Pero Chainlink expone además una **Candlestick API** que
no se consideró:

```
GET https://priceapi.dataengine.chain.link/api/v1/history/rows
    ?symbol=BTCUSD&resolution=1m&from=<unix>&to=<unix>
    Authorization: Bearer <token de /api/v1/authorize>
→ {"s":"ok","candles":[[ts, open, high, low, close, volume], ...]}
```

- Devuelve **OHLC histórico**, refrescado cada minuto (y hay endpoint de
  streaming a 1 s).
- **`BTCUSD` aparece en el catálogo** de ejemplo de `/api/v1/symbol_info`.
- Resoluciones desde **1m**; la tabla de granularidad admite ventanas de años.
- Autenticación con JWT vía `/api/v1/authorize` (`login` = user ID,
  `password` = API key), no el HMAC del servicio principal.

**Esto reabre el backtesting contra Chainlink**, que §6 daba por imposible.

> ⚠️ **Tres salvedades, ninguna verificable sin pagar la suscripción:**
>
> 1. **No está confirmado que `BTCUSD` de la Candlestick API sea el mismo dato
>    que el stream `btc-usd` que liquida los mercados.** La doc lo describe como
>    *«aggregated trading data»*, que podría ser un agregado distinto del
>    benchmark. Es exactamente el error del que §6 advierte con los agregadores
>    on-chain — hay que verificarlo antes de fiarse, no asumirlo.
> 2. **La retención no está documentada.** La tabla de resoluciones sugiere años,
>    pero no hay política publicada.
> 3. **La resolución mínima es 1m, y la liquidación usa el precio en el instante
>    exacto del boundary**, no el cierre del minuto. En ventanas ajustadas —las
>    únicas que importan— el close de 1m puede no reproducir la liquidación.

### 11.2.d Marco de decisión

Lo que $150/mes compra, y lo que no:

| Problema | ¿Lo resuelve? |
|---|---|
| Liquidar en vivo contra la fuente real (elimina el 1,43% de error y el 9,1% de diferidos) | ✅ Sí — es la solución definitiva |
| Backtesting con precios reales de Chainlink | 🟡 Probablemente, sujeto a las 3 salvedades |
| Etiquetas históricas para backtest | ⚪ Innecesario — Gamma ya las da gratis y al 100% (§11.1.b) |
| Señal de entrada | ❌ No — Binance sigue siendo más rápido (§4) |

**Recomendación:** no contratar todavía. El cambio a `5e-5` (§11.1.b) es gratis y
recorta el error de liquidación un 61%; conviene medir el residuo real durante
unas semanas antes de gastar $1.800/año. La cifra que justifica o descarta el
gasto es cuánto cuesta ese 1,43% restante en dólares — y con Martingale ×1.5 eso
solo se sabe observando producción, no en backtest (§11.1.b, primer hallazgo).

### 11.3 Retención de `chainlink_ticks` — sin política definida

§7.4 dice *«prever purga o agregación antes de dejarlo corriendo semanas»* y no
define ninguna. A 172.800 filas/día son ~5,2 M filas/mes; en SQLite (el fallback
por defecto sin `DATABASE_URL`) eso degrada las consultas por rango del backtest.

Falta decidir y escribir: ventana de retención en crudo, si se agrega a barras de
5 min y con qué método, quién dispara la purga (¿el propio tick del trader?) y
qué pasa si el proceso se cae con la tabla creciendo. Sin esto, activar
`CL_RECORD_TICKS` en el VPS es un problema diferido, no resuelto.

### 11.4 Contrato de estado y tests — sin especificar

Dos huecos de integración que el plan de §7 no cubre:

- **`state.py`**: §7.7 pide tiles de dashboard con frescura y divergencia, pero no
  define cómo `chainlink_feed.py` publica esos valores al estado compartido
  (thread-safe) ni qué expone `/state`. El dashboard no puede implementarse sin
  ese contrato.
- **Tests**: el repo mantiene 199 tests pasando (`RUTA.md` §Fase 4) y ni §7 ni
  §8 mencionan cobertura para lo nuevo. Como mínimo hacen falta: parseo E18 con
  `Decimal`, descarte por frescura, reconexión con socket nuevo (§2.3), y
  fail-open del filtro de divergencia cuando el feed está caído.

### 11.5 Checklist de go-live del 4-ago-2026

No existe un procedimiento de verificación. Antes de fiarse del feed:

1. Suscribir a ambos topics y confirmar que **no** devuelven 500.
2. Verificar el payload real contra el esquema de §1 — §10 avisa de que puede
   cambiar durante el soak testing.
3. Medir la cadencia real y la distribución `timestamp − payload.timestamp`
   durante ≥ 1 h, y **calibrar `CL_TWAP_STALE_SECONDS` con ese dato** en vez del
   default de 15 s puesto a ojo.
4. Confirmar que `btc/usd` (con barra) es el formato de símbolo correcto para
   estos topics — difiere del de `crypto_prices` (§1).
5. Solo entonces activar `CL_RECORD_TICKS`, y **con la política de §11.3 decidida**.

### 11.6 ✅ Documentos desincronizados — resuelto

El análisis dejaba otros documentos contradiciéndolo. Corregido:

- **`PLAN.md` §Resolución de operaciones** describía el umbral como «0.001% … 3
  de cada 100 ventanas», y **§Riesgos** trataba la discrepancia Binance↔Chainlink
  como *«ya observado una vez»* cuando es un fenómeno del **2,03%** de los trades.
  Ambas secciones actualizadas con las cifras medidas y enlazadas a §11.1.b.
- **`ARCHIVOS.md`** no listaba `bot/backtest.py` ni `tests/`. Añadidos, junto con
  los módulos nuevos y `data/`.

> **Regla para futuras sesiones:** una medición que contradiga a `PLAN.md` o
> `ARCHIVOS.md` obliga a actualizarlos en la misma sesión. Un número corregido
> en un solo documento es peor que no corregirlo — quien lea el otro no sabrá
> que está desactualizado.

---

## 12. 📌 Estado a día de hoy (3-ago-2026)

| Elemento | Estado |
|---|---|
| Análisis TWAP | ✅ Completo y re-verificado |
| Topics RTDS TWAP | ❌ Siguen devolviendo 500 — lanzan mañana |
| `bot/gamma_history.py` | ✅ **Implementado** — 100% cobertura sobre 3.000 ventanas |
| `bot/threshold_study.py` | ✅ **Implementado** — medición reproducible (§11.1.b) |
| Etiquetas del backtest | ✅ **Migradas a Gamma** — `--labels`, default `gamma` |
| `bot/chainlink_feed.py` | ✅ **Implementado**, apagado por defecto — listo para mañana |
| `bot/chainlink_recorder.py` | ✅ **Implementado** con purga por retención |
| Dashboard: badge del feed | ✅ **Implementado** — estado, edad y divergencia |
| `NEAR_FLAT_THRESHOLD` | ✅ **Aplicado: `1e-5` → `5e-5`** (§12.1) |
| Credenciales Chainlink | ✅ Investigado: self-serve, $150/mes (§11.2.b) |
| Señal de divergencia | ⚪ Pendiente — necesita semanas de tape para calibrar |
| TWAP como fuente de liquidación | ⚪ Descartado por ahora — §3 lo desaconseja |

### 12.1 ✅ `NEAR_FLAT_THRESHOLD` aplicado — `1e-5` → `5e-5`

```python
# bot/binance_api.py
NEAR_FLAT_THRESHOLD = 5e-5   # 0.005% — antes 1e-5
```

**Validado fuera de la muestra de calibración.** Repetido sobre las 1.000
ventanas más recientes (distintas de las 3.016 con las que se eligió el valor):

| Umbral | Liquidadas | Mal liquidadas | Tasa de error | Diferidas |
|---|---|---|---|---|
| `1e-5` (antes) | 962 | 45 | **4.68%** | 3.3% |
| **`5e-5` (ahora)** | 886 | **18** | **2.03%** | 11.0% |

**El error de liquidación cae un 57%** — de ~1 de cada 21 trades a ~1 de cada
49. El coste es diferir el 11% de las ventanas en vez del 3,3%, que es
precisamente el intercambio que §11.1.b midió y encontró favorable: la
sobre-apuesta baja un 31% pese a diferir más.

Tests: `test_threshold_is_the_calibrated_value` fija el valor y
`test_window_between_old_and_new_threshold_now_defers` cubre la banda nueva
(0,003%), donde vivían la mayoría de las liquidaciones erróneas. Suite: **233
pasan**.

> **Rehacer cada varias semanas** con `python -m bot.threshold_study`. El óptimo
> depende del régimen de volatilidad y no es un valor permanente.

### 12.2 Qué se implementó y qué falta del §7 original

| § | Elemento | Estado |
|---|---|---|
| 7.1 | Cliente RTDS | ✅ `chainlink_feed.py` — probado en vivo contra el relay |
| 7.2 | Señal de divergencia | 🟡 `feed.divergence()` existe; **falta cablear el veto** en `strategy_streak.py` (sin calibrar) |
| 7.3 | TWAP como fuente de liquidación | ⚪ No implementado — §3 lo desaconseja y §11.1.b lo hace casi irrelevante |
| 7.4 | Grabador de tape | ✅ `chainlink_recorder.py` + `chainlink_ticks` + purga |
| 7.5 | Etiquetas de backtest | ✅ `--labels gamma` por defecto |
| 7.6 | Configuración | ✅ 6 campos, todos apagados |
| 7.7 | Dashboard | ✅ Badge con edad y divergencia; ⚪ falta la columna «fuente de resolución» en el historial |
| 7.8 | Dependencias | ✅ Ninguna nueva |

### 12.3 Hallazgos de implementación no previstos en el análisis

1. 🔴 **`resolution_source` era `VARCHAR(8)` y `"chainlink"` ocupa 9.** §7.3
   afirmaba «cabe»; no cabía. Ampliado a `VARCHAR(16)`, con un mecanismo nuevo
   (`_WIDEN_COLUMNS`) porque `_add_missing_columns` solo añade columnas, nunca
   ensancha una existente — en PostgreSQL habría fallado al escribir.
2. **El backtest entraba siempre al `limit_cap`.** `backtest.py` calculaba
   `min(cap, entry_candle["open"])`, y `open` es el precio de BTC (~$63.000),
   así que el mínimo siempre era el cap. Es conservador (peor caso de entrada),
   pero es accidental, no una decisión. Sin corregir; anotado.
3. **Las etiquetas de Gamma cambian el resultado del backtest de forma
   material:** P&L combinado $652 con Gamma vs $518 con Binance sobre 2.901
   ventanas, y win rates 1-2 puntos distintos. El Fade parece mejor de lo que es
   cuando se etiqueta con Binance (55,6% vs 53,3% real).
