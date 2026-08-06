# 📁 ARCHIVOS — Streak Snapper v2

> Referencia completa de todos los archivos del bot, su función y dependencias.

---

## 🌳 Árbol del proyecto (bot/)

```
bot/
├── __init__.py              ← Package init (vacío)
│
├── main.py                  ← 🚀 Entry point (un hilo trader por activo)
├── config.py                ← ⚙️ Configuración (env vars)
│                               RUNTIME_FIELDS = BASE_FIELDS + registro
├── runtime_field.py         ← 🏷️ RuntimeField: rango, persistencia y cómo se pinta
├── state.py                 ← 🔒 Estado compartido (thread-safe), uno por activo
├── logger.py                ← 📝 Logging por mercado
│
├── strategies/              ← 📋 Registro de estrategias
│   ├── __init__.py          ←    REGISTRY, params(), enabled_for(), desempate
│   ├── base.py              ←    StrategyDescriptor + StrategyContext
│   ├── ss_fade.py           ←    Forma 1 (envoltorio; la lógica sigue en
│   │                             strategy_streak.py)
│   └── ss_trend.py          ←    Forma 2, apagada por defecto
│
├── binance_api.py           ← 📊 Cliente Binance (BTC/ETH/SOL klines)
├── strategy_streak.py       ← 🧠 Señales + sizing (flat/kelly/martingala)
├── regime.py                ← 🚦 Filtros de régimen: horario, volatilidad, rango
├── streak_trader.py         ← 🔄 Thread principal del ciclo 5min
│
├── market.py                ← 🔍 Descubrimiento de tokens (Gamma API)
├── price_feed.py            ← 📡 WebSocket precios bid/ask (CLOB)
├── dashboard.py             ← 🌐 Flask dashboard + API REST
├── auth.py                  ← 🔐 Login, sesión y guardia de arranque seguro
├── trade_queries.py         ← 🔎 Filtros, paginación y series de métricas (SQL)
├── db.py                    ← 🗄️ Modelos SQLAlchemy + persistencia
├── backtest.py              ← 📈 Backtest sobre klines históricos
│                               Features de Binance, etiquetas de Gamma (--labels)
├── gamma_history.py         ← 🏷️ Etiquetas resueltas de Gamma + caché en disco
├── threshold_study.py       ← 📐 Calibración de NEAR_FLAT_THRESHOLD (§11.1.b)
├── chainlink_feed.py        ← 🔗 Cliente RTDS TWAP (off por defecto)
├── chainlink_recorder.py    ← 💾 Grabador de tape + purga por retención
│
├── trader.py                ← 📦 [LEGACY] Trader original (no usado)
│
├── templates/
│   ├── base.html            ← Esqueleto común (cabecera, nav, assets)
│   ├── dashboard.html       ← Panel principal
│   ├── settings.html        ← Configuración
│   └── login.html           ← Acceso (standalone, sin cabecera)
│
├── static/
│   ├── dashboard.js         ← Polling de /state y render en vivo
│   ├── charts.js            ← Chart.js: capital, drawdown, win rate, P&L
│   ├── trades.js            ← Tabla: filtros, paginación 25, export CSV
│   ├── settings.js          ← Lógica de /settings
│   ├── dashboard.css        ← Overrides sobre Notika (todos los colores aquí)
│   └── vendor/              ← Bootstrap, Chart.js, Bootstrap Icons, Notika
│       └── NOTICE.md        ← Licencias y cómo actualizarlos
│
└── archive/                 ← 🗃️ Estrategias viejas
    ├── strategy_mm.py       ← Box Builder Market Making
    ├── strategy_corridor.py ← Corridor Collector
    └── corridor_trader.py   ← Thread del Corridor
```

Fuera de `bot/`:

```
tests/                          ← 🧪 383 tests
├── test_binance_api.py         ←  30 tests
├── test_strategy_streak.py     ←  47 tests (señales, sizing flat/kelly, ciclo 4h)
├── test_db.py                  ←  40 tests (incluye migración de symbol)
├── test_streak_trader.py       ← 118 tests
├── test_regime.py              ←  24 tests (funciones puras, sin red)
├── test_strategies.py          ←  35 tests (registro, params, desempate)
├── test_chainlink_feed.py      ←  19 tests (E18, frescura, 500, fail-open)
├── test_chainlink_recorder.py  ←   8 tests (batching, purga, fallo de DB)
└── test_dashboard.py           ←  62 tests (auth, filtros, CSV, registro, activo)

scripts/                        ← 🔬 Medición (Fase 8, docs/RUTA.md)
├── signal_search.py            ← Gradúa señales candidatas contra Gamma
├── oos_validation.py           ← Ordena en la primera mitad, mide en la segunda
├── preopen_edge.py             ← ROI al precio previo a la apertura vs a +60 s
├── regime_filter.py            ← Franja horaria, volatilidad y rango
├── cap_impact.py               ← El cap como filtro de entrada (Fase A.1)
└── price_calibration.py        ← Calibración del precio de mercado

data/                        ← Todo regenerable, nada versionado
├── gamma_outcomes.json      ← Caché de etiquetas de Gamma
├── klines_5m_cache.json     ← Caché de velas de Binance (scripts/)
├── preopen_prices.json      ← Cotizaciones previas a la apertura
└── streak_snapper.db        ← SQLite si no hay DATABASE_URL
```

---

## 📄 Archivo por archivo

### `bot/main.py` — Entry point

**Rol:** Arranca el bot. Inicializa DB, configura estado, lanza **un
`StreakSnapperTrader` por símbolo de `SS_SYMBOLS`** + dashboard Flask.

**Dependencias internas:**
- `bot.config.load_config()` — Carga config de env vars
- `bot.state.state_for(symbol).configure()` — Aplica config a cada estado
- `bot.db.init_db()` — Inicializa PostgreSQL/SQLite
- `bot.dashboard.create_app()` — Crea app Flask
- `bot.dashboard.start_price_fetcher()` — Inicia poller de spot por activo
- `bot.streak_trader.StreakSnapperTrader` — Un thread por activo

**Ejecutar:** `python run.py` → `bot.main.main()`

---

### `bot/config.py` — Configuración

**Rol:** Dataclass `Config` con todos los parámetros cargados desde variables de
entorno, y `RUNTIME_FIELDS` — el catálogo de lo que se puede cambiar en caliente
desde `/settings`.

`RUNTIME_FIELDS = BASE_FIELDS + strategies.params()`: los parámetros de cada
estrategia los aporta su descriptor, así que declarar uno basta para tener
parseo, rango, `POST /config`, persistencia en `bot_config` y su widget en la
UI. Hay una guardia contra nombres duplicados.

**Env vars que lee:**
```
TRADING_MODE, STARTING_BANKROLL,
CHAIN_ID, SIGNATURE_TYPE, PRIVATE_KEY, PROXY_WALLET,
CLOB_HOST, GAMMA_HOST, CLOB_WS_URL,
PORT, DASHBOARD_HOST, POLL_INTERVAL_MS,
MARKET_RETRY_SECONDS, FIRST_PRICE_TIMEOUT,
SS_ENABLED, SS_MODE, SS_SYMBOLS,
SS_FADE_BASE_BET, SS_FADE_LIMIT_CAP, SS_FADE_STREAK_MIN,
SS_TREND_BASE_BET, SS_TREND_LIMIT_CAP, SS_TREND_MIN_STRENGTH,
SS_SIZING, SS_KELLY_FRACTION, SS_MARTINGALE_MULT,
SS_TRADING_HOURS, SS_VOL_MIN_PCT, SS_VOL_MAX_PCT, SS_RANGE_MAX_PCT,
CL_* (ver docs/CHAINLINK_TWAP.md)
```

---

### `bot/runtime_field.py` — Declaración de un parámetro

**Rol:** `RuntimeField` — nombre, tipo, rango, si persiste, y cómo se pinta
(`label`, `hint`, `step`, `scale`, `choice_labels`). Vive aparte de `config.py`
para que `bot/strategies/` pueda declarar parámetros sin importar config, que
ahora importa el registro.

`scale` es la conversión de presentación: `ss_trend_min_strength` se guarda como
fracción y se muestra en porcentaje, y esa relación se declara una vez aquí en
lugar de repetirse en el JS de carga y en el de guardado.

---

### `bot/strategies/` — Registro de estrategias

**Rol:** Una estrategia se declara como dato (`StrategyDescriptor`), no
heredando de una clase base: difieren demasiado en cómo generan señal (leer una
vela, dejar una orden puesta, escuchar liquidaciones) como para compartir
superclase. Lo común es el papeleo.

**Campos del descriptor:** `id`, `name`, `description`, `notes`, `params`,
`symbols` (vacío = todos), `priority` (desempate), `is_enabled(state)`,
`enabled_when` (espejo declarativo para la UI) y `evaluate(ctx) -> list[Signal]`.

**Funciones del paquete:**
- `ids()` / `all_descriptors()` / `get(id)` — inventario
- `params()` — alimenta `RUNTIME_FIELDS`
- `enabled_for(state, symbol)` — qué corre en esta ventana, por prioridad
- `resolve_conflicts(signals)` — `(kept, dropped)` cuando dos señales apuntan a
  lados opuestos; gana la de mayor prioridad (fade 100, trend 50)
- `to_json(state)` — lo que `/state` sirve y `/settings` renderiza

`ss_fade` y `ss_trend` son envoltorios: su lógica sigue en `strategy_streak.py`.

---

### `bot/regime.py` — Filtros de régimen

**Rol:** Decidir cuándo **no** operar. Funciones puras sobre velas
(`{ts, open, high, low, close}`), testeables sin red:

- `hours_filter(spec)` — franjas UTC (`"13-21,21-24"`)
- `volatility_filter(...)` — ATR de 1h entre percentiles
- `range_filter(...)` — rango de 2h bajo un percentil
- `evaluate(...)` — los tres juntos → `RegimeVerdict(allowed, reason, detail)`

Los percentiles se calculan sobre `PERCENTILE_LOOKBACK` (2 días) en vez de
constantes fijas, que dejarían de significar lo mismo al cambiar el régimen.
Todos apagados por defecto: se probaron ~20 filtros contra 35 días.

---

### `bot/state.py` — Estado compartido

**Rol:** Clase `BotState` con lock `threading.RLock()`. Almacena:
- Configuración runtime del Streak Snapper
- Precios bid/ask/mid en vivo
- Estado del Martingale (multiplicador actual, racha de pérdidas)
- Lista de trades en memoria (cache)
- Log de eventos
- Historial de precios para gráficos del dashboard

- Contador de ventanas descartadas por motivo (`skips`)

**Exports:**
- `STATES = {symbol: BotState}` — Un estado por activo operado
- `state_for(symbol)` / `active_states()` — Acceso y alta perezosa
- `STATE` — Alias del estado de BTC, para compatibilidad con dashboard y tests
- `Trade` — Dataclass de trade en memoria

`snapshot()` deriva `strategy_stats` y el bloque `strategies` del registro, así
que una estrategia nueva aparece en el dashboard sin tocar este archivo.

---

### `bot/binance_api.py` — Cliente Binance

**Rol:** Obtener datos de mercado desde la API pública de Binance. Sin API key.
Todas las funciones aceptan `symbol` (`btc` / `eth` / `sol`); `SYMBOL_PAIRS` es
el único sitio donde el nombre corto del slug de Polymarket se traduce al par de
Binance. XRP y DOGE tienen mercado pero cotizan 3-6 centavos de spread contra 1,
y el edge medido no llega a 2: quedan fuera.

**Funciones:**
| Función | Retorna | Uso |
|---|---|---|
| `get_5min_windows(n=16, symbol)` | Lista de ventanas 5m con dirección | Forma 1 — detección de rachas |
| `get_last_closed_4h_candle(symbol)` | Última vela 4h **cerrada** | Forma 2 — tendencia |
| `get_5min_candles(n, symbol)` | Velas OHLC | Filtros de régimen |
| `get_window_direction(window_ts, symbol)` | `"UP"` / `"DOWN"` / `None` | Liquidar trades al cierre de ventana |
| `get_btc_spot_price(symbol)` | Precio spot actual | Display en dashboard |

`get_window_direction` devuelve `None` (y se recurre a Gamma) si la vela aún no
ha cerrado, no está en el rango consultado, o cerró exactamente plana.

**Endpoint:** `api.binance.com/api/v3/klines` (público, rate-limited)

---

### `bot/strategy_streak.py` — Motor de estrategias

**Rol:** Generar señales de entrada, dimensionar la apuesta y gestionar el
estado Martingale. Los descriptores de `bot/strategies/` envuelven estos
métodos; la lógica vive aquí.

**Clase principal:** `StreakSnapperStrategy`

**Métodos clave:**
| Método | Descripción |
|---|---|
| `get_fade_signal()` | Detecta racha ≥ N ventanas misma dirección → señal contraria |
| `get_trend_signal()` | Lado fijado por la última vela 4h **cerrada** → señal a favor |
| `_size_for(strategy, cap)` | Único punto de dimensionado: `flat` / `kelly` / `martingale` |
| `on_win(strategy)` | Resetea multiplicador a 1.0 (persiste en DB) |
| `on_loss(strategy)` | Multiplica por `ss_martingale_mult_factor` (persiste en DB) |

`_size_for` dimensiona al `limit_cap`, no al precio de fill: el cap es el peor
precio que aceptamos, así que un fill mejor solo sale más conservador. Todos los
modos pasan por el mismo techo (`MAX_BANKROLL_FRACTION = 0.10`) y el mismo suelo
(`MIN_SHARES = 5`), y `kelly` devuelve 0 shares —no opera— cuando no ve ventaja
al cap. `MEASURED_WIN_PROB` sale de la Fase 8; el de `ss_trend` está por debajo
de su precio a propósito.

**Dataclass:** `StreakSignal` — estrategia, dirección, limit_cap, shares,
multiplier, loss_streak, signal_reason.

---

### `bot/streak_trader.py` — Thread principal

**Rol:** Ciclo infinito sincronizado a ventanas de 5 minutos.

**Clase:** `StreakSnapperTrader(threading.Thread)`

**Ciclo por ventana (`_run_one_window`):**
1. `market.load_market_for_current_window(symbol=…)` → tokens UP/DOWN
2. `_resolve_pending_trades()` → resuelve trades de ventanas pasadas
3. `_confirm_binance_resolutions()` → confirma/corrige con Gamma
4. `PriceFeed.start()` → WebSocket bid/ask
5. `_check_regime()` → si un filtro rechaza, se cuenta el motivo y se espera al
   cierre. Va **antes** de las señales: una ventana filtrada cuesta una llamada
   a Binance en vez del camino completo
6. `strategies.enabled_for(state, symbol)` → cada descriptor evalúa; un fallo se
   registra y no tumba la ventana
   ↳ `resolve_conflicts()` si apuntan a lados opuestos: gana la de más prioridad
7. `_execute_signal()` → LIMIT BUY en CLOB V2 + guardar en DB con su `symbol`
8. Esperar fin de ventana
9. `_resolve_pending_trades(wait_for_slug=...)` → liquidar ESTA ventana antes de
   abrir la siguiente, para que la Martingala esté al día
10. Repetir

**Helpers relevantes:**
| Función | Descripción |
|---|---|
| `_floor_to_tick(price)` | Redondea a la baja al tick de $0.01 con `Decimal` (con floats, `0.29` caía a `0.28`) |
| `_resolve_once()` | Una pasada de liquidación: Binance primero, Gamma si Binance no decide |
| `_confirm_binance_resolutions()` | Contrasta con Gamma lo liquidado por Binance y corrige el P&L |

---

### `bot/db.py` — Capa de persistencia

**Rol:** Modelos SQLAlchemy + helpers con manejo automático de Flask app context.

**Modelos:**
| Modelo | Tabla | Campos clave |
|---|---|---|
| `TradeModel` | `trades` | **symbol**, strategy, direction, entry_price, shares, cost, pnl, status, resolution_source |
| `MartingaleStateModel` | `martingale_state` | strategy, **symbol**, multiplier, loss_streak — `UNIQUE(strategy, symbol)` |
| `BotConfigModel` | `bot_config` | key, value |
| `ChainlinkTickModel` | `chainlink_ticks` | symbol, ts, price |

**Helpers:**
| Función | Descripción |
|---|---|
| `init_db(app, url)` | Inicializa SQLAlchemy, crea tablas, aplica columnas nuevas |
| `_add_missing_columns()` | Añade columnas introducidas después de crear la tabla (`create_all()` no lo hace) |
| `_migrate_martingale_symbol()` | Reconstruye `martingale_state` para añadir el `UNIQUE(strategy, symbol)`: SQLite no puede añadirlo en caliente |
| `_backfill_symbol()` | Rellena con `'btc'` las filas anteriores al multi-activo |
| `db_context()` | Context manager que pushea Flask app context (seguro en threads) |
| `get_or_create_martingale_state(strategy, symbol)` | Carga/crea estado Martingale |
| `reset_martingale_state(strategy, symbol)` | Reset ×1.0 tras ganar |
| `advance_martingale_state(strategy, factor, symbol)` | Multiplica tras perder |

La unicidad por `(strategy, symbol)` es lo que impide que una racha perdedora en
BTC redimensione la siguiente entrada de ETH.

Los tres helpers de Martingale devuelven un `MartingaleSnapshot` (NamedTuple),
no la instancia ORM: hacen `commit()`, y un commit expira todos los atributos,
así que el objeto queda inservible en cuanto se cierra el app context.

---

### `bot/market.py` — Descubrimiento de tokens

**Rol:** Buscar los token IDs de UP/DOWN para una ventana vía Gamma API.

**Funciones clave:**
- `load_market_for_current_window()` — Bloquea hasta encontrar el mercado
- `fetch_market()` — One-shot fetch
- `MarketTokens` — Dataclass con slug, window_ts, up_token_id, down_token_id

---

### `bot/price_feed.py` — WebSocket de precios

**Rol:** Suscribirse al WebSocket de Polymarket CLOB v2 para recibir bid/ask/mid en tiempo real.

**Clase:** `PriceFeed`
- `start()` / `stop()` — Control de ciclo de vida
- Callback `on_price(side, bid, ask, mid)` → `state.update_price()`
- Auto-reconexión en desconexiones

---

### `bot/dashboard.py` — API REST + Dashboard

**Rol:** Servir el panel web y endpoints de configuración.

**Endpoints:**
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Dashboard HTML |
| GET | `/settings` | Configuración HTML |
| GET | `/state` | JSON con todo el estado del bot. `?symbol=eth` elige qué mercado describe el panel en vivo |
| GET | `/api/trades` | Página del historial. Filtros: strategy, **symbol**, status, mode, direction, from, to, q |
| GET | `/api/trades.csv` | El mismo conjunto filtrado, en CSV |
| GET | `/api/metrics/series` | Capital, drawdown y win rate. `?symbol=` recorta a un mercado |
| POST | `/config` | Actualizar configuración |
| POST | `/config/reset` | Descartar overrides guardados y volver al `.env` |
| GET | `/healthz` | Health check |

Los KPIs (`stats`, `strategy_stats`, `symbol_stats`) se agregan **en SQL sobre
toda la tabla** `trades`, agrupando por `(strategy, symbol)`; la lista `trades`
del payload son solo las últimas `TRADE_TABLE_LIMIT` (100) filas, para la tabla
del historial.

`/state` sirve además `strategies` (el registro) y `fields` (la declaración de
cada `RuntimeField`), que es de donde `/settings` se dibuja entero.

---

### `bot/logger.py` — Logging

**Rol:** Logger thread-aware que escribe al `BotState` del thread actual.

**Funciones:** `info()`, `ok()`, `warn()`, `err()`, `transient()`
- `set_context(state)` — Vincular un BotState al thread actual
- Mensajes transient usan `\r` para overwrite en consola

---

## 📦 Archivos no modificados

| Archivo | Estado |
|---|---|
| `bot/__init__.py` | Sin cambios (vacío) |
| `bot/logger.py` | Sin cambios |
| `bot/price_feed.py` | Sin cambios |
| `bot/trader.py` | Legacy — no se importa, conservado como referencia |

---

## 🗃️ Archivos de archivo

| Archivo | Estrategia original |
|---|---|
| `bot/archive/strategy_mm.py` | Box Builder Market Making |
| `bot/archive/strategy_corridor.py` | Corridor Collector |
| `bot/archive/corridor_trader.py` | Thread del Corridor (15m) |
