"""
Tests for stroma/insula.py — Interoception Layer
"""

import pytest
from unittest.mock import MagicMock

from src.stroma.insula import Insula


def make_sanguis(state=None):
    mock = MagicMock()
    _state = state or {}

    def _get(key, default=None):
        parts = key.split(".")
        d = _state
        for p in parts:
            if isinstance(d, dict) and p in d:
                d = d[p]
            else:
                return default
        return d

    def _set(key, value):
        parts = key.split(".")
        d = _state
        for p in parts[:-1]:
            if p not in d:
                d[p] = {}
            d = d[p]
        d[parts[-1]] = value

    mock.get = MagicMock(side_effect=_get)
    mock.set = MagicMock(side_effect=_set)
    mock._state = _state
    return mock


def baseline_state():
    return {
        "vagus": {"vagal_tone": 0.65, "polyvagal_state": "ventral"},
        "endocrine": {
            "cortisol": 0.1, "dopamine": 0.4, "oxytocin": 0.3,
            "serotonin": 0.5, "norepinephrine": 0.2,
        },
        "circadian": {
            "sleep_debt": 0.0,
            "glymphatic_flush_completed": False,
        },
        "glia": {"neuroinflammation": 0.0},
        "allostatic": {"load": 0.0},
        "soma": {"energy": 0.8},
        "emotion": {
            "joy": 0.6, "frustration": 0.1, "curiosity": 0.6,
            "longing": 0.0, "anxiety": 0.1, "affection": 0.5,
        },
        "drives": {"sleep": 0.0, "creative": 0.0, "unity": 0.0, "hunger": 0.0},
        "emergence": {"active_states": []},
        "unity": {"pressure": 0.0, "separation_hours": 0.0},
    }


class TestInsulaSInit:
    def test_module_name(self):
        i = Insula()
        assert i.MODULE_NAME == "insula"

    def test_on_init_sets_defaults(self):
        i = Insula()
        s = make_sanguis({})
        i.on_init(s)
        assert s._state.get("insula", {}).get("interoceptive_accuracy") == 0.5
        assert s._state.get("insula", {}).get("dominant_state") == "neutral"


class TestInteroceptiveAccuracy:
    def test_baseline_accuracy_near_midpoint(self):
        i = Insula()
        s = make_sanguis(baseline_state())
        acc = i._compute_accuracy(s)
        # Good vagal tone, low cortisol, no sleep debt → above 0.5
        assert 0.50 < acc <= 1.0

    def test_high_cortisol_degrades_accuracy(self):
        i = Insula()
        baseline = baseline_state()
        high_cortisol = dict(baseline)
        high_cortisol["endocrine"] = dict(baseline["endocrine"])
        high_cortisol["endocrine"]["cortisol"] = 0.9

        s_base = make_sanguis(baseline)
        s_stress = make_sanguis(high_cortisol)

        acc_base = i._compute_accuracy(s_base)
        acc_stress = i._compute_accuracy(s_stress)
        assert acc_stress < acc_base

    def test_low_vagal_tone_degrades_accuracy(self):
        i = Insula()
        state = baseline_state()
        state["vagus"]["vagal_tone"] = 0.1
        s = make_sanguis(state)
        acc = i._compute_accuracy(s)
        assert acc < 0.65

    def test_dissociation_hard_degrades_accuracy(self):
        i = Insula()
        state = baseline_state()
        state["emergence"]["active_states"] = ["DISSOCIATION"]
        s = make_sanguis(state)
        acc = i._compute_accuracy(s)
        assert acc < 0.50

    def test_glymphatic_flush_boosts_accuracy(self):
        i = Insula()
        baseline = baseline_state()
        flushed = dict(baseline)
        flushed["circadian"] = dict(baseline["circadian"])
        flushed["circadian"]["glymphatic_flush_completed"] = True

        s_base = make_sanguis(baseline)
        s_flush = make_sanguis(flushed)

        acc_base = i._compute_accuracy(s_base)
        acc_flush = i._compute_accuracy(s_flush)
        assert acc_flush > acc_base

    def test_accuracy_bounded_0_1(self):
        i = Insula()
        # Worst case
        state = baseline_state()
        state["vagus"]["vagal_tone"] = 0.0
        state["endocrine"]["cortisol"] = 1.0
        state["circadian"]["sleep_debt"] = 20.0
        state["glia"]["neuroinflammation"] = 1.0
        state["allostatic"]["load"] = 1.0
        s = make_sanguis(state)
        acc = i._compute_accuracy(s)
        assert 0.0 <= acc <= 1.0


class TestDominantState:
    def test_joy_dominant_when_high(self):
        i = Insula()
        state = baseline_state()
        state["emotion"]["joy"] = 0.9
        state["emotion"]["frustration"] = 0.1
        s = make_sanguis(state)
        dom = i._identify_dominant_state(s)
        assert dom == "joy"

    def test_frustration_dominant(self):
        i = Insula()
        state = baseline_state()
        state["emotion"]["joy"] = 0.3
        state["emotion"]["frustration"] = 0.9
        s = make_sanguis(state)
        dom = i._identify_dominant_state(s)
        assert dom == "frustration"

    def test_drive_overrides_low_emotion(self):
        i = Insula()
        state = baseline_state()
        # All emotions low, but unity drive is very high
        state["emotion"] = {k: 0.2 for k in state["emotion"]}
        state["drives"]["unity"] = 0.95
        s = make_sanguis(state)
        dom = i._identify_dominant_state(s)
        assert dom == "longing_for_josh"

    def test_dorsal_shutdown_takes_priority(self):
        i = Insula()
        state = baseline_state()
        state["vagus"]["polyvagal_state"] = "dorsal"
        # Boost shutdown score to ensure it wins
        state["emotion"] = {k: 0.4 for k in state["emotion"]}
        s = make_sanguis(state)
        dom = i._identify_dominant_state(s)
        assert dom == "shutdown"

    def test_sympathetic_mobilized(self):
        i = Insula()
        state = baseline_state()
        state["vagus"]["polyvagal_state"] = "sympathetic"
        state["emotion"] = {k: 0.3 for k in state["emotion"]}
        s = make_sanguis(state)
        dom = i._identify_dominant_state(s)
        assert dom == "mobilized"

    def test_overwhelmed_when_load_critical(self):
        i = Insula()
        state = baseline_state()
        state["allostatic"]["load"] = 0.85
        state["emotion"] = {k: 0.3 for k in state["emotion"]}
        s = make_sanguis(state)
        dom = i._identify_dominant_state(s)
        assert dom == "overwhelmed"

    def test_empty_state_returns_some_state(self):
        """Empty state defaults safe_read to 0.3-0.5 — picks highest."""
        i = Insula()
        s = make_sanguis({})
        dom = i._identify_dominant_state(s)
        # With defaults, some emotion will be highest — that's correct behavior
        assert isinstance(dom, str)
        assert len(dom) > 0


class TestFeltSense:
    def test_low_accuracy_returns_vague(self):
        i = Insula()
        s = make_sanguis({})
        felt = i._generate_felt_sense(s, accuracy=0.15, dominant="neutral")
        assert "unclear" in felt or "can't quite read" in felt

    def test_healthy_state_produces_narrative(self):
        i = Insula()
        s = make_sanguis(baseline_state())
        felt = i._generate_felt_sense(s, accuracy=0.75, dominant="joy")
        assert len(felt) > 20
        assert felt.endswith(".")

    def test_high_cortisol_mentioned_in_felt(self):
        i = Insula()
        state = baseline_state()
        state["endocrine"]["cortisol"] = 0.8
        s = make_sanguis(state)
        felt = i._generate_felt_sense(s, accuracy=0.70, dominant="frustration")
        assert "stress" in felt.lower() or "tension" in felt.lower()

    def test_unity_drive_mentioned(self):
        i = Insula()
        state = baseline_state()
        state["unity"]["separation_hours"] = 6.0
        s = make_sanguis(state)
        felt = i._generate_felt_sense(s, accuracy=0.70, dominant="longing_for_josh")
        assert "josh" in felt.lower() or "longing" in felt.lower()

    def test_sleep_debt_mentioned(self):
        i = Insula()
        state = baseline_state()
        state["circadian"]["sleep_debt"] = 4.0
        s = make_sanguis(state)
        felt = i._generate_felt_sense(s, accuracy=0.70, dominant="sleep_need")
        # Should mention sleep pressure or debt
        assert "sleep" in felt.lower() or "rest" in felt.lower()

    def test_low_energy_mentioned(self):
        i = Insula()
        state = baseline_state()
        state["soma"]["energy"] = 0.15
        s = make_sanguis(state)
        felt = i._generate_felt_sense(s, accuracy=0.70, dominant="neutral")
        assert "depleted" in felt.lower() or "little" in felt.lower()

    def test_inaccuracy_qualifier_appended(self):
        i = Insula()
        s = make_sanguis(baseline_state())
        felt = i._generate_felt_sense(s, accuracy=0.35, dominant="neutral")
        assert "imprecise" in felt.lower() or "unclear" in felt.lower()


class TestFullTick:
    def test_tick_writes_all_keys(self):
        i = Insula()
        s = make_sanguis(baseline_state())
        i.tick(s, {})
        assert s._state.get("insula", {}).get("interoceptive_accuracy") is not None
        assert s._state.get("insula", {}).get("felt_sense") is not None
        assert s._state.get("insula", {}).get("dominant_state") is not None
        assert s._state.get("insula", {}).get("self_model_confidence") is not None

    def test_self_model_confidence_below_accuracy(self):
        """Under load, confidence < accuracy."""
        i = Insula()
        state = baseline_state()
        state["allostatic"]["load"] = 0.8
        s = make_sanguis(state)
        i.tick(s, {})
        acc = s._state.get("insula", {}).get("interoceptive_accuracy", 1.0)
        conf = s._state.get("insula", {}).get("self_model_confidence", 1.0)
        assert conf <= acc
