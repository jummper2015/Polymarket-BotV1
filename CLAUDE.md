# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working
with code in this repository.

## What this is

ME$IRVE — a Python bot ("Streak Snapper v2") that trades Polymarket's
**up/down 5-minute** prediction markets, plus a Flask dashboard, a
landing page, and an admin login. One trader thread per asset
(`SS_SYMBOLS`, default `btc` only).

The bot operates ONE strategy, `temporal_arb`, which takes directional
positions on Polymarket BTC 5-min markets based on TWAP-60s deviation
from strike. The strategy was rebuilt 2026-09-21 with a local TWAP-60s
source (gap vs official <$5), a profit-lock completion path, and a
martingale hedge path. Both new paths default OFF for safety.

Stack: Python 3.12, Flask, SQLite, requests, websocket-client. No
frontend build step — vanilla JS + Bootstrap for dashboard, hand-written
SVG for the landing page.

## Active strategy (Fase TA — 2026-09-23)

| Path | Descripción | Estado |
|---|---|---|
| Entry (Path A) | Compra líder cuando BTC cruza el strike | ✅ activo |
| Pair completion | Cierra par cuando segundo leg cheap | ✅ activo |
| Hedge Recovery (Path B) | Compra opuesto si primer leg cae fuerte | ✅ activo |
| Stop-loss | 3 disparadores: time threshold, trailing, catastrófico | ✅ activo |
| TWAP-aware hedge (Path C) | Hedge anticipado si TWAP-60s se opone | ⚪ OFF (manual) |
| Profit-Lock completion (Path D) | Cierre forzado tras 30s en ganancias | ⚪ OFF (manual) |
| Martingale hedge (Path E) | Compra opuesta con qty multiplicada en pérdida | ⚪ OFF (manual) |
| Late Pair Taker (LPT) | Compra ambos lados si suma ≤ cap al cierre | ⚪ OFF |

Defaults conservatively OFF for all new features. Manual activation via
/settings. `bot/state.py` defaults are also conservative (`ta_min_itm_pct=0.025`,
`ta_min_normalized_impulse=0.8`). For validation the operator applied a
tighter config to `bot_config` (overrides defaults):

| Campo | Default código | VPS actual |
|---|---|---|
| `ta_min_itm_pct` | 0.025 | **0.05** |
| `ta_min_normalized_impulse` | 0.8 | **0.7** |
| `ta_shares_per_leg` | 5 | **30** |
| `ta_complete_cap` | 0.82 | 0.82 |
| `ta_bailout_sec` | 60 | 60 |
| `ta_entry_cutoff_sec` | 150 | **90** |
| `ta_hedge_drop_pct` | 0.40 | 0.40 |
| `ta_hedge_max_sum` | 0.92 | **0.94** |
| `ta_stop_loss_time_sec` | 60 | 60 |
| `ta_stop_loss_threshold` | 0.25 | **0.35** |
| `ta_trailing_stop_pct` | 0.30 | 0.30 |
| `ta_catastrophic_loss_pct` | 0.50 | **0.45** |
| `starting_bankroll` | 1000.0 (.env) | $1000.00 |

## Commands

```bash
python run.py                                    # arranca trader + dashboard (paper por defecto)
PORT=5055 python run.py                          # puerto alternativo
python -m pytest tests/ -q                       # suite completa (~7s, 581 passed + 1 skipped)
python -m pytest tests/test_temporal_arb.py -q   # solo TA tests
python -m pytest tests/test_dashboard.py -q      # solo tests del dashboard
```

No hay linter ni type-checker configurado. Los scripts `pnpm` en
`package.json` y los árboles TypeScript en `lib/` y `artifacts/` son
restos de la plantilla del workspace y no tienen relación con el bot.

El bot **rechaza arrancar** si `DASHBOARD_HOST` no es loopback y
`DASHBOARD_PASSWORD` está vacío (`bot/auth.py:verify_startup_config`) — el
dashboard puede pasar el bot a dinero real. Para uso local:
`DASHBOARD_HOST=127.0.0.1`.

## Architecture

`run.py` → `bot/main.py:main()` arranca, en un proceso:

- Poller de spot price
- Un hilo `StreakSnapperTrader` por símbolo en `SS_SYMBOLS`
- Background thread `coinbase_ticker_feed` (WebSocket BTC-USD, 90s buffer)
- App Flask en hilo principal

Cada símbolo es completamente independiente — su propio `BotState`, sus
propias filas de martingale (legacy), su propio loop de ventana.

### El ciclo de ventana (`bot/streak_trader.py`)

1. `market.load_market_for_current_window(symbol=...)` — resuelve IDs de
   tokens UP/DOWN de Polymarket.
2. `_resolve_pending_trades()` + `_confirm_binance_resolutions()` — liquida
   ventanas pasadas.
3. `PriceFeed` (CLOB v2 WebSocket) arranca y se mantiene conectado durante
   toda la ventana.
4. `_observe()` (en `temporal_arb.py`) se ejecuta cada ~4s con el
   contexto del window.
5. Liquida esta ventana antes de abrir la siguiente.

### `bot/strategies/temporal_arb.py`

Función principal: `_observe(ctx)`. Aplica los paths en orden (Path A → B
→ C → D → E → Bailout). Cada tick:
1. Llama `get_strike()` (TWAP-60s local con fallback a Coinbase candle OPEN
   y luego Chainlink).
2. Compara spot vs strike usando TWAP rolling 60s como referencia (no spot
   crudo) cuando `ta_use_twap_signal=true`.
3. `find_leader_side()` decide UP/DOWN según el signo del itm_pct.
4. Acumula tranches en `accumulating` → `half_open` cuando la primera pata
   está llena.
5. En `half_open`, dispara los paths según el estado del par y del precio.

Helper clave: `find_leader_side(spot, strike, ask_up, ask_dn, min_itm_pct,
min_ask, max_ask, twap=None)` — acepta `twap` opcional que se usa como
precio de referencia en lugar de spot.

### Strike source: TWAP-60s local

`bot/polymarket_twap_tracker.py` mantiene un buffer rolling de 90s de
ticks BTC-USD desde Coinbase. Expone:
- `get_state(window_ts, current_ts, strike, current_btc)` — para el
  ventana de resolución [T+240, T+300]
- `get_rolling_twap(current_ts, lookback_seconds=60)` — para la ventana
  de entrada (cualquier momento)
- `should_hedge(...)` — para decisión de hedge durante los últimos 60s
- `feed_from_ticker_buffer(...)` — pull ticks del buffer global

### State y persistencia

- `bot/state.py` — un `BotState` por símbolo detrás de un `RLock`, en
  `STATES = {symbol: BotState}`. Se carga de `.env` + `bot_config` table.
- Precedencia de config al arrancar: filas `bot_config` en DB anulan
  `.env`. `/settings` puede persistir cambios sin restart.
- `bot/db.py` — Flask-SQLAlchemy sobre `data/streak_snapper.db` (SQLite
  para dev local). Tablas: `trades`, `martingale_state`, `bot_config`,
  `chainlink_ticks`.

### Landing pública + auth

- `/` → `landing.html` (público, 8 secciones con scroll-reveal animations)
- `/login` → `login.html` (Notika split: panel verde + form card)
- `/dashboard` → `dashboard.html` (auth requerida, contiene KPIs + charts
  + métricas por estrategia)
- `/settings` → `settings.html` (editor de RuntimeFields)
- `/state` → JSON con todo el state (KPI tiles, pnl_breakdown por
  período, strategy_stats, etc.)

`bot/auth.py:init_auth(app)` configura el `before_request` hook que
bloquea todas las rutas excepto `login`, `healthz`, `static`, `index`
(landing).

## Conventions

- Comentarios y docstrings en inglés explicando el *porqué*. Mensajes de
  log y UI del dashboard en español. Mantener ambos.
- Afirmaciones medidas (win rates, umbrales, drawdowns) pertenecen a
  `docs/AUDITORIA_TA_2026-09-18.md` con el tamaño de muestra y el comando
  usado.
- `bot/trader.py` y `bot/archive/*` son código muerto conservado como
  referencia; nada los importa.
- Frontend: vanilla JS + Bootstrap/Chart.js/Notika bajo
  `bot/static/vendor/`. Sin build step. Colores en `dashboard.css`.
- `data/*.csv`, `data/gamma_outcomes.json` y `*.db` son artefactos
  regenerables en el gitignore.

## Don't

- No usar `git pull` en VPS — está en detached HEAD con commits propios,
  divergencia con origin/main. Workflow: scp archivos + restart.
- No sobreescribir `bot/main.py` desde local — tiene código VPS-específico
  (`ss_martingale_mult_factor`, `polymarket_balance` import). Restaurar
  de `git checkout e8f8c6b -- bot/main.py` antes de cualquier cambio.
- No dejar `ta_profit_lock_enabled=true` o `ta_mart_hedge_enabled=true`
  sin validación previa — ambos paths son recoveries agresivos que pueden
  amplificar pérdidas en trending markets.
- No commitear archivos `data/` o `*.db` — son regenerables y
  contienen datos del bankroll.

## Deployment caveat

`.replit` despliega con `gunicorn --bind 0.0.0.0:5000 main:app`, pero el
`main.py` raíz es un stub de plantilla sin objeto `app`, y gunicorn
serviría el dashboard sin arrancar nunca el hilo de trading. El punto de
entrada correcto es `python run.py`.
