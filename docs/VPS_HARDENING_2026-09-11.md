# VPS hardening — sesión 2026-09-11

Estado del VPS antes y después de esta sesión.

## Antes

| Aspecto | Estado |
|---|---|
| Servicio | `polymarket-bot.service` activo, restart counter 8846 (Sep 7) |
| Modo | paper (por accidente: `PAPER_MODE=true` ignorado, default = paper) |
| `.env` perms | `666 ubuntu:ubuntu` — world-readable |
| Password dashboard | `PolyBot2024` — trivial |
| `DASHBOARD_HOST` | `0.0.0.0` — expuesto a internet |
| `DASHBOARD_SECRET_KEY` | no definido — sesiones mueren al reiniciar |
| fail2ban | solo jail `sshd` activo |
| nginx | ya instalado con HTTPS para `polytradebot.cloud` |
| Cert Let's Encrypt | válido hasta 2026-11-05 |
| Latencia VPS → Polymarket | CLOB 142 ms, Gamma 44 ms |
| Latencia VPS → Binance | **HTTP 451 (geo-bloqueado)** — irrelevante, bot usa Coinbase |

## Después

| Aspecto | Estado |
|---|---|
| Modo | paper, **explícito** (`TRADING_MODE=paper`) |
| `.env` perms | `600 root:root` |
| Password dashboard | `token_urlsafe(32)` regenerado |
| `DASHBOARD_SECRET_KEY` | `token_hex(32)` regenerado |
| `DASHBOARD_HOST` | `127.0.0.1` — solo loopback |
| Backup secretos | `/root/.polymarket-bot-secrets` (chmod 600) |
| fail2ban jail | `polymarket-dashboard` activa (4 fallos / 10 min → 1 h ban) |
| `.env` sincronizado con `bot_config` | TA params coherentes |
| Scaffolding `scripts/generate_api_keys.py` | desplegado, syntax OK |
| HTTPS para `polytradebot.cloud` | sin cambios (ya estaba bien) |

## Cambios pendientes

- [ ] Apuntar el dominio (si decides usar uno distinto) y `certbot --nginx -d …`.
- [ ] Definir el bankroll real (recomendado $300 inicial).
- [ ] Fondear wallet dedicada en USDC.e antes de pasar a real.
- [ ] Re-ejecutar el comando `scripts/generate_api_keys.py` cuando tengas PRIVATE_KEY + PROXY_WALLET listos.

## Geo-bloqueo Binance — estado confirmado 2026-09-13

Confirmado en producción:

- `api.binance.com` y `fapi.binance.com` devuelven **HTTP 451** (`Service unavailable from a restricted location according to 'b. Eligibility'`) desde la IP de Hostinger.
- El bot trae `bot/binance_api.py` como cliente Binance (fuente primaria en `_prices_loop`, `bot/dashboard.py:51`), pero captura todas las excepciones y devuelve `None` en silencio; `STATE.spot_price` queda stale y `temporal_arb._observe` cae al guard `if spot is None: return` (skip sin ruido).
- El VPS resuelve el problema desplegando `bot/coinbase_api.py` (8.8 KB, **no presente en este repo**) — drop-in sobre `binance_api.py`. Misma interfaz (`get_btc_spot_price`, `get_window_direction`, `get_atr4`, `get_current_window_open`, etc.) contra `api.exchange.coinbase.com`. Distribución de `trades.resolution_source` desde el deploy: **95 % coinbase, 5 % gamma** (ver `SELECT resolution_source, COUNT(*) FROM trades GROUP BY 1`).
- Implicación operativa: cualquier script que pegue a Binance directamente desde este VPS (`scripts/price_calibration.py`, `scripts/signal_search.py`, `scripts/oos_validation.py`) **falla con HTTP 451** al primer `requests.get`. Si necesitas calibrar umbrales con datos recientes, hazlo desde una máquina con IP no restringida (mi laptop, por ejemplo) y sube los resultados via `git push`.
- Acción sugerida a futuro: commitear `bot/coinbase_api.py` a este repo (o factor común `bot/spot_source.py` con `source=binance|coinbase`) para que el código local refleje el código en producción. Hoy el deploy manual sigue funcionando porque los dos archivos viven sólo en `/opt/polymarket-bot/bot/`.

## Archivos tocados

- `/opt/polymarket-bot/.env` — reescrito
- `/opt/polymarket-bot/.env.backup_YYYYMMDD_HHMMSS` — copia del anterior
- `/root/.polymarket-bot-secrets` — backup de la nueva password
- `/etc/fail2ban/filter.d/polymarket-dashboard.conf` — filtro nuevo
- `/etc/fail2ban/jail.d/polymarket-dashboard.local` — jail nueva
- `/opt/polymarket-bot/scripts/generate_api_keys.py` — script endurecido
