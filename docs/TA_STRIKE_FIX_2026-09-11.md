# TA — fix strike fetch (Coinbase → Polymarket price API)

**Fecha**: 2026-09-11
**Severidad**: alta — el bot operaba con strikes 300s stale, perdiéndose entradas tempranas
**Estado**: aplicado en `bot/strategies/temporal_arb.py`, verificado en ventana 17:30 UTC

---

## Síntomas observados

- Los logs `[TA] 🔍 eval` solo aparecían **3-4 minutos después** del inicio de la ventana.
- En el dashboard la sección "Comportamiento del Precio" se quedaba en placeholder "Esperando datos del WebSocket...".
- Los `SKIP_ASK_*` aparecían agrupados en los últimos 60-90s de la ventana, no distribuidos.
- Cuando aparecía el strike, su valor correspondía a la vela 5-min **anterior**, no a la actual (verificado: log con `strike=$77,893.45` mientras la ventana era `btc-updown-5m-1789146000` abierta a 17:00:00 — el strike era en realidad el de la vela que cubría 16:55–17:00).

## Causa raíz

`bot/coinbase_api.py:get_current_window_open(symbol, window_ts)`:
- Llama a `GET https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=300`.
- La API de Coinbase **NO devuelve la vela en formación** — solo velas completas.
- Aplica `list(reversed(data))` bajo el supuesto erróneo "Coinbase returns newest → oldest".
- Toma `raw[-1]` como "vela más reciente".
- Compara `candle_ts != window_ts` → siempre falla para el boundary actual (porque `raw[-1]` es la vela del boundary **anterior**, `boundary - 300`).
- Resultado: `get_current_window_open(window_ts=current_boundary)` devuelve `None` el 100 % de las veces (verificado en vivo con 3 polls consecutivos).

Cuando por coincidencia el ciclo del bot se desfasa 300s respecto al ciclo de Coinbase, los timestamps "alinean" pero la vela que devuelve Coinbase es la de hace 5 min, no la actual. El bot operaba entonces con un strike 300s viejo, lo que sesgaba `itm_pct` sistemáticamente — solo después de 3-4 minutos de drift BTC vs el strike viejo el `itm_pct` cruzaba el umbral `0.02%` y los logs empezaban a aparecer.

## Diagnóstico — cómo se verificó

Desde el VPS (`/opt/polymarket-bot`, venv activo):

```python
from bot.coinbase_api import _get_candles, get_current_window_open
import time
now = int(time.time()); boundary = now - (now % 300)
raw = _get_candles(300, product_id="BTC-USD")
# raw[-1] ts = boundary - 300  (NUNCA coincide con boundary)
# get_current_window_open(boundary) = None  (siempre)
# get_current_window_open(boundary - 300) = strike (de la ventana anterior)
```

En cambio, `bot/polymarket_price.get_strike(window_ts, symbol)` golpea `GET https://polymarket.com/api/crypto/crypto-price?symbol=btc&eventStartTime=<ISO>` y devuelve el `openPrice` real de la ventana:

```json
{"openPrice": 77916, "closePrice": 77803.32, "completed": false, ...}
```

Verificado:
- `get_strike(current_boundary)` → strike correcto (cacheado).
- `get_strike(boundary - 300)` → mismo strike (Polymarket no distingue pasado/presente para ventanas cerradas).
- `get_strike(boundary + 300)` → `None` (esperado, ventana futura).

## Fix aplicado

**Archivo**: `bot/strategies/temporal_arb.py`
**Diff** (backup: `bot/strategies/temporal_arb.py.bak_strikefix_20260911_171700`):

```diff
 def _observe(ctx: StrategyContext) -> None:
     """Temporal-Arb tick, called every OBSERVE_TICK_SECONDS during the window."""
     from .. import logger
-    from ..coinbase_api import get_current_window_open
+    from ..polymarket_price import get_strike

     state  = ctx.state
     ...
@@
         # Gate 2: fetch the window's opening price (the "strike") once per window.
         if ta.strike is None:
-            open_px = get_current_window_open(symbol, window_ts)
+            open_px = get_strike(window_ts, symbol)
             if open_px is None:
                 return
             ta.strike = open_px
```

**Cambio total**: 2 líneas. Riesgo bajo — `get_strike` ya está en uso por `coin_flip_dog`, `spread_harvest`, `box_builder`.

## Aplicación

```bash
cd /opt/polymarket-bot
cp bot/strategies/temporal_arb.py bot/strategies/temporal_arb.py.bak_strikefix_$(date -u +%Y%m%d_%H%M%S)
sed -i 's|from ..coinbase_api import get_current_window_open|from ..polymarket_price import get_strike|' bot/strategies/temporal_arb.py
sed -i 's|open_px = get_current_window_open(symbol, window_ts)|open_px = get_strike(window_ts, symbol)|' bot/strategies/temporal_arb.py
# validar sintaxis
venv/bin/python -c "import ast; ast.parse(open('bot/strategies/temporal_arb.py').read()); print('OK')"
# reiniciar
systemctl restart polymarket-bot.service
```

## Verificación post-fix

Ventana 17:30:00 UTC (slug `btc-updown-5m-1789147800`):

```
[17:30:05] ⚡ [TA] ✅ Strike obtenido: $77,916.00
[17:30:05] 🎯 [TA] 🎯 líder mispriced  DOWN @ 0.600  itm=-0.138%  strike=77,916.00  spot=77,808.78  slice=80/80  left=295s
```

- Strike obtenido en **5 segundos** (antes: 3-4 minutos).
- Strike **correcto** para la ventana en curso (boundary 17:30 → open 77,916).
- Señal real de entrada detectada desde T+5s.
- Logs `[TA] 🔍 eval` aparecen cada 20s durante toda la ventana (antes: solo al final).

## Notas adicionales

### SKIP_ASK_LOW vs SKIP_ASK_HIGH

| Skip | Condición | Significado |
|---|---|---|
| `SKIP_ASK_HIGH` | `leader_ask > TA_MAX_ASK` | El mercado ya repriced al líder — no hay gap que explotar. |
| `SKIP_ASK_LOW`  | `leader_ask < TA_MIN_ASK` | El mercado despreció al líder — el lado ganador está muy barato, sospecha de reversal. |

Si solo se ve uno de los dos, refleja la dirección del mercado:
- BTC persistentemente **bajo** el strike → `itm < 0` → leader = DOWN → `leader_ask = ask_dn` → SKIP_ASK_LOW.
- BTC persistentemente **sobre** el strike → `itm > 0` → leader = UP → `leader_ask = ask_up` → SKIP_ASK_HIGH.

En la sesión del 2026-09-11 BTC estaba en racha bajista → solo SKIP_ASK_LOW. No es bug, es consistencia.

### Override de umbrales en DB

`.env` declara `TA_MIN_ASK=0.55`, pero la fila en `bot_config` (`SELECT key, value FROM bot_config WHERE key LIKE 'ta_%_ask'`) tiene `ta_min_ask = 0.5`. Por la regla del proyecto (CLAUDE.md → "filas en DB anulan `.env`"), **el valor efectivo en runtime es 0.50**. Ajustar desde `/settings` (botón Guardar) o:

```sql
DELETE FROM bot_config WHERE key = 'ta_min_ask';
```

…luego reiniciar el servicio. Por instrucción del operador, **se deja en 0.50** hasta próximo ajuste manual.

## Archivos

- **Fix aplicado en**: `/opt/polymarket-bot/bot/strategies/temporal_arb.py`
- **Backup pre-fix**: `/opt/polymarket-bot/bot/strategies/temporal_arb.py.bak_strikefix_20260911_171700`
- **Log de la sesión**: `/opt/polymarket-bot/logs/bot.log`
- **Servicio**: `polymarket-bot.service` (systemd, reiniciado a las 17:17:18 UTC)

## Comandos de verificación rápida

```bash
# 1. ¿Está el cambio aplicado?
grep -n "get_strike\|get_current_window_open" /opt/polymarket-bot/bot/strategies/temporal_arb.py
# esperado: 329  -> from ..polymarket_price import get_strike
# esperado: 693  -> open_px = get_strike(window_ts, symbol)

# 2. ¿Servicio corriendo?
systemctl is-active polymarket-bot.service
# esperado: active

# 3. ¿El strike se obtiene en los primeros segundos de la ventana?
# (revisar tras un par de ventanas)
tail -n 200 /opt/polymarket-bot/logs/bot.log | grep -E "Strike obtenido|líder mispriced"

# 4. ¿Override de umbral?
sqlite3 /opt/polymarket-bot/data/streak_snapper.db "SELECT key, value FROM bot_config WHERE key LIKE 'ta_%_ask';"
```

## Lecciones / follow-ups

1. **`_get_candles` y `list(reversed(data))` en coinbase_api.py** — el comentario dice "newest first, reverse" pero la API actual de Coinbase devuelve oldest first. El bug del strike es uno; cualquier otro consumidor de `_get_candles` (TA spot, LPT, scripts) puede tener el mismo problema. Auditoría recomendada.
2. **Observabilidad del IDLE branch en `_observe`** — cuando el strike fetch falla, `_observe()` aborta sin loguear nada. Considerar añadir un contador de fallos consecutivos y un warn cada 30-60s.
3. **`get_strike` debería ser el path por defecto** para todos los descriptores que necesiten el strike — TA era el único que seguía yendo a Coinbase. El commit `25d1609` ("cambiar spot price a Binance") tocó spot pero no strike.
