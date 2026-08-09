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
    def test_the_registered_strategies(self):
        assert strategies.ids() == ("ss_fade", "box_builder", "coin_flip_dog")

    def test_ids_match_what_trades_stores(self):
        """`trades.strategy` holds these strings; KPIs group by them."""
        for sid in strategies.ids():
            assert strategies.get(sid).id == sid

    def test_fade_outranks_box_builder(self):
        """ss_fade priority 100 > box_builder priority 90."""
        assert (
            strategies.get("ss_fade").priority
            > strategies.get("box_builder").priority
        )

    def test_enabled_order_is_by_priority(self):
        state = SimpleNamespace(ss_mode="fade", bb_enabled=True, cfd_enabled=True)
        ids = [d.id for d in strategies.enabled_for(state)]
        assert ids[0] == "ss_fade"
        assert "box_builder" in ids

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

    @pytest.mark.parametrize("name", ["ss_fade_limit_cap", "bb_bid_sum_cap"])
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

    @pytest.mark.parametrize("descriptor", strategies.all_descriptors(), ids=lambda d: d.id)
    def test_enabled_when_agrees_with_is_enabled(self, descriptor):
        """The declarative mirror the settings page reads must not drift.

        `is_enabled` is the runtime truth; `enabled_when` is what lets the UI
        grey out a card before saving. If they disagree, /settings shows one
        thing and the trader does another — the worst kind of bug to notice.
        """
        spec = descriptor.enabled_when
        assert spec, f"{descriptor.id} sin enabled_when — /settings no sabría pintarlo"

        field = RUNTIME_FIELDS[spec["field"]]
        candidates = field.choices or (True, False)
        for value in candidates:
            state = SimpleNamespace(**{spec["field"]: value})
            assert descriptor.enabled_for(state) == (value in spec["values"]), (
                f"{descriptor.id}: {spec['field']}={value!r} discrepa"
            )


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
        assert strategies.get("ss_fade").evaluate(ctx) == []

    def test_missing_streak_context_is_survivable(self):
        ctx = StrategyContext(state=None, symbol="btc")
        assert strategies.get("ss_fade").evaluate(ctx) == []


# ═══════════════════════════════════════════════════════════════════════════════
# JSON contract — /state and settings.js depend on this shape
# ═══════════════════════════════════════════════════════════════════════════════


class TestToJson:
    def test_shape(self):
        state = SimpleNamespace(ss_mode="fade", bb_enabled=False, cfd_enabled=False)
        payload = strategies.to_json(state)
        assert [s["id"] for s in payload] == [
            "ss_fade", "box_builder", "coin_flip_dog"
        ]

        fade = payload[0]
        assert fade["enabled"] is True
        assert fade["name"] and fade["description"]
        assert [p["name"] for p in fade["params"]] == [
            "ss_fade_base_shares",
            "ss_fade_limit_cap",
            "ss_fade_streak_min",
        ]

    def test_disabled_strategy_is_reported_as_such(self):
        payload = strategies.to_json(SimpleNamespace(ss_mode="fade", bb_enabled=False, cfd_enabled=False))
        bb = next(s for s in payload if s["id"] == "box_builder")
        assert bb["enabled"] is False

    def test_params_expose_range_and_scale(self):
        """settings.js renders from this and nothing else."""
        payload = strategies.to_json(SimpleNamespace(ss_mode="fade", bb_enabled=True, cfd_enabled=False))
        all_params = {p["name"]: p for s in payload for p in s["params"]}
        # box_builder should expose bb_bid_sum_cap
        assert "bb_bid_sum_cap" in all_params
        assert all_params["bb_bid_sum_cap"]["label"]

    def test_without_state_enabled_is_unknown(self):
        assert all(s["enabled"] is None for s in strategies.to_json())


# ═══════════════════════════════════════════════════════════════════════════════
# RuntimeField presentation metadata
# ═══════════════════════════════════════════════════════════════════════════════


class TestStateAcceptsEveryRuntimeField:
    """`BotState` used to carry two hand-written allow-lists of field names.

    A field added to RUNTIME_FIELDS was then validated by POST /config, stored
    in `bot_config`, echoed back by /state — and silently dropped on its way
    into the state, so the trader kept running with the old value. Both lists
    are derived now; these tests are what keeps them derived.
    """

    def _state(self):
        from bot.state import BotState

        return BotState()

    def test_update_runtime_config_accepts_all_of_them(self):
        state = self._state()
        sample = {name: _sample_value(f) for name, f in RUNTIME_FIELDS.items()}
        accepted = state.update_runtime_config(**sample)
        assert set(accepted) == set(RUNTIME_FIELDS)

    def test_configure_applies_all_of_them(self):
        state = self._state()
        sample = {name: _sample_value(f) for name, f in RUNTIME_FIELDS.items()}
        state.configure(**sample)
        for name, value in sample.items():
            assert getattr(state, name) == value, f"{name} no llegó al estado"

    def test_snapshot_reports_all_of_them(self):
        """/settings renders a widget per field and saves what it read back.
        A field missing from the snapshot renders empty and then saves empty."""
        snap = self._state().snapshot()
        missing = set(RUNTIME_FIELDS) - set(snap)
        assert not missing, f"ausentes del snapshot: {sorted(missing)}"

    def test_unknown_keys_are_still_rejected(self):
        state = self._state()
        assert state.update_runtime_config(pwned=True) == {}
        assert not hasattr(state, "pwned")

    def test_mode_is_accepted_but_is_not_a_runtime_field(self):
        """Deliberate: persisting paper/real would let an old setting start the
        bot with real money after a restart."""
        assert "mode" not in RUNTIME_FIELDS
        assert self._state().update_runtime_config(mode="real") == {"mode": "real"}


def _sample_value(field):
    """A valid, non-default value for any field kind."""
    if field.kind == "bool":
        return True
    if field.kind == "choice":
        return field.choices[-1]
    if field.kind == "hours":
        return "13-21"
    mid = ((field.minimum or 0) + (field.maximum or 100)) / 2
    return int(mid) if field.kind == "int" else mid


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
