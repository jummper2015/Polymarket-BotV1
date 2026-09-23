# ME$IRVE — Polymarket BTC 5-min Bot

Bot automatizado que opera los mercados **up/down de Bitcoin a 5 minutos**
en [Polymarket](https://polymarket.com). Una sola estrategia activa
(`temporal_arb`) con strike source TWAP-60s local, pares balanceados,
hedge recovery, stop-loss dinámico y dos paths nuevos (profit-lock y
martingale) listos para activar manualmente.

Web dashboard en tiempo real con KPIs, métricas por estrategia, libro
de órdenes en vivo y control de configuración. Landing pública en
`https://polytradebot.cloud/` con auth rediseñado.

---

## Estado actual (2026-09-23)

- **Estrategia activa:** `temporal_arb` (única estrategia operativa)
- **Modo:** paper mode con bankroll **$1000.00**
- **Strike source:** local TWAP-60s desde Coinbase WebSocket
  (gap vs oficial < $5)
- **Estrategia configurada:** conservador (validación 3-5 días)
- **Despliegue:** VPS Hostinger LT (`srv1702650`) en Vilnius, Lithuania
- **Landing:** `https://polytradebot.cloud/`
- **Auth:** Notika split layout con green panel + form card

### Features operativas

| Path | Descripción | Estado |
|---|---|---|
| **Entry (Path A)** | Compra líder cuando BTC cruza el strike | ✅ activo |
| **Pair completion** | Cierra par cuando segundo leg cheap | ✅ activo |
| **Hedge Recovery (Path B)** | Compra opuesto si primer leg cae fuerte | ✅ activo |
| **Stop-loss** | 3 disparadores: time threshold, trailing, catastrófico | ✅ activo |
| **TWAP-aware hedge (Path C)** | Hedge anticipado si TWAP-60s se opone | ⚪ OFF (manual) |
| **Profit-Lock completion (Path D)** | Cierre forzado tras 30s en ganancias | ⚪ OFF (manual) |
| **Martingale hedge (Path E)** | Compra opuesta con qty multiplicada en pérdida | ⚪ OFF (manual) |
| **Late Pair Taker (LPT)** | Compra ambos lados si suma ≤ cap al cierre | ⚪ OFF |
| **TWAP entry signal** | Reemplaza spot por TWAP rolling 60s | ✅ activo |

---

## Arranque rápido

```bash
# Instalar dependencias
pip install -r requirements.txt

# Copiar y editar variables de entorno
cp .env.example .env
# Editar: DASHBOARD_PASSWORD, STARTING_BANKROLL, etc.

# Arrancar (paper mode por defecto)
python run.py

# Suite de tests
python -m pytest tests/ -q
# Output: 581 passed, 1 skipped
```

El dashboard queda en `http://localhost:5000`.

> ⚠️ El bot **rechaza arrancar** si `DASHBOARD_HOST` no es loopback y
> `DASHBOARD_PASSWORD` está vacío (`bot/auth.py:verify_startup_config`).
> Para uso local: `DASHBOARD_HOST=127.0.0.1`.

---

## Comandos útiles

```bash
# Suite completa
python -m pytest tests/ -q

# Verificar operativa en VPS
ssh root@76.13.251.202
systemctl status polymarket-bot

# Log en vivo
tail -f /opt/polymarket-bot/logs/bot.log | grep -E "TA_|💰|🛡"

# Trades resueltos últimas 24h
sqlite3 -header -column /opt/polymarket-bot/data/streak_snapper.db \
  "SELECT COUNT(*) total, SUM(won=1) wins, ROUND(100.0*SUM(won=1)/COUNT(*),1) wr, ROUND(SUM(pnl),2) pnl FROM trades WHERE opened_at > datetime('now','-1 day');"
```

---

## Arquitectura

```
run.py
  ↓
bot/main.py:main()
  ↓ (proceso único)
  ├── Spot price poller (CoinGecko fallback)
  ├── [por símbolo en SS_SYMBOLS]
  │     └── bot/streak_trader.py — StreakSnapperTrader
  │           ├── PriceFeed (CLOB v2 WebSocket)
  │           ├── coinbase_ticker_feed (background thread, 90s rolling)
  │           └── temporal_arb._observe() (cada 4s durante la ventana)
  ├── coinbase_ticker_feed.start() — WebSocket Coinbase BTC-USD
  └── bot/dashboard.py — Flask app
        ├── GET /              → landing.html (público)
        ├── GET /login         → login rediseñado (Notika split)
        ├── GET /dashboard     → dashboard.html (auth required)
        ├── GET /settings      → settings.html (RuntimeFields editor)
        ├── GET /metrics       → métricas adicionales
        └── GET /state         → JSON state (KPI tiles, pnl_breakdown, etc.)
```

### Ciclo de ventana (5 min)

1. `bot/market.py` — resuelve IDs de tokens UP/DOWN de Polymarket vía Gamma.
2. `bot/polymarket_price.py:get_strike()` — obtiene strike via rolling TWAP-60s.
3. `_resolve_pending_trades()` + `_confirm_binance_resolutions()` — liquida pasadas.
4. `_observe()` se ejecuta cada ~4s con el contexto (window_ts, secs_left, ask_up/dn, spot).
5. Lógica temporal_arb (ver sección "Estrategia").

---

## Estrategia: `temporal_arb`

Archivo: `bot/strategies/temporal_arb.py`. Una sola función `_observe()` aplica los
caminos en orden:

1. **Path A — Pair completion:** si `first_px + opp_ask ≤ ta_complete_cap`,
   compra el segundo leg y completa el par (lock instantáneo).
2. **Path B — Hedge Recovery:** si el primer leg cae ≥ `ta_hedge_drop_pct` y
   `first_px + hedge_ask ≤ ta_hedge_max_sum`, compra el lado opuesto para
   acotar la pérdida.
3. **Path C — TWAP-aware hedge** *(OFF)*: en los últimos 60s, si la
   TWAP-60s oficial proyecta el lado opuesto con `|margin| ≥ $50`,
   hedge anticipado.
4. **Path D — Profit-Lock completion** *(OFF)*: tras `profit_lock_min_secs`
   (30s) con la pata en ganancias, fuerza cierre si `pair ≤ profit_lock_cap`
   ($1).
5. **Path E — Martingale hedge** *(OFF)*: tras `mart_hedge_min_secs` (30s) en
   pérdidas, compra opuesta con `qty × mult × (1 + loss_pct)` hasta
   `mart_hedge_max_rounds` (3).
6. **Bailout** si todo falla: a T-60s la pata queda sola y se liquida.

### Strike source: TWAP-60s local

Archivo: `bot/polymarket_twap_tracker.py`. Background WebSocket thread
suscrito a `wss://ws-feed.exchange.coinbase.com`, canal `ticker`, producto
`BTC-USD`. Mantiene un buffer rolling de 90s de ticks.

Cuando `_observe()` necesita una señal:
- `get_rolling_twap(current_ts, lookback_seconds=60)` — TWAP de los últimos 60s
- Si el buffer tiene <10 ticks → devuelve None → fallback a spot

Esto suaviza el ruido intra-spread y produce menos flips de dirección que
el spot crudo.

---

## Configuración

Toda la config se gestiona desde `/settings` en el dashboard (bot_config
table + .env override). Defaults conservadores:

| Campo | Default | Propuesto validación |
|---|---|---|
| `ta_min_itm_pct` | 0.025 | **0.05** |
| `ta_min_normalized_impulse` | 0.8 | **0.7** |
| `ta_shares_per_leg` | 5 | **30** |
| `ta_complete_cap` | 0.82 | **0.82** |
| `ta_bailout_sec` | 60 | **60** |
| `ta_entry_cutoff_sec` | 150 | **90** |
| `ta_hedge_drop_pct` | 0.40 | **0.40** |
| `ta_hedge_max_sum` | 0.92 | **0.94** |
| `ta_stop_loss_time_sec` | 60 | **60** |
| `ta_stop_loss_threshold` | 0.25 | **0.35** |
| `ta_trailing_stop_pct` | 0.30 | **0.30** |
| `ta_catastrophic_loss_pct` | 0.50 | **0.45** |
| `ta_profit_lock_enabled` | false | false |
| `ta_mart_hedge_enabled` | false | false |
| `ta_use_twap_signal` | true | true |
| `ta_twap_lookback_sec` | 60 | 60 |
| `ta_lpt_enabled` | false | false |

Para más detalle de cada parámetro y conflictos entre ellos:
**`docs/AUDITORIA_TA_2026-09-18.md`**.

---

## Despliegue

Workflow documentado en `docs/DEPLOY_VPS_HOSTINGER.md`. Resumen:

- **VPS:** Hostinger LT, Ubuntu 24.04, hostname `srv1702650`
- **IP:** 76.13.251.202
- **Usuario:** root
- **Repo:** `/opt/polymarket-bot/` (detached HEAD `e8f8c6b`)
- **Servicio:** `systemctl {start,stop,restart,status} polymarket-bot`
- **Deploy workflow:** git push → `sshpass scp <files>` → `cp bot/file.py`
  → `systemctl restart polymarket-bot`. **Nunca `git pull`** — divergencia
  con VPS.

### Backup antes de cambios destructivos
```bash
cp -r /opt/polymarket-bot /opt/polymarket-bot.bak.$(date +%Y%m%d)
```

---

## Documentación relacionada

| Doc | Propósito |
|---|---|
| `CLAUDE.md` | Guía para Claude Code sobre este repo |
| `docs/AUDITORIA_TA_2026-09-18.md` | Análisis de conflictos TA + config óptima |
| `docs/ESTADO_BOT_2026-09-17.md` | Estado completo al cierre de sesión |
| `docs/DEPLOY_VPS_HOSTINGER.md` | Deploy paso a paso |
| `docs/PAPER_TO_REAL_RUNBOOK.md` | Migración paper → real |
| `docs/RUTA.md` | Roadmap del proyecto |
| `docs/PLAN.md` | Plan estratégico |
| `docs/ARCHIVOS.md` | Mapa de archivos |

---

## Licencia y disclaimer

El trading automatizado comporta riesgo. Este bot opera por defecto en
**paper mode**. La activación en modo real requiere credenciales L2
configuradas explícitamente. Use bajo su responsabilidad.
