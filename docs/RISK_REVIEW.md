# 🛡️ RISK_REVIEW — Revisión de riesgos del bot

> Adaptado de la skill `prediction-market-risk-review` de ECC para ME$IRVE.
> Ejecutar **antes de cualquier cambio que toque credenciales, autenticación,
> datos de portafolio, automatización o ejecución**. Antes del flip
> `TRADING_MODE=paper → real` este doc es **obligatorio**.

---

## 1. Advice Boundary (Límite de recomendación)

El bot ME$IRVE es un sistema de **ejecución automatizada**, no un asistente
financiero. Cualquier mensaje del dashboard, log o interfaz debe ser
**informativo**, no una recomendación de buy/sell/hold/size.

- ❌ **Nunca** mostrar "COMPRA AHORA" / "VENDE YA" / "tamaño sugerido: N shares"
- ✅ Permitir mostrar "señal: líder=UP, itm_pct=0.07, ventana=132s" — datos
  crudos para que el operador decida manualmente vía `/settings`
- ✅ Permitir mostrar el resultado post-trade (win/loss, P&L) — histórico

**Checklist:**
- [ ] Ningún mensaje del dashboard sugiere tamaño de posición
- [ ] Ningún log sugiere acción inmediata
- [ ] El panel `/settings` permite modificar parámetros pero el botón de
      "guardar" requiere confirmación explícita

---

## 2. Venue And Regulatory Boundary (Límite venue/regulatorio)

### Geoblock de Polymarket

**Crítico.** Polymarket bloquea por IP. Solo **Lituania** (Hostinger EU DC
`eu-west-2` recomendado) está permitida. US está bloqueada.

- VPS actual: Hostinger Vilnius, Lithuania (AS47583) — ✅ permitido
- Verificación pre-deploy: `curl https://polymarket.com/api/geoblock` desde VPS
- ❌ **Nunca** correr el bot desde US, ni siquiera para paper mode (mismo
  endpoint, misma IP)
- ❌ **Nunca** usar VPN para "saltarse" el geoblock — términos de Polymarket
  lo prohíben explícitamente

### Límites de cuenta

- Polymarket aplica límites de posición y volumen por cuenta
- Verificar en `/settings` que `ta_shares_per_leg * ta_max_legs` no excede
  límites individuales
- El bot **no intenta evadir** rate limits de la API (CLOB v2 WebSocket ya
  mantiene conexión persistente, no se rotan)

**Checklist:**
- [ ] Geoblock verificado desde VPS con `curl`
- [ ] Sin VPN activo en el host del bot
- [ ] Parámetros de sizing revisados contra límites Polymarket
- [ ] `pyproject.toml`/`requirements.txt` pinneado (evita upgrades sorpresa)

---

## 3. Data Quality (Calidad de datos)

### Fuentes de precio (oracles)

El bot usa **tres oracles en cascada** (ver `docs/ORACLE_SOURCES.md` para
detalle):

| Fuente | Latencia | Confiabilidad | Uso |
|---|---|---|---|
| TWAP-60s local | <1s | Alta (gap <$5 vs oficial) | Strike primario |
| Coinbase candle OPEN | ~5s tras cierre vela | Alta | Fallback 1 |
| Chainlink (Polymarket oficial) | ~3min | Oficial pero lento | Fallback 2 |

**Reglas:**
- ❌ **Nunca** mezclar precios de exchanges distintos sin etiquetar la fuente
- ❌ **Nunca** usar precio spot crudo como strike (siempre TWAP o candle OPEN)
- ❌ **Nunca** aceptar trades de hasta 60s para cierre de ventana si el gap
  oficial vs local > $10
- ✅ Loggear la fuente de cada strike en `bot.log`

### Liquidez y spread del libro Polymarket

El bot verifica antes de cada trade:
- `ask_líder ∈ [ta_min_ask, ta_max_ask]` — fuera de este rango, no opera
- `par ≤ ta_complete_cap` (default 0.82) — si la suma de las dos patas supera
  el cap, aborta el pair completion

**Checklist:**
- [ ] `bot/polymarket_price.py` registra timestamp + source de cada tick
- [ ] `bot/strategies/temporal_arb.py` valida `min_itm_pct` antes de comprar
- [ ] Spread del libro filtrado por `BB_ARM_MIN_SPREAD`, `CFD_MAX_COA`,
      `NRC ask∈[0.97,0.995]`

---

## 4. Security (Seguridad)

### Manejo de claves

- `PRIVATE_KEY`, `PROXY_WALLET`, `POLY_API_KEY/SECRET/PASSPHRASE`,
  `DASHBOARD_PASSWORD`, `DASHBOARD_SECRET_KEY` → **solo en `.env`**, nunca
  en código, logs, ni commits
- `.env` está en `.gitignore` — verificar antes de cualquier commit
- ❌ **Nunca** hacer echo de `.env` en chat, logs, ni en issues de GitHub
- ❌ **Nunca** commitear `data/*.db` (contiene historial de bankroll)
- ❌ **Nunca** usar la misma `PRIVATE_KEY` para hot wallet bot y treasury
  personal

### Scopes de API

- Polymarket CLOB API: usar **read-only scopes** por defecto
- L2 key derivation (POLY_API_KEY/SECRET/PASSPHRASE) se genera **una sola vez**
  con `scripts/generate_api_keys.py` y se almacena cifrado en `.env`
- Si una key se filtra: rotar inmediatamente y revisar `data/streak_snapper.db`
  para detectar trades no autorizados

### Circuit breakers

El bot tiene stop-loss por ventana (3 disparadores en `temporal_arb.py`):
time threshold, trailing stop, catastrófico. **Pendiente** (post-doc):
agregar circuit breakers independientes según `llm-trading-agent-security`:
- Consecutive-losses (halt tras N pérdidas seguidas)
- Hourly-drawdown (halt si P&L < umbral en 1h)
- Daily-spend-cap (cumple USD cap de trade, no de losses)

### Approval humana antes de ejecución

- `TRADING_MODE=paper` (default) — no envía órdenes reales, todo simulado
- `TRADING_MODE=real` — **requiere** haber ejecutado este checklist completo y
  firmado abajo
- `DASHBOARD_PASSWORD` vacío + `DASHBOARD_HOST != 127.0.0.1` → el bot
  **rechaza arrancar** (`bot/auth.py:verify_startup_config`)

**Checklist:**
- [ ] `.env` no commiteado (verificar con `git ls-files | grep -E '\.env'`)
- [ ] `PRIVATE_KEY` distinto del treasury personal
- [ ] `POLY_API_KEY` regenerado con `scripts/generate_api_keys.py` (no
      reutilizar keys de la US-VPS)
- [ ] `DASHBOARD_PASSWORD` configurado (mínimo 16 caracteres)
- [ ] `DASHBOARD_HOST=127.0.0.1` o `DASHBOARD_PASSWORD` seteado
- [ ] Circuit breakers verificados (test en paper mode primero)

---

## 5. Privacy (Privacidad)

- El bot **no recolecta** datos de usuario — opera sobre la wallet del
  operador únicamente
- El dashboard expone datos del bot a través de Flask — bindear a
  `127.0.0.1:5000` (no `0.0.0.0`)
- Nginx reverso-proxy añade TLS en 443/80
- ❌ **Nunca** exponer el dashboard públicamente sin autenticación
- ❌ **Nunca** compartir screenshots que contengan `PROXY_WALLET`,
  balances, o P&L detallado sin sanitizar

**Checklist:**
- [ ] Dashboard bind a `127.0.0.1`, no `0.0.0.0`
- [ ] Nginx config restringe acceso (`/etc/nginx/sites-available/polybot`)
- [ ] Logs no contienen PRIVATE_KEY ni secretos

---

## 6. Audit Log

Toda decisión del bot debe quedar registrada, **incluso las que no ejecutan
trades** (no solo los trades exitosos). Esto es un pendiente — actualmente
`data/streak_snapper.db` tabla `trades` solo guarda trades ejecutados.

**Pendiente (post-doc):**
- Tabla `observations` para señales rechazadas (por qué no entró)
- Tabla `state_snapshots` para reconstruir estado al momento del trade
- Esto facilita post-mortem de incidentes

---

## 7. Output Contract

Resultado de ejecutar este review:

1. **scope reviewed** — qué se cambió (archivos, configs)
2. **pass/warn/fail findings** — cada checklist arriba con estado
3. **blocked actions** — qué no se puede hacer hasta resolver findings
4. **required mitigations** — qué hay que cambiar
5. **safe next step** — el commit + deploy concreto que sigue

Si cualquier paso execution-capable (envío de orden real) está solicitado,
**requerir** una segunda revisión firmada por el operador.

---

## 8. Firma (gate pre real-mode)

Antes de cambiar `TRADING_MODE=real`:

```
TRADING_MODE=paper → TRADING_MODE=real

Fecha: ____________
Operador: ____________
Findings críticos resueltos:
  [ ] _______________
  [ ] _______________
  [ ] _______________

Tests: pytest tests/ -q → 599+ passed (las 3 fallas en test_spread_harvest.py
son pre-existentes y no afectan producción — ver docs/PRODUCTION_AUDIT.md)

Geoblock verificado: ___

Firma: ____________
```

Una vez firmado, desplegar con el procedimiento estándar
(`docs/PAPER_TO_REAL_RUNBOOK.md`).