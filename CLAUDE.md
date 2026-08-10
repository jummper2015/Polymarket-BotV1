# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python bot ("Streak Snapper v2") that trades Polymarket's **up/down 5-minute** prediction markets, plus a Flask dashboard. One trader thread per asset (`SS_SYMBOLS`, `btc`/`eth`/`sol`, default `btc` only).

**Estrategias activas** (Fase B):

| Strategy | Mode | Edge | Status |
|---|---|---|---|
| `box_builder` | Maker — cotiza en ambos lados en la primera mitad | Colecta el spread: par redime a $1 sin riesgo direccional | **Activa** — BB_ENABLED=true cuando haya credenciales maker |
| `coin_flip_dog` | Taker late — entra a T-30..T-90 | Intra-ventana: underdog ask 0,22–0,47, coa ≤ 0,20 | **Activa** — CFD_ENABLED=true para acumular datos |
| `temporal_arb` | Taker observe — compra el líder cuando BTC cruzó el strike pero el libro no repriceó | itm_pct ≥ 0,05 %; ask líder 0,40–0,55; segunda pata si par ≤ 0,82; compra en tranches de `ta_order_slice` shares para reducir impacto | **Activa** — TA_ENABLED=true |
| `near_res` | Taker late — entra a T-5..T-20 s en el casi-ganador seguro | ask ∈ [0,97, 0,995]; recauda 1–3 ¢/share al settlement | **Activa** — NRC_ENABLED=true; riesgo de cola alto |

**Desactivadas** (fuera del registro, módulos preservados como referencia):
- `ss_fade` — medida +3,74%/op (Fase 8, t=+1,32). Módulo en `bot/strategies/ss_fade.py`.
- `ss_trend` — medida −4,22%/operación (Fase 8, t=−2,61). Módulo en `bot/strategies/ss_trend.py`.
- `spread_harvest` — observación pura, nunca operó. Módulo en `bot/strategies/spread_harvest.py`.

Defaults: `SS_SIZING=flat`, `BB_ENABLED=false`, `CFD_ENABLED=false`, `TA_ENABLED=false`, `NRC_ENABLED=false`.

⚠️ **`replit.md` está desactualizado.** Los archivos precisos son `README.md`, `docs/ARCHIVOS.md`, `docs/RUTA.md`, `docs/PLAN.md` y `.env.example`.

## Commands

```bash
python run.py                                    # arranca trader + dashboard (paper por defecto)
PORT=5055 python run.py                          # puerto alternativo

python -m pytest tests/ -q                       # suite completa (493 tests, ~7s)
python -m pytest tests/test_db.py -q             # un archivo
python -m pytest tests/test_db.py::test_name -q  # un test

python -m bot.backtest --windows 2900 --mode both --labels gamma --csv data/out.csv
python -m bot.threshold_study --windows 3000
python scripts/price_calibration.py --windows 2000
python scripts/signal_search.py
python scripts/oos_validation.py
python scripts/preopen_edge.py
python scripts/regime_filter.py
```

No hay linter ni type-checker configurado. Los scripts `pnpm` en `package.json` y los árboles TypeScript en `lib/` y `artifacts/` son restos de la plantilla del workspace y no tienen relación con el bot.

El bot **rechaza arrancar** si `DASHBOARD_HOST` no es loopback y `DASHBOARD_PASSWORD` está vacío (`bot/auth.py:verify_startup_config`) — el dashboard puede pasar el bot a dinero real. Para uso local: `DASHBOARD_HOST=127.0.0.1`.

Backtests y scripts de calibración golpean las APIs públicas de Binance y Gamma; tardan minutos y necesitan timeouts generosos.

## Architecture

`run.py` → `bot/main.py:main()` arranca, en un proceso: un poller de precio spot y un hilo `StreakSnapperTrader` **por símbolo en `cfg.ss_symbols`**, un feed opcional Chainlink TWAP (solo BTC), y la app Flask en el hilo principal. Cada símbolo es completamente independiente — su propio `BotState`, sus propias filas de martingala, su propio loop de ventana.

### El ciclo de ventana — `bot/streak_trader.py`

Una iteración de `_run_one_window()` por boundary de 5 minutos:

1. `market.load_market_for_current_window(symbol=…)` — resuelve IDs de tokens UP/DOWN de la API Gamma para el slug actual.
2. `_resolve_pending_trades()` + `_confirm_binance_resolutions()` — liquida ventanas pasadas.
3. `PriceFeed` (CLOB v2 WebSocket) arranca y se mantiene conectado durante toda la ventana.
4. `_check_regime()` → `bot/regime.py`. Corre **antes** de las señales.
5. Evalúa señales: `descriptor.evaluate(ctx)` para cada estrategia habilitada.
6. `_execute_signal()` — ejecuta la señal taker (fade/CFD).
7. `_wait_out_window(tokens)` — espera el cierre de la ventana; toca `observe` y `evaluate_late` cada `OBSERVE_TICK_SECONDS = 4 s`. **Box builder vive aquí** — su máquina de estados se ejecuta en el hook `observe`.
8. Liquida **esta** ventana antes de abrir la siguiente.

El paso 7 está basado en carga: si ninguna estrategia tiene `observe` ni `evaluate_late`, es un único `wait` bloqueante — sin overhead.

### Pre-fetch de tokens

El slug siguiente es determinista (`window_ts + 300`), así que `_wait_out_window` dispara `_start_prefetch` en background durante la espera ociosa. Elimina el round-trip a Gamma del camino crítico del ciclo siguiente (~1–3 s).

### Espera inteligente de WebSocket

`_ws_ready` es un `threading.Event` que el feed dispara al recibir el primer precio. El ciclo hace `_ws_ready.wait(timeout=2.0)` en lugar de `time.sleep(2.0)`. En conexión sana sale en < 200 ms.

### Resolución: dos fuentes, en orden

Binance liquida al cierre de vela; Gamma (el resultado oficial de Polymarket) publica ~3 minutos después. `_resolve_once()` pregunta primero a Binance (`resolution_source="binance"`), y `_confirm_binance_resolutions()` luego revalida contra Gamma y corrige el P&L en caso de discrepancia. Velas near-flat (`|Δ| ≤ NEAR_FLAT_THRESHOLD`) devuelven `None` de Binance y difieren a Gamma.

### Sizing — `flat` | `kelly` | `martingale`

`StreakStrategy._size_for()` es el único punto de entrada; `SS_SIZING` elige el modo y todos los modos pasan por el mismo techo de riesgo (`MAX_BANKROLL_FRACTION = 0.10`, suelo `MIN_SHARES = 5`). Default `flat`.

### Box Builder — `bot/strategies/box_builder.py`

Máquina de estados ejecutada en `observe` cada 4 s:

```
START (armed=None)
  ├─ libro ausente o secs < QUOTE_CUTOFF_SEC    → armed=False (BB_SKIP_*)
  ├─ ask_UP + ask_DOWN < BB_ARM_MIN_SPREAD      → armed=False (BB_SKIP_NARROW)
  └─ place_maker_bid en UP y DOWN               → armed=True

armed=True, 0 fills
  └─ cada BB_REPRICE_INTERVAL s: si > BB_REPRICE_BEHIND del best_bid → repreciar

armed=True, 1 fill (p1 = precio llenado)
  ├─ secs > BB_BAILOUT_SEC:
  │    si other_ask ≤ BB_COMPLETE_TAKER_CAP − p1 → lift (taker)  → BOX_COMPLETE
  │    else                                       → subir bid maker
  └─ secs ≤ BB_BAILOUT_SEC (T-90):
       COA ≥ BB_MIN_COA_HOLD y favorece el lado → HOLD pata descubierta
       else                                      → CUT a best_bid

armed=True, 2 fills → BOX_COMPLETE — hold pasivo

secs ≤ BB_CANCEL_ALL_SEC (T-10) → cancelar todo
```

**Nuevos métodos CLOB en `StreakSnapperTrader`:**
- `_place_maker_bid(token_id, price, shares)` — GTC post_only; reintentar 1 c más bajo si crosses book.
- `_place_taker_order(token_id, side, price, shares)` — GTC marketable.
- `_cancel_token_orders(token_id)` — cancela todas las órdenes en ese token.
- `_get_position_size(token_id)` — consulta `data-api.polymarket.com/positions`.
- `_record_box_fill(tokens, direction, token_id, price, shares)` — persiste la pata en `trades` con `strategy="box_builder"`.

Paper mode: `_place_maker_bid` devuelve un id sintético; `_get_position_size` devuelve 0.0 siempre (la máquina de estados corre, las órdenes no son reales).

### Coin-Flip Dog — `bot/strategies/coin_flip_dog.py`

Señal tardía ejecutada en `evaluate_late`:
- Gate 1: `seconds_left ∈ [CFD_ENTRY_MIN_LEFT, CFD_ENTRY_MAX_LEFT]` (30–90 s).
- Gate 2: `coa = |mark − strike| / ATR4 ≤ CFD_MAX_COA` (0,20).
- Gate 3: underdog ask ∈ [0,22, 0,47].
- Una entrada por ventana. Taker GTC. Aguanta hasta resolución.

### El registro de estrategias — `bot/strategies/`

Un descriptor es un `StrategyDescriptor` (datos), no una subclase. Campos clave:

| Campo | Propósito |
|---|---|
| `evaluate(ctx)` | Señal al abrir la ventana (ss_fade) |
| `observe(ctx)` | Tick cada 4 s durante la ventana (box_builder) |
| `evaluate_late(ctx)` | Señal tardía dentro de la ventana (coin_flip_dog) |
| `enabled_when` | Espejo declarativo de `is_enabled` para /settings |
| `priority` | Desempate cuando dos estrategias apuntan al lado contrario |

**Nada en este paquete puede importar `bot.config`** — config importa el registro para construir `RUNTIME_FIELDS`.

`StrategyContext` ahora incluye `trader: Any = None` para que `observe` pueda colocar órdenes CLOB a través del trader (donde vive el cliente CLOB).

### Filtros de régimen — `bot/regime.py`, todos apagados por defecto

`hours_filter`, `volatility_filter`, `range_filter`. Percentiles computados sobre una ventana rodante `PERCENTILE_LOOKBACK` (2 días). **Todos apagados por defecto a propósito.**

### Estado, config y precedencia

- `bot/state.py` — un `BotState` por símbolo detrás de un `RLock`, en `STATES = {symbol: BotState}`.
- Precedencia de config al arrancar: **filas `bot_config` en DB anulan `.env`** (`_apply_persisted_overrides`), porque `/settings` tiene un botón Guardar.
- `RUNTIME_FIELDS` en `bot/config.py` es el catálogo de parámetros editables en runtime, construido como `BASE_FIELDS + strategies.params()`. Declara un parámetro de **estrategia** en su descriptor y uno **del bot** en `BASE_FIELDS`.

### Dashboard — `bot/dashboard.py`

Rutas: `/`, `/settings`, `/login`, `/logout`, `/state`, `/api/trades`, `/api/trades.csv`, `/api/metrics/series`, `POST /config`, `POST /config/reset`, `/healthz`.

KPIs (`stats`, `strategy_stats`) se agregan **en SQL sobre toda la tabla `trades`** (`_aggregate_db_stats`); la lista `trades` en el payload son solo las últimas 100 filas.

### Persistencia — `bot/db.py`

Flask-SQLAlchemy sobre `DATABASE_URL`, con fallback a `data/streak_snapper.db` (SQLite) para dev local. Tablas: `trades`, `martingale_state`, `bot_config`, `chainlink_ticks`.

### Chainlink TWAP — apagado por defecto

`bot/chainlink_feed.py` + `bot/chainlink_recorder.py`, todos los flags `CL_*` default off. Es un **filtro**, no fuente de resolución. Checklist: `docs/CHAINLINK_TWAP.md`.

## Conventions

- Comentarios y docstrings en inglés explicando el *porqué*. Mensajes de log y UI del dashboard en español. Mantener ambos.
- Afirmaciones medidas (win rates, umbrales, drawdowns) pertenecen a `docs/RUTA.md` con el tamaño de muestra y el comando usado.
- `bot/trader.py` y `bot/archive/*` son código muerto conservado como referencia; nada los importa.
- Frontend: vanilla JS + Bootstrap/Chart.js/Notika bajo `bot/static/vendor/`. Sin build step. Todos los colores en `dashboard.css`.
- `data/*.csv`, `data/gamma_outcomes.json` y `*.db` son artefactos regenerables en el gitignore.

### Candidate strategies — `Revisar Estrategias/`

Implementaciones de referencia autónomas (`.py` + `readme.md`). **No están conectadas al bot** y no son importables desde `bot/`.

| Directorio | Idea en una línea | Estado de integración |
|---|---|---|
| `spread_harvest_maker/` | Bid pasivo en libro ancho (0,40–0,48), lado underdog. | Deactivada — código en `bot/strategies/spread_harvest.py` |
| `box_builder/` | Bids en ambos lados cap $0,94; par redime a $1,00. | **Integrada** en `bot/strategies/box_builder.py` |
| `mid_price_continuation/` | Compra el lado líder en banda 40–55c. | Pendiente integración |
| `corridor/` | Compra 15m leader + lado opuesto de la ventana 5m final. | Pendiente (requiere mercados 5m y 15m simultáneos) |
| `liq_cascade_chaser/` | Compra la continuación de una cascada de liquidaciones. | Pendiente (requiere feed de liquidaciones) |
| `small_liq_continuation/` | Mismo señal, tier barato ($25K–$500K). | Pendiente (requiere feed de liquidaciones) |
| `streak_snapper/` | El original de lo que se publicó como `ss_fade`. | Ya en `bot/` |

## Deployment caveat

`.replit` despliega con `gunicorn --bind 0.0.0.0:5000 main:app`, pero el `main.py` raíz es un stub de plantilla sin objeto `app`, y gunicorn serviría el dashboard sin arrancar nunca el hilo de trading. El punto de entrada correcto es `python run.py`.
