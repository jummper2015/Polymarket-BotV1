# Polymarket BTC Up/Down 5m Bot

Bot automatizado que opera los mercados de predicción **BTC up/down de 5 minutos** en [Polymarket](https://polymarket.com). Incluye dashboard web en tiempo real con KPIs, historial de operaciones y gráfico de precios.

---

## Índice

1. [Cómo funciona](#cómo-funciona)
2. [Estrategia de trading](#estrategia-de-trading)
3. [Estructura del proyecto](#estructura-del-proyecto)
4. [Requisitos](#requisitos)
5. [Instalación local](#instalación-local)
6. [Instalación en Replit](#instalación-en-replit)
7. [Variables de entorno](#variables-de-entorno)
8. [Modos de operación](#modos-de-operación)
9. [Dashboard web](#dashboard-web)
10. [Pasar a modo real](#pasar-a-modo-real)
11. [Advertencias de riesgo](#advertencias-de-riesgo)

---

## Cómo funciona

Polymarket ofrece mercados de predicción binarios de 5 minutos sobre el precio de Bitcoin. Cada ventana tiene dos tokens:

- **UP** — paga $1 si BTC sube durante la ventana.
- **DOWN** — paga $1 si BTC baja durante la ventana.

El precio de cada token oscila entre $0 y $1 y refleja la probabilidad implícita del mercado. El bot monitorea estos precios en tiempo real a través del WebSocket de Polymarket y ejecuta órdenes cuando se cumplen condiciones específicas.

---

## Estrategia de trading

El bot sigue estas reglas en orden estricto para cada ventana de 5 minutos:

### 1. Cargar mercado
Al inicio de cada ventana, resuelve los token IDs de UP y DOWN para el slug actual (`btc-updown-5m-<timestamp>`) usando la API Gamma de Polymarket.

### 2. Esperar el último minuto
El bot permanece inactivo hasta que queda `LAST_MINUTE_SECONDS` (por defecto 60 s) para que termine la ventana. Operar cerca del cierre reduce la exposición temporal.

### 3. Verificar precio de entrada
Al entrar al último minuto, si algún token ya cotiza por encima del `TRIGGER_PRICE`, la oportunidad se descarta (`SKIP`). Solo se opera cuando el precio **cruza el trigger desde abajo**.

### 4. Ventana de entrada (primeros 45 s del último minuto)
Las nuevas posiciones solo se abren durante los **primeros 45 segundos** del último minuto. Pasado ese punto se cierra la ventana de entrada y el bot ya no abre operaciones nuevas en esa ventana.

### 5. Disparo de orden
Cuando `precio >= TRIGGER_PRICE` para UP o DOWN dentro del período de entrada:
- Se compra al **precio de mercado observado** (no al trigger, que es solo señal).
- El monto invertido es `BUY_AMOUNT` USDC.
- Máximo `MAX_TRADES_PER_WINDOW` operaciones iniciales por ventana.

### 6. Cobertura (hedge)
Mientras se mantiene la posición, si el precio del lado comprado sube hasta `HEDGE_THRESHOLD` (por defecto 0.96), se compra el lado contrario con la misma cantidad de shares, bloqueando ganancia en cualquier resultado.

### 7. Salida de emergencia (últimos 10 s)
Si al llegar a los últimos 10 segundos la posición sigue abierta y **no se colocó hedge**, el bot vende al mejor precio disponible para materializar la ganancia antes del settlement.

### 8. Settlement
Al cierre de la ventana, las posiciones que no se vendieron (porque se hizo hedge o porque el precio llegó a settlement) se resuelven según el precio final:
- Si el lado comprado gana (`precio ≥ 0.99`), paga $1 por share → ganancia.
- Si pierde (`precio ≤ 0.05`), el token vale $0 → pérdida del costo.

---

## Estructura del proyecto

```
polymarket-bot/
│
├── bot/
│   ├── __init__.py
│   ├── main.py          # Punto de entrada: lanza trader + dashboard
│   ├── config.py        # Configuración desde variables de entorno
│   ├── state.py         # Estado compartido thread-safe (BotState)
│   ├── logger.py        # Logger con buffer circular y emojis
│   ├── market.py        # Carga mercado activo desde Gamma API
│   ├── price_feed.py    # WebSocket con reconexión automática
│   ├── trader.py        # Loop principal: trigger → compra → hedge → venta
│   ├── dashboard.py     # Servidor Flask (UI + API /state)
│   ├── templates/
│   │   └── dashboard.html
│   └── static/
│       ├── dashboard.css
│       └── dashboard.js
│
├── run.py               # Entrada: python run.py
├── requirements.txt
├── .env.example         # Plantilla de variables de entorno
└── README.md
```

---

## Requisitos

- Python 3.11+
- pip

Dependencias principales (ver `requirements.txt`):

```
flask
flask-cors
requests
websocket-client
py-clob-client
eth-account
```

---

## Instalación local

```bash
# 1. Clonar el repositorio
git clone <url-del-repo>
cd polymarket-bot

# 2. Crear entorno virtual (recomendado)
python -m venv .venv
source .venv/bin/activate      # Linux / macOS
.venv\Scripts\activate         # Windows

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar variables de entorno
cp .env.example .env
# Editar .env con tus valores

# 5. Iniciar el bot
python run.py
```

El dashboard queda disponible en `http://localhost:5000`.

---

## Instalación en Replit

1. Importar o clonar el proyecto en Replit.
2. Replit instala las dependencias automáticamente al detectar `requirements.txt`.
3. Agregar los secretos necesarios en **Tools → Secrets**:
   - `PRIVATE_KEY` y `PROXY_WALLET` solo si usas modo real.
   - El resto de variables opcionales según necesidad.
4. Ejecutar el workflow **Start application** (`python run.py`).
5. La UI se muestra en el panel de preview en el puerto 5000.

---

## Variables de entorno

| Variable | Por defecto | Descripción |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` (simulación) o `real` (fondos reales) |
| `TRIGGER_PRICE` | `0.90` | Precio mínimo de un token para abrir posición (0.01–0.99) |
| `BUY_AMOUNT` | `5.0` | USDC a invertir por operación |
| `STARTING_BANKROLL` | `1000.0` | Bankroll inicial para cálculo de P&L |
| `MAX_TRADES_PER_WINDOW` | `1` | Máximo de posiciones iniciales por ventana (1–10) |
| `HEDGE_THRESHOLD` | `0.96` | Precio al que se activa la cobertura del lado contrario (0.50–0.99) |
| `LAST_MINUTE_SECONDS` | `60` | Segundos antes del cierre en los que se activa la vigilancia (10–240) |
| `PRIVATE_KEY` | — | Clave privada de la wallet Polygon (**solo modo real**) |
| `PROXY_WALLET` | — | Dirección del proxy wallet de Polymarket (**solo modo real**) |
| `CHAIN_ID` | `137` | Chain ID de Polygon |
| `SIGNATURE_TYPE` | `2` | Tipo de firma Polymarket (2 = proxy wallet) |
| `PORT` | `5000` | Puerto del dashboard web |
| `DASHBOARD_HOST` | `0.0.0.0` | Host de escucha del dashboard |
| `CLOB_HOST` | `https://clob.polymarket.com` | Endpoint REST del CLOB |
| `GAMMA_HOST` | `https://gamma-api.polymarket.com` | Endpoint de la Gamma API |
| `CLOB_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | WebSocket de precios |
| `POLL_INTERVAL_MS` | `50` | Intervalo de polling del loop de trading (ms) |
| `MARKET_RETRY_SECONDS` | `3` | Segundos entre reintentos al cargar el mercado |
| `FIRST_PRICE_TIMEOUT` | `5` | Segundos de espera para recibir el primer precio |

---

## Modos de operación

### Paper (simulación)
Por defecto. No se envía ninguna orden real a Polymarket. Las compras y ventas se simulan con los precios de mercado en tiempo real. El P&L se calcula igual que en modo real. Ideal para validar la estrategia sin riesgo.

### Real
Requiere `PRIVATE_KEY` y `PROXY_WALLET`. Las órdenes se envían al CLOB de Polymarket como órdenes GTC (Good-Till-Cancelled). Usa fondos reales de tu wallet de Polygon.

Puedes cambiar entre modos desde el dashboard en tiempo real sin reiniciar el bot.

---

## Dashboard web

Disponible en `http://localhost:5000` (o el puerto configurado).

| Sección | Descripción |
|---|---|
| **Header** | Precio BTC en tiempo real, modo actual, estado del bot, estado WebSocket |
| **KPIs** | Bankroll, P&L resuelto, Win Rate, conteo de trades, ventanas operadas |
| **Configuración** | Ajuste en tiempo real de trigger, monto, hedge, último minuto y modo |
| **Gráfico** | Precio UP/DOWN en tiempo real con canvas |
| **Historial** | Tabla de todas las operaciones con estado, precio de entrada/salida y P&L |
| **Log** | Registro de eventos del bot con niveles y timestamps |

### Endpoints API

| Ruta | Método | Descripción |
|---|---|---|
| `/` | GET | Dashboard HTML |
| `/state` | GET | Snapshot JSON completo del estado del bot |
| `/config` | POST | Actualizar configuración en tiempo real |
| `/healthz` | GET | Health check (`{"ok": true}`) |

---

## Pasar a modo real

1. Crear una cuenta en [Polymarket](https://polymarket.com) y depositar USDC en Polygon.
2. Obtener tu `PRIVATE_KEY` de la wallet de Polygon que usas en Polymarket.
3. Obtener la dirección del `PROXY_WALLET` de Polymarket (visible en la URL de tu perfil o en las herramientas de desarrollador).
4. Configurar las variables de entorno:
   ```
   TRADING_MODE=real
   PRIVATE_KEY=0x...
   PROXY_WALLET=0x...
   ```
5. Reiniciar el bot.
6. El dashboard mostrará un checklist de readiness antes de permitir el modo real.

---

## Advertencias de riesgo

- **Los mercados de predicción son instrumentos especulativos de alto riesgo.** Puedes perder la totalidad del capital invertido.
- El modo paper no garantiza los mismos resultados en modo real (slippage, liquidez, latencia).
- Nunca compartas ni subas a un repositorio público tu `PRIVATE_KEY`.
- Empieza siempre con montos pequeños (`BUY_AMOUNT`) para validar el comportamiento en producción.
- Este software se provee tal cual, sin garantía de ningún tipo.
