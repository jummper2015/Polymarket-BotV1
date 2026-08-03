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

- [ ] Punto 6 de `revisar.md`: operar un solo lado durante las 4h y seguir el
      ciclo hasta ganar. Es un cambio de **estrategia**, no de dashboard —
      toca `strategy_streak.py` y el Martingale.
- [ ] Servir con gunicorn en vez del servidor de desarrollo de Werkzeug
      (`main.py` usa `app.run()`; gunicorn ya está en `requirements.txt`).
      Importa más ahora que el panel puede quedar expuesto.

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
