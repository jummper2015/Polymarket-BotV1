"""Entry point: launches 3 trader threads (BTC, SOL, ETH) + Corridor Trader (BTC15) + Flask dashboard."""
from __future__ import annotations

from . import logger
from .config import load_config
from .corridor_trader import CorridorTrader
from .dashboard import create_app, start_price_fetcher
from .state import STATES, TRADING_STATES
from .trader import Trader


def main() -> None:
    cfg = load_config()

    # Configure all markets (including btc15 which shares general settings)
    for sym, state in STATES.items():
        state.configure(
            mode=cfg.mode,
            has_credentials=cfg.has_credentials,
            trigger_price=cfg.trigger_price,
            buy_amount=cfg.buy_amount,
            starting_bankroll=cfg.starting_bankroll,
            max_trades_per_window=cfg.max_trades_per_window,
            hedge_threshold=cfg.hedge_threshold,
            last_minute_seconds=cfg.last_minute_seconds,
        )

    if cfg.is_real and not cfg.has_credentials:
        logger.warn(
            "TRADING_MODE=real but PRIVATE_KEY/PROXY_WALLET are not set — "
            "the bot will refuse to place orders. Switch to paper or add credentials."
        )

    start_price_fetcher()

    # Start regular 5m traders for BTC, SOL, ETH
    for sym in TRADING_STATES:
        trader = Trader(cfg, sym)
        trader.start()
        logger.ok(f"trader [{sym.upper()}] started", icon="▶")

    # Start Corridor Trader for BTC15 (15-min window; idles when cc_enabled=False)
    corridor = CorridorTrader(cfg)
    corridor.start()
    logger.ok("trader [BTC15 Corridor] started", icon="🌙")

    app = create_app()
    logger.ok(f"dashboard listening on http://{cfg.dashboard_host}:{cfg.dashboard_port}", icon="🌐")
    app.run(
        host=cfg.dashboard_host,
        port=cfg.dashboard_port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
