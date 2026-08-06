# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python bot ("Streak Snapper v2") that trades Polymarket's **up/down 5-minute** prediction markets, plus a Flask dashboard. One trader thread per asset (`SS_SYMBOLS`, `btc`/`eth`/`sol`, default `btc` only), two strategy forms (`ss_fade`, `ss_trend`).

Defaults after the Fase 8 measurement: `SS_MODE=fade` (**`ss_trend` is off** — it loses −4.22%/trade), `SS_SIZING=flat` (**the martingale is opt-in**, not the default path), `SS_FADE_LIMIT_CAP=0.52`.

⚠️ **`README.md` and `replit.md` are stale.** They describe the previous multi-market Trigger / Market Making / Early Entry bot, which was archived. The accurate references are `docs/ARCHIVOS.md` (file-by-file map), `docs/RUTA.md` (phase log, measured results, open items), `docs/PLAN.md` (current phase plan: Fase A base, Fase B new strategies) and `.env.example`.

## Commands

```bash
python run.py                                    # start trader + dashboard (paper mode by default)
PORT=5055 python run.py                          # bind another port if 5000 is taken

python -m pytest tests/ -q                       # full suite (383 tests, ~5s)
python -m pytest tests/test_db.py -q             # one file
python -m pytest tests/test_db.py::test_name -q  # one test

python -m bot.backtest --windows 2900 --mode both --labels gamma --csv data/out.csv
python -m bot.threshold_study --windows 3000     # calibrate NEAR_FLAT_THRESHOLD
python scripts/price_calibration.py --windows 2000
python scripts/signal_search.py                  # grade candidate signals vs Gamma labels
python scripts/oos_validation.py                 # split the sample, rank in half 1, test in half 2
python scripts/preopen_edge.py                   # ROI at the pre-open price vs at +60 s
python scripts/regime_filter.py                  # re-measure the regime filters
```

There is no linter or type-checker wired up for the Python side. The `pnpm` scripts in `package.json` and the `lib/` + `artifacts/` TypeScript trees are workspace-template leftovers and are unrelated to the bot.

The bot **refuses to start** if `DASHBOARD_HOST` is non-loopback and `DASHBOARD_PASSWORD` is empty (`bot/auth.py:verify_startup_config`) — the dashboard can flip the bot to real money. For local runs use `DASHBOARD_HOST=127.0.0.1`.

Backtests and calibration scripts hit the public Binance and Gamma APIs; they take minutes and need generous `timeout`s.

## Architecture

`run.py` → `bot/main.py:main()` starts, in one process: a spot-price poller and a `StreakSnapperTrader` daemon thread **per symbol in `cfg.ss_symbols`**, an optional Chainlink TWAP feed (BTC only), and the Flask app on the main thread. Every symbol is fully independent — its own `BotState`, its own martingale rows, its own window loop.

### The window cycle — `bot/streak_trader.py`

One iteration of `_run_one_window()` per 5-minute boundary:

1. `market.load_market_for_current_window(symbol=…)` — resolve UP/DOWN token IDs from the Gamma API for the current slug.
2. `_resolve_pending_trades()` + `_confirm_binance_resolutions()` — settle past windows.
3. `PriceFeed` (CLOB v2 WebSocket) starts and stays connected for the whole window.
4. `_check_regime()` → `bot/regime.py`. Runs **before** the signals so a filtered window costs one Binance call instead of the whole signal path, and so the skip reason is recorded even when a signal would have fired.
5. `strategy.get_fade_signal()` / `get_trend_signal()` per `ss_mode`.
6. `_execute_signal()` — skips the window with `SKIP_ASK_ABOVE_CAP` when the ask is above the cap, otherwise limit BUY at `min(limit_cap, current_ask)`, floored to the $0.01 tick, persisted to `trades` with its `symbol`. The skip is load-bearing: `min()` alone left a resting bid *under* the ask, booked as a fill in paper and as an unverified GTC order in real (`docs/RUTA.md` Fase A.1).

   In real mode the fill is then verified before anything is persisted (`_settle_order`): a full fill is usually already reported in the POST response, an unfilled order is cancelled and counted as `SKIP_NO_FILL` with **no** position recorded, and a partial fill is kept at the size that actually filled with the remainder cancelled. `matched_shares()` returns `None` rather than `0.0` for an unintelligible response, because "did not fill" and "no idea" need different handling — on `None` the position *is* recorded, since an unrecorded real position is money that never resolves. None of this path runs in paper, so it is covered by tests with a fake CLOB only.
7. Wait for window close, then settle **this** window before the next one opens.

Step 7 is load-bearing: the martingale multiplier for the next entry is only correct once the window that just closed has resolved.

### Resolution: two sources, in order

Binance settles at candle close; Gamma (the official Polymarket outcome) publishes ~3 minutes later, longer than the window itself. So `_resolve_once()` asks Binance first (`resolution_source="binance"`), and `_confirm_binance_resolutions()` later re-checks against Gamma and corrects P&L on disagreement. Near-flat candles (`|Δ| ≤ NEAR_FLAT_THRESHOLD`, `bot/binance_api.py`) return `None` from Binance and defer to Gamma — the two feeds straddle the open price differently, so calling those windows from Binance was measurably wrong.

### Sizing — `flat` | `kelly` | `martingale`

`StreakStrategy._size_for()` is the single entry point; `SS_SIZING` picks the mode and every mode passes through the same risk ceiling (`MAX_BANKROLL_FRACTION = 0.10`, floor `MIN_SHARES = 5`). Sizing happens at `limit_cap`, not at the fill price: the cap is the worst price we accept, so a better fill can only make the realised stake more conservative.

Default is `flat` because the measured edge (+3.74%/trade at 53.8%, `docs/RUTA.md` Fase 8) justifies ~4% of bankroll and a martingale bets ~100× that. `kelly` uses `MEASURED_WIN_PROB` × `ss_kelly_fraction` and **returns 0 shares (no trade) when Kelly sees no edge at that cap** — that is a feature, not a bug to route around.

`martingale` is still there and still correct: `bot/strategy_streak.py` owns the state, `bot/db.py` persists it (`martingale_state`, unique on `(strategy, symbol)`), and `min_recovering_factor(cap) = 1/(1-cap)` in `bot/config.py` is the single source of truth for whether a factor recovers a losing cycle (`main.py` warns at startup when it doesn't; default 2.1, not 1.5, because of the 0.52 cap — `docs/RUTA.md` Fase 4.7).

### Regime filters — `bot/regime.py`, all off by default

Pure functions over candle dicts (`{ts, open, high, low, close}`), so they test without network. `hours_filter` (UTC bands), `volatility_filter` (1h ATR between percentiles) and `range_filter` (2h range under a percentile) each return a `RegimeVerdict(allowed, reason, detail)`. Percentiles are computed over a rolling `PERCENTILE_LOOKBACK` (2 days) rather than fixed constants, so they follow the volatility regime instead of silently expiring.

They are **all off by default on purpose**: ~20 filters were tested against 35 days, so the best of them looks good by construction. Skips are counted by reason in `BotState.skips` and surface in `/state`, which is what makes "with filter" vs "without" comparable on live data instead of on the sample the filter was chosen from.

### The trend cycle and the tie-break

`ss_trend` locks one side for a 4h block anchored to the **last closed** 4h candle, and the lock outlives the block while the martingale is still recovering. When fade and trend point at opposite sides of the same window, **fade wins the tie-break** and trend is dropped — this was the other way round until Fase 8 measured trend as the losing side (98 of 1,152 fade entries had been discarded in favour of it). Buying both sides is never an option: it is a guaranteed wash that also feeds each martingale a fake win and a fake loss.

### State, config, and precedence

- `bot/state.py` — one `BotState` per symbol behind an `RLock`, in `STATES = {symbol: BotState}` (use `state_for(symbol)` / `active_states()`); `STATE` remains an alias of the BTC state for dashboard and test back-compat. Holds live prices, order book, martingale mirror, skip counters, a bounded trade cache and the event log. `snapshot()` builds the `/state` payload.
- Config precedence at startup: **`bot_config` DB rows override `.env`** (`_apply_persisted_overrides`), because `/settings` has a Save button. `mode` (paper/real) is deliberately *not* persistable — see the comment above `RUNTIME_FIELDS`.
- `RUNTIME_FIELDS` in `bot/config.py` is the catalogue of runtime-editable parameters, built as `BASE_FIELDS + strategies.params()`. Declare a **strategy** parameter in its descriptor and a **bot-wide** one in `BASE_FIELDS`; either way you get parsing, range-checking, `POST /config`, persistence *and* the widget on `/settings` at once. Do not add ad-hoc validation in `dashboard.py` or a hand-written `<input>` in `settings.html`.

### The strategy registry — `bot/strategies/`

A strategy is a `StrategyDescriptor` (data), not a subclass: they differ too much in *how* they produce signals for a common superclass to say anything useful. The descriptor carries `id`, `name`, `description`, `params`, `symbols` (empty = all), `priority`, `is_enabled(state)`, `enabled_when` and `evaluate(ctx) -> list[Signal]`.

- `ss_fade` / `ss_trend` are wrappers — their logic still lives in `bot/strategy_streak.py`. `ss_mode` remains their switch; Fase B strategies bring their own `ss_<id>_enabled` boolean.
- The direction tie-break is `strategies.resolve_conflicts()`, ordered by `priority` (fade 100, trend 50). It is not in the trader any more, so a new descriptor joins the tie-break for free.
- `enabled_when` is a declarative mirror of `is_enabled` that `/settings` evaluates client-side to grey out a card before saving. Two sources of truth for one fact, so `tests/test_strategies.py` asserts they agree for every value of the field — keep that test passing.
- **Nothing in this package may import `bot.config`** — config imports the registry to build `RUNTIME_FIELDS`. Field declarations come from `bot/runtime_field.py`.

`RuntimeField` also carries its own presentation (`label`, `hint`, `step`, `scale`, `choice_labels`), and `/state` serves `fields` + `strategies` so `settings.js` renders every widget generically. No frontend file names a parameter.

### Dashboard — `bot/dashboard.py`

Routes: `/`, `/settings`, `/login`, `/logout`, `/state`, `/api/trades`, `/api/trades.csv`, `/api/metrics/series`, `POST /config`, `POST /config/reset`, `/healthz`.

KPIs (`stats`, `strategy_stats`, `symbol_stats`) are aggregated **in SQL over the whole `trades` table** (`_aggregate_db_stats`, grouped by `(strategy, symbol)`); the `trades` list in the payload is only the last 100 rows for the history table. Never compute KPIs from that list. `bot/trade_queries.py` holds the filter/pagination/series SQL and takes `symbol` both as a `build_query` filter and as a `metric_series` cut. `?symbol=eth` on `/state` switches which market the live panel describes; the per-symbol KPI block always covers all of them.

Auth is a `before_request` gate in `bot/auth.py`: one shared password, signed session cookie, no user table. XHR paths (`/api/*`, `/state`, `/config`) get a 401 JSON body; browsers get a redirect to `/login`.

### Persistence — `bot/db.py`

Flask-SQLAlchemy over `DATABASE_URL`, falling back to `data/streak_snapper.db` (SQLite) for local dev. Tables: `trades`, `martingale_state`, `bot_config`, `chainlink_ticks`.

`trades` and `martingale_state` carry a `symbol` column, and `martingale_state` is unique on `(strategy, symbol)` so a losing run on BTC can't resize ETH's next entry. Pre-multi-asset rows are backfilled to `btc`; because SQLite can't add a UNIQUE constraint in place, `_migrate_martingale_symbol()` rebuilds that table when it predates the column.

Two gotchas the code works around, both already burned once:

- The trader runs off the request cycle, so every DB access goes through the `db_context()` context manager, which pushes a Flask app context.
- The martingale helpers return a `MartingaleSnapshot` NamedTuple, **not** the ORM instance — a `commit()` expires all attributes, leaving the object unusable once the context closes. `session.merge()` likewise returns a *different* instance; mutate the merged one.

`create_all()` doesn't add columns to existing tables, so `_add_missing_columns()` / `_widen_columns()` run at init as a lightweight migration step.

### Chainlink TWAP — off by default

`bot/chainlink_feed.py` + `bot/chainlink_recorder.py`, all `CL_*` flags default off. It is a **filter**, not a resolution source: Polymarket settles against a different Chainlink stream. Every failure path is fail-open — the bot traded fine without this feed and must keep doing so. Analysis and go-live checklist: `docs/CHAINLINK_TWAP.md`.

## Conventions

- Comments and docstrings are in English and explain *why*, usually citing the bug or measurement behind a choice. Log messages and dashboard UI strings are in Spanish. Keep both.
- Measured claims (win rates, thresholds, drawdowns) belong in `docs/RUTA.md` with the sample size and command used to produce them. Several defaults were chosen for capital survival, not edge — and `ss_trend` is worse than edgeless: Fase 8 measures it at **−4.22% per trade** (48.2% over 1,725 signals, at the real pre-open price), which is why `SS_MODE` defaults to `fade`. `ss_fade`'s +3.74% is itself below significance (t=+1.32). Don't present either as profitable.
- `bot/trader.py` and `bot/archive/*` are dead code kept as reference; nothing imports them.
- Frontend is vanilla JS + vendored Bootstrap/Chart.js/Notika under `bot/static/vendor/`. No build step. All color overrides live in `dashboard.css`.
- `data/*.csv`, `data/gamma_outcomes.json` and `*.db` are gitignored regenerable artifacts.

### Candidate strategies — `Revisar Estrategias/`

Self-contained reference implementations (`.py` + `readme.md`, sometimes `research.md`), **not wired into the bot** and not importable from `bot/`. Each README carries its own measured autopsy; read it before touching the code. `docs/PLAN.md` Fase B is the order they get integrated and why.

| Directory | Idea in one line | What the bot still lacks |
|---|---|---|
| `spread_harvest_maker/` | Rest one bid inside an abnormally wide book (0.40–0.48, underdog side), never cross the spread. | Maker orders (`post_only`) + cancellation |
| `box_builder/` | Lowball bids on **both** sides capped at $0.94 combined; a filled pair redeems for exactly $1.00, direction-neutral. | Maker orders + cancellation |
| `mid_price_continuation/` | Buy the leading side at market, **only** in the 40–55c band, hold to resolution. | Nothing — spot + book already available |
| `corridor/` | Buy the 15m leader and the opposite side of the final 5m window; the pair is floored at $1 and pays $2 inside the "corridor". | Trading 5m and 15m at once (both markets exist) |
| `liq_cascade_chaser/` | Buy the continuation side of a large liquidation cascade at 50–85c. | Liquidation feed + recorded tape |
| `small_liq_continuation/` | Same signal, cheap tier ($25K–$500K liquidations, 30–45c entries). | Liquidation feed + recorded tape |
| `streak_snapper/` | The original of what shipped as `ss_fade`/`ss_trend`. | — (already in `bot/`) |

The two maker strategies are the ones with a *structural* reason to be positive (they collect the spread instead of predicting direction), which is why they lead Fase B. The two liquidation strategies come last for a data reason, not a difficulty one: **liquidations are not served historically**, so a `bot/liquidation_feed.py` + recorder (Bybit v5 `allLiquidation.<SYM>`, verified working; Binance `!forceOrder@arr` optional and blocked in this dev environment) has to run for weeks before either can be validated. Follow the `chainlink_recorder.py` pattern.
- Frontend is vanilla JS + vendored Bootstrap/Chart.js/Notika under `bot/static/vendor/`. No build step. All color overrides live in `dashboard.css`.
- `data/*.csv`, `data/gamma_outcomes.json` and `*.db` are gitignored regenerable artifacts.
- Frontend is vanilla JS + vendored Bootstrap/Chart.js/Notika under `bot/static/vendor/`. No build step. All color overrides live in `dashboard.css`.
- `data/*.csv`, `data/gamma_outcomes.json` and `*.db` are gitignored regenerable artifacts.

## Deployment caveat

`.replit` deploys with `gunicorn --bind 0.0.0.0:5000 main:app`, but the root `main.py` is an unrelated template stub with no `app` object, and gunicorn would serve the dashboard without ever starting the trader thread. The working entry point is `python run.py`.
