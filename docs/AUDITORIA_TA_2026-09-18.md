# Auditoría operativa + mapa de configuración TA — 2026-09-18

Documento vivo: operativa actual del bot, descripción exacta de cada
parámetro de la estrategia `temporal_arb`, y conflictos detectados.

---

## 1. Operativa actual del bot en VPS

| Métrica | Valor |
|---|---|
| Servicio | `polymarket-bot` activo, restart en crash (RestartSec=10) |
| Modo | paper (no opera dinero real) |
| Símbolo | BTC |
| Estrategias activas | `temporal_arb` (las demás `coin_flip_dog`, `box_builder`, `near_res`, `spread_harvest` están deshabilitadas) |
| Strike source | local TWAP-60s desde Coinbase WebSocket |
| Coinbase ticker feed | conectado (background thread) |
| Latencia CLOB | < 200 ms taker orders |
| Strikes por ventana | frescos, únicos (cada ventana con su valor) |

### Rendimiento (586 trades totales)

| Métrica | Valor |
|---|---|
| Trades totales | 586 |
| Wins / Losses | 292 / 294 |
| Win rate | 49.8% (≈ 50/50) |
| P&L total | **−$512** (estrategia está perdiendo) |
| Trades 24h | 14 (alta frecuencia — ~1 cada 1.7h) |
| Stop-loss catastróficos | activos (visible en log 12:43:12) |

**Diagnóstico:** win rate de ~50% combinado con un P&L negativo indica que
**las comisiones + slippage no se compensan con la edge direccional**. La
estrategia está en punto de equilibrio; el edge mecánico no es suficiente
para superar los costos. Hay que revisar `ta_complete_cap` (cuánto se
asegura) o reducir frecuencia de trades (subir `ta_min_itm_pct` o `ta_min_normalized_impulse`).

### Estado en vivo

- WebSocket CLOB Polymarket conectado
- Coinbase ticker feed BTC-USD conectado
- Bot acaba de ejecutar un stop-loss catastrófico (12:43:12): perdió -$60 USD,
  hedge en UP@0.9
- Posición actual: trade #586 abierto (UP @0.9)

---

## 2. Mapa completo de configuración TA

Cada parámetro tiene: **default** (código), **valor actual** (DB/env),
**rango válido** (RuntimeField) y **descripción exacta**.

### A. Activación

| Key | Default | Actual | Descripción |
|---|---|---|---|
| `ta_enabled` | `false` | `true` | Switch maestro de la estrategia. `false` = nunca entra. |

### B. Entrada direccional

| Key | Default código | Default lógico | Actual | Rango | Descripción |
|---|---|---|---|---|---|
| `ta_min_itm_pct` | `0.025` | 0.05 | **0.02** | ≥0 | **Umbral mínimo de "in-the-money"**. BTC debe haberse movido `≥min_itm_pct%` del strike para que la pata ganadora esté "barata". Por debajo: coin-flip, no hay edge. |
| `ta_min_ask` | `0.40` | 0.40 | **0.55** | 0.40–0.55 | **Precio mínimo** del leader ask. Por debajo: el mercado YA repricó, no queda edge. |
| `ta_max_ask` | `0.55` | 0.55 | **0.62** | 0.40–0.62 | **Precio máximo** del leader ask. Por encima: el leader ya está overpriced, no hay mispricing. |
| `ta_entry_cutoff_sec` | `150.0` | 150.0 | **120.0** | — | No entrar a partir de T-N. Cierra la ventana de entrada. |
| `ta_shares_per_leg` | `5.0` | 5.0 | **80.0** | — | Tamaño objetivo de la primera pata (en shares). |
| `ta_order_slice` | `5.0` | 5.0 | **80.0** | — | Tamaño de cada orden taker. Si `<=shares_per_leg` se manda todo en una sola orden; si menor, se reparte en múltiples tranches cada ~4s. **Config actual: 80=80, va todo en 1 orden.** |
| `ta_bailout_sec` | `60.0` | 60.0 | **45.0** | — | Si la segunda pata no cierra, se abandona y se liquida la primera a mercado / se resuelve normal. |
| `ta_cancel_all_sec` | `10.0` | 10.0 | `10.0` | — | Cancela todas las órdenes resting a T-10s. No se usa (todo es taker). |
| `ta_stop_hold_gate_sec` | — | — | **45.0** | — | Atributo en DB pero NO se lee en el código (`getattr(state, "ta_stop_hold_gate_sec", …)` no aparece). Probablemente vestigio. **No tiene efecto.** |

### C. Cierre del par (Path A)

| Key | Default código | Actual | Descripción |
|---|---|---|---|
| `ta_complete_cap` | `0.82` | **0.88** | Máximo costo total del par (entry + second_ask) para aceptar. A 0.82 lock = 0.18. A 0.88 lock = 0.12. **Tu config actual es 0.88 → más permisivo (cierra pares más caros, menos edge).** |

### D. Late Pair Taker (LPT)

| Key | Default código | Actual | Descripción |
|---|---|---|---|
| `ta_lpt_enabled` | `true` | **`false`** | Switch. Si está activo, cuando ya pasó el `ta_entry_cutoff_sec` y `ta_min_itm_pct` no se cumplió, intenta comprar AMBOS lados si la suma es ≤ `ta_lpt_cap`. **Tu config lo tiene desactivado.** |
| `ta_lpt_cap` | `0.90` | **0.92** | Suma máxima `ask_up + ask_dn` para LPT. A 0.90 lock = 0.10. **Actual 0.92 = más permisivo.** |
| `ta_lpt_min_left` | `20.0` | **20.0** | Segundos restantes mínimos para que LPT dispare. |
| `ta_lpt_max_left` | `148.0` | **148.0** | Segundos restantes máximos para que LPT dispare (justo bajo el entry_cutoff). |

**Nota:** LPT desactivado en tu config → esta lógica nunca corre. La línea
1467 dice `ta_lpt_enabled` = `false` en DB. Para habilitarla: `ta_lpt_enabled=true`.

### E. Hedge Recovery (Path B)

| Key | Default código | Actual | Descripción |
|---|---|---|---|
| `ta_hedge_enabled` | `true` | **`true`** | Switch. Compra el lado OPUESTO cuando la primera pata ha caído fuerte, para acotar la pérdida. |
| `ta_hedge_drop_pct` | `0.40` | **0.30** | Fire si `current_ask ≤ entry_px * (1 − drop_pct)`. A 0.30 = triggea con 30% de caída (más temprano). A 0.40 = triggea con 40% (más tarde). **Tu 0.30 = hedge más agresivo.** |
| `ta_hedge_max_sum` | `0.92` | **0.96** | Máximo `entry_px + hedge_ask` para que el hedge sea rentable. A 0.96 lock = 0.04 (poco). A 0.92 lock = 0.08. **Tu 0.96 = acepta hedges más caros (menos rentable).** |

### F. TWAP-aware hedge (Path C — nuevo)

| Key | Default código | Actual | Descripción |
|---|---|---|---|
| `ta_twap_hedge_enabled` | `false` | **`false`** | Switch. En los últimos 60s, si la TWAP-60s oficial proyecta el lado opuesto a tu posición con `\|margin\| ≥ ta_twap_hedge_margin`, abre el lado opuesto. **Desactivado por diseño (conservador).** |
| `ta_twap_hedge_margin` | `50.0` | **50.0** | Margen mínimo en USD ($) entre el TWAP-60s actual y el `required_avg` para que se dispare. Más alto = más selectivo. |
| `ta_twap_hedge_max_sum` | `0.92` | **0.92** | Máximo `entry_px + hedge_ask` para que un TWAP hedge sea rentable. |

### G. Stop-loss

| Key | Default código | Actual | Descripción |
|---|---|---|---|
| `ta_stop_loss_enabled` | `true` | **`true`** | Switch. Activa los 3 disparadores abajo. |
| `ta_stop_loss_time_sec` | `60.0` | **120.0** | Espera mínima desde la entrada antes de evaluar stop normal. Pasado este tiempo, si `loss_pct ≥ ta_stop_loss_threshold`, fire. **Tu 120s = más permisivo (más paciencia).** |
| `ta_stop_loss_threshold` | `0.25` | **0.50** | Pérdida mínima (fracción) para que el stop normal dispare. A 0.25 = triggea con 25% de pérdida. **Tu 0.50 = muy permisivo, espera a perder 50%.** |
| `ta_catastrophic_loss_pct` | `0.50` | **0.50** | Pérdida mínima para stop catastrófico INMEDIATO (sin gate de tiempo). A 0.50 = triggea con 50%. |
| `ta_trailing_stop_enabled` | `true` | **`true`** | Switch del trailing stop. |
| `ta_trailing_stop_pct` | `0.30` | **0.50** | Drawdown máximo desde el peak del ask. Si `peak - current ≥ peak * pct`, fire. **Tu 0.50 = más permisivo que default 0.30.** |

**Diagnóstico del stop-loss:** tus 3 parámetros son **más permisivos que los
defaults**: `time_sec=120` (vs 60), `threshold=0.50` (vs 0.25), `trailing=0.50`
(vs 0.30). En conjunto el bot **tolera pérdidas grandes antes de cortar**.
Esto explica parte del −$512.

### H. Filtros técnicos (antes de la entrada)

| Key | Default código | Actual | Descripción |
|---|---|---|---|
| `ta_use_atr` | `true` | **`true`** | Filtrar entradas por ATR(14). Si el impulso (BTC vs strike en $) es < `ta_min_normalized_impulse * ATR`, skip. |
| `ta_min_normalized_impulse` | `0.8` | **0.3** | Impulso normalizado mínimo (en ATR). A 0.8 = requiere 80% del ATR. **Tu 0.3 = MUY permisivo, deja pasar impulsos pequeños.** |
| `ta_use_rsi` | `true` | **`true`** | Filtrar por RSI(14). No compra UP si `RSI > ta_rsi_overbought`. No compra DOWN si `RSI < ta_rsi_oversold`. |
| `ta_rsi_overbought` | `75.0` | **75.0** | RSI sobre el cual NO comprar UP. |
| `ta_rsi_oversold` | `25.0` | **25.0** | RSI bajo el cual NO comprar DOWN. |
| `ta_use_volume` | `false` | **`true`** | Filtrar por volumen. (Default código era `false` pero en tu DB está `true`.) |
| `ta_min_volume_ratio` | `1.5` | **1.5** | Volumen actual debe ser ≥ 1.5× el promedio. |

**Diagnóstico del filtro ATR:** `ta_min_normalized_impulse=0.3` es 2.7× más
permisivo que el default. Esto deja pasar entradas con impulsos muy
pequeños, que probablemente sean ruido → explica parte del WR=49.8%.

### I. Streak Snapper (referencia, no es TA)

| Key | Default | Actual | Descripción |
|---|---|---|---|
| `ss_enabled` | `false` | `true` | Switch de Streak Snapper. |
| `ss_sizing` | `flat` | `flat` | Modo de sizing: `flat`, `kelly`, `martingale`. |
| `ss_kelly_fraction` | 0.25 | 0.25 | Fracción de Kelly (cuando `sizing=kelly`). |
| `ss_martingale_mult_factor` | 2.1 | 2.1 | Multiplicador tras pérdida (cuando `sizing=martingale`). |
| `ss_max_entry_age` | 60 | **180** | Edad máxima (segundos) de la racha a operar. |
| `ss_range_max_pct` | 100 | 100 | Rango máx (% del ATR) para entrar. |
| `ss_vol_min_pct` / `ss_vol_max_pct` | 0/100 | 0/100 | Filtros de volatilidad (% del ATR). |
| `ss_trading_hours` | (vacío) | (vacío) | Horas activas (CSV vacío = 24/7). |

---

## 3. Conflictos entre parámetros

Hay **8 conflictos reales** que pueden hacer que el bot pierda dinero o no
entre cuando debería. Los listo por severidad.

### 🔴 Conflictos críticos

#### C1. `ta_min_normalized_impulse = 0.3` vs el resto del filtro

`ta_min_normalized_impulse=0.3` deja pasar impulsos del 30% del ATR(14). Para
BTC 5-min, ATR(14) ≈ $50-150 → el bot entra con impulsos de solo $15-45.
Eso es **ruido intra-ventana**, no señal real.

**Consecuencia:** Muchas entradas en coin-flip territory (50/50) que
acumulan slippage + comisión sin edge. WR se queda en ~50% y el P&L neto
es negativo.

**Acción:** subir a `0.6-0.8`. Solo aceptar impulsos de ≥60% del ATR
(real signal).

#### C2. `ta_complete_cap = 0.88` vs `ta_min_ask = 0.55` + `ta_max_ask = 0.62`

`ta_complete_cap = 0.88` significa que el bot acepta second_ask hasta
`0.88 − 0.62 = 0.26` (cuando compró al max). Lock = 0.12.

Pero `ta_hedge_max_sum = 0.96` permite un hedge aún más caro (lock = 0.04).

**Consecuencia:** el bot cierra pares con 12 ¢ de lock en lugar de 18 ¢
(default). La estrategia gana menos por trade ganador, sin que los
perdedores sean menores.

**Acción:** bajar a `ta_complete_cap = 0.82-0.84` para asegurar más lock
por par. Reduce la frecuencia pero mejora el ratio risk/reward.

#### C3. Stop-loss permisivo en triple combinación

Tres parámetros juntos son más permisivos que el default:

| Parámetro | Default código | Tu config | Efecto |
|---|---|---|---|
| `ta_stop_loss_time_sec` | 60 | **120** | Espera el doble de tiempo antes de evaluar |
| `ta_stop_loss_threshold` | 0.25 | **0.50** | Acepta pérdidas 2× mayores |
| `ta_trailing_stop_pct` | 0.30 | **0.50** | Acepta drawdown 67% mayor |

**Consecuencia:** Combinados, un trade perdedor puede llegar a −50% sin
disparar stop. El stop catastrófico (50%) sí dispara, pero después de que
la pérdida ya está materializada. En la ventana de tiempo entre
`time_sec=120` y el threshold de 50%, hay ~60s donde el bot está
expuesto sin protección.

**Acción:** restaurar defaults: `time_sec=60`, `threshold=0.30`,
`trailing_pct=0.35`.

### 🟡 Conflictos moderados

#### C4. `ta_hedge_drop_pct = 0.30` vs `ta_hedge_max_sum = 0.96`

`ta_hedge_drop_pct = 0.30` hace que el hedge se dispare cuando la primera
pata cae 30% (más temprano). Pero `ta_hedge_max_sum = 0.96` permite
aceptar hedges muy caros (lock = 0.04).

**Consecuencia:** El bot abre hedges ANTES (cuando todavía hay tiempo de
encontrar mejor precio) PERO acepta locks pequeños (0.04 = 4 ¢/share).
Combinado: hedge rápido pero poco rentable.

**Acción:** o (a) bajar `ta_hedge_drop_pct` a 0.40-0.45 (más selectivo,
hedge más caro) o (b) bajar `ta_hedge_max_sum` a 0.90 (mayor lock). Las
dos van en direcciones opuestas; recomendaría (a) para trades con más
tiempo de reacción.

#### C5. `ta_lpt_enabled = false` vs el resto de la cadena LPT

LPT está completamente deshabilitado. Cuando llega a T-90s sin haber
entrado, no hace nada. La posición queda vacía hasta el cierre.

**Consecuencia:** se pierden oportunidades de "late pair" cuando el spread
se cierra cerca del final. Si quieres habilitarla, ajusta:
- `ta_lpt_cap = 0.92` (actual) → bajar a 0.85-0.88 (más selectivo, solo
  pares con lock real).
- `ta_lpt_min_left = 20` → ok.
- `ta_lpt_max_left = 148` → ok, justo bajo entry_cutoff de 120s. Hmm,
  `max_left > entry_cutoff` está mal: significa que LPT puede dispararse
  incluso antes de que cierre la ventana de entrada. ¿Es eso lo que
  quieres?

#### C6. `ta_min_itm_pct = 0.02` vs `ta_max_ask = 0.62`

`ta_min_itm_pct = 0.02` = 0.02% = en BTC a $77k, eso es solo **$15.4** de
movimiento. Es un umbral muy bajo que se cumple con ruido intra-spread.

`ta_max_ask = 0.62` = compra hasta 62¢. Esto significa que el bot puede
pagar caro (62¢) por un movimiento de solo $15 (que es ~0.02% del strike).

**Consecuencia:** La ratio riesgo/reward es desfavorable: pagas 62¢ para
ganar $1, basándote en que BTC se moverá solo $15. Probabilidad
sub-50%. WR ≈ 50% × 0.62 payoff = espera negativa.

**Acción:** subir `ta_min_itm_pct` a 0.04-0.05 (más selectivo, edge real).

### 🟢 Conflictos menores / informativos

#### C7. `ta_use_volume = true` vs código que dice `False`

El default código es `False`, pero tu DB tiene `True`. Ambos están OK; el
filtro de volumen se aplica en código (línea 800+). Si la señalización de
volumen no es fiable, set `ta_use_volume=false` para evitar filtrar
oportunidades válidas.

#### C8. `ta_stop_hold_gate_sec = 45` no se usa

Configurado en DB pero el código no lo lee. Es vestigio. Puedes borrarlo
o dejarlo (no afecta).

---

## 4. Recomendaciones priorizadas

| # | Acción | Impacto | Esfuerzo |
|---|---|---|---|
| 1 | `ta_min_normalized_impulse: 0.3 → 0.6` | Alto (reduce entradas ruidosas) | Bajo |
| 2 | `ta_complete_cap: 0.88 → 0.82` | Alto (más lock por par) | Bajo |
| 3 | `ta_min_itm_pct: 0.02 → 0.04` | Alto (mejor ratio risk/reward) | Bajo |
| 4 | Restaurar stop-loss defaults | Alto (corta pérdidas antes) | Bajo |
| 5 | `ta_lpt_cap: 0.92 → 0.88` (si activas LPT) | Medio | Bajo |
| 6 | `ta_hedge_drop_pct: 0.30 → 0.40` | Medio | Bajo |

**Esperado:** subir el WR de 49.8% → ~55-58% y revertir el P&L de −$512.

**Cuidado:** no cambies todos los parámetros a la vez. Hazlo de uno en uno
y observa el efecto durante ~50-100 trades (5-7 días) antes de cambiar
el siguiente.

---

## 5. Riesgos del estado actual

1. **Bot pierde dinero en paper mode** (−$512 / 586 trades). Si se
   activara real mode con estos parámetros, perdería dinero real.

2. **Stop-loss muy permisivo** permite pérdidas grandes. Cada stop
   catastrófico ejecutado es ~$60 USD perdidos.

3. **LPT desactivado** = se pierden oportunidades de pairs en los últimos
   20-148s. Si decides habilitarlo, revisar `ta_lpt_cap`.

4. **Alta frecuencia de trades** (~1.7h entre trades) acumula slippage.
   Reducir la frecuencia mejoraría el P&L.
