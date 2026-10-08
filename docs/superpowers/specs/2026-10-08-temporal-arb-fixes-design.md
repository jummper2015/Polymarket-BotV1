# Temporal Arb: Mart-Hedge compounding + Cutoff estricto + Filtro horario

**Fecha:** 2026-10-08
**Estado:** Aprobado por usuario (2026-10-08)
**Branch objetivo:** `main` (con snapshot VPS preservado)
**Modo de despliegue:** paper, validación ≥ 24h antes de live

## Contexto

El usuario reporta tres problemas en la operativa del bot en producción (VPS `76.13.251.202`,
1499 trades en DB, `TRADING_MODE=paper` actualmente):

1. **Compras después de 120 segundos del cutoff.** El cutoff `ta_entry_cutoff_sec=120s`
   solo bloquea entradas nuevas en `IDLE`. Dentro de una ventana ya abierta se siguen
   disparando tranches de accumulating, mart-hedge rounds, y hedge-recovery sin
   restricción temporal.
2. **Mart-Hedge ejecuta solo una ronda y el multiplicador 2.5x constante no recupera
   pérdidas.** El spec 2026-10-06 ancló `mult=2.5` a `first_shares_filled` sin
   compounding. El usuario quiere **martingala clásica**: R1 = first×2, R2 = round1×2
   (= first×4), con `max_rounds=2`. Despliegue máximo por ventana = first_shares × 6.
3. **Posiciones se abren sin buscar cobertura y la operación resulta perdedora.**
   Combinación de los dos puntos anteriores más flags de configuración que no estaban
   disparando Mart-Hedge cuando correspondía.

Adicionalmente: análisis de horarios para crear whitelist automática de horas donde
el bot ha ganado históricamente, y auditoría de bugs silenciosos.

## Decisiones de diseño (usuario aprobó 2026-10-08)

| Tema | Decisión |
|---|---|
| Mart-Hedge sizing | Martingala clásica. R_n = round_{n-1}_qty × mult. `max_rounds=2`, `mult=2.0`. |
| Cutoff 120s | Estricto. Bloquea TODA compra (IDLE, accumulating, Path A/B/C/D/E). |
| Filtro horario | Whitelist automático desde análisis de DB. Botón de override manual en `/settings`. |
| Entrega | Análisis + fixes + deploy en una sola pasada. Snapshot antes. |
| Modo inicial | Paper (`TRADING_MODE=paper`). Validación ≥ 24h antes de flip a real. |

## Cambios

### 1. Mart-Hedge compounding — `bot/strategies/temporal_arb.py`

**Cambio semántico de `ta_mart_hedge_mult`:**
- Antes (2026-10-06 spec): constante anclado a `first_shares_filled` cada ronda.
- Ahora: **incremento por ronda**. R1 = first_shares × mult. R_n (n≥2) = round_{n-1}_qty × mult.

**Defaults propuestos (estricto cambio de defaults):**
- `ta_mart_hedge_mult` = 2.0 (era 2.5)
- `ta_mart_hedge_max_rounds` = 2 (sin cambio)
- `ta_mart_hedge_min_secs` = 5 (sin cambio)
- `ta_mart_hedge_loss_fallback_pct` = 0.50 (sin cambio)
- `ta_mh_monitor_secs` = 30.0 (sin cambio)

**Comportamiento resultante:**
- first_shares=20 → R1 = 40, R2 = 80. Despliegue máx = 120 shares × ~$0.50 ≈ $60/ventana.
- first_shares=30 → R1 = 60, R2 = 120. Despliegue máx = 180 shares × ~$0.50 ≈ $90/ventana.

**Reglas que se mantienen (no cambian):**
- Side-change filter: R_n solo dispara si el ganador actual != lado comprado en R_{n-1}.
- Loss fallback: si pérdida de la pata anterior ≥ 50%, R_n dispara aunque no haya side-change.
- Monitor secs entre rondas: 30s.
- Edge-lock suprimido post-MH (Path A y Path D bloqueadas si `mart_hedge_rounds >= 1`).
- HOLD si el ganador actual == mh_side (no se abre ronda en mismo lado ganador).

**Reglas nuevas:**
- **Cutoff gate**: R_n requiere `secs >= ta_entry_cutoff_sec`. Si `secs < cutoff`, no se ejecuta.
- **Estado**: tracking consistente de `round_N_qty` para que la siguiente ronda pueda mult sobre él.

### 2. Cutoff estricto — `bot/strategies/temporal_arb.py`

**Cambios en `_observe()`:**

```text
IDLE
  [existente] secs < q_cut → phase = closed
        + nuevas gates abajo

ACCUMULATING
  [existente] secs <= bail_sec → stop_reason="bailout"
  [NUEVO] secs < q_cut → stop_reason="cutoff_strict" (error log explícito)

HALF_OPEN
  [NUEVO gate al inicio de half_open] secs < q_cut → bail out, NO mart-hedge,
                                          NO Path A/B/C/D, solo HOLD hasta resolución.
                                          Loguear "TA_HOLD_CUTOFF" una vez.

PATH E (mart-hedge)
  [NUEVO] gates explícitos: secs >= ta_entry_cutoff_sec para R1 y R2.
```

**Excepción:** LPT (Late Pair Taker) — su ventana operativa ya es T-20..T-118 (por
defaults de `lpt_min_left=20` y `lpt_max_left=118`). Si LPT dispara, dispara dentro
del límite. Verificar empíricamente que la condición `lpt_max_left ≤ entry_cutoff_sec`
se mantiene, sino ajustar `lpt_max_left`.

**Bailout y graceful close:**
- Cuando se active el cutoff en posición abierta, transición automática a `phase="closed"`
  con un log único "TA_CUTOFF_STRICT — primera pata queda hasta resolución".
- No se intenta cancelar órdenes existentes (no hay resting orders en taker).

### 3. Whitelist horario — nuevo `bot/strategies/market_hours_filter.py`

**Diseño del módulo:**

```text
class MarketHoursFilter:
    is_whitelisted(ts_epoch, symbol) -> bool:
        return utc_hour(ts) in whitelist_hours  # per symbol
```

- **Almacenamiento**: `bot_config.whitelist_hours_btc` (CSV "0,1,2,...,23"),
  `bot_config.whitelist_hours_eth`, `whitelist_hours_sol`. Aplica por símbolo.
- **Default seguro**: whitelist vacía = **sin filtro** (degradación segura). Si el
  script de análisis no encuentra horas rentables, el bot opera normal, no se rompe.
- **Comportamiento**: registrado como StrategyDescriptor con `evaluate()` que devuelve
  `[]` cuando la hora está en blacklist y `None` cuando está en whitelist (deja a las
  otras estrategias decidir).
- **Descriptor**: prioridad alta (90) para que se evalúe antes que temporales,
  coin-flip, etc.

**Script de análisis — `scripts/analyze_trade_hours.py`:**

```python
# Pseudo-código
SELECT
  strftime('%H', created_at, 'unixepoch') AS utc_hour,
  strategy,
  COUNT(*) AS n_trades,
  SUM(pnl) AS net_pnl,
  AVG(pnl) AS avg_pnl,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS win_rate,
  MIN(pnl) AS max_loss,
  MAX(pnl) AS max_win
FROM trades
WHERE created_at >= ? AND strategy IN (...)
GROUP BY utc_hour, strategy
```

- **Inputs**: ventana temporal (default: últimos 7 días o todo si <7d de datos).
- **Outputs**:
  - Tabla Markdown en stdout.
  - JSON a `data/hour_analysis_<UTC_timestamp>.json`.
  - Recomendación de whitelist por símbolo con regla `n_trades ≥ 20 AND net_pnl > 0 AND win_rate ≥ 50%`.

### 4. Bugs silenciosos a auditar

| # | Sospecha | Acción |
|---|---|---|
| 1 | Accumulating puede comprar tranches aunque `secs < 120`. | Fix en #2. |
| 2 | `current_winning_side(neutral_band_pct=0.0)` produce whipsaw UP↔DOWN cuando spot oscila cerca del strike. | Evaluar si `neutral_band_pct=0.0001` reduce mart-hedge innecesarios. Cambio de default, no configurable (test antes de aplicar). |
| 3 | Si `ta_profit_lock_min_secs > ta_bailout_sec`, profit-lock nunca se evalúa (ya validado: defaults 30 vs 60, OK). | Verificación de defaults. |
| 4 | `mh_monitor_secs=30` con cutoff 120s deja 90s para R1+monitor+R2 → a veces R2 no completa. | Documentado. NO cambiamos monitor_secs. |
| 5 | `first_leg_filled_at` se asigna en IDLE (single tranche, línea 1447) Y en accumulating→half_open (línea 588). Coherente hoy. | Verificar tras cambios de cutoff. |
| 6 | Mart-Hedge usa `first_shares_filled`, no `first_shares_target`. Si accumulating corta antes, mart-hedge qty es sobre el partial fill. | Documentar y revisar. Si afecta producción, plantear fix en spec aparte. |

### 5. Cambios en `bot/state.py` y dashboard

- **Nuevos campos en `BotState`**:
  - `whitelist_hours_btc: str` = "" (default vacío)
  - `whitelist_hours_eth: str` = "" (default vacío)
  - `whitelist_hours_sol: str` = "" (default vacío)
- **Nuevos `RuntimeField`** en `BASE_FIELDS` (config.py) para exponer las tres whitelists.
- **Dashboard `/settings`**: input CSV para cada símbolo, validar que sean enteros 0-23.

### 6. Tests — `tests/`

**`tests/test_temporal_arb.py` — casos nuevos:**
- `test_mh_r1_qty_is_first_x_mult`: dado first_shares=30 y mult=2.0, R1 dispara y
  mart_hedge_qty=60.
- `test_mh_r2_qty_is_r1_x_mult`: tras R1 con qty=60 y side-change, R2 se llena con qty=120.
- `test_mh_no_r3_fires`: tras R2, mart_hedge_rounds=2 y no hay R3.
- `test_mh_cutoff_blocks_r1`: si secs < ta_entry_cutoff_sec, R1 no dispara aunque side_changed=True.
- `test_mh_cutoff_blocks_r2`: si R1 disparó a secs=130 y R2 candidacy llega a secs=110 (< cutoff), R2 no dispara.
- `test_cutoff_blocks_accumulating`: en accumulating, si secs < cutoff, stop_reason="cutoff_strict", no compra más tranches.
- `test_cutoff_blocks_hedge_paths`: en half_open, si secs < cutoff, ni Path A ni Path B ni Path D disparan.
- `test_cutoff_does_not_block_lpt`: LPT puede disparar dentro de su ventana aunque secs < cutoff (verificar).

**`tests/test_market_hours.py` (nuevo):**
- `test_blacklist_blocks`: hora 14 en blacklist, evaluate() devuelve [].
- `test_whitelist_passes`: hora 14 en whitelist, evaluate() devuelve None.
- `test_empty_whitelist_no_filter`: whitelist="" → todas las horas pasan.
- `test_invalid_hour_raises`: "abc" en whitelist → ValueError al cargar.

**`tests/test_analyze_hours.py` (nuevo):**
- `test_pnl_by_hour_groups_correctly`: trades sintéticos → buckets correctos.
- `test_min_trades_threshold_filters`: 5 trades en una hora → no incluida en whitelist recomendada.

### 7. Despliegue — VPS Hostinger `76.13.251.202`

**Procedimiento** (basado en `polymarket-bot-production.md`):

1. Snapshot VPS a `/opt/polymarket-bot/.backups/mh_compound_<TS>/`.
2. `sshpass -e ssh root@76.13.251.202 "cd /opt/polymarket-bot && source venv/bin/activate && python scripts/analyze_trade_hours.py --days 7"`:
   - Output se captura a local en `data/hour_analysis_<TS>.json` y `data/hour_analysis_<TS>.md`.
3. Aplicar los cambios en local. Correr `pytest -q` localmente — todos verdes.
4. `scp` archivos modificados al VPS (sin overwrite total; preservar customizaciones VPS como `cfd_enabled=True` y `bot/coinbase_ticker_feed.py` no tracked).
5. Patch quirúrgico a `bot/state.py` para añadir los 3 campos de whitelist sin tocar existentes.
6. Reemplazar `bot/strategies/temporal_arb.py`, `bot/strategies/__init__.py` (registro),
   `bot/strategies/market_hours_filter.py` (nuevo), `bot/config.py`, `bot/dashboard.py`,
   `bot/static/settings.js`, `bot/templates/settings.html`.
7. Reemplazar `tests/test_temporal_arb.py` y `tests/test_market_hours.py` (nuevo).
8. Crear `scripts/analyze_trade_hours.py` en VPS.
9. En VPS: `cd /opt/polymarket-bot && source venv/bin/activate && python -m pytest tests/ -q`. Si verde, continuar. Si rojo, restaurar desde backup.
10. Aplicar whitelist recomendada al `bot_config` solo si el JSON dice "recomendado":

    ```sql
    INSERT INTO bot_config(key, value) VALUES('whitelist_hours_btc', '0,3,7,12,...')
       ON CONFLICT(key) DO UPDATE SET value=excluded.value;
    ```

11. `systemctl restart polymarket-bot`.
12. Monitoreo 30 min:
    - `tail -f /opt/polymarket-bot/logs/bot.log` — ver decisiones TA.
    - `tail -f /opt/polymarket-bot/logs/bot.error.log` — confirmar 0 errores nuevos.
    - Dashboard tile "security" (ya añadido en commit `05c624c`) debe estar OK.
13. Después de 24h sin incidentes: notificar al usuario. La decisión de `TRADING_MODE=live`
    sigue siendo del usuario.

**Rollback:**
- `systemctl stop polymarket-bot`
- `cp -r /opt/polymarket-bot/.backups/mh_compound_<TS>/* /opt/polymarket-bot/`
- `systemctl start polymarket-bot`

### 8. Riesgos identificados

| Riesgo | Mitigación |
|---|---|
| Mart-Hedge compounding con max_rounds=2 duplica el bankroll en riesgo. | Snapshot previo. Deploy en paper. Visualizar P&L durante 24h antes de live. |
| Whitelist horario vacía = sin filtro (degradación segura) por defecto. | Si `whitelist_hours_btc=""`, no se filtra. No se rompe nada. |
| Análisis con <20 muestras por hora produce whitelist ruidosa. | Umbral mínimo configurable. Si no hay horas aptas, whitelist = "" (sin filtro). |
| Compilations en VPS distintas a local. | venv dedicado, mismo Python (3.12 según `__pycache__` reciente). |
| `TRADING_MODE=paper` no ejercita fill real. | El compounding se simula igual. Documentar gap. |

## Criterios de éxito

**24h post-deploy:**
- `bot.error.log` no contiene errores nuevos (`grep ERROR bot.error.log | wc -l` ≤ baseline).
- `bot.log` muestra decisiones TA razonables: ningún mart-hedge round sin side-change o loss fallback.
- Ningún trade con timestamps de compra post-cutoff (validar con SQL).
- Whitelist aplicada coincide con la recomendada por el análisis.

**7 días post-deploy (medición de EV):**
- Si `TA_ENABLED=true` y `TRADING_MODE=paper`, calcular P&L de trades ejecutadas vs
  baseline pre-deploy (últimas 200 trades antes del snapshot).
- Si P&L empeora > 20%, rollback automático y re-análisis.

## Fuera de alcance

- Cambios en box_builder, coin_flip_dog, near_res (no los pidió el usuario).
- Live trading (queda a decisión del usuario tras 24h paper).
- Reescribir balance poller (ya fix en commit anterior).
- UI dashboard para análisis horario (solo `/settings` con input CSV).

## Aprobaciones

- [x] Usuario aprobó diseño (Opción A conservadora) — 2026-10-08
- [ ] Usuario revisa este spec antes de pasar a writing-plans
