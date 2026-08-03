# 📋 Resumen — Streak Snapper (Moon Dev's Bot #1)

## 🎯 Tesis de la estrategia

Las ventanas de 5 minutos de BTC (up/down) muestran un comportamiento **anti-persistente**: después de una racha de 4+ ventanas consecutivas en la misma dirección (UP o DOWN) cuya suma acumulada de movimiento supera **3x el ATR de 1 hora**, la siguiente ventana REVIERTE ~54.3% de las veces. El bot apuesta contra la racha (la "desvanece") comprando el lado contrario al inicio de la siguiente ventana, con un límite estricto de precio.

## 📊 Datos que respaldan la tesis

| Métrica | Valor |
|---|---|
| Ventanas analizadas | 104,762 (52 semanas de datos reales de BTC) |
| Ventanas con señal (4+ streak + >3x ATR) | 8,802 |
| Win rate del fade | **54.3%** |
| Peor trimestre en backtest | 52.2% (positivo en los 5 trimestres) |
| Win rate SIN filtro ATR | 50.7% (el filtro ATR es crítico) |
| Datos live (1,642 ventanas) | Tras 3 UPs seguidas, DOWN ganó 61.6% (n=164) |

## ⚙️ Parámetros clave

| Parámetro | Valor | Explicación |
|---|---|---|
| `STREAK_MIN` | 4 | Mínimo de ventanas consecutivas misma dirección |
| `STRETCH_MULT` | 3.0 | Movimiento acumulado > 3x el ATR de 1h |
| `ATR_WINDOWS` | 12 | ATR = media móvil de \|cambio 5min\| sobre 12 ventanas (= 1 hora) |
| `CUM_WINDOWS` | 4 | Movimiento acumulado = suma de las últimas 4 ventanas de la racha |
| `LIMIT_CAP` | $0.52 | Precio máximo de entrada (pagar 55¢ quema la ventaja) |
| `ENTRY_WINDOW_SEC` | 20s | Solo entrar en los primeros 20s de la nueva ventana |
| `CANCEL_AFTER_SEC` | 60s | Cancelar orden si no se llena en 60s — nunca perseguir |
| `SIZE_USD` | $10 | Apuesta plana de $10 (fase de validación, primeros 200 trades) |
| `KILL_SWITCH_WR` | 50% | Pausar entradas si win rate < 50% en últimas 50 trades |
| `KILL_SWITCH_MIN_TRADES` | 20 | Solo activar kill switch tras 20 trades resueltos |

## 🔄 Flujo del bot (por cada ventana de 5 minutos)

```
1. ESPERAR → Siguiente ventana de 5 min (btc-updown-5m-{timestamp})
2. ABRIR VENTANA → En los primeros 20 segundos:
   a. Resolver trades pendientes (vía gamma outcomePrices)
   b. Verificar kill switch (win rate trailing-50)
   c. Obtener historial de últimas 16 ventanas (API Moon Dev + gamma oracle)
   d. Calcular señal:
      - streak_len = ventanas consecutivas misma dirección
      - atr = media de |movimiento| en últimas 12 ventanas
      - cum_move = suma de movimiento en últimas 4 ventanas
      - stretch_ratio = |cum_move| / atr
      - SEÑAL = streak_len >= 4 AND stretch_ratio > 3.0
3. SI HAY SEÑAL:
   a. fade_side = dirección contraria a la racha
   b. Buscar token_id del mercado actual (UP o DOWN)
   c. Colocar orden LIMIT BUY a ≤ $0.52 (min(0.52, best_ask))
   d. Esperar 60s → si se llena, mantener hasta resolución
                    → si no se llena, cancelar y saltar ventana
4. SI NO HAY SEÑAL → Loggear skip con razón (SKIP_NO_STREAK o SKIP_STRETCH)
5. REPETIR desde 1
```

## 🧩 Componentes técnicos

| Componente | Detalle |
|---|---|
| **Cliente CLOB** | `py_clob_client_v2` (V1 deprecado, lanza `PolyApiException`) |
| **Cuenta** | AUG14 (separada de la cuenta principal OG) |
| **Firma** | Tipo 2 (Gnosis Safe) en Polygon (chain_id=137) |
| **Feed de ticks** | Moon Dev API — ticks reales de BTC para calcular ATR y movimiento |
| **Oráculo de dirección** | Gamma API `outcomePrices` (fuente primaria); dirección por ticks como fallback |
| **Order book** | `clob.polymarket.com/book` — best bid/ask |
| **Posiciones** | `data-api.polymarket.com/positions` — verificar fills |
| **Modo papel** | `PAPER_MODE = False` por defecto (LIVE FIRE). Cambiar a True para pruebas |

## 🚫 Reglas estrictas (no modificar)

1. ❌ **NO contar rachas simples** — sin el filtro 3x ATR, el edge colapsa a 50.7%
2. ❌ **NUNCA pagar > 52¢** — todo el edge (54.3%) depende de entrar barato (~51¢)
3. ❌ **NO perseguir** — cancelar si no se llena en 60s
4. ❌ **NO salir a mitad de ventana** — el 54.3% se mide open-to-close
5. ✅ Entrar en los **primeros 20 segundos** (la liquidez near-mid vive ahí; bids tardíos fallan 95%)
6. ✅ **Máximo 1 posición por ventana**
7. ✅ **Saltar ante cualquier gap de datos** — nunca operar con datos falsos o faltantes
8. ✅ Los winners se redimen con `OG_redeem.py` (flujo separado)

## 🛡️ Kill Switch

- Mide el win rate sobre las **últimas 50 operaciones resueltas**
- Si el win rate cae por debajo de **50%** (~2 sigmas por debajo del 54.3% del backtest), **pausa todas las entradas**
- Solo se activa tras mínimo **20 trades resueltos**
- Requiere revisión manual para reactivar

## 📝 Logging

Cada ventana se registra en `data/streak_snapper_log.csv` con columnas:

`snapshot_time | window_slug | streak_len | streak_dir | cum_move_usd | atr_usd | stretch_ratio | fade_side | limit_price | action | entry_price | shares | outcome | won | pnl_usd`

Se loggean **todas** las ventanas (entradas Y saltos), con razones detalladas:
- `ENTER` — operación ejecutada
- `NO_FILL` — orden no se llenó en 60s
- `SKIP_NO_STREAK` — no hay racha suficiente
- `SKIP_STRETCH` — racha existe pero sin estiramiento 3x ATR
- `SKIP_FEED_GAP` — datos insuficientes
- `SKIP_NO_MARKET` — mercado no encontrado
- `SKIP_LATE` — se perdió la ventana de 20s
- `KILL_SWITCH` — entradas pausadas por kill switch
- `ORDER_FAILED` — fallo al colocar la orden

## 💰 Expectativa y riesgos

- **Edge bruto:** 54.3% a entrada ~$0.50 ≈ **4 centavos de expectativa por share** (antes de fees y slippage)
- **Riesgo principal:** Es un edge **muy fino** — 2 centavos de slippage cortan el edge a la mitad
- **Perfil:** Grinder, no cohete. Sobrevive solo con disciplina estricta de precio (≤52¢) y volumen
- **Disclaimer del autor:** "No he resuelto el mercado de 5 minutos — este bot es una prueba de si un edge fino y bien documentado sobrevive el contacto con el libro real"

## 🚀 Arranque

```bash
python streak_snapper.py
```

Requisitos:
- `.env` en raíz del repo con `PRIVATE_KEY_AUG14`, `PUBLIC_KEY_AUG14`, `MOONDEV_API_KEY`
- `py_clob_client_v2` instalado
- Feed de ticks BTC (Moon Dev API)
- `PAPER_MODE = True` para pruebas sin riesgo

---

*Estrategia creada por Moon Dev 🌙 — transmitida en vivo en YouTube. Usar bajo propio riesgo.*
