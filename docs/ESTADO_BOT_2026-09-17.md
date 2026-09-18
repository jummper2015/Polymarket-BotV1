# Estado del bot — 2026-09-17

Documento vivo que resume dónde está el bot y qué está disponible. Actualizar
al final de cada sesión con cambios relevantes.

---

## TL;DR

- Bot activo en VPS `srv1702650` (76.13.251.202), Vilnius, Lithuania.
- **Strike source:** local TWAP-60s desde Coinbase WebSocket (gap vs oficial: $0-5).
- **Strikes frescos por ventana** verificado en producción (cada ventana con su valor único).
- **TA (Temporal Arb)** operando con strikes precisos; Pair net positivo post-fix.
- **TWAP-aware hedge** implementado y desplegado, pero **`ta_twap_hedge_enabled=False`** por default — código inerte, listo para activar vía /settings cuando se quiera.
- **Tests:** 555 passed localmente, 61 de los nuevos verificados en VPS.

---

## Bug crítico resuelto: strike stale

### Síntomas (antes del fix)
- Bot usaba el mismo strike durante 30+ minutos (6+ ventanas 5-min).
- Lecturas invertidas: BTC debajo del strike real → bot compraba UP → perdía.
- Trade #290, #291, #292 documentados como pérdidas consecutivas con strike stale.

### Causa raíz
- `bot/chainlink_strike.py:get_strike_at()` retorna `latest_price` siempre que
  `latest_updated_at <= window_ts` — siempre cierto dentro del heartbeat de
  ~30 min del aggregator `0xF403…eE88c` de Chainlink legacy.
- Polymarket usa strike desde Chainlink `btc-usd-twap-60s` (TWAP, no spot).
- Polimarket's `crypto-price` devuelve agregado 30-min en lugar de 5-min strike.

### Cadena de fixes (5 commits en `main`)

| Commit | Cambio | Impacto |
|---|---|---|
| `54eecf4` | Coinbase candle OPEN → priority #1 | Bot ya no depende de Chainlink stale |
| `8568aca` | Query sin `start/end` (incluye candle forming) | El candle del boundary actual se incluye |
| `208a920` | Fallback a `raw[-1].close` si boundary aún no publicado | Coinbase REST tiene delay ~5-30s post-window-open |
| `024d732` | **Local TWAP-60s** priority #1 (WebSocket Coinbase) | Gap vs oficial: $0-5 (vs $20-30 con OPEN) |
| `6e32b65` | Fix `bot.logger` vs stdlib logging | Error `'Logger' object has no attribute 'ok'` |

### Verificación post-fix
- 50 ventanas con strikes únicos (cada ventana con su valor propio).
- 18:30 ventana: TWAP-60s local = $76,682.38, Coinbase OPEN = $76,682.13 → gap $0.25 (0.0003%).
- Primer par post-fix (ventana 20:15): UP@0.55 + DOWN@0.25 → DOWN ganó → neto par +$10.40.

---

## Estado actual del VPS

### Servicios
- `polymarket-bot.service` activo, restart en crash (RestartSec=10).
- Dashboard en http://polytradebot.cloud (protegido con password).
- Coinbase ticker feed WebSocket conectado (background daemon thread).

### Archivos críticos
| Archivo | Estado |
|---|---|
| `/opt/polymarket-bot/bot/coinbase_ticker_feed.py` | TWAP-60s WebSocket source |
| `/opt/polymarket-bot/bot/coinbase_api.py` | `get_5min_candle_open_at` con fallback close |
| `/opt/polymarket-bot/bot/chainlink_strike.py` | Legacy fallback (sigue funcionando, pero no se usa normalmente) |
| `/opt/polymarket-bot/bot/polymarket_price.py` | Priority chain: TWAP → Coinbase OPEN → Chainlink → Polymarket |
| `/opt/polymarket-bot/bot/main.py` | **VPS-specific** (commit `e8f8c6b`), NUNCA sobreescribir desde local |

### Configuración runtime efectiva
```
ta_enabled=true
ta_min_itm_pct=0.02 (override .env, default es 0.025 post-deploy)
ta_shares_per_leg=80
ta_order_slice=80
ta_lpt_enabled=true, cap=0.92
ta_hedge_enabled=true, drop_pct=0.30, max_sum=0.96
ta_twap_hedge_enabled=FALSE (nuevo, default conservador)
ta_twap_hedge_margin=50.0
ta_twap_hedge_max_sum=0.92
```

---

## Features implementadas pero inactivas

### 1. TWAP-aware early hedge (Path C en temporal_arb)

**Qué hace:** En los últimos 60s de la ventana, si la posición está `half_open`
y la TWAP-60s oficial proyecta el lado opuesto con margen ≥ $50, compra el lado
opuesto como hedge ANTES de que el precio caiga lo suficiente para activar Path B.

**Cuándo se activa:** Manual via /settings → `ta_twap_hedge_enabled = true`.

**Por qué está desactivado por default:** Es código nuevo sin validar en
producción. Su activación es opt-in.

**Logs esperados cuando se active:**
```
🧭 TWAP EARLY HEDGE  primera=UP@0.550  proy_TWAP_winner=DOWN  margin=-$300.00
   hedge=DOWN@0.350  costo_total=0.900  breakeven_locked=+0.1000/share  left=45s
```

**Observability:** `state.record_observation("TA_TWAP_HEDGE")` se registra.

### 2. `MIN_ITM_PCT_DEFAULT = 0.025` (vs 0.05 antes)

**Qué hace:** Más trades potenciales (umbral más bajo para entrada).
**No-op en producción** porque `.env` tiene `TA_MIN_ITM_PCT=0.02`, que sobrescribe.
Para aprovechar el default nuevo, editar `.env` o cambiar via /settings.

---

## Tests

### Suite local (555 passed)
```
python -m pytest tests/ -q --ignore=tests/test_spread_harvest.py
# 555 passed, 1 skipped
```

### Archivos de test relevantes
- `tests/test_polymarket_price.py` (22 tests): priority chain, cache validation.
- `tests/test_coinbase_ticker_feed.py` (20 tests): buffer mechanics, parser, TWAP math, lifecycle.
- `tests/test_polymarket_twap_tracker.py` (13 tests): state math, projection, hedge.
- `tests/test_temporal_arb.py` (47 tests): incluye 7 nuevos para Path C.

### Fallas pre-existentes (no relacionadas)
- `tests/test_spread_harvest.py::TestObservation`: 3 fallas. Estrategia desactivada,
  no se importa desde `bot/`. Mantenidas como referencia histórica.

---

## Cómo conectar y operar

### SSH
```bash
sshpass -e ssh -o StrictHostKeyChecking=accept-new root@76.13.251.202
# Password en env var SSHPASS (NUNCA en línea de comandos)
```

### Comandos útiles
```bash
# Ver estado
systemctl is-active polymarket-bot

# Restart (esperar ≥10s por RestartSec)
systemctl restart polymarket-bot && sleep 10 && systemctl is-active polymarket-bot

# Logs en vivo
tail -f /opt/polymarket-bot/logs/bot.log
tail -f /opt/polymarket-bot/logs/bot.error.log

# Buscar strikes recientes
grep -aE "Strike obtenido" /opt/polymarket-bot/logs/bot.log | tail -10

# Buscar errores
grep -aE "error|Error|ERROR|fallback" /opt/polymarket-bot/logs/bot.log | tail -20

# Trades recientes
sqlite3 -header -column /opt/polymarket-bot/data/streak_snapper.db \
  "SELECT id, direction, ROUND(entry_price,3) as px, ROUND(pnl,2) as pnl, won, datetime(opened_at) FROM trades ORDER BY id DESC LIMIT 10;"

# Estado del ticker feed
cd /opt/polymarket-bot && source venv/bin/activate
PYTHONPATH=/opt/polymarket-bot python3 -c "from bot.state import STATE; print(f'enabled={STATE.ta_twap_hedge_enabled}')"
```

### Deploy workflow
**NUNCA `git pull`** — VPS está en detached HEAD `e8f8c6b` con commits propios.
Workflow seguro:
1. Commit local + push a origin/main
2. `sshpass -e scp <archivos> root@76.13.251.202:/opt/polymarket-bot/`
3. SSH → `cp file.py bot/file.py` (o `cp file.py bot/subdir/`)
4. `systemctl restart polymarket-bot`
5. Wait ≥10s, check `is-active`

**Excepción:** `bot/main.py` tiene código VPS-específico (`ss_martingale_mult_factor`,
`polymarket_balance` import). Restaurar SIEMPRE desde `git checkout e8f8c6b -- bot/main.py`
antes de cualquier deploy que toque main.py.

---

## Deuda técnica / Mejoras pendientes

### Resolver en algún momento
1. **`bot/polymarket_balance.py:131` AttributeError** — `state.update_polymarket_balance`
   no existe. Bot sigue corriendo (este thread falla solo, no afecta trading).
   Probablemente renombrado/movido en algún commit que no se mergeó a VPS.

2. **3 tests de `spread_harvest`** fallando — estrategia desactivada, tests no
   migrados. Mantenidos por referencia; podrían eliminarse o arreglarse.

3. **`pre-fetch` de tokens** — el `_start_prefetch` reduce latencia pero no se
   ha medido empíricamente el impacto.

### Posibles siguientes pasos
- **Backtest con strikes TWAP-60s** — validar que las señales habrían ganado con
  strikes precisos. Necesita histórico de ticker BTC-USD (guardar local).
- **Reducir `TA_MIN_ITM_PCT` en .env** — ahora 0.02, podría ser 0.015 con strikes precisos.
- **Activar `ta_twap_hedge_enabled`** — observado durante 1-2 días sin él, luego activar.
- **Strategy 4: Hold Winner con TWAP dinámico** — actualmente HOLD WINNER es estático
  (≥0.96). Podría ser "TWAP dice UP gana con 95% probabilidad, hold".
- **Per-second persistence de ticks** — guardar para backtest, ocupa ~50MB/día.

---

## Decisiones de diseño (por si reabrimos)

### Por qué Coinbase TWAP-60s y no Chainlink Data Streams oficial
- Chainlink Data Streams requiere autenticación HMAC y plan de pago.
- Coinbase BTC-USD es la fuente más líquida del mundo; TWAP local se aproxima al
  oficial dentro de $0-5.
- Trade-off: gratis + baja latencia vs exactitud de $0.

### Por qué default conservador en TWAP hedge
- Código nuevo sin validar en producción.
- Activar manualmente permite observar comportamiento sin compromiso.
- Si falla, el peor caso es no hedge → mismo resultado que sin feature.

### Por qué `.env` overrides en runtime
- Permite cambiar parámetros sin redeploy.
- Settings persistidos en DB (`bot_config` table).
- Cambios aplican al siguiente ciclo de ventana (no requieren restart).

---

## Estado del código en git

```
main
├── 4d64578 feat: TWAP-aware early hedge + lower MIN_ITM_PCT_DEFAULT
├── 6e32b65 fix: use bot.logger module instead of stdlib logging
├── 024d732 feat: Local TWAP-60s from Coinbase WebSocket
├── 208a920 fix: fall back to previous candle close when boundary candle not yet published
├── 8568aca fix: query Coinbase without start/end so forming candle is included
├── 54eecf4 fix: Coinbase 5-min candle OPEN as primary strike source
├── 284a0b0 img (assets)
├── 3d37f60 Create img
├── 3ede64d chore: ignore .backups/
└── 7695784 refactor: Strike sourcing Chainlink→Coinbase→Polymarket
```

VPS está en `e8f8c6b` (VPS-specific detached HEAD con commits no en origin/main).

---

## Memorias relevantes (auto-load en cada sesión)

- `polymarket-bot-production.md` — estado actual del bot, credenciales, ops procedures.
- `polymarket-bot-vps-current.md` — historia del VPS (US → Lithuania migration).
- `polymarket-strike-source.md` — cadena de sourcing del strike con pitfalls conocidos.
- `polymarket-geoblock-list.md` — geoblock de Polymarket, Lithuania es de los pocos EU permitidos.
