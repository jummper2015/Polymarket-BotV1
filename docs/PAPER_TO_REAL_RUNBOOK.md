# Paper → Real: validación y runbook

> **Regla de oro:** El bot solo pasa a real si **todas** estas casillas están en verde. Si una falla, se queda en paper y se repite la semana.

## 0. Valoración por estrategia (snapshot 2026-09-11, 7 días de paper)

| Estrategia | Trades | WR | Avg win | Avg loss | EV/trade | P&L total | **Decisión** |
|---|---:|---:|---:|---:|---:|---:|---|
| `temporal_arb` | 645 | 54.4% | +$36.32 | -$27.92 | **+$7.03** | +$4,540.80 | ✅ **MANTENER** — único motor |
| `near_res` | 17 | 88.2% | +$0.64 | -$39.40 | **-$4.17** | -$69.20 | ❌ **DESECHAR** — EV negativo por diseño |
| `box_builder` | 3 | 33.3% | +$27.60 | -$20.40 | **-$4.40** | -$13.20 | ⚠️ **DESACTIVADO** — muestra insuficiente |
| `coin_flip_dog` | 1 | 0% | — | -$20.10 | — | -$20.10 | ⚠️ **DESACTIVADO** — muestra insuficiente |

### Por qué `near_res` se desecha (88% WR suena bien pero es trampa)

```
E = 0.88 × $0.64 + 0.12 × (-$39.40) = +$0.56 - $4.73 = -$4.17/trade
```

Captura residuals de 1–3¢ cuando gana, pero cuando pierde se come los ~$40 enteros. Es el clásico "many small wins, few catastrophic losses". **Con bankroll de $300, 5 pérdidas seguidas liquidan la cuenta.**

No hay tuneo que arregle un ratio riesgo:recompensa de ~60:1 en contra. La única salida posible sería rediseñar la mecánica (salir si el precio baja antes del settlement, o mejorar el payoff con un edge distinto), no tocar parámetros.

### Por qué `temporal_arb` es robusto

- EV por trade +$7.03 con 645 trades → **estadísticamente significativo**.
- Racha perdedora máxima en 7 días: **3 trades** (no entra en territorio de martingala peligrosa).
- Gana en todas las 24 horas UTC → sin horario que evitar.
- Asimetría 1.30:1 (avg win / avg loss) **y** win rate > 50% — combinación que aguanta slippage realista.

### Mejoras recomendadas para `temporal_arb` antes de real

1. **Sizing conservador** en el primer día real: `ta_shares_per_leg = 80` (validado) → bankroll real debe absorber max-DD $318 observado + buffer 30% → **bankroll mínimo recomendado: $500**.
2. **Order slicing**: mantener `ta_order_slice = 80` (= shares_per_leg, un solo chunk). En real, si se nota slippage, reducir a 40.
3. **Stop-loss de tiempo**: ya hay `ta_bailout_sec=45` — verificar que se respeta bajo latencia real.
4. **Bankroll incremental**: empezar con $200, sumar $200/semana si la semana cierra positivo, hasta $1,000.

## 1. Criterios para promover paper → real

Antes de tocar `TRADING_MODE=real`, la última semana de paper debe cumplir:

| Criterio | Umbral | Cómo medirlo |
|---|---|---|
| Trades totales | ≥ 350 (~50/día × 7) | `sqlite3 data/streak_snapper.db "SELECT COUNT(*) FROM trades WHERE opened_at >= datetime('now','-7 day')"` |
| Win rate global | ≥ 53% | sobre los trades de la última semana |
| P&L neto semanal | > 0 | sobre los trades de la última semana |
| Max drawdown intradía | < 20% del bankroll | serie cumulativa de P&L |
| Distribución de losses consecutivos | ≤ 8 en cualquier racha | `SELECT MAX(streak) FROM …` con ventana de trades |
| Tasa de error (`Traceback` en `logs/bot.error.log`) | < 1 / 1000 ventanas | `grep -c Traceback logs/bot.error.log` vs trades |
| Uptime del servicio | > 95% de la semana | `systemctl show polymarket-bot.service -p ActiveEnterTimestamp` × restart counter |
| Latencia `clob.polymarket.com` p95 desde el VPS | < 250 ms | medir durante una hora de operativa |
| Fail2ban | 0 bans legítimos de tu IP | `fail2ban-client status polymarket-dashboard` |
| Cert Let's Encrypt | > 14 días para expirar | `certbot certificates` |

**Análisis por estrategia activa:**
- Si `temporal_arb` representa > 90% de los trades y es rentable → OK para TA-only.
- Si tienes `box_builder`/`coin_flip_dog`/`near_res` activos, cada uno necesita WR ≥ 50% con muestra ≥ 30 trades.
- Las estrategias con < 30 trades en la semana **no se evalúan** — se mantienen desactivadas para real hasta acumular muestra.

## 2. Pre-flight (cuando todas las casillas en verde)

1. **Crear wallet dedicada al bot** (NO tu main wallet). Fondear con USDC.e por el bankroll decidido + 10% de buffer para gas.
2. **Generar API keys**:
   ```bash
   cd /opt/polymarket-bot
   # Editar .env: PRIVATE_KEY= y PROXY_WALLET=
   nano .env
   venv/bin/python scripts/generate_api_keys.py --write
   ```
3. **Verificar que `TRADING_MODE=paper` sigue activo** mientras haces esto.
4. **Reducir sizing para el primer día real** — el equivalente a un 25% del bankroll objetivo:
   - Bankroll objetivo $300 → primer día con $75 de exposición máxima.
   - En `bot_config`: `ta_shares_per_leg = 20` (vs 80 actual).
5. **Cambiar `TRADING_MODE=real`** en `.env`:
   ```bash
   sed -i 's/^TRADING_MODE=.*/TRADING_MODE=real/' .env
   systemctl restart polymarket-bot.service
   ```
6. **Verificar arranque**: el log debe decir algo equivalente a "CLOB client initialized" o similar; **NO** debe seguir diciendo "paper mode".
7. **Mirar el dashboard los primeros 30 minutos en directo** — confirmar que la primera orden aparece y se ejecuta, y que la resolución llega por Gamma (no por Coinbase, ya no aplica).

## 3. Señales de paro automático

Si observas **cualquiera** de estas, vuelve a paper inmediatamente:

- 3 trades perdedores consecutivos con `cost > $30` cada uno → `TRADING_MODE=paper` y a revisar.
- Error de CLOB auth persistente (ver `bot.error.log`).
- Resolución `gamma` que contradice la decisión interna del bot 2 veces seguidas.
- Balance de USDC.e cae por debajo del 50% del inicial sin explicación.
- `fail2ban` banea tu propia IP (significa que el regex está mal; ajustar `ignoreip`).

## 4. Operación normal

- **Logs**: `journalctl -u polymarket-bot.service -f` para stdout/stderr; `tail -f /opt/polymarket-bot/logs/bot.log` para las decisiones.
- **Trades del día**: `sqlite3 data/streak_snapper.db "SELECT id, opened_at, direction, strategy, shares, ROUND(pnl,2) FROM trades WHERE date(opened_at) = date('now') ORDER BY id DESC"`.
- **P&L semanal**: `sqlite3 data/streak_snapper.db "SELECT date(opened_at) d, COUNT(*), ROUND(SUM(pnl),2) FROM trades GROUP BY d ORDER BY d DESC LIMIT 7"`.
- **Fail2ban**: `fail2ban-client status polymarket-dashboard` para bans actuales.

## 5. Rollback de emergencia

Si algo va mal:

```bash
# 1. Apagar el bot
sudo systemctl stop polymarket-bot.service

# 2. Cancelar órdenes abiertas en el CLOB (manual, desde polymarket.com UI)

# 3. Volver a paper
sudo sed -i 's/^TRADING_MODE=.*/TRADING_MODE=paper/' /opt/polymarket-bot/.env

# 4. Arrancar
sudo systemctl start polymarket-bot.service

# 5. Diagnosticar con logs
journalctl -u polymarket-bot.service -n 200 --no-pager
```
