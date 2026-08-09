"""Entry point: launches Streak Snapper trader + Flask dashboard.

Simplified — single market (BTC), single trader (StreakSnapperTrader).
The old Trader / CorridorTrader / multi-symbol infrastructure is archived.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from . import logger
from .auth import auth_enabled, verify_startup_config
from .chainlink_feed import ChainlinkTwapFeed
from .chainlink_recorder import ChainlinkRecorder
from .config import Config, coerce_overrides, load_config, min_recovering_factor
from .dashboard import create_app, start_price_fetcher
from .db import get_all_config, init_db
from .state import STATE, active_states, state_for
from .streak_trader import StreakSnapperTrader


def _start_status_publisher(feed: ChainlinkTwapFeed, window_s: int) -> None:
    """Push feed status into STATE once a second for the dashboard.

    A poll rather than a push from `on_tick`: the tile has to keep reporting
    while the feed is *silent*, and silence is exactly when it matters.
    """
    def _publish() -> None:
        while True:
            time.sleep(1.0)
            try:
                spot = STATE.spot_price
                divergence = (
                    feed.divergence(spot, window_s) if spot else None
                )
                STATE.set_chainlink_status(feed.status(), divergence)
            except Exception:
                pass  # a dashboard tile is never worth crashing a thread over

    threading.Thread(target=_publish, name="chainlink-status", daemon=True).start()


def _start_chainlink_feed(cfg: Config) -> ChainlinkTwapFeed | None:
    """Start the TWAP feed if enabled, and wire the tape recorder to it.

    Off by default. The topics only went live 4-ago-2026, and even once they
    work this feed can't settle or originate a trade — it filters
    (docs/CHAINLINK_TWAP.md §3). Any failure here is logged and swallowed: the
    bot traded fine without this feed and must keep doing so.
    """
    if not cfg.cl_twap_enabled:
        logger.info("[chainlink] feed desactivado (CL_TWAP_ENABLED=false)", icon="🔗")
        return None

    recorder = ChainlinkRecorder(retention_days=cfg.cl_tick_retention_days) \
        if cfg.cl_record_ticks else None

    def _on_tick(symbol, window_s, value, observed_at, received_at):
        if recorder is not None:
            recorder.record(symbol, window_s, value, observed_at, received_at)

    try:
        feed = ChainlinkTwapFeed(
            stale_seconds=cfg.cl_twap_stale_seconds,
            on_tick=_on_tick,
        )
        feed.start()
    except Exception as exc:
        logger.warn(f"[chainlink] no se pudo arrancar el feed: {exc} — el bot sigue")
        return None

    STATE.configure(cl_enabled=True)
    _start_status_publisher(feed, int(cfg.cl_twap_window or 30))
    logger.ok(
        f"[chainlink] feed TWAP arrancado  ventana={cfg.cl_twap_window}s  "
        f"frescura<{cfg.cl_twap_stale_seconds}s  "
        f"grabador={'on' if recorder else 'off'}",
        icon="🔗",
    )
    return feed


def _apply_persisted_overrides() -> None:
    """Apply settings saved from /settings on top of the environment defaults.

    The DB wins over the .env on purpose: a panel with a Save button should keep
    what you saved. /settings marks which fields are overridden and offers a
    reset, so a stale override can't quietly outrank the environment forever.
    """
    try:
        raw = get_all_config()
    except Exception as exc:
        logger.warn(f"could not read saved settings: {exc} — using .env values")
        return

    if not raw:
        return

    overrides, rejected = coerce_overrides(raw)

    if rejected:
        logger.warn(
            f"saved settings ignored (unknown or out of range): {', '.join(sorted(rejected))}"
        )

    if overrides:
        for state in active_states().values():
            state.configure(**overrides)
        detail = "  ".join(f"{k}={v}" for k, v in sorted(overrides.items()))
        logger.ok(
            f"configuración guardada aplicada sobre .env — {detail}",
            icon="⚙",
        )


def _warn_if_martingale_cannot_recover() -> None:
    """Warn when the configured factor can't clear a losing cycle at the cap.

    A martingale only keeps recovering while `f > 1/(1-p)`. Below that, "keep
    doubling until you win" is arithmetically a slower way to lose: the losses
    behind the cycle outgrow what the next win pays back. The factor is shared
    by both strategies, so the tighter of the two caps sets the bar. Checked
    against the *effective* values, after saved overrides are applied.

    Silent unless the martingale is the sizing actually in use — the factor
    still exists in the config under `flat` and `kelly`, it just isn't read.
    """
    if STATE.ss_sizing != "martingale":
        return

    factor = STATE.ss_martingale_mult_factor
    strategies = []
    if STATE.ss_mode in ("fade", "both"):
        strategies.append(("Fade", STATE.ss_fade_limit_cap))
    if STATE.ss_mode in ("trend", "both"):
        strategies.append(("Trend", STATE.ss_trend_limit_cap))

    for label, cap in strategies:
        needed = min_recovering_factor(cap)
        if factor > needed:
            continue
        logger.warn(
            f"[SS {label}] el multiplicador ×{factor:.2f} NO recupera al precio "
            f"máximo {cap:.2f}: hace falta más de ×{needed:.2f}. Con este ajuste, "
            f"seguir el ciclo hasta ganar aumenta la pérdida en vez de cerrarla.",
            icon="⚠",
        )


def _check_port_available(host: str, port: int) -> str | None:
    """Return an error string if `host:port` can't be bound, else None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        return str(exc)
    return None


def main() -> None:
    cfg = load_config()

    # Refuse to start an unprotected dashboard on a public interface — checked
    # before anything else so we never leave a trader running behind a panel
    # that can flip it to real money.
    auth_error = verify_startup_config(cfg.dashboard_host)
    if auth_error:
        logger.err(f"arranque abortado: {auth_error}")
        raise SystemExit(1)

    # Configure state from config. Every market gets the same settings — they
    # are strategy parameters, not per-asset ones — but each keeps its own
    # prices, trades and martingale cycle.
    for symbol in cfg.ss_symbols:
        state_for(symbol).configure(
            mode=cfg.mode,
            has_credentials=cfg.has_credentials,
            starting_bankroll=cfg.starting_bankroll,
            ss_enabled=cfg.ss_enabled,
            ss_mode=cfg.ss_mode,
            ss_fade_base_shares=cfg.ss_fade_base_shares,
            ss_fade_limit_cap=cfg.ss_fade_limit_cap,
            ss_fade_streak_min=cfg.ss_fade_streak_min,
            ss_trend_base_shares=cfg.ss_trend_base_shares,
            ss_trend_limit_cap=cfg.ss_trend_limit_cap,
            ss_trend_min_strength=cfg.ss_trend_min_strength,
            # Fase B — Box Builder
            bb_enabled=cfg.bb_enabled,
            bb_shares_per_leg=cfg.bb_shares_per_leg,
            bb_bid_sum_cap=cfg.bb_bid_sum_cap,
            bb_arm_min_spread=cfg.bb_arm_min_spread,
            bb_complete_taker_cap=cfg.bb_complete_taker_cap,
            bb_complete_maker_cap=cfg.bb_complete_maker_cap,
            bb_quote_cutoff_sec=cfg.bb_quote_cutoff_sec,
            bb_bailout_sec=cfg.bb_bailout_sec,
            bb_cancel_all_sec=cfg.bb_cancel_all_sec,
            bb_reprice_interval=cfg.bb_reprice_interval,
            bb_reprice_behind=cfg.bb_reprice_behind,
            bb_min_coa_hold=cfg.bb_min_coa_hold,
            ss_sizing=cfg.ss_sizing,
            ss_kelly_fraction=cfg.ss_kelly_fraction,
            ss_martingale_mult_factor=cfg.ss_martingale_mult_factor,
            ss_trading_hours=cfg.ss_trading_hours,
            ss_vol_min_pct=cfg.ss_vol_min_pct,
            ss_vol_max_pct=cfg.ss_vol_max_pct,
            ss_range_max_pct=cfg.ss_range_max_pct,
            cl_twap_enabled=cfg.cl_twap_enabled,
            cl_twap_window=cfg.cl_twap_window,
            cl_twap_stale_seconds=cfg.cl_twap_stale_seconds,
            cl_divergence_max=cfg.cl_divergence_max,
            cl_record_ticks=cfg.cl_record_ticks,
        )

    if cfg.is_real and not cfg.has_credentials:
        logger.warn(
            "TRADING_MODE=real but PRIVATE_KEY/PROXY_WALLET are not set — "
            "the bot will refuse to place orders. Switch to paper or add credentials."
        )

    # Initialize database
    try:
        init_db()
        logger.ok("database initialized", icon="🗄️")
    except Exception as exc:
        logger.warn(f"database init failed: {exc} — running without persistence")
    else:
        _apply_persisted_overrides()

    _warn_if_martingale_cannot_recover()

    # Start BTC price fetcher (CoinGecko, for dashboard display)
    start_price_fetcher()

    # Start Chainlink TWAP feed (no-op unless CL_TWAP_ENABLED)
    cl_feed = _start_chainlink_feed(cfg)

    # One trader thread per market. They share nothing but the config: each has
    # its own BotState, its own martingale row and its own DB queries.
    traders = [StreakSnapperTrader(cfg, symbol) for symbol in cfg.ss_symbols]
    for trader in traders:
        trader.start()
    # Report the EFFECTIVE settings, which may include saved overrides.
    # Report the sizing that's actually in use — announcing "martingale=×2.1"
    # under flat sizing described a setting the bot wasn't reading.
    if STATE.ss_sizing == "martingale":
        sizing_detail = f"martingale ×{STATE.ss_martingale_mult_factor}"
    elif STATE.ss_sizing == "kelly":
        sizing_detail = f"kelly ×{STATE.ss_kelly_fraction:.2f}"
    else:
        sizing_detail = "flat"

    logger.ok(
        f"Streak Snapper started — markets={','.join(cfg.ss_symbols).upper()} "
        f"mode={STATE.ss_mode} "
        f"fade_shares={STATE.ss_fade_base_shares} fade_cap={STATE.ss_fade_limit_cap} "
        f"trend_shares={STATE.ss_trend_base_shares} trend_cap={STATE.ss_trend_limit_cap} "
        f"sizing={sizing_detail}",
        icon="🚀",
    )

    # Start dashboard. Werkzeug's per-request access log drowns out the bot's
    # own log at 1 req/s from the dashboard poller, so keep it at WARNING.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app = create_app()

    # Werkzeug calls sys.exit(1) on a bind failure, so check first — otherwise
    # the whole process dies with only Werkzeug's generic message.
    port_error = _check_port_available(cfg.dashboard_host, cfg.dashboard_port)
    if port_error:
        logger.err(
            f"no se pudo abrir el puerto {cfg.dashboard_port}: {port_error}. "
            f"Otro proceso ya lo está usando — deténlo, o arranca este bot con "
            f"PORT=<otro puerto> python run.py"
        )
        for trader in traders:
            trader.stop()
        if cl_feed is not None:
            cl_feed.stop()
        raise SystemExit(1)

    logger.ok(
        f"dashboard listening on http://{cfg.dashboard_host}:{cfg.dashboard_port}"
        f"  ({'protegido con contraseña' if auth_enabled() else 'sin contraseña, solo local'})",
        icon="🌐",
    )
    app.run(
        host=cfg.dashboard_host,
        port=cfg.dashboard_port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
