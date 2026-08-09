# Streak Snapper v2 — Polymarket BTC/ETH/SOL Bot

Bot automatizado que opera los mercados **up/down de 5 minutos** en [Polymarket](https://polymarket.com). Incluye un dashboard Flask en tiempo real con KPIs, métricas por estrategia, libro de órdenes en vivo y control de configuración.

---

## Estrategias activas

| Estrategia | Modo | Edge | Estado |
|---|---|---|---|
| `box_builder` | Maker — cotiza en ambos lados en la primera mitad | Par redime a $1 sin riesgo direccional | **Activa** — `BB_ENABLED=true` con credenciales maker |
| `coin_flip_dog` | Taker tardío — entra a T-30..T-90 | Underdog ask 0,22–0,47, coa ≤ 0,20 | **Activa** — `CFD_ENABLED=true` para acumular datos |

**Desactivadas** (módulos preservados como referencia):
- `ss_fade` — medida +3,74%/op (Fase 8) pero descontinuada en esta fase.
- `ss_trend` — medida −4,22%/op (t=−2,61). Sin edge.
- `spread_harvest` — solo observación, nunca operó.

---

## Arranque rápido

```bash
# Instalar dependencias
pip install -r requirements.txt

# Copiar y editar variables de entorno
cp .env.example .env

# Arrancar (paper por defecto)
python run.py

# Puerto alternativo
PORT=5055 python run.py
```

El dashboard queda disponible en `http://localhost:5000`.

> **Nota:** El bot rechaza arrancar si `DASHBOARD_HOST` no es loopback y `DASHBOARD_PASSWORD` está vacío. Para uso local: `DASHBOARD_HOST=127.0.0.1`.

---

## Comandos útiles

```bash
# Suite de tests (493 tests, ~7s)
python -m pytest tests/ -q

# Backtest completo (golpea Binance + Gamma, tarda minutos)
python -m bot.backtest --windows 2900 --mode both --labels gamma --csv data/out.csv

# Estudio de umbrales
python -m bot.threshold_study --windows 3000

# Calibración de precios
python scripts/price_calibration.py --windows 2000
```

---

## Arquitectura

`run.py` → `bot/main.py:main()` arranca un poller de precio spot, un hilo `StreakSnapperTrader` por símbolo en `SS_SYMBOLS`, un feed opcional Chainlink TWAP (solo BTC) y la app Flask en el hilo principal.

### Ciclo de ventana (`bot/streak_trader.py`)

1. `market.load_market_for_current_window()` — resuelve IDs de tokens UP/DOWN.
2. `_resolve_pending_trades()` + `_confirm_binance_resolutions()` — liquida ventanas pasadas.
3. `PriceFeed` (CLOB v2 WebSocket) arranca y se mantiene durante toda la ventana.
4. `_check_regime()` — filtros de régimen (todos off por defecto).
5. Evalúa señales: `descriptor.evaluate(ctx)` para cada estrategia habilitada.
6. `_execute_signal()` — ejecuta señal taker (coin_flip_dog).
7. `_wait_out_window()` — espera cierre; `observe` y `evaluate_late` cada 4 s. **Box Builder vive aquí.**
8. Liquida esta ventana antes de abrir la siguiente.

### Box Builder

Máquina de estados ejecutada en `observe` cada 4 s. Coloca bids maker en UP y DOWN en la primera mitad de la ventana (`BB_QUOTE_CUTOFF_SEC`). Cuando ambas patas llenan, el par redime a $1,00 — sin riesgo direccional, ≥ 6 c/par bloqueado.

### Coin-Flip Dog

Señal tardía en `evaluate_late` (T-30 a T-90 s). Entra en el lado underdog cuando `ask ∈ [0,22, 0,47]` y `coa = |mark − strike| / ATR4 ≤ 0,20`.

### Resolución: dos fuentes

Binance liquida al cierre de vela (`resolution_source="binance"`). Gamma (resultado oficial Polymarket) publica ~3 min después y corrige discrepancias en `_confirm_binance_resolutions()`.

---

## Variables de entorno principales

| Variable | Por defecto | Descripción |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` o `real` |
| `STARTING_BANKROLL` | `1000.0` | Bankroll inicial |
| `SS_SYMBOLS` | `btc` | Mercados a operar (`btc`, `eth`, `sol`) |
| `SS_ENABLED` | `true` | Activa el trader |
| `SS_SIZING` | `flat` | `flat`, `kelly` o `martingale` |
| `BB_ENABLED` | `false` | Activa Box Builder (requiere maker orders) |
| `CFD_ENABLED` | `false` | Activa Coin-Flip Dog |
| `PORT` | `5000` | Puerto del dashboard |
| `DASHBOARD_HOST` | `0.0.0.0` | Host del dashboard |
| `DASHBOARD_PASSWORD` | — | Contraseña (obligatoria si host no es loopback) |
| `PRIVATE_KEY` | — | Clave privada wallet Polygon (solo modo real) |
| `PROXY_WALLET` | — | Proxy wallet Polymarket (solo modo real) |

Ver `.env.example` para la lista completa.

---

## Fuente de datos

| Dato | Fuente | Uso |
|---|---|---|
| Precio spot BTC/ETH/SOL | **Binance** REST (velas 5m y 4h) | Resolución de ventanas, filtros de régimen, ATR |
| Precios UP/DOWN en tiempo real | **Polymarket CLOB v2 WebSocket** | Señales, sizing, libro de órdenes |
| Resultado oficial | **Gamma API** (Polymarket) | Confirmación de resolución ~3 min post-cierre |
| Precio spot en dashboard | CoinGecko (cada 10 s) | Solo visualización en el header |
| TWAP (opcional) | **Chainlink** on-chain | Filtro adicional — off por defecto (`CL_TWAP_ENABLED=false`) |

Binance es la fuente primaria para la resolución de resultados. Ver [Binance puede seguir usándose](#binance).

---

## Dashboard web

Disponible en `http://localhost:5000`.

| Ruta | Descripción |
|---|---|
| `/` | Dashboard principal (KPIs, precios, libro, log) |
| `/settings` | Configuración en tiempo real |
| `/state` | JSON snapshot del estado activo |
| `/api/trades` | Historial de trades paginado |
| `/api/trades.csv` | Exportación CSV |
| `/api/metrics/series` | Series temporales de métricas |
| `POST /config` | Actualizar parámetros en caliente |
| `POST /config/reset` | Resetear a valores del `.env` |
| `/healthz` | Health check |

---

## Pasar a modo real

1. Crear cuenta en [Polymarket](https://polymarket.com) y depositar USDC en Polygon.
2. Obtener `PRIVATE_KEY` de tu wallet Polygon.
3. Obtener la dirección `PROXY_WALLET` de Polymarket.
4. Configurar en `.env`:
   ```
   TRADING_MODE=real
   PRIVATE_KEY=0x...
   PROXY_WALLET=0x...
   ```
5. Reiniciar el bot. El dashboard muestra el checklist de readiness.

---

## Advertencias de riesgo

- Los mercados de predicción son instrumentos especulativos de alto riesgo.
- El modo paper no garantiza los mismos resultados en modo real.
- Nunca compartas tu `PRIVATE_KEY` ni la subas a un repositorio público.
- Empieza siempre con montos pequeños para validar el comportamiento en producción.
- Este software se provee tal cual, sin garantía de ningún tipo.
