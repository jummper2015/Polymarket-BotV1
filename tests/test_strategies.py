"""The strategy registry — bot/strategies/.

What these tests protect is the promise the registry makes: declaring a
descriptor is enough. If a parameter stops reaching RUNTIME_FIELDS, or a
strategy stops being asked for signals, the bot silently runs with less than it
was configured to run with — no exception, no log line.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import strategies
from bot.config import BASE_FIELDS, RUNTIME_FIELDS
from bot.runtime_field import RuntimeField
from bot.strategies.base import StrategyContext, StrategyDescriptor


def _signal(strategy, direction):
    """Minimal stand-in for StreakSignal — resolve_conflicts reads two fields."""
    return SimpleNamespace(strategy=strategy, direction=direction)


def _descriptor(sid, **kw):
    kw.setdefault("name", sid)
    kw.setdefault("description", "")
    kw.setdefault("evaluate", lambda ctx: [])
    kw.setdefault("is_enabled", lambda st: True)
    return StrategyDescriptor(id=sid, **kw)


# ═══════════════════════════════════════════════════════════════════════════════
# Registry contents
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_the_two_migrated_forms_are_registered(self):
        assert strategies.ids() == ("ss_fade", "ss_trend")

    def test_ids_match_what_trades_stores(self):
        """`trades.strategy` holds these strings; KPIs group by them."""
        for sid in strategies.ids():
            assert strategies.get(sid).id == sid

    def test_fade_outranks_trend(self):
        """Fase 8: fade +3.74%/op, trend −4.22%. The tie-break follows that."""
        assert (
            strategies.get("ss_fade").priority
            > strategies.get("ss_trend").priority
        )

    def test_enabled_order_is_by_priority(self):
        state = SimpleNamespace(ss_mode="both")
        assert [d.id for d in strategies.enabled_for(state)] == ["ss_fade", "ss_trend"]

    def test_unknown_strategy_is_none(self):
        assert strategies.get("no_existe") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Parameters reach the config layer
# ═══════════════════════════════════════════════════════════════════════════════


class TestParamsFlowIntoRuntimeFields:
    def test_every_strategy_param_is_a_runtime_field(self):
        """The whole point of A4: declaring a param is the only step."""
        for descriptor in strategies.all_descriptors():
            for param in descriptor.params:
                assert RUNTIME_FIELDS[param.name] is param

    def test_runtime_fields_is_base_plus_registry(self):
        assert len(RUNTIME_FIELDS) == len(BASE_FIELDS) + len(strategies.params())

    def test_no_param_name_collides_with_a_base_field(self):
        """A collision would silently drop one of the two from the settings UI."""
        base = {f.name for f in BASE_FIELDS}
        for param in strategies.params():
            assert param.name not in base

    def test_params_carry_the_metadata_settings_renders(self):
        for param in strategies.params():
            assert param.label, f"{param.name} sin label — saldría con su nombre crudo"

    @pytest.mark.parametrize("name", ["ss_fade_limit_cap", "ss_trend_min_strength"])
    def test_migrated_params_kept_their_validation(self, name):
        """Ranges are unchanged by the move; POST /config still refuses junk."""
        field = RUNTIME_FIELDS[name]
        assert field.coerce(2.0) == (False, None)
        ok, _ = field.coerce(field.minimum)
        assert ok


# ═══════════════════════════════════════════════════════════════════════════════
# Enablement — ss_mode stays the switch for the two original forms
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnablement:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("fade", ["ss_fade"]),
            ("trend", ["ss_trend"]),
            ("both", ["ss_fade", "ss_trend"]),
        ],
    )
    def test_ss_mode_still_decides(self, mode, expected):
        state = SimpleNamespace(ss_mode=mode)
        assert [d.id for d in strategies.enabled_for(state)] == expected

    def test_a_descriptor_that_raises_is_off(self):
        """Fail closed: a broken toggle must not license live trading."""
        def boom(_state):
            raise RuntimeError("boom")

        assert _descriptor("x", is_enabled=boom).enabled_for(object()) is False

    def test_symbols_restrict_which_markets_run_it(self):
        """`corridor` will need this — it requires a 15m market to exist too."""
        anywhere = _descriptor("anywhere")
        btc_only = _descriptor("btc_only", symbols=("btc",))
        assert anywhere.supports("sol") and btc_only.supports("btc")
        assert not btc_only.supports("sol")

    def test_enabled_for_filters_by_symbol(self, monkeypatch):
        btc_only = _descriptor("btc_only", symbols=("btc",))
        monkeypatch.setattr(strategies, "_REGISTERED", (btc_only,))
        state = SimpleNamespace(ss_mode="both")
        assert strategies.enabled_for(state, "btc") == [btc_only]
        assert strategies.enabled_for(state, "eth") == []


# ═══════════════════════════════════════════════════════════════════════════════
# Conflict resolution — the tie-break that Fase 8 inverted
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveConflicts:
    def test_fade_survives_a_disagreement(self):
        kept, dropped = strategies.resolve_conflicts(
            [_signal("ss_trend", "UP"), _signal("ss_fade", "DOWN")]
        )
        assert [s.strategy for s in kept] == ["ss_fade"]
        assert [s.strategy for s in dropped] == ["ss_trend"]

    def test_order_does_not_matter(self):
        kept, _ = strategies.resolve_conflicts(
            [_signal("ss_fade", "DOWN"), _signal("ss_trend", "UP")]
        )
        assert [s.strategy for s in kept] == ["ss_fade"]

    def test_agreement_keeps_everyone(self):
        """Both sides of the same direction is one trade each, not a wash."""
        signals = [_signal("ss_fade", "UP"), _signal("ss_trend", "UP")]
        kept, dropped = strategies.resolve_conflicts(signals)
        assert len(kept) == 2 and dropped == []

    def test_single_signal_is_untouched(self):
        kept, dropped = strategies.resolve_conflicts([_signal("ss_fade", "UP")])
        assert len(kept) == 1 and dropped == []

    def test_empty_input(self):
        assert strategies.resolve_conflicts([]) == ([], [])

    def test_unregistered_strategy_loses_to_a_registered_one(self):
        """A retired id in the table must not outrank a live strategy."""
        kept, dropped = strategies.resolve_conflicts(
            [_signal("ss_viejo", "UP"), _signal("ss_fade", "DOWN")]
        )
        assert [s.strategy for s in kept] == ["ss_fade"]
        assert [s.strategy for s in dropped] == ["ss_viejo"]


# ═══════════════════════════════════════════════════════════════════════════════
# evaluate() — the wrapper around the two migrated forms
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvaluate:
    def test_fade_delegates_to_the_streak_strategy(self):
        sig = _signal("ss_fade", "UP")
        streak = SimpleNamespace(
            get_fade_signal=lambda: sig,
            get_trend_signal=lambda: None,
        )
        ctx = StrategyContext(state=None, symbol="btc", streak=streak)
        assert strategies.get("ss_fade").evaluate(ctx) == [sig]

    def test_no_signal_is_an_empty_list_not_none(self):
        """The trader does `signals.extend(...)`; None would blow up there."""
        streak = SimpleNamespace(
            get_fade_signal=lambda: None,
            get_trend_signal=lambda: None,
        )
        ctx = StrategyContext(state=None, symbol="btc", streak=streak)
        for sid in ("ss_fade", "ss_trend"):
            assert strategies.get(sid).evaluate(ctx) == []

    def test_missing_streak_context_is_survivable(self):
        ctx = StrategyContext(state=None, symbol="btc")
        assert strategies.get("ss_fade").evaluate(ctx) == []


# ═══════════════════════════════════════════════════════════════════════════════
# JSON contract — /state and settings.js depend on this shape
# ═══════════════════════════════════════════════════════════════════════════════


class TestToJson:
    def test_shape(self):
        state = SimpleNamespace(ss_mode="fade")
        payload = strategies.to_json(state)
        assert [s["id"] for s in payload] == ["ss_fade", "ss_trend"]

        fade = payload[0]
        assert fade["enabled"] is True
        assert fade["name"] and fade["description"]
        assert [p["name"] for p in fade["params"]] == [
            "ss_fade_base_shares",
            "ss_fade_limit_cap",
            "ss_fade_streak_min",
        ]

    def test_disabled_strategy_is_reported_as_such(self):
        payload = strategies.to_json(SimpleNamespace(ss_mode="fade"))
        assert payload[1]["id"] == "ss_trend"
        assert payload[1]["enabled"] is False

    def test_params_expose_range_and_scale(self):
        """settings.js renders from this and nothing else."""
        strength = [
            p
            for s in strategies.to_json(SimpleNamespace(ss_mode="both"))
            for p in s["params"]
            if p["name"] == "ss_trend_min_strength"
        ][0]
        assert strength["scale"] == 100.0     # fraction stored, percent shown
        assert strength["max"] == 0.10
        assert strength["label"]

    def test_without_state_enabled_is_unknown(self):
        assert all(s["enabled"] is None for s in strategies.to_json())


# ═══════════════════════════════════════════════════════════════════════════════
# RuntimeField presentation metadata
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuntimeFieldJson:
    def test_defaults_to_the_field_name_as_label(self):
        assert RuntimeField("x", "float").to_json()["label"] == "x"

    def test_choice_labels_fall_back_to_the_choices(self):
        field = RuntimeField("x", "choice", choices=("a", "b"))
        assert field.to_json()["choice_labels"] == ["a", "b"]

    def test_choice_labels_are_used_when_given(self):
        field = RuntimeField(
            "x", "choice", choices=("a", "b"), choice_labels=("Uno", "Dos")
        )
        assert field.to_json()["choice_labels"] == ["Uno", "Dos"]
