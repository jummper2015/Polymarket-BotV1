# 📁 ARCHIVOS — Streak Snapper v2

> Referencia completa de todos los archivos del bot, su función y dependencias.

---

## 🌳 Árbol del proyecto (bot/)

```
bot/
├── __init__.py              ← Package init (vacío)
│
├── main.py                  ← 🚀 Entry point
├── config.py                ← ⚙️ Configuración (env vars)
├── state.py                 ← 🔒 Estado compartido (thread-safe)
├── logger.py                ← 📝 Logging por mercado
│
├── binance_api.py           ← 📊 Cliente Binance (BTC klines)
├── strategy_streak.py       ← 🧠 Motor de estrategias + Martingale
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
tests/                          ← 🧪 278 tests
├── test_binance_api.py         ← 28 tests
├── test_strategy_streak.py     ← 27 tests
├── test_db.py                  ← 28 tests
├── test_streak_trader.py       ← 118 tests
├── test_chainlink_feed.py      ← 19 tests (E18, frescura, 500, fail-open)
├── test_chainlink_recorder.py  ←  8 tests (batching, purga, fallo de DB)
└── test_dashboard.py           ← 45 tests (auth, filtros, CSV, drawdown, saldo)

data/
├── gamma_outcomes.json      ← Caché de etiquetas de Gamma (no versionar)
└── streak_snapper.db        ← SQLite si no hay DATABASE_URL
```

---

## 📄 Archivo por archivo

### `bot/main.py` — Entry point

**Rol:** Arranca el bot. Inicializa DB, configura estado, lanza StreakSnapperTrader + dashboard Flask.

**Dependencias internas:**
- `bot.config.load_config()` — Carga config de env vars
- `bot.state.STATE.configure()` — Aplica config al estado
- `bot.db.init_db()` — Inicializa PostgreSQL/SQLite
- `bot.dashboard.create_app()` — Crea app Flask
- `bot.dashboard.start_price_fetcher()` — Inicia poller CoinGecko
- `bot.streak_trader.StreakSnapperTrader` — Thread del bot

**Ejecutar:** `python run.py` → `bot.main.main()`

---

### `bot/config.py` — Configuración

**Rol:** Dataclass `Config` con todos los parámetros cargados desde variables de entorno.

**Env vars que lee:**
```
TRADING_MODE, STARTING_BANKROLL,
CHAIN_ID, SIGNATURE_TYPE, PRIVATE_KEY, PROXY_WALLET,
CLOB_HOST, GAMMA_HOST, CLOB_WS_URL,
PORT, DASHBOARD_HOST, POLL_INTERVAL_MS,
MARKET_RETRY_SECONDS, FIRST_PRICE_TIMEOUT,
SS_ENABLED, SS_MODE,
SS_FADE_BASE_BET, SS_FADE_LIMIT_CAP, SS_FADE_STREAK_MIN,
SS_TREND_BASE_BET, SS_TREND_LIMIT_CAP,
SS_MARTINGALE_MULT
```

---

### `bot/state.py` — Estado compartido

**Rol:** Clase `BotState` con lock `threading.RLock()`. Almacena:
- Configuración runtime del Streak Snapper
- Precios bid/ask/mid en vivo
- Estado del Martingale (multiplicador actual, racha de pérdidas)
- Lista de trades en memoria (cache)
- Log de eventos
- Historial de precios para gráficos del dashboard

**Exports:**
- `STATE` — Instancia global de BotState
- `STATES = {"btc": STATE}` — Dict para compatibilidad con dashboard
- `Trade` — Dataclass de trade en memoria

---

### `bot/binance_api.py` — Cliente Binance

**Rol:** Obtener datos BTC desde la API pública de Binance. Sin API key.

**Funciones:**
| Función | Retorna | Uso |
|---|---|---|
| `get_5min_windows(n=16)` | Lista de ventanas 5m con dirección | Forma 1 — detección de rachas |
| `get_4h_trend()` | Vela 4h actual con dirección | Forma 2 — tendencia |
| `get_4h_trend_cached(last_ts)` | Trend con marca de caché | Forma 2 — tendencia |
| `get_window_direction(window_ts)` | `"UP"` / `"DOWN"` / `None` | Liquidar trades al cierre de ventana |
| `get_btc_spot_price()` | Precio spot BTC actual | Display en dashboard |

`get_window_direction` devuelve `None` (y se recurre a Gamma) si la vela aún no
ha cerrado, no está en el rango consultado, o cerró exactamente plana.

**Endpoint:** `api.binance.com/api/v3/klines` (público, rate-limited)

---

### `bot/strategy_streak.py` — Motor de estrategias

**Rol:** Generar señales de entrada y gestionar el estado Martingale.

**Clase principal:** `StreakSnapperStrategy`

**Métodos clave:**
| Método | Descripción |
|---|---|
| `get_fade_signal()` | Detecta racha ≥ 4 ventanas misma dirección → señal contraria |
| `get_trend_signal()` | Detecta dirección vela 4h → señal a favor |
| `on_win(strategy)` | Resetea multiplicador a 1.0 (persiste en DB) |
| `on_loss(strategy)` | Multiplica ×1.5 (persiste en DB) |

**Dataclass:** `StreakSignal` — Contiene dirección, limit_cap, bet_amount, multiplier, loss_streak.

---

### `bot/streak_trader.py` — Thread principal

**Rol:** Ciclo infinito sincronizado a ventanas de 5 minutos.

**Clase:** `StreakSnapperTrader(threading.Thread)`

**Ciclo por ventana (`_run_one_window`):**
1. `market.load_market_for_current_window()` → tokens UP/DOWN
2. `_resolve_pending_trades()` → resuelve trades de ventanas pasadas
3. `_confirm_binance_resolutions()` → confirma/corrige con Gamma
4. `PriceFeed.start()` → WebSocket bid/ask
5. `strategy.get_fade_signal()` / `get_trend_signal()` → señales
   ↳ si apuntan a lados opuestos, se descartan ambas
6. `_execute_signal()` → LIMIT BUY en CLOB V2 + guardar en DB
7. Esperar fin de ventana
8. `_resolve_pending_trades(wait_for_slug=...)` → liquidar ESTA ventana antes de
   abrir la siguiente, para que la Martingala esté al día
9. Repetir

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
| `TradeModel` | `trades` | strategy, direction, entry_price, shares, cost, pnl, status, resolution_source |
| `MartingaleStateModel` | `martingale_state` | strategy, multiplier, loss_streak |
| `BotConfigModel` | `bot_config` | key, value |

**Helpers:**
| Función | Descripción |
|---|---|
| `init_db(app, url)` | Inicializa SQLAlchemy, crea tablas, aplica columnas nuevas |
| `_add_missing_columns()` | Añade columnas introducidas después de crear la tabla (`create_all()` no lo hace) |
| `db_context()` | Context manager que pushea Flask app context (seguro en threads) |
| `get_or_create_martingale_state(strategy)` | Carga/crea estado Martingale |
| `reset_martingale_state(strategy)` | Reset ×1.0 tras ganar |
| `advance_martingale_state(strategy, factor)` | Multiplica tras perder |

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
| GET | `/state` | JSON con todo el estado del bot |
| POST | `/config` | Actualizar configuración |
| GET | `/healthz` | Health check |

Los KPIs (`stats`, `strategy_stats`) se agregan **en SQL sobre toda la tabla**
`trades`; la lista `trades` del payload son solo las últimas
`TRADE_TABLE_LIMIT` (100) filas, para la tabla del historial.

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
