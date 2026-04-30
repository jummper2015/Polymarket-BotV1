# Polymarket BTC Up/Down 5m Bot

## Overview
A Python bot that trades Polymarket's BTC up/down 5-minute prediction markets. It watches the YES (UP) and NO (DOWN) token prices over a WebSocket feed, fires a single buy order per 5-minute window when a trigger price is reached, never sells, and holds until window settlement. Includes a Flask web dashboard with KPIs, live prices, trade history, and a chart.

## Tech Stack
- **Language:** Python 3.11
- **Trading SDK:** py-clob-client (Polymarket CLOB)
- **Live data:** websocket-client (Polymarket WSS market channel)
- **Web:** Flask + Flask-CORS
- **Frontend:** static HTML/CSS/JS (vanilla, canvas chart)

## Project Layout
```
bot/
  __init__.py
  main.py            # spawns trader + dashboard threads
  config.py          # env-driven config (mode, trigger, buy size, urls)
  state.py           # thread-safe BotState with snapshot for dashboard
  logger.py          # console logger with emoji icons + ring buffer
  market.py          # Gamma API market loader (slug → token ids)
  price_feed.py      # WebSocket subscription with auto-reconnect
  trader.py          # main loop: trigger, buy once, hold to settlement, resolve
  dashboard.py       # Flask app: GET /, /api/state, /api/healthz
  templates/dashboard.html
  static/dashboard.css, dashboard.js
run.py               # entrypoint -> python run.py
```

## Configuration (env vars)
- `TRADING_MODE` — `paper` (default) or `real`
- `TRIGGER_PRICE` — default `0.90`
- `BUY_AMOUNT` — USDC notional, default `5.0`
- `CHAIN_ID` — default `137` (Polygon)
- `SIGNATURE_TYPE` — default `2` (Polymarket proxy wallet)
- `PRIVATE_KEY` — required only for `real` mode
- `PROXY_WALLET` — required only for `real` mode
- `PORT` — dashboard port, default `5000`

## Running
- Workflow: **Start application** runs `python run.py` on port 5000.
- Default mode is `paper` (no real funds at risk).
- To switch to real trading, set `TRADING_MODE=real`, `PRIVATE_KEY`, and `PROXY_WALLET` as secrets, then restart.

## Behavior Summary
1. Compute current 5-minute window slug from system time.
2. Resolve UP/DOWN token ids via Gamma API.
3. Subscribe to WebSocket book/price_change/best_bid_ask.
4. When either side ≥ trigger price (0.90), place a single GTC buy at the trigger price for `BUY_AMOUNT` USDC.
5. Hold the position until the 5-minute window expires (no selling — never).
6. Resolve P&L from the settlement prices, log the trade, advance to next window.

## Dashboard
- `GET /` — single-page UI with live KPIs, prices, trade table, log, and an UP/DOWN price chart.
- `GET /api/state` — JSON snapshot polled by the UI every 1s.
- `GET /api/healthz` — health check.

## Notes
- The two artifact workflows (`artifacts/api-server`, `artifacts/mockup-sandbox`) come from the workspace template and are unused; they remain in `NOT_STARTED` state and can be ignored.
- The Replit reverse proxy isn't required for this project — the Flask app binds directly to `PORT` and is served via the workflow's webview.
