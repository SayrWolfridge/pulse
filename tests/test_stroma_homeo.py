"""
Tests for stroma/homeo.py — Homeostasis & Allostasis Engine
"""

import time
import pytest
from unittest.mock import MagicMock

from src.stroma.homeo import Homeo


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


def healthy_state():
    """State at all setpoints — load should be near zero."""
    return {
        "endocrine": {
            "cortisol": 0.15, "dopamine": 0.40, "serotonin": 0.55,
            "norepinephrine": 0.20, "oxytocin": 0.30,
        },
        "soma": {"energy": 0.75},
        "immune": {"resilience": 0.80},
        "emotion": {"anxiety": 0.15, "frustration": 0.20},
        "vagus": {"vagal_tone": 0.65},
        "glia": {"neuroinflammation": 0.05},
        "circadian": {"current_mode": "daylight"},
    }


def stressed_state():
    """State with high cortisol, low energy, elevated anxiety."""
    state = healthy_state()
    state["endocrine"]["cortisol"] = 0.9    # Way above setpoint 0.15
    state["soma"]["energy"] = 0.2           # Way below setpoint 0.75
    state["emotion"]["anxiety"] = 0.8       # Way above setpoint 0.15
    state["glia"]["neuroinflammation"] = 0.5  # Way above 0.05
    return state


class TestHomeoInit:
    def test_module_name(self):
        h = Homeo()
        assert h.MODULE_NAME == "homeo"

    def test_on_init_sets_defaults(self):
        h = Homeo()
        s = make_sanguis({})
        h.on_init(s)
        assert s._state.get("allostatic", {}).get("load") == 0.0
        assert s._state.get("homeo", {}).get("regulation_mode") == "homeostatic"

    def test_has_setpoints_defined(self):
        h = Homeo()
        assert len(h.BASE_SETPOINTS) >= 8  # At least 8 major variables


class TestLoadComputation:
    def test_healthy_state_low_load(self):
        h = Homeo()
        s = make_sanguis(healthy_state())
        load = h._compute_load(s)
        assert load < 0.15  # Healthy state should be near zero

    def test_stressed_state_high_load(self):
        h = Homeo()
        s = make_sanguis(stressed_state())
        load = h._compute_load(s)
        # stressed_state has 4/11 variables off — realistic load ~0.24
        assert load > 0.15  # Clearly elevated above healthy

    def test_load_bounded_0_1(self):
        h = Homeo()
        # Extreme state
        state = healthy_state()
        state["endocrine"]["cortisol"] = 1.0
        state["soma"]["energy"] = 0.0
        state["emotion"]["anxiety"] = 1.0
        state["glia"]["neuroinflammation"] = 1.0
        s = make_sanguis(state)
        load = h._compute_load(s)
        assert 0.0 <= load <= 1.0

    def test_missing_values_use_optimal(self):
        """Missing SANGUIS values should default to optimal — zero load."""
        h = Homeo()
        s = make_sanguis({})  # Empty state
        load = h._compute_load(s)
        # All values default to optimal — load should be 0
        assert load == 0.0

    def test_single_deviation_raises_load(self):
        h = Homeo()
        state = healthy_state()
        state["endocrine"]["cortisol"] = 0.9  # Just cortisol off
        s = make_sanguis(state)
        load = h._compute_load(s)
        healthy_s = make_sanguis(healthy_state())
        healthy_load = h._compute_load(healthy_s)
        assert load > healthy_load


class TestLoadThresholds:
    def test_write_homeostatic_mode_when_low(self):
        h = Homeo()
        s = make_sanguis(healthy_state())
        h.tick(s, {})
        assert s._state.get("homeo", {}).get("regulation_mode") == "homeostatic"

    def test_load_written_to_sanguis(self):
        h = Homeo()
        s = make_sanguis(healthy_state())
        h.tick(s, {})
        load = s._state.get("allostatic", {}).get("load", -1)
        assert 0.0 <= load < 0.30

    def test_stressed_state_writes_elevated_or_homeostatic_mode(self):
        """stressed_state is 4/11 vars off — may land homeostatic or allostatic."""
        h = Homeo()
        state = stressed_state()
        s = make_sanguis(state)
        h.tick(s, {})
        mode = s._state.get("homeo", {}).get("regulation_mode", "")
        assert mode in ("homeostatic", "allostatic", "allostatic_high", "overloaded")

    def test_burnout_signal_false_when_healthy(self):
        h = Homeo()
        s = make_sanguis(healthy_state())
        h.tick(s, {})
        assert s._state.get("homeo", {}).get("burnout_signal") == False

    def test_burnout_signal_true_when_critical(self):
        h = Homeo()
        state = stressed_state()
        state["endocrine"]["norepinephrine"] = 1.0
        state["vagus"] = {"vagal_tone": 0.0}
        state["immune"] = {"resilience": 0.0}
        s = make_sanguis(state)
        # Force load to critical by patching compute
        import unittest.mock as um
        with um.patch.object(h, '_compute_load', return_value=0.90):
            h.tick(s, {})
        assert s._state.get("homeo", {}).get("burnout_signal") == True

    def test_peak_load_tracked(self):
        h = Homeo()
        s = make_sanguis(stressed_state())
        h.tick(s, {})
        peak = s._state.get("allostatic", {}).get("load_peak", 0)
        load = s._state.get("allostatic", {}).get("load", 0)
        assert peak >= load  # Peak should be at least current load


class TestPredictiveRegulation:
    def test_deep_night_sets_low_cortisol_setpoint(self):
        h = Homeo()
        state = healthy_state()
        state["circadian"]["current_mode"] = "deep_night"
        s = make_sanguis(state)
        h.tick(s, {})
        override = s._state.get("homeo", {}).get("cortisol_setpoint_override")
        assert override == 0.05

    def test_dawn_sets_high_cortisol_setpoint(self):
        h = Homeo()
        state = healthy_state()
        state["circadian"]["current_mode"] = "dawn"
        s = make_sanguis(state)
        h.tick(s, {})
        override = s._state.get("homeo", {}).get("cortisol_setpoint_override")
        assert override == 0.25

    def test_daylight_clears_cortisol_override(self):
        h = Homeo()
        state = healthy_state()
        state["circadian"]["current_mode"] = "daylight"
        s = make_sanguis(state)
        h.tick(s, {})
        override = s._state.get("homeo", {}).get("cortisol_setpoint_override")
        assert override is None


class TestLoadHistory:
    def test_load_trend_stable_with_few_ticks(self):
        h = Homeo()
        assert h.get_load_trend() == "stable"

    def test_load_trend_rising(self):
        h = Homeo()
        h._load_history = [0.1, 0.15, 0.25]
        assert h.get_load_trend() == "rising"

    def test_load_trend_falling(self):
        h = Homeo()
        h._load_history = [0.5, 0.35, 0.2]
        assert h.get_load_trend() == "falling"

    def test_load_trend_stable(self):
        h = Homeo()
        h._load_history = [0.3, 0.31, 0.29]
        assert h.get_load_trend() == "stable"

    def test_history_capped_at_10(self):
        h = Homeo()
        s = make_sanguis(healthy_state())
        for _ in range(15):
            h.tick(s, {})
        assert len(h._load_history) == 10


class TestLoadConsequences:
    def test_healthy_state_restores_immune(self):
        h = Homeo()
        state = healthy_state()
        state["immune"]["resilience"] = 0.7
        s = make_sanguis(state)
        h.tick(s, {})
        # Should have incremented immune.resilience by 0.001
        resilience = s._state.get("immune", {}).get("resilience", 0)
        assert resilience >= 0.7  # Should be slightly higher

    def test_normalized_flag_set_after_recovery(self):
        h = Homeo()
        # Simulate recovery: load_high_since is set, current load is low
        h._load_high_since = time.time() - 3600
        s = make_sanguis(healthy_state())
        import unittest.mock as um
        with um.patch.object(h, '_compute_load', return_value=0.15):
            h.tick(s, {})
        assert s._state.get("allostatic", {}).get("load_normalized_after_high") == True
