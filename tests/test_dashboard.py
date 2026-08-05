"""Tests for the dashboard: auth, trade queries, export and metric series.

The dashboard had no tests at all, which mattered more once it grew a login and
a switch that can put the bot on real money.
"""

from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASSWORD = "clave-de-prueba"


@pytest.fixture(autouse=True)
def _reset_db_module():
    import bot.db as db_mod

    db_mod._app = None
    db_mod._initialized = False
    yield
    if db_mod._app is not None:
        try:
            with db_mod._app.app_context():
                db_mod.db.session.remove()
        except Exception:
            pass
        db_mod._app = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Auth reads the environment on every call, so isolate it per test."""
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-key")


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_dash_")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def app(db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from bot.dashboard import create_app

    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_app(db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    from bot.dashboard import create_app

    application = create_app()
    application.config["TESTING"] = True
    return application


def _login(client):
    return client.post("/login", data={"password": PASSWORD})


def _make_trade(**kw):
    from bot.db import TradeModel

    defaults = dict(
        strategy="ss_trend", direction="UP", token_id="tok",
        window_slug="btc-updown-5m-1785000000", window_ts=1785000000,
        limit_cap=0.52, entry_price=0.5, shares=5.0, cost=2.5, shares_count=5.0,
        multiplier=1.0, loss_streak=0, mode="paper", status="open",
    )
    defaults.update(kw)
    return TradeModel(**defaults)


def _seed(app, trades):
    from bot.db import db

    with app.app_context():
        for t in trades:
            db.session.add(t)
        db.session.commit()


# ── authentication ────────────────────────────────────────────────────────────


class TestAuthDisabled:
    """With no password set the panel stays open — the guard is what refuses to
    bind to a public interface, not the request handler."""

    def test_pages_are_reachable(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/state").status_code == 200

    def test_login_redirects_when_pointless(self, client):
        resp = client.get("/login")
        assert resp.status_code == 302


class TestAuthEnabled:
    def test_pages_redirect_to_login(self, auth_app):
        resp = auth_app.test_client().get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_api_returns_401_not_a_redirect(self, auth_app):
        # XHR can act on a status code; it can't follow a redirect to HTML.
        client = auth_app.test_client()
        for path in ("/state", "/api/trades", "/api/metrics/series"):
            assert client.get(path).status_code == 401, path

    def test_config_post_is_blocked(self, auth_app):
        # The endpoint that can switch the bot to real money.
        resp = auth_app.test_client().post("/config", json={"mode": "real"})
        assert resp.status_code == 401

    def test_healthz_stays_public(self, auth_app):
        # Monitors shouldn't need credentials.
        assert auth_app.test_client().get("/healthz").status_code == 200

    def test_wrong_password_rejected(self, auth_app):
        client = auth_app.test_client()
        assert client.post("/login", data={"password": "nope"}).status_code == 401
        assert client.get("/").status_code == 302

    def test_correct_password_grants_access(self, auth_app):
        client = auth_app.test_client()
        assert _login(client).status_code == 302
        assert client.get("/").status_code == 200
        assert client.get("/state").status_code == 200

    def test_logout_ends_session(self, auth_app):
        client = auth_app.test_client()
        _login(client)
        client.get("/logout")
        assert client.get("/").status_code == 302

    def test_next_param_only_accepts_relative_paths(self, auth_app):
        # An absolute URL here would make the login form an open redirect.
        client = auth_app.test_client()
        resp = client.post("/login",
                           data={"password": PASSWORD, "next": "https://evil.test/x"})
        assert "evil.test" not in resp.headers["Location"]

    def test_next_param_preserves_deep_link(self, auth_app):
        client = auth_app.test_client()
        resp = client.post("/login", data={"password": PASSWORD, "next": "/settings"})
        assert resp.headers["Location"].endswith("/settings")


class TestStartupGuard:
    """Refusing to start beats starting wide open."""

    def test_public_bind_without_password_is_refused(self):
        from bot.auth import verify_startup_config

        assert verify_startup_config("0.0.0.0") is not None

    def test_loopback_without_password_is_allowed(self):
        from bot.auth import verify_startup_config

        assert verify_startup_config("127.0.0.1") is None

    def test_password_allows_public_bind(self, monkeypatch):
        from bot.auth import verify_startup_config

        monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
        assert verify_startup_config("0.0.0.0") is None


# ── /state ────────────────────────────────────────────────────────────────────


class TestState:
    def test_includes_chainlink(self, client):
        # dashboard.js reads status.chainlink; it used to be dropped here, which
        # left the CL badge stuck on "off" whatever the feed was doing.
        assert "chainlink" in client.get("/state").get_json()["status"]

    def test_excludes_trades(self, client):
        # Trades moved to /api/trades — /state is polled once a second.
        assert "trades" not in client.get("/state").get_json()

    def test_exposes_chainlink_config(self, client):
        config = client.get("/state").get_json()["config"]
        for field in ("cl_twap_enabled", "cl_twap_window", "cl_record_ticks"):
            assert field in config

    def test_exposes_the_trend_cycle(self, client):
        # The dashboard shows which side is locked and until when; without
        # these the martingale card can't tell a cycle from a fresh signal.
        trend = client.get("/state").get_json()["martingale"]["trend"]
        for field in ("cycle_side", "cycle_anchor_ts", "last_strength"):
            assert field in trend

    def test_exposes_the_trend_threshold(self, client):
        config = client.get("/state").get_json()["config"]
        assert "ss_trend_min_strength" in config


class TestStateServesTheRegistry:
    """/settings renders from this payload and nothing else.

    Before A4 the template hard-coded one block per strategy and settings.js
    hard-coded one line per parameter, which is how `ss_sizing` and the regime
    filters ended up accepted by POST /config but invisible on screen.
    """

    def test_lists_every_registered_strategy(self, client):
        from bot import strategies

        payload = client.get("/state").get_json()["strategies"]
        assert [s["id"] for s in payload] == list(strategies.ids())

    def test_each_strategy_carries_what_a_card_needs(self, client):
        for strategy in client.get("/state").get_json()["strategies"]:
            assert strategy["name"] and strategy["description"]
            assert strategy["enabled"] in (True, False)
            assert strategy["enabled_when"], "sin esto la tarjeta no se atenúa"
            for param in strategy["params"]:
                assert param["label"] and param["kind"]

    def test_config_covers_every_runtime_field(self, client):
        """A field the payload omits is a widget rendered empty and then saved
        empty — the silent way to reset a parameter nobody touched."""
        from bot.config import RUNTIME_FIELDS

        config = client.get("/state").get_json()["config"]
        assert set(RUNTIME_FIELDS) <= set(config)

    def test_field_schema_matches_runtime_fields(self, client):
        from bot.config import RUNTIME_FIELDS

        fields = client.get("/state").get_json()["fields"]
        assert set(fields) == set(RUNTIME_FIELDS)

    def test_schema_carries_ranges_so_the_input_can_bound_itself(self, client):
        cap = client.get("/state").get_json()["fields"]["ss_fade_limit_cap"]
        assert cap["min"] == pytest.approx(0.10)
        assert cap["max"] == pytest.approx(0.99)
        assert cap["kind"] == "float"

    def test_sizing_and_regime_fields_are_reachable_from_the_ui(self, client):
        """The gap A4 closed: configurable by POST, invisible on screen."""
        fields = client.get("/state").get_json()["fields"]
        for name in ("ss_sizing", "ss_kelly_fraction", "ss_trading_hours",
                     "ss_vol_min_pct", "ss_vol_max_pct", "ss_range_max_pct"):
            assert fields[name]["label"], f"{name} sin label"


class TestTrendThresholdConfig:
    def test_accepts_a_valid_threshold(self, client):
        resp = client.post("/config", json={"ss_trend_min_strength": 0.012})
        assert resp.status_code == 200
        config = client.get("/state").get_json()["config"]
        assert config["ss_trend_min_strength"] == pytest.approx(0.012)

    # Out-of-range values are dropped, not rejected — the pre-existing contract
    # of _build_config_updates for every field. What matters here is that the
    # bad value never reaches STATE.
    def test_ignores_a_threshold_above_the_range(self, client):
        # 0.10 is 10% in four hours; past that nothing would ever trade.
        before = client.get("/state").get_json()["config"]["ss_trend_min_strength"]
        resp = client.post("/config", json={"ss_trend_min_strength": 0.5})

        assert "ss_trend_min_strength" not in resp.get_json()["updated"]
        after = client.get("/state").get_json()["config"]["ss_trend_min_strength"]
        assert after == before

    def test_ignores_a_negative_threshold(self, client):
        before = client.get("/state").get_json()["config"]["ss_trend_min_strength"]
        resp = client.post("/config", json={"ss_trend_min_strength": -0.01})

        assert "ss_trend_min_strength" not in resp.get_json()["updated"]
        assert (
            client.get("/state").get_json()["config"]["ss_trend_min_strength"] == before
        )


class TestBalance:
    def test_open_trades_are_committed_not_available(self, app, client):
        _seed(app, [
            _make_trade(status="won", pnl=2.5, cost=2.5),
            _make_trade(status="open", cost=7.0),
            _make_trade(status="open", cost=3.0),
        ])
        stats = client.get("/state").get_json()["stats"]

        assert stats["committed"] == pytest.approx(10.0)
        assert stats["available"] == pytest.approx(stats["bankroll"] - 10.0)
        # The reported symptom: balance looked unchanged after opening a trade.
        assert stats["available"] < stats["bankroll"]

    def test_no_open_trades_means_nothing_committed(self, app, client):
        _seed(app, [_make_trade(status="won", pnl=2.5)])
        stats = client.get("/state").get_json()["stats"]

        assert stats["committed"] == pytest.approx(0.0)
        assert stats["available"] == pytest.approx(stats["bankroll"])


# ── /api/trades ───────────────────────────────────────────────────────────────


class TestTradesPagination:
    def test_default_page_size_is_25(self, app, client):
        _seed(app, [_make_trade() for _ in range(60)])
        data = client.get("/api/trades").get_json()

        assert len(data["items"]) == 25
        assert data["total"] == 60
        assert data["pages"] == 3

    def test_last_page_holds_the_remainder(self, app, client):
        _seed(app, [_make_trade() for _ in range(60)])
        assert len(client.get("/api/trades?page=3").get_json()["items"]) == 10

    def test_pages_do_not_overlap(self, app, client):
        _seed(app, [_make_trade() for _ in range(60)])
        first = {t["id"] for t in client.get("/api/trades?page=1").get_json()["items"]}
        second = {t["id"] for t in client.get("/api/trades?page=2").get_json()["items"]}
        assert not (first & second)

    def test_per_page_is_capped(self, app, client):
        # Otherwise ?per_page=1000000 is a way to make the bot dump its table.
        _seed(app, [_make_trade() for _ in range(30)])
        data = client.get("/api/trades?per_page=99999").get_json()
        assert data["per_page"] <= 200


class TestTradesFilters:
    @pytest.fixture(autouse=True)
    def _seed_mixed(self, app):
        _seed(app, [
            _make_trade(strategy="ss_fade", status="won", direction="UP", pnl=1.0),
            _make_trade(strategy="ss_fade", status="lost", direction="DOWN", pnl=-2.0),
            _make_trade(strategy="ss_trend", status="won", direction="UP", pnl=3.0),
            _make_trade(strategy="ss_trend", status="open", direction="DOWN", mode="real"),
        ])

    def test_filter_by_strategy(self, client):
        assert client.get("/api/trades?strategy=ss_fade").get_json()["total"] == 2

    def test_filter_by_status(self, client):
        assert client.get("/api/trades?status=won").get_json()["total"] == 2

    def test_filter_by_direction(self, client):
        assert client.get("/api/trades?direction=DOWN").get_json()["total"] == 2

    def test_filter_by_mode(self, client):
        assert client.get("/api/trades?mode=real").get_json()["total"] == 1

    def test_filters_combine(self, client):
        data = client.get("/api/trades?strategy=ss_trend&status=won").get_json()
        assert data["total"] == 1


    def test_all_is_the_same_as_no_filter(self, client):
        assert client.get("/api/trades?strategy=all").get_json()["total"] == 4

    def test_search_matches_slug(self, client):
        assert client.get("/api/trades?q=btc-updown-5m").get_json()["total"] == 4

    def test_search_matches_id(self, client):
        first = client.get("/api/trades").get_json()["items"][0]["id"]
        data = client.get(f"/api/trades?q={first}").get_json()
        assert data["total"] == 1 and data["items"][0]["id"] == first

    def test_search_with_no_match_is_empty(self, client):
        assert client.get("/api/trades?q=zzzz").get_json()["total"] == 0


class TestTradesSymbolFilter:
    """`build_query` supported `symbol` before the endpoint passed it through,
    so /api/trades?symbol=eth returned every market — a filter that looks
    applied and isn't is worse than one that doesn't exist."""

    @pytest.fixture(autouse=True)
    def _seed_symbols(self, app):
        _seed(app, [
            _make_trade(symbol="btc", strategy="ss_fade", status="won", pnl=1.0),
            _make_trade(symbol="eth", strategy="ss_fade", status="lost", pnl=-2.0),
            _make_trade(symbol="eth", strategy="ss_trend", status="won", pnl=3.0),
        ])

    def test_filter_by_symbol(self, client):
        data = client.get("/api/trades?symbol=eth").get_json()
        assert data["total"] == 2
        assert {t["symbol"] for t in data["items"]} == {"eth"}

    def test_no_filter_spans_every_market(self, client):
        assert client.get("/api/trades").get_json()["total"] == 3

    def test_combines_with_strategy(self, client):
        data = client.get("/api/trades?symbol=eth&strategy=ss_fade").get_json()
        assert data["total"] == 1

    def test_csv_export_honours_it(self, client):
        """The export follows the filters on screen; it used to ignore this one."""
        body = client.get("/api/trades.csv?symbol=eth").get_data(as_text=True)
        assert body.count("\n") >= 3          # header + 2 rows
        assert ",btc," not in body

    def test_series_can_be_cut_by_symbol(self, client):
        """The equity curve of one market, not of all of them added together."""
        one = client.get("/api/metrics/series?symbol=eth").get_json()
        assert one["resolved_trades"] == 2

    def test_series_without_symbol_spans_every_market(self, client):
        every = client.get("/api/metrics/series").get_json()
        assert every["resolved_trades"] == 3


class TestTradesDateFilter:
    def test_window_range_is_respected(self, app, client):
        base = 1785000000
        _seed(app, [
            _make_trade(window_ts=base),
            _make_trade(window_ts=base + 86_400),
            _make_trade(window_ts=base + 172_800),
        ])
        data = client.get(f"/api/trades?from={base + 86_400}").get_json()
        assert data["total"] == 2


# ── CSV export ────────────────────────────────────────────────────────────────


class TestCsvExport:
    def test_downloads_as_attachment(self, app, client):
        _seed(app, [_make_trade()])
        resp = client.get("/api/trades.csv")

        assert resp.status_code == 200
        assert "text/csv" in resp.headers["Content-Type"]
        assert "attachment" in resp.headers["Content-Disposition"]

    def test_row_per_trade_plus_header(self, app, client):
        _seed(app, [_make_trade() for _ in range(7)])
        rows = list(csv.DictReader(io.StringIO(
            client.get("/api/trades.csv").get_data(as_text=True))))
        assert len(rows) == 7

    def test_honours_active_filters(self, app, client):
        # Downloading everything while the table shows a filtered view would
        # be a surprise.
        _seed(app, [
            _make_trade(strategy="ss_fade"),
            _make_trade(strategy="ss_trend"),
            _make_trade(strategy="ss_trend"),
        ])
        rows = list(csv.DictReader(io.StringIO(
            client.get("/api/trades.csv?strategy=ss_trend").get_data(as_text=True))))
        assert len(rows) == 2

    def test_header_is_stable(self, app, client):
        from bot.dashboard import CSV_COLUMNS

        _seed(app, [_make_trade()])
        reader = csv.reader(io.StringIO(client.get("/api/trades.csv").get_data(as_text=True)))
        assert next(reader) == CSV_COLUMNS


# ── metric series ─────────────────────────────────────────────────────────────


class TestMetricSeries:
    def _resolved(self, pnl, minutes, **kw):
        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        return _make_trade(
            status="won" if pnl > 0 else "lost",
            won=pnl > 0, pnl=pnl,
            resolved_at=base + timedelta(minutes=minutes),
            **kw,
        )

    def test_equity_accumulates_from_the_starting_bankroll(self, app, client):
        from bot.state import STATE

        STATE.starting_bankroll = 1000.0
        _seed(app, [
            self._resolved(10.0, 5),
            self._resolved(-4.0, 10),
            self._resolved(6.0, 15),
        ])
        equity = client.get("/api/metrics/series").get_json()["equity"]

        assert [p["equity"] for p in equity] == [1010.0, 1006.0, 1012.0]

    def test_drawdown_matches_a_hand_computed_case(self, app, client):
        from bot.state import STATE

        STATE.starting_bankroll = 1000.0
        # Peak 1020 then down to 1005 → deepest drawdown is 15.
        _seed(app, [
            self._resolved(20.0, 5),
            self._resolved(-15.0, 10),
            self._resolved(5.0, 15),
        ])
        data = client.get("/api/metrics/series").get_json()

        assert data["max_drawdown"] == pytest.approx(15.0)
        assert data["max_drawdown_pct"] == pytest.approx(15.0 / 1020.0, rel=1e-4)
        assert [p["drawdown"] for p in data["drawdown"]] == [0.0, 15.0, 10.0]

    def test_drawdown_is_zero_on_a_monotonic_climb(self, app, client):
        _seed(app, [self._resolved(5.0, 5), self._resolved(5.0, 10)])
        assert client.get("/api/metrics/series").get_json()["max_drawdown"] == 0.0

    def test_win_rate_is_cumulative(self, app, client):
        _seed(app, [
            self._resolved(1.0, 5), self._resolved(-1.0, 10),
            self._resolved(1.0, 15), self._resolved(1.0, 20),
        ])
        rates = [p["cumulative"] for p in
                 client.get("/api/metrics/series").get_json()["win_rate"]]
        assert rates == pytest.approx([1.0, 0.5, 2 / 3, 0.75])

    def test_open_trades_are_excluded(self, app, client):
        # An unresolved trade has no P&L; including it would flatten the curve
        # with a point that says nothing.
        _seed(app, [self._resolved(5.0, 5), _make_trade(status="open")])
        assert client.get("/api/metrics/series").get_json()["resolved_trades"] == 1

    def test_split_per_strategy(self, app, client):
        _seed(app, [
            self._resolved(10.0, 5, strategy="ss_fade"),
            self._resolved(-3.0, 10, strategy="ss_trend"),
        ])
        by_strategy = client.get("/api/metrics/series").get_json()["equity_by_strategy"]
        assert set(by_strategy) == {"ss_fade", "ss_trend"}

    def test_per_strategy_drawdown_reports_no_percentage(self, app, client):
        # Those curves run from zero, not from a bankroll, so a percentage
        # against their running peak is nonsense — a $2 peak then a $5.40 dip
        # would read as 270%.
        _seed(app, [
            self._resolved(2.0, 5, strategy="ss_fade"),
            self._resolved(-5.4, 10, strategy="ss_fade"),
        ])
        dd = client.get("/api/metrics/series").get_json()["strategy_drawdown"]
        assert dd["ss_fade"]["max_drawdown"] == pytest.approx(5.4)
        assert dd["ss_fade"]["max_drawdown_pct"] is None

    def test_empty_table_returns_empty_series(self, client):
        data = client.get("/api/metrics/series").get_json()
        assert data["equity"] == [] and data["max_drawdown"] == 0.0
