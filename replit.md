# Polymarket Multi-Market Up/Down 5m Bot

## Overview
A Python bot that trades Polymarket's BTC, SOL, and ETH up/down 5-minute prediction markets. It watches the UP and DOWN token prices over a WebSocket feed, supports two strategies (Trigger and Market Making), and includes a Flask web dashboard with KPIs, live prices, trade history, and activity log.

## Tech Stack
- **Language:** Python 3.11
- **Trading SDK:** py-clob-client (Polymarket CLOB)
- **Live data:** websocket-client (Polymarket WSS market channel)
- **Web:** Flask + Flask-CORS
- **Frontend:** static HTML/CSS/JS (vanilla)

## Project Layout
```
bot/
  __init__.py
  main.py            # spawns 3 trader threads (BTC, SOL, ETH) + dashboard
  config.py          # env-driven config (mode, trigger, buy size, urls)
  state.py           # thread-safe BotState with snapshot for dashboard
  logger.py          # console logger with per-market routing + ring buffer
  market.py          # Gamma API market loader (slug → token ids)
  price_feed.py      # WebSocket subscription with auto-reconnect
  trader.py          # main loop: trigger → buy → hedge → settle
  strategy_mm.py     # Market Making strategy (buy both sides near close)
  dashboard.py       # Flask app: GET /, /state, /config, /healthz
  templates/
    dashboard.html   # Multi-market overview page
    settings.html    # Configuration page
  static/
    dashboard.css
    dashboard.js
main.py              # gunicorn entry (main:app) + python main.py
run.py               # convenience entrypoint -> python run.py
```

## Configuration (env vars)
- `TRADING_MODE` — `paper` (default) or `real`
- `TRIGGER_PRICE` — default `0.90`
- `BUY_AMOUNT` — USDC notional, default `5.0`
- `HEDGE_THRESHOLD` — default `0.96`
- `LAST_MINUTE_SECONDS` — default `60`
- `MAX_TRADES_PER_WINDOW` — default `1`
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
1. Compute current 5-minute window slug from system time for each market (BTC, SOL, ETH).
2. Resolve UP/DOWN token ids via Gamma API.
3. Subscribe to WebSocket book/price_change/best_bid_ask.
4. **Trigger strategy:** When either side >= trigger price during the last minute, buy at the observed market price. Hedge when the initial side reaches the hedge threshold or in the last 10 seconds.
5. **Market Making strategy:** Buy both sides simultaneously near the window close.
6. Resolve P&L from the settlement prices, log the trade, advance to next window.

## Dashboard
- `GET /` — multi-market overview with global KPIs, per-market panels, trade table, activity log.
- `GET /settings` — configuration page for strategy parameters.
- `GET /state` — JSON snapshot polled by the UI every 1s.
- `POST /config` — update runtime configuration via JSON.
- `GET /healthz` — health check (`{"ok": true}`).

## Notes
- The two artifact workflows (`artifacts/api-server`, `artifacts/mockup-sandbox`) come from the workspace template and are unused; they remain in `NOT_STARTED` state and can be ignored.
- The Replit reverse proxy isn't required for this project — the Flask app binds directly to `PORT` and is served via the workflow's webview.
