# Estado del bot — 2026-09-23

Documento vivo. Supersedes `ESTADO_BOT_2026-09-17.md`. Actualizar al
final de cada sesión con cambios relevantes.

---

## TL;DR

- Bot activo en VPS `srv1702650` (76.13.251.202), Vilnius, Lithuania.
- Bankroll **$1000.00** (DB reseteada, backup en `data/streak_snapper.db.pre_reset_*.bak`).
- Una sola estrategia activa: **`temporal_arb`** (paper mode).
- Strike source: **local TWAP-60s** desde Coinbase WebSocket (gap < $5).
- Config conservadora aplicada para validación de 3-5 días.
- Landing pública en https://polytradebot.cloud/ + auth rediseñado (Notika).
- Tests: **581 passed, 1 skipped**.

---

## 1. Operativa actual del VPS

| Métrica | Valor |
|---|---|
| Servicio | `polymarket-bot` activo, restart en crash (RestartSec=10) |
| Modo | paper (no opera dinero real) |
| Símbolo | BTC (default `SS_SYMBOLS=btc`) |
| Estrategia | `temporal_arb` única activa |
| Strike source | TWAP-60s local desde Coinbase WebSocket |
| Coinbase ticker feed | conectado (background thread) |
| Latencia CLOB | < 200 ms taker orders |
| Strike por ventana | único, fresco, alineado con resolución |
| P&L desde reset | $0 (recién reseteado, sin trades aún) |
| Bankroll inicial | $1000.00 (DB) |

---

## 2. Comportamiento por ventana (cada 5 min)

`_observe()` se ejecuta cada ~4s. En orden:

1. **Entry** — `find_leader_side(spot=twap)` si `|itm_pct| ≥ 0.05` y
   `|twap-strike|/ATR(14) ≥ 0.7`. Acumula hasta 30 shares.
2. **Path A** (Pair completion) — `first_px + opp_ask ≤ 0.82` → cierra par.
3. **Path B** (Hedge Recovery) — caída ≥40% y `sum ≤ 0.94` → hedge.
4. **Stop-loss** — tras 60s con pérdida ≥35%, o trailing drawdown ≥30%.
5. **Bailout** — a T-60s si nada de lo anterior llenó.

Paths C (TWAP-aware hedge), D (Profit-Lock) y E (Mart-Hedge) están
deshabilitados. Manual activation via `/settings` cuando los baselines
métricas sean saludables.

---

## 3. Configuración runtime efectiva

`bot_config` table en VPS (overrides .env). Aplicada 2026-09-23 para
validación de 3-5 días:

| Campo | Valor |
|---|---|
| `ta_enabled` | true |
| `ta_min_itm_pct` | 0.05 |
| `ta_min_normalized_impulse` | 0.7 |
| `ta_shares_per_leg` | 30 |
| `ta_order_slice` | 30 |
| `ta_complete_cap` | 0.82 |
| `ta_bailout_sec` | 60 |
| `ta_entry_cutoff_sec` | 90 |
| `ta_hedge_drop_pct` | 0.40 |
| `ta_hedge_max_sum` | 0.94 |
| `ta_stop_loss_time_sec` | 60 |
| `ta_stop_loss_threshold` | 0.35 |
| `ta_trailing_stop_pct` | 0.30 |
| `ta_catastrophic_loss_pct` | 0.45 |
| `ta_profit_lock_enabled` | false |
| `ta_mart_hedge_enabled` | false |
| `ta_use_twap_signal` | true |
| `ta_twap_lookback_sec` | 60 |
| `ta_lpt_enabled` | false |
| `ta_use_atr` | true |
| `ta_use_rsi` | true |
| `ta_use_volume` | false |
| `starting_bankroll` | $1000.00 |

---

## 4. Features implementadas (2026-09)

| Feature | Estado | Notas |
|---|---|---|
| TWAP-60s source (Coinbase WS) | ✅ activo | gap < $5 vs oficial |
| TWAP entry signal (rolling 60s) | ✅ activo | suaviza ruido vs spot |
| Profit-Lock completion (Path D) | ⚪ OFF manual | espera 30s, fuerza cierre si profit y sum ≤ cap |
| Martingale hedge (Path E) | ⚪ OFF manual | qty × 2 × (1+loss_pct) cuando en pérdida |
| Polimarket balance poller | ⚪ error pre-existente | `update_polymarket_balance` no existe en BotState |
| Landing pública `/` | ✅ activo | 8 secciones + scroll animations |
| Login rediseñado `/login` | ✅ activo | Notika split layout + favicon SVG |
| P&L breakdown endpoint | ✅ activo | yesterday/today/week/month/total + pct |
| Bot state `bot_config` | ✅ activo | overrides .env en arranque |

---

## 5. Cómo conectar y operar

### SSH
```bash
sshpass -e ssh -o StrictHostKeyChecking=accept-new root@76.13.251.202
# Password en env var SSHPASS (NUNCA en línea de comandos)
```

### Comandos útiles
```bash
# Ver estado
systemctl is-active polymarket-bot

# Log en vivo (filtrado por strategy)
tail -f /opt/polymarket-bot/logs/bot.log | grep -E "TA_|💰|🛡|Strike"

# Stats últimas 24h
sqlite3 -header -column /opt/polymarket-bot/data/streak_snapper.db \
  "SELECT COUNT(*) total, SUM(won=1) wins, ROUND(100.0*SUM(won=1)/COUNT(*),1) wr,
          ROUND(SUM(pnl),2) pnl FROM trades WHERE opened_at > datetime('now','-1 day');"

# P&L breakdown por período
curl -s -b /tmp/c.txt http://127.0.0.1:5000/state | python3 -c "
import sys, json
data = json.load(sys.stdin)
for k, v in data.get('pnl_breakdown', {}).items():
    print(f'  {k:10s}  pnl={v[\"pnl\"]:+8.2f} USD  pct={v[\"pct\"]}% vs base')"
```

### Monitoreo de la nueva config (próximos 3-5 días)

Métricas clave a vigilar:
- **WR** ≥ 53% (baseline: 49.8%)
- **P&L total** ≥ 0 (baseline: -$512)
- **Trades/día** ~10-15 (baseline: ~30, ahora más selectivo)
- **Drawdown** ≤ $50 (5% del bankroll)

Criterios de revisión:
- Si WR < 51% tras 50+ trades → subir `ta_min_itm_pct` a 0.07
- Si drawdown > $80 → reducir `ta_shares_per_leg` a 20
- Si demasiados SKIP_WEAK_IMPULSE → bajar `ta_min_normalized_impulse` a 0.6

---

## 6. Deploy workflow

**NUNCA** `git pull` — VPS está en detached HEAD `e8f8c6b` con commits
propios divergentes. Workflow seguro:

```bash
# 1. Commit + push local
git commit -am "..." && git push origin main

# 2. Copiar archivos vía scp
sshpass -e scp bot/file.py root@76.13.251.202:/opt/polymarket-bot/

# 3. SSH + colocar en su sitio + restart
sshpass -e ssh root@76.13.251.202 'cd /opt/polymarket-bot && \
  cp bot/file.py bot/file.py && \
  systemctl restart polymarket-bot && \
  sleep 10 && systemctl is-active polymarket-bot'
```

**Excepción `bot/main.py`** — Restaurar SIEMPRE desde git antes de
cualquier cambio que lo toque:
```bash
git checkout e8f8c6b -- bot/main.py
# Editar aditivamente con patch en /tmp
```

---

## 7. Backup y rollback

Backup del DB pre-reset preservado en:
```
/opt/polymarket-bot/data/streak_snapper.db.pre_reset_20260923_153313.bak
```
(~500 KB, contiene 586 trades + P&L histórico para análisis comparativo)

Para restaurar:
```bash
systemctl stop polymarket-bot
cp data/streak_snapper.db.pre_reset_20260923_153313.bak data/streak_snapper.db
systemctl start polymarket-bot
```

---

## 8. Próximos pasos sugeridos

1. **Día 1-2:** Monitorear trades. Confirmar que filtros son suficientemente
   estrictos (esperar ~10-15 trades/día, no 30).
2. **Día 3:** Evaluar WR vs baseline. Si < 51% → ajustar `ta_min_itm_pct` a 0.07.
3. **Día 5:** Si WR ≥ 53% y P&L > 0 → considerar activar `ta_profit_lock_enabled=true`
   para bloquear profit antes.
4. **Día 7+:** Si estable → considerar `ta_mart_hedge_enabled=true` con
   `max_rounds=2` y `mult=1.5` (config conservador).

---

## 9. Riesgos activos

1. **Pre-existente:** `polymarket_balance.py:131` AttributeError en thread
   poller (`update_polymarket_balance` no existe en BotState). No afecta
   trading pero acumula stack traces en `bot.error.log`. Pendiente fix.
2. **Bajo:** La nueva config reduce trade frequency ~50%. Si la edge
   mecánica no es suficiente, P&L puede empeorar por menor volumen.
   Monitorear durante la validación.
3. **Medio:** Profit-Lock y Mart-Hedge están OFF. Si tras 5 días el WR
   sigue < 50%, estos paths podrían ayudar — pero requieren testing
   adicional antes de activar en producción.

---

## 10. Memorias relevantes

- `polymarket-bot-production.md` — credenciales, ops procedures
- `polymarket-strike-source.md` — cadena completa de strike source + pitfalls
- `polymarket-geoblock-list.md` — geoblock, Lithuania permitida
- `polymarket-bot-vps-current.md` — historia del VPS (US → LT migration)

Documentos relacionados:
- `docs/AUDITORIA_TA_2026-09-18.md` — análisis de conflictos TA + justificación de la nueva config
- `docs/RUTA.md` — roadmap del proyecto
- `docs/PLAN.md` — plan estratégico
- `docs/ARCHIVOS.md` — mapa de archivos
