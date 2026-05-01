"""Entry point: launches the trader thread and the Flask dashboard."""
from __future__ import annotations

from . import logger
from .config import load_config
from .dashboard import create_app, start_btc_fetcher
from .state import STATE
from .trader import Trader


def main() -> None:
    cfg = load_config()
    STATE.configure(
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

    # Start background BTC price fetcher
    start_btc_fetcher()

    trader = Trader(cfg)
    trader.start()

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
