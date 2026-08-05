# 🗺️ RUTA — Streak Snapper v2

> Guía de trabajo por fases para futuras sesiones de desarrollo.

---

## ✅ Fase 1 — Migración y limpieza (COMPLETADA)

- [x] Archivar estrategias viejas en `bot/archive/`
  - `strategy_mm.py` → Box Builder Market Making
  - `strategy_corridor.py` → Corridor Collector
  - `corridor_trader.py` → Thread del Corridor
- [x] Eliminar imports y referencias a estrategias viejas
- [x] Simplificar `state.py`, `config.py`, `main.py`, `dashboard.py`

---

## ✅ Fase 2 — Core del Streak Snapper (COMPLETADA)

- [x] `bot/binance_api.py` — Cliente BTC klines 5m y 4h
- [x] `bot/strategy_streak.py` — Motor de ambas formas + Martingale
- [x] `bot/streak_trader.py` — Thread principal del ciclo 5min
- [x] `bot/db.py` — Modelos SQLAlchemy + persistencia
- [x] Correcciones: DB app context, `ss_enabled` en config, `pyproject.toml`

---

## ✅ Fase 3 — Dashboard HTML (COMPLETADA)

- [x] `bot/templates/settings.html`
  - Selector `ss_mode`: fade / trend / both
  - Campos: `ss_fade_base_bet`, `ss_fade_limit_cap`, `ss_fade_streak_min`
  - Campos: `ss_trend_base_bet`, `ss_trend_limit_cap`
  - Campo: `ss_martingale_mult_factor`
  - Toggle `ss_enabled`
- [x] `bot/templates/dashboard.html`
  - Mostrar estado actual del Martingale (multiplicador, racha pérdidas)
  - Stats separadas por estrategia (ss_fade / ss_trend)
  - Precios bid/ask en vivo
  - Libro de órdenes en vivo (bids/asks/volumen/spread)
  - Tiempo restante del evento (countdown formateado)
  - Historial de trades desde DB
- [x] `bot/static/dashboard.js` — Lógica JS completa
- [x] `bot/static/dashboard.css` — Estilos para todos los componentes

---

## ✅ Fase 3.5 — Mejoras de estabilidad (COMPLETADA)

- [x] WebSocket se mantiene conectado durante toda la ventana (antes se apagaba a los 3s)
- [x] Fix: sincronización de trades DB ↔ STATE (métricas P&L ahora correctas)
- [x] Fix: `_on_close` no emite warning cuando el cierre es intencional
- [x] Bot espera al siguiente boundary de 5 min al iniciar
- [x] `STATE.load_trades_from_db()` — KPIs sobreviven reinicios
- [x] Libro de órdenes en vivo desde WS `book` events (bids/asks top 10 + volumen)
- [x] Tiempo restante del evento formateado (4m 32s en vez de "272s")

---

## ✅ Fase 4 — Testing (COMPLETADA)

- [x] Probar en modo paper: `python run.py` — funciona
- [x] Probar con SQLite local (sin DATABASE_URL) — funciona
- [x] Test unitarios — 199/199 pasan:
  - `tests/test_binance_api.py` — 26 tests ✅
  - `tests/test_strategy_streak.py` — 27 tests ✅
  - `tests/test_db.py` — 28 tests ✅ (CRUD trades, Martingale, BotConfig, init_db)
  - `tests/test_streak_trader.py` — 118 tests ✅ (tick, resolución, stats, migración)
- [ ] Probar con PostgreSQL real (con DATABASE_URL) (pendiente)

---

## ✅ Fase 4.5 — Revisión de código y correcciones (COMPLETADA)

Auditoría completa + ejecución en vivo. Corregido:

- [x] **Dashboard muerto** — `renderPrices()` usaba `ph` sin declarar, lanzando un
      `ReferenceError` que el `try/catch` de `refresh()` se tragaba. Gráfico,
      métricas por estrategia, tabla de operaciones y log **nunca** se
      renderizaban.
- [x] **Resolución no persistía en memoria** — `session.merge()` devuelve otra
      instancia; el estado se escribía en la copia y el bucle de sincronización
      leía el original, dejando los trades como `open` para siempre.
- [x] **Martingala una ventana por detrás** — se entraba a la ventana siguiente
      antes de resolver la anterior. Ahora Binance liquida al instante del
      cierre y Gamma confirma después. Verificado en vivo: ventana cerrada a las
      13:30:00, liquidada a las 13:30:05, entrada siguiente a las 13:30:08 ya con
      el multiplicador nuevo (×1.50).
- [x] **Feed de resolución** — Polymarket resuelve con **Chainlink**, no con
      Binance. En ventanas casi planas los feeds discrepan (observado). Se
      difiere a Gamma por debajo de `NEAR_FLAT_THRESHOLD` (~3% de las ventanas).
- [x] **Precio límite un tick por debajo** — `math.floor(0.29/0.01)` da 28. En
      modo real la orden quedaba por debajo del ask y no se llenaba. Ahora
      `Decimal`.
- [x] **KPIs sobre 100 trades** — `/state` calculaba P&L, win rate, ROI y
      bankroll con la misma página de 100 filas de la tabla. Ahora se agregan en
      SQL sobre toda la tabla.
- [x] **Señales contradictorias** — en modo `both` se compraban ambos lados de la
      misma ventana (neutro garantizado). Ahora se omite la ventana.
- [x] **Vela 4h congelada** — la dirección se fijaba en la primera lectura y no
      se actualizaba durante 4 h aunque la vela girase.
- [x] Libro de órdenes: el backend ya lo enviaba pero no existía UI. Añadida.
- [x] Fallo de puerto ocupado: mataba el proceso tras loguear "dashboard
      listening". Ahora se comprueba antes y el mensaje explica cómo resolverlo.
- [x] Log de acceso de Werkzeug (1 req/s) silenciado — ahogaba el log del bot.
- [x] Varios: doble prefijo `[SS SS_TREND]`, `gamma_host` hardcodeado, carga sin
      límite de trades al arrancar, código muerto.

- [x] Estados Martingale no cargaban en el primer arranque: los helpers de `db.py`
      devolvían la instancia ORM después de un `commit()`, que expira todos sus
      atributos. Ahora devuelven un `MartingaleSnapshot`.
- [x] `*.db` añadido a `.gitignore` — `streak_snapper.db` se habría subido al repo.

### Pendiente de esta revisión

- [ ] La configuración de `/settings` no se persiste (`bot_config` está
      declarada pero no se usa) — se pierde en cada reinicio. Decidir si la DB
      debe sobrescribir las variables de entorno al arrancar, o al revés.

---

## ✅ Fase 4.6 — Rediseño del dashboard (Notika) + pendientes de `revisar.md`

> Tema: [Notika](https://github.com/puikinsh/notika) de Colorlib (MIT con
> atribución). Assets vendorizados en `bot/static/vendor/`, **sin Vite** — el
> despliegue sigue necesitando solo Python. Ver `bot/static/vendor/NOTICE.md`.

**Peticiones de `revisar.md`, todas implementadas:**

- [x] **Gráficos** de curva de capital, drawdown, win rate (acumulado y móvil) y
      P&L por estrategia — Chart.js, alimentados por `/api/metrics/series`
- [x] **Filtros** de búsqueda (estrategia, estado, lado, modo, fechas, texto) y
      **descarga CSV** que respeta los filtros activos
- [x] **Paginación de 25** registros — `/api/trades`, filtrada y paginada en SQL
- [x] **Autenticación** — `DASHBOARD_PASSWORD` + sesión Flask firmada
- [x] **Saldo corregido** — separa disponible de comprometido

**Correcciones no previstas, encontradas durante el trabajo:**

- [x] 🔴 **El dashboard no tenía ninguna autenticación** y `POST /config` puede
      cambiar el bot a modo real. Ahora todo requiere sesión salvo `/healthz`,
      el CORS está cerrado, y **el bot se niega a arrancar** si escucharía en
      `0.0.0.0` sin contraseña.
- [x] 🔴 **El badge de Chainlink estaba muerto**: `dashboard.js` leía
      `status.chainlink` y `/state` no lo incluía.
- [x] 🔴 **`resolution_source` era `VARCHAR(8)`** y `"chainlink"` ocupa 9.
      Ampliado, con migración para PostgreSQL (`_WIDEN_COLUMNS` en `db.py`).
- [x] `/state` enviaba ~100 trades por segundo para pintar 50. Ya no los envía.
- [x] Plantilla base única — la cabecera estaba duplicada y había divergido.
- [x] El polling se detiene con la pestaña oculta y los errores de red dejan de
      tragarse en silencio (antes un backend caído parecía un panel congelado).
- [x] Campos de Chainlink y botón de restaurar `.env` en `/settings`: ambos
      existían en el backend desde el principio, sin UI que los usara.
- [x] `tests/test_dashboard.py` — **45 tests nuevos**, antes no había ninguno.

**Pendiente de este bloque:**

- [x] Punto 6 de `revisar.md` → **Fase 4.7**, más abajo.
- [ ] Servir con gunicorn en vez del servidor de desarrollo de Werkzeug.
      ⚠️ **La nota original de este punto tenía dos datos mal:**
      - El `app.run()` está en **`bot/main.py:218`**, no en `main.py`. El
        `main.py` de la raíz es un stub (`print("Hello from repl-nix-workspace!")`).
      - 🔴 **`.replit:6` ya intenta servir con gunicorn y está roto**:
        `run = ["gunicorn", "--bind", "0.0.0.0:5000", "main:app"]` apunta a ese
        stub, que no expone ningún `app`. El deployment falla con *Failed to
        find attribute 'app' in 'main'*. El workflow de desarrollo funciona
        porque usa `python run.py`.

      Y el cambio no es cosmético. Gunicorn solo sirve WSGI: no ejecutaría
      `init_db()`, `start_price_fetcher()`, el feed de Chainlink ni el
      `StreakSnapperTrader`. Hace falta un entrypoint WSGI que arranque esos
      hilos, y además:
      - **`--workers 1` es obligatorio.** Varios workers son varios
        `StreakSnapperTrader` → órdenes duplicadas y martingalas divergentes.
        Y `STATE` es memoria de proceso: con 2+ workers el dashboard mostraría
        datos distintos según quién respondiese. Concurrencia solo con
        `--threads`.
      - `deploymentTarget = "autoscale"` (`.replit:4`) es incompatible por lo
        mismo — escalar a 2 instancias son 2 bots operando. El target correcto
        es `reserved-vm`.
      - `_check_port_available()` (`bot/main.py:118`) y el log «dashboard
        listening» dejan de aplicar.

---

## ✅ Fase 4.7 — Ciclo 4h de un solo lado (punto 6 de `revisar.md`)

> Fecha: 5-ago-2026. Suite: **304 tests** (antes 278).

**Qué hace ahora `ss_trend`:**

La tendencia se mide sobre la **vela 4h ya cerrada** y su lado se opera durante
las **4h siguientes**. Si el bloque termina con pérdidas sin recuperar, el ciclo
**se prorroga hasta ganar** antes de volver a mirar la tendencia.

```
vela 4h N (cerrada)          bloque N+1 (48 ventanas de 5 min)
|-------------------|        |-----------------------------------|
 open --------> close         lado fijo = dirección(N)
 |mov| >= umbral?  sí  ---->  se opera ese lado
                    no  ---->  no se opera el bloque N+1
```

| Situación | Acción |
|---|---|
| Ciclo abierto, dentro del bloque anclado | operar `cycle_side` |
| Bloque expirado, **multiplicador > 1** | **prorrogar** hasta ganar |
| Bloque expirado, multiplicador == 1 | cerrar ciclo y reevaluar |
| Sin ciclo, movimiento ≥ umbral | abrir ciclo, operar |
| Sin ciclo, movimiento < umbral | sin señal |

Ganar cierra el ciclo. Si aún queda bloque, la siguiente ventana lo reabre con
el mismo lado a ×1.0 — sale de las reglas, sin caso especial.

- [x] `bot/binance_api.py` — `get_last_closed_4h_candle()` con `strength` con
      signo y el bloque que licencia
- [x] `bot/db.py` — `martingale_state.cycle_side` y `cycle_anchor_ts`, en la
      misma fila que el multiplicador (una escritura, imposible desincronizar),
      con migración en `_LATE_COLUMNS`. Helpers `open_cycle` / `close_cycle`;
      `reset_martingale_state()` cierra también el ciclo
- [x] `bot/strategy_streak.py` — motor del ciclo
- [x] `bot/streak_trader.py` — en señales contradictorias **prevalece Trend** y
      se descarta Fade, en vez de omitir la ventana: si no, fade podría dejar un
      ciclo perdedor sin cerrar indefinidamente
- [x] `bot/backtest.py`, dashboard, `/settings`, `.env.example`

### ⚠️ Reversión consciente de la corrección de Fase 4.5

La entrada «**Vela 4h congelada**» de Fase 4.5 catalogó como bug que la
dirección se fijase y no se actualizara durante 4 h. **Eso es exactamente lo
que el punto 6 pide**, así que se ha revertido a propósito. La diferencia que lo
hace correcto ahora: se lee la vela **cerrada**, no la que se está formando —
una vela recién abierta tiene `close == open` y no hay tendencia que leer. No
volver a "corregirlo".

### 🔴 La aritmética del ×1,5 no cerraba el ciclo

Comprando `s` shares a precio `p`, un ciclo solo se recupera indefinidamente si

```
factor > 1 / (1 − p)
```

Para ×1,5 eso exige `p < 0,333`, y `SS_TREND_LIMIT_CAP` es **0,52**. Con base 5
shares a 0,52 el neto de ganar en el intento 3 ya era **−1,10**: «seguir hasta
ganar» agrandaba la pérdida en vez de cerrarla.

- **`SS_MARTINGALE_MULT`: 1.5 → 2.1** (mínimo teórico 2,083 a 0,52).
- `min_recovering_factor()` en `bot/config.py` es la fuente única del cálculo;
  `bot/main.py` avisa al arrancar si el factor configurado no recupera, y la
  previsualización de `/settings` ahora muestra **el neto si ganas en cada
  paso**, no solo el tamaño de la apuesta.
- ⚠️ El factor es **compartido** por ambas estrategias y el cap de Fade (0,60)
  exige ×2,5. Con 2,1 el aviso salta para Fade.

### 🔴 El backtest de `ss_trend` miraba el futuro

`build_4h_trend_map()` mapeaba cada ventana a la vela 4h **que la contenía**, o
sea a un cierre que aún no había ocurrido. Sustituida por
`build_4h_signal_map()`, que usa la última vela **cerrada antes** de la ventana.
Medido sobre 3.000 ventanas etiquetadas por Gamma:

| Mapa | Win rate | P&L |
|---|---|---|
| Antiguo (con sesgo) | 54,4% | +$669 |
| Nuevo (vela cerrada) | **48,7%** | **−$1.210** |

**Todos los resultados de `ss_trend` publicados antes de este cambio estaban
inflados por 5,7 puntos de win rate.**

### 📉 `SS_TREND_MIN_STRENGTH = 0.008` — y qué mide de verdad

Barrido sobre **10.000 ventanas (34,7 días)** con etiquetas de Gamma
(`python -m bot.backtest --mode trend --min-strength X --factor Y`):

| umbral | trades | win rate | z | P&L ×1,5 | P&L ×2,1 | maxDD ×2,1 | mayor trade |
|---|---|---|---|---|---|---|---|
| 0,00% | 9980 | 49,1% | −1,86 | −$2.057 | +$13.581 | −$36.509 | $40.163 |
| 0,30% | 5453 | 48,9% | −1,61 | −$779 | +$7.038 | −$3.940 | $4.337 |
| 0,50% | 3872 | 48,5% | −1,83 | −$600 | +$4.954 | −$3.940 | $4.337 |
| **0,80%** | 1764 | 48,3% | −1,43 | −$158 | +$2.195 | **−$892** | **$983** |
| 1,20% | 735 | 47,3% | −1,44 | −$105 | +$898 | −$892 | $983 |

Dos conclusiones que conviene no perder:

1. **El win rate está por debajo del 50% en todos los umbrales** (47,3–49,1%,
   z entre −1,1 y −1,9). Esta señal **no tiene edge medible**; no hay umbral que
   "elegir bien". Sobre 3.000 ventanas salía 51,9% a 0,8%, y con 10.000 se dio
   la vuelta a 48,3% — era ruido.
2. **El P&L positivo de ×2,1 es un artefacto de bankroll infinito.** Sin umbral,
   el simulador llega a apostar **$40.163 en una sola ventana de 5 minutos** y a
   un drawdown de −$36.509. Ni es financiable con el bankroll de $1.000 por
   defecto, ni es llenable en el libro de Polymarket.

0,8% se elige por **supervivencia de capital**, no por rentabilidad: es el
umbral más bajo cuyo peor caso (−$892 de drawdown, $983 de trade máximo) todavía
cabe en el bankroll por defecto. Re-medir antes de operar en real.

### Decisión sobre Chainlink

Se mantiene **Binance + Gamma** como fuente de resolución. Adoptar Chainlink
exigiría el stream `btc-usd` de pago ($150/mes, sin tier gratuito, sin SDK de
Python — **[§11.2.b](CHAINLINK_TWAP.md)**), y el propio documento recomienda
medir el residuo del umbral `5e-5` unas semanas antes de gastarlo. En
consecuencia, el pendiente «cablear el veto por divergencia» de la Fase 7 queda
**descartado**: Chainlink no se usará para detectar impulsos ni retrocesos.

---

## 🔲 Fase 5 — Producción / VPS (PENDIENTE)

- [ ] Configurar `DATABASE_URL` en el VPS (PostgreSQL)
- [ ] Configurar `.env` con `PRIVATE_KEY`, `PROXY_WALLET`, `TRADING_MODE=real`
- [ ] Systemd service o similar para auto-reinicio
- [ ] Logging a archivo (rotativo)
- [ ] Monitoreo: alertas si Martingale llega a ×5+
- [ ] Backup automático de la DB

---

## 🔲 Fase 6 — Mejoras futuras (IDEAS)

- [ ] Reincorporar filtro ATR (3x stretch) para la Forma 1 (mejora el edge)
- [ ] Kill switch automático: pausar si win rate < 50% en últimas 50 operaciones
- [ ] Soporte multi-símbolo (SOL, ETH además de BTC)
- [ ] Alertas Telegram/Discord en trades y pérdidas
- [ ] Backtesting con datos históricos de Binance

### 📊 Base en dólares en vez de shares — analizado 5-ago-2026, **no implementado**

> Escenario evaluado: sustituir `shares = 5 × mult` por `coste = $5 × mult`, con
> `shares = coste / precio`. Decisión: **dejarlo como está**. Se registra aquí
> para no volver a derivarlo.

**El problema que resolvería es real.** «5 shares base» no es un tamaño de
apuesta: su coste depende del precio de llenado. Sobre los 65 trades reales de
la DB, los precios de entrada van de **0,18 a 0,60** (mediana 0,51) y solo el
**43 % llena al cap**, así que el escalón base del martingale oscila entre
**$0,90 y $3,00** — 3,3× de diferencia. Una martingala existe para controlar el
dinero en riesgo escalón a escalón, y hoy lo decide el mercado.

**Lo que NO cambiaría:**

- La condición de recuperación. El beneficio de ganar pasa de `s(1−p)` a
  `d(1−p)/p`, pero la razón beneficio/riesgo es `(1−p)/p` en ambos: son las
  mismas cuotas escritas de otra forma. **`factor > 1/(1−p)` sigue igual**, y el
  ×2,1 sigue siendo el valor correcto.
- El valor esperado. Es **cero en ambos esquemas** a precios justos. La
  diferencia es varianza y exposición, no rentabilidad.

**Lo que costaría:** en el backtest todos los fills son al cap, así que el
cambio es un escalado exacto de $2,60 → $5,00 por trade base (×1,92). Al umbral
0,8 % con ×2,1: drawdown **−$892 → −$1.715** y mayor trade **$983 → $1.890**.
Ambos superan el bankroll de $1.000 por defecto. La base equivalente a la
exposición actual sería **$2,60**, no $5.

**Riesgo nuevo:** a precios bajos la base en dólares compra muchas más shares —
a 0,18 el sexto escalón son **1.134 shares** frente a 204 hoy. Habría que
comprobar la profundidad del libro, cosa que hoy no se hace.

Si se retoma, toca: `ss_*_base_shares` → `*_base_stake` (config, `.env.example`,
`/settings`, `state.py`, `dashboard.py`), el suelo `max(5.0, …)` de
`strategy_streak.py` —que es un suelo **en shares**— y `MartingaleSim` en
`backtest.py`. Verificar antes el tamaño mínimo de orden de Polymarket.

---

## ✅ Fase 7 — Chainlink TWAP + Binance (IMPLEMENTADA, salvo 1 decisión)

> Análisis, medición y estado: **[CHAINLINK_TWAP.md](CHAINLINK_TWAP.md)**
> Fecha: 3-ago-2026. Los topics TWAP del relay lanzan el **4-ago-2026**.

**Completado:**

- [x] `bot/gamma_history.py` — etiquetas históricas de Gamma con caché en disco.
      Medido: **100% de cobertura** sobre 3.000 ventanas (10,4 días).
- [x] `bot/threshold_study.py` — medición reproducible del umbral, con el coste
      del diferimiento incluido (`python -m bot.threshold_study`).
- [x] Etiquetas del backtest desde Gamma — flag `--labels`, default `gamma`.
      Cambia el resultado de forma material: **$652 vs $518** de P&L combinado.
- [x] `bot/chainlink_feed.py` — cliente RTDS, apagado por defecto.
      **Probado en vivo:** el 500 previo al lanzamiento se registra una vez y el
      bot sigue operando (fail-open verificado).
- [x] `bot/chainlink_recorder.py` + tabla `chainlink_ticks` + purga por retención
      (`CL_TICK_RETENTION_DAYS`, default 30 días).
- [x] Contrato con `state.py` (`set_chainlink_status`) y badge en el dashboard
      con edad del tick y divergencia.
- [x] Config: 6 campos nuevos, todos apagados; `.env.example` documentado.
- [x] Tests: 27 nuevos (feed + grabador). Suite total **231 pasan**.
- [x] Investigadas las credenciales de Chainlink: **self-serve, $150/mes**,
      sin tier gratuito (**[§11.2.b](CHAINLINK_TWAP.md)**).

- [x] 🔴 **`NEAR_FLAT_THRESHOLD`: `1e-5` → `5e-5`** en `bot/binance_api.py`.
      Validado fuera de muestra sobre 1.000 ventanas frescas: el error de
      liquidación pasa de **4,68% → 2,03%** (−57%), a cambio de diferir el 11%
      en vez del 3,3%. **[§12.1](CHAINLINK_TWAP.md)**.
      ⚠️ **`1.5e-4` —lo que recomendaba el análisis original— era PEOR que el
      valor de partida**: sobre-apuesta un 24% más. Descartado.

**Pendiente a partir del 4-ago-2026:**

- [ ] Ejecutar el **checklist de go-live** (**[§11.5](CHAINLINK_TWAP.md)**) y
      calibrar `CL_TWAP_STALE_SECONDS` con la cadencia real observada
- [ ] Arrancar el grabador (`CL_RECORD_TICKS=true`) — lo que no se grabe se
      pierde para siempre
- [ ] Cablear el veto por divergencia en `strategy_streak.py` (el cálculo ya
      existe; **calibrar con semanas de tape antes de activarlo**)
- [ ] Columna «fuente de resolución» en el historial de trades (ya en la DB)

**Correcciones a las premisas iniciales** (detalle en el documento):

- El feed TWAP **no** es la fuente de resolución — los mercados resuelven contra
  el stream normal `data.chain.link/streams/btc-usd`
- La ventaja de Binance es **~0.31 s**, no 2.4 s — el topic `crypto_prices` de
  RTDS ya es Binance remuestreado a 1 Hz
- **Sí existe historial de Chainlink** — la Candlestick API sirve OHLC histórico,
  contra lo que concluía §6 (**[§11.2.c](CHAINLINK_TWAP.md)**). Aun así el
  backtest se etiqueta con Gamma, que es gratis y es la verdad de liquidación.

---

## 🔬 Fase 8 — ¿Existe edge direccional? (MEDIDO, 5-ago-2026)

> Pregunta de partida: ¿es la tendencia de 4h una buena forma de elegir lado, y
> hay alternativas mejores? Respuesta corta: **la señal de 4h está del lado
> equivocado de un efecto real**, y el efecto real es la reversión.
>
> Scripts: `scripts/signal_search.py`, `scripts/oos_validation.py`,
> `scripts/preopen_edge.py`. Datos: 10.119 ventanas etiquetadas por Gamma
> (35,2 días) y 3.734 con cotización previa a la apertura.

### 🔴 El precio que se medía no era el precio de entrada

`scripts/price_calibration.py` concluía que el mercado está perfectamente
calibrado (z=+0.00, n=3.986) y que por tanto ninguna señal puede ganar. **Esa
conclusión era un artefacto de muestreo.** `/prices-history` tiene fidelidad de
1 minuto, así que «la primera cotización a partir de +15 s» cae en realidad a
una **mediana de +67 s** dentro de la ventana. A esa altura el precio ya sabe
cómo va la ventana — información mucho mejor que cualquier señal previa.

El bot no entra a +67 s. Entra en los primeros segundos, y **la cotización
previa a la apertura es plana**: 0,505 durante los 15 minutos anteriores, con
libro real (bid 0,50 × 400 sh / ask 0,51 × 693 sh, **spread de 1 centavo**).

Medido sobre las mismas operaciones (`scripts/preopen_edge.py`, sección C):

| Momento de entrada | precio medio | brecha/share | ROI |
|---|---|---|---|
| Previo a la apertura | 0,514 | +0,0240 | **+4,67%** |
| A +60 s (lo que se medía antes) | 0,529 | +0,0091 | +1,71% |

### 📉 `ss_trend` no es neutral: está en el lado perdedor

Al precio previo real, más medio spread:

| Estrategia | n | acierto | precio | ROI/op | t |
|---|---|---|---|---|---|
| **ss_trend 4h (>0,8%)** | 1725 | 48,2% | 0,504 | **−4,22%** | −1,77 |
| ss_trend 4h, sin ventanas con racha | 1335 | 46,8% | 0,504 | −7,08% | −2,61 |
| **ss_fade racha≥4** | 1150 | 53,8% | 0,519 | **+3,74%** | +1,32 |
| anti-trend 4h (invertida) | 1725 | 51,8% | 0,506 | +2,22% | +0,94 |

Por tramos de ~8,8 días (ROI/op):

| Estrategia | T1 | T2 | T3 | T4 | total |
|---|---|---|---|---|---|
| ss_trend 4h | −8,3% | −8,2% | −2,4% | +3,9% | −4,2% |
| anti-trend 4h | +6,3% | +6,2% | +0,4% | −5,8% | +2,2% |
| fade racha≥4 | +14,1% | −3,8% | +5,4% | +2,2% | +3,7% |

### ✅ La reversión es real; el momentum no

`scripts/oos_validation.py` parte los 35 días en dos mitades, ordena 49 señales
en la primera y las mide en la segunda:

- **r = +0,727** entre acierto en entrenamiento y en prueba.
- Solo el **22%** de las señales cambia de lado (con ruido puro sería ~50%).
- `fade racha≥4`: 54,3% dentro de muestra → **53,4% fuera de muestra**.
- `reversion 24w` se mantiene sobre 50% en los 4 tramos; `trend 4h` no.

O sea: la dirección de una ventana de 5 min **no es ruido puro**, revierte
débilmente. La señal de 4h apuesta a continuación, que es el lado contrario.

### ⚠️ Pero el edge no es significativo todavía

Al ask real, `fade racha≥4` da +3,74% con **t = +1,32**. Consistente entre
mitades (+4,20% y +3,29%) pero por debajo de significancia. Harían falta
~2.539 operaciones ≈ **78 días** al ritmo actual (33 señales/día) para
confirmarlo o descartarlo.

### 🔴 Tres defectos concretos de la implementación actual

1. **El cap de Fade (0,60) paga muy por encima del valor justo.** La señal vale
   0,538; pagar 0,60 es −6,2 puntos garantizados. Los 5 trades reales de
   `ss_fade` en la DB entraron a **0,558 de media**. El cap debería estar en
   ~0,52.
2. **El desempate favorece a la señal equivocada.** `streak_trader.py` descarta
   Fade cuando choca con Trend. Ocurre en el 9% de los Fade (98 de 1.152).
3. **La martingala apuesta ~100× lo que justifica el edge.** Con 53,8% a 0,519
   Kelly pide el **4,03% del bankroll** ($40 sobre $1.000; cuarto de Kelly $10).
   El backtest de la Fase 4.7 llega a apostar **$983 en una sola ventana**.

### Pendiente

- [x] Bajar `SS_FADE_LIMIT_CAP` a 0,52 — hecho en la Fase A
- [x] Invertir el desempate: que prevalezca Fade, no Trend — hecho en la Fase A
- [x] Decidir sobre `ss_trend`: **apagado** (`SS_MODE=fade`). No invertido: el
      tramo T4 es negativo también al revés, así que la inversión no es estable
- [x] Sustituir la martingala por sizing fijo/Kelly — `SS_SIZING=flat` por
      defecto en la Fase A; la martingala sigue disponible
- [ ] Entrar lo más cerca posible de la apertura: esperar a +60 s cuesta 3 puntos de ROI

---

## 🏗️ Fase A — Base para decidir con datos (IMPLEMENTADA, 5-ago-2026)

> Plan: **[PLAN.md](PLAN.md)** § Fase A. El objetivo no era ganar más sino
> **dejar de perder por defectos medidos** y montar el instrumento: multi-activo
> triplica la muestra y baja el tiempo de validación de ~78 días a ~26.

### A1 — Arreglos medidos

| Cambio | Antes | Ahora | Por qué |
|---|---|---|---|
| `SS_FADE_LIMIT_CAP` | 0,60 | **0,52** | La señal vale 0,538; los 5 trades reales entraron a 0,558 |
| `SS_MODE` | `both` | **`fade`** | `ss_trend` pierde 4,22% por operación |
| `SS_SIZING` | — | **`flat`** | Kelly pide el 4% del bankroll; la martingala apostaba ~100× eso |
| Desempate | Trend | **Fade** | La regla anterior descartaba 98 de 1.152 entradas de Fade |

`StreakStrategy._size_for()` es el único punto de dimensionado
(`flat` | `kelly` | `martingale`), dimensiona al cap y no al fill, y todos los
modos pasan por el mismo techo del 10% del bankroll y suelo de 5 shares. `kelly`
no opera cuando no ve ventaja al cap, que es como `ss_trend` se dimensiona a
cero en vez de apostar contra su propia medición.

### A2 — Filtros de régimen (`bot/regime.py`)

Horario UTC, banda de percentiles de ATR de 1h y rango de 2h, sobre ventana
móvil de 2 días en vez de constantes. **Todos apagados por defecto**: se
probaron ~20 filtros contra 35 días, así que el mejor luce bien por
construcción. Cada rechazo se cuenta por motivo y sale en el dashboard, que es
lo que permite comparar «con filtro» contra «sin filtro» sobre datos nuevos en
lugar de sobre la muestra que los eligió.

### A3 — Multi-activo BTC / ETH / SOL

Un hilo trader y un `BotState` por símbolo de `SS_SYMBOLS` (por defecto solo
`btc`). `symbol` recorre `binance_api`, `market`, `state` y `db`, con
`UNIQUE(strategy, symbol)` en `martingale_state` para que una racha perdedora en
BTC no redimensione la entrada de ETH. XRP y DOGE se descartan: 3-6 centavos de
spread contra 1, y el edge medido no llega a 2.

### A4 — Registro de estrategias (`bot/strategies/`)

Un descriptor por estrategia con sus parámetros; `RUNTIME_FIELDS` se construye
concatenando los campos base con los del registro, y `/settings` se renderiza
desde `/state`. Declarar un parámetro pasa de tocar cinco archivos a tocar uno.

**Lo que esto destapó:** `ss_sizing`, `ss_kelly_fraction` y los cuatro filtros de
régimen llevaban una fase enteros aceptándose por `POST /config` **sin aparecer
en la interfaz**, precisamente porque el HTML estaba escrito a mano.

### A5 — Métricas por estrategia y activo

Selector de activo, tarjetas por estrategia (del registro, con ROI) y por
activo, y desglose de ventanas descartadas por motivo. Dos filtros que parecían
aplicarse y no se aplicaban: `/api/trades?symbol=` se perdía antes de llegar a
`build_query`, y `/api/metrics/series` ignoraba `symbol`, así que la curva de
capital de un mercado sumaba el P&L de todos.

### Verificación

- `python -m pytest tests/ -q` → **383 tests** (298 antes de la fase)
- Un test que mentía, corregido: `TestContradictorySignals` reimplementaba el
  desempate dentro del propio test, así que seguía verde afirmando que ganaba
  Trend mucho después de que el trader conservara Fade

### Lo que esta fase NO promete

Ningún filtro llega a significancia con 35 días de datos. Esto elimina pérdidas
medidas y construye el instrumento para decidir; no convierte el bot en
rentable. La única cifra con respaldo sólido sigue siendo que lo que había
perdía dinero de forma evitable.

### Pendiente de la fase

- [ ] Encender ETH y SOL (`SS_SYMBOLS=btc,eth,sol`) cuando BTC lleve unos días
      verde con la base nueva
- [ ] Volver a medir con `python scripts/regime_filter.py` sobre operaciones
      nuevas — es el script que decide si los filtros se quedan
- [ ] Verificación en vivo en paper del ciclo completo con la base nueva
