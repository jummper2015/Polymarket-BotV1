"""Spread-Harvest, etapa de observación.

Todo aquí es sobre funciones puras y estado en memoria: las dos puertas y el
precio de la puja se calculan sin red, que es lo que permite probarlas.
"""

import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.strategies import spread_harvest as sh


# Un libro ancho de verdad: los dos asks suman 1,12.
WIDE_UP, WIDE_DOWN = 0.58, 0.54
# Uno normal: suman 1,02. Los creadores de mercado están presentes.
TIGHT_UP, TIGHT_DOWN = 0.52, 0.50


def _gates(**kw):
    """Ventana cotizable por defecto; cada test rompe una sola cosa."""
    args = dict(
        mark=63_010.0, strike=63_000.0, atr4=100.0,   # coa = 0,10
        ask_up=WIDE_UP, ask_down=WIDE_DOWN,
        seconds_left=120.0,
    )
    args.update(kw)
    return sh.evaluate_gates(**args)


class TestGates:
    """Las dos puertas de la tesis: moneda al aire y libro ancho."""

    def test_the_default_case_is_quotable(self):
        assert _gates()["quotable"] is True

    def test_coa_is_the_cushion_in_units_of_atr(self):
        """No es un umbral en dólares: 30$ es mucho o poco según la volatilidad."""
        v = _gates(mark=63_030.0, strike=63_000.0, atr4=100.0)
        assert v["coa"] == pytest.approx(0.30)
        assert v["coin_flip"] is True

        # El mismo cushion de 30$ con la volatilidad a la cuarta parte ya no es
        # una moneda al aire: el precio se ha ido lejos en términos de su rango.
        v = _gates(mark=63_030.0, strike=63_000.0, atr4=25.0)
        assert v["coa"] == pytest.approx(1.20)
        assert v["coin_flip"] is False
        assert v["reason"] == "SH_SKIP_COA"

    def test_the_cushion_is_symmetric(self):
        """Da igual el lado: lo que importa es la distancia al strike."""
        arriba = _gates(mark=63_020.0, strike=63_000.0)
        abajo = _gates(mark=62_980.0, strike=63_000.0)
        assert arriba["coa"] == abajo["coa"] == pytest.approx(0.20)

    def test_coa_boundary_is_inclusive(self):
        assert _gates(mark=63_040.0, atr4=100.0)["coin_flip"] is True    # 0,40
        assert _gates(mark=63_041.0, atr4=100.0)["coin_flip"] is False   # 0,41

    def test_a_tight_book_is_refused(self):
        """Sin spread que cosechar no hay estrategia."""
        v = _gates(ask_up=TIGHT_UP, ask_down=TIGHT_DOWN)
        assert v["ask_sum"] == pytest.approx(1.02)
        assert v["wide_book"] is False
        assert v["quotable"] is False
        assert v["reason"] == "SH_SKIP_SPREAD"

    def test_ask_sum_boundary_is_inclusive(self):
        assert _gates(ask_up=0.55, ask_down=0.55)["wide_book"] is True    # 1,10
        assert _gates(ask_up=0.55, ask_down=0.54)["wide_book"] is False   # 1,09

    def test_both_gates_must_open_together(self):
        """Cada una por separado no basta, y por eso se miden juntas."""
        assert _gates(mark=63_500.0)["quotable"] is False                 # solo libro
        assert _gates(ask_up=TIGHT_UP, ask_down=TIGHT_DOWN)["quotable"] is False

    def test_the_time_band_closes_the_window(self):
        """Fuera de T-120 → T-30 no se cotiza aunque todo lo demás encaje."""
        assert _gates(seconds_left=240.0)["quotable"] is False   # demasiado pronto
        assert _gates(seconds_left=10.0)["quotable"] is False    # demasiado tarde
        assert _gates(seconds_left=180.0)["quotable"] is True    # borde
        assert _gates(seconds_left=30.0)["quotable"] is True     # borde
        assert _gates(seconds_left=240.0)["reason"] == "SH_OUT_OF_BAND"

    def test_the_underdog_is_the_cheaper_side(self):
        assert _gates(ask_up=0.54, ask_down=0.58)["dog"] == "UP"
        assert _gates(ask_up=0.58, ask_down=0.54)["dog"] == "DOWN"

    def test_missing_data_never_reports_a_coin_flip(self):
        """Sin strike o sin ATR no hay veredicto — y no cotizar es el lado seguro."""
        for kw in ({"strike": None}, {"mark": None}, {"atr4": None}, {"atr4": 0.0}):
            v = _gates(**kw)
            assert v["quotable"] is False
            assert v["coin_flip"] is False
            assert v["reason"] == "SH_NO_DATA"

    def test_a_missing_book_is_not_a_missing_coin_flip(self):
        """El WS puede hipar sin que eso invalide el cushion ya calculado."""
        v = _gates(ask_up=None, ask_down=None)
        assert v["reason"] == "SH_NO_BOOK"
        assert v["coa"] is not None and v["coin_flip"] is True
        assert v["quotable"] is False


class TestQuotePrice:
    """Dónde iría la puja. Nunca cruza el ask: es la puja, no la toma."""

    def test_a_tick_above_the_best_bid(self):
        assert sh.quote_price_for(0.44, 0.58) == pytest.approx(0.45)

    def test_never_below_the_floor(self):
        """Los llenados baratos fueron tóxicos: 32-35% de acierto sobre 184."""
        assert sh.quote_price_for(0.20, 0.58) == pytest.approx(sh.QUOTE_FLOOR)

    def test_never_above_the_cap(self):
        assert sh.quote_price_for(0.55, 0.60) == pytest.approx(sh.QUOTE_CAP)

    def test_never_crosses_the_ask(self):
        """Si la banda obligara a igualar o superar el ask, no hay puja posible."""
        assert sh.quote_price_for(0.44, 0.45) is None

    def test_no_bid_means_no_quote(self):
        assert sh.quote_price_for(None, 0.58) is None
        assert sh.quote_price_for(0.0, 0.58) is None


class _FakeState:
    """Lo mínimo que `_observe` toca de BotState."""

    def __init__(self, asks=(WIDE_UP, WIDE_DOWN)):
        self._asks = asks
        self.observations = {}
        self.last_up_bid = 0.44
        self.last_down_bid = 0.40
        self._lock = threading.Lock()

    def get_asks(self):
        return self._asks

    def record_observation(self, key, count=1):
        self.observations[key] = self.observations.get(key, 0) + count


def _ctx(state, window_ts, seconds_left=120.0, symbol="btc"):
    return SimpleNamespace(
        state=state, symbol=symbol, seconds_left=seconds_left,
        tokens=SimpleNamespace(window_ts=window_ts, slug=f"btc-{window_ts}"),
        streak=None,
    )


@pytest.fixture(autouse=True)
def _clean_accumulator():
    sh._WINDOWS.clear()
    yield
    sh._WINDOWS.clear()


@pytest.fixture
def _no_network(monkeypatch):
    """La red se corta de raíz: si algún test la tocara, fallaría en vez de colgarse."""
    monkeypatch.setattr(
        "bot.polymarket_price.get_strike_and_mark",
        lambda ts, symbol="btc": (63_000.0, 63_010.0),
    )
    monkeypatch.setattr("bot.binance_api.get_atr4", lambda symbol="btc": 100.0)


class TestObservation:
    """La acumulación por ventana, que es el dato que produce esta etapa."""

    def test_a_quotable_window_is_counted_once(self, _no_network):
        state = _FakeState()
        # Varios ticks de la misma ventana...
        for _ in range(3):
            sh._observe(_ctx(state, 1_000))
        assert state.observations == {}         # aún no ha cerrado

        # ...y al cambiar de ventana se publica.
        sh._observe(_ctx(state, 1_300))
        assert state.observations["SH_WINDOWS"] == 1
        assert state.observations["SH_QUOTABLE"] == 1

    def test_a_tight_book_window_is_counted_as_such(self, _no_network):
        state = _FakeState(asks=(TIGHT_UP, TIGHT_DOWN))
        sh._observe(_ctx(state, 1_000))
        sh._observe(_ctx(state, 1_300))
        assert state.observations["SH_WINDOWS"] == 1
        assert "SH_QUOTABLE" not in state.observations
        assert state.observations["SH_SKIP_SPREAD"] == 1

    def test_one_quotable_tick_is_enough(self, _no_network):
        """La puerta puede abrirse y cerrarse dentro de la ventana."""
        state = _FakeState(asks=(TIGHT_UP, TIGHT_DOWN))
        sh._observe(_ctx(state, 1_000))
        state._asks = (WIDE_UP, WIDE_DOWN)      # el libro se abre a mitad
        sh._observe(_ctx(state, 1_000))
        sh._observe(_ctx(state, 1_300))
        assert state.observations["SH_QUOTABLE"] == 1

    def test_symbols_accumulate_independently(self, _no_network):
        """Un libro ancho en BTC no debe contarse como oportunidad en ETH."""
        state = _FakeState()
        sh._observe(_ctx(state, 1_000, symbol="btc"))
        sh._observe(_ctx(state, 1_000, symbol="eth"))
        assert set(sh._WINDOWS) == {"btc", "eth"}

    def test_it_never_produces_a_signal(self):
        """El punto de esta etapa: mide, no opera."""
        assert sh.DESCRIPTOR.evaluate(_ctx(_FakeState(), 1_000)) == []

    def test_it_is_off_by_default(self):
        assert sh.DESCRIPTOR.enabled_for(SimpleNamespace()) is False
        assert sh.DESCRIPTOR.enabled_for(
            SimpleNamespace(sh_observe_enabled=True)
        ) is True

    def test_it_never_wins_a_tie_break(self):
        """Prioridad 0: no emite señales, así que no puede desplazar a estrategias activas."""
        from bot import strategies

        assert sh.DESCRIPTOR.priority < strategies.get("box_builder").priority
