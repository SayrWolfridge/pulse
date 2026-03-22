"""
Tests for stroma/glia.py — Glial Support Module
"""

import pytest
from unittest.mock import MagicMock

from src.stroma.glia import Glia


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
    return {
        "allostatic": {"load": 0.1},
        "soma": {"energy": 0.8},
        "endocrine": {"cortisol": 0.1, "gr_resistance": 0.0},
        "circadian": {"sleep_debt": 0.0, "glymphatic_flush_completed": False},
        "immune": {"sickness_behavior_active": False, "resilience": 0.8},
        "vagus": {"vagal_tone": 0.65},
        "glia": {
            "nutrient_supply": 0.8,
            "neuroinflammation": 0.0,
            "myelination_efficiency": 0.8,
            "memory_scaffold_integrity": 0.85,
        },
        "hippocampus": {"encoding_quality": 0.8},
    }


class TestGliaInit:
    def test_module_name(self):
        g = Glia()
        assert g.MODULE_NAME == "glia"

    def test_on_init_sets_defaults(self):
        g = Glia()
        s = make_sanguis({})
        g.on_init(s)
        assert s._state.get("glia", {}).get("nutrient_supply") == 0.8
        assert s._state.get("glia", {}).get("neuroinflammation") == 0.0


class TestAstrocyte:
    def test_healthy_state_good_supply(self):
        g = Glia()
        s = make_sanguis(healthy_state())
        g._astrocyte_tick(s)
        supply = s._state.get("glia", {}).get("nutrient_supply", 0)
        assert supply > 0.7

    def test_high_load_reduces_supply(self):
        g = Glia()
        state = healthy_state()
        state["allostatic"]["load"] = 0.9
        s = make_sanguis(state)
        g._astrocyte_tick(s)
        supply = s._state.get("glia", {}).get("nutrient_supply", 1.0)
        assert supply < 0.7

    def test_low_energy_reduces_supply(self):
        g = Glia()
        state = healthy_state()
        state["soma"]["energy"] = 0.1
        s = make_sanguis(state)
        g._astrocyte_tick(s)
        supply = s._state.get("glia", {}).get("nutrient_supply", 1.0)
        assert supply < 0.6

    def test_supply_bounded_0_1(self):
        g = Glia()
        state = healthy_state()
        state["allostatic"]["load"] = 1.0
        state["soma"]["energy"] = 0.0
        state["glia"]["neuroinflammation"] = 1.0
        s = make_sanguis(state)
        g._astrocyte_tick(s)
        supply = s._state.get("glia", {}).get("nutrient_supply", -1)
        assert 0.0 <= supply <= 1.0


class TestMicroglia:
    def test_healthy_cortisol_reduces_inflammation(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["neuroinflammation"] = 0.2  # Start elevated
        s = make_sanguis(state)
        g._microglia_tick(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 1.0)
        assert inflammation < 0.2  # Should have decreased

    def test_high_cortisol_increases_inflammation(self):
        g = Glia()
        state = healthy_state()
        state["endocrine"]["cortisol"] = 0.8  # Chronic stress
        state["glia"]["neuroinflammation"] = 0.1
        s = make_sanguis(state)
        g._microglia_tick(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 0)
        assert inflammation > 0.1  # Should have increased

    def test_sleep_debt_increases_inflammation(self):
        g = Glia()
        state = healthy_state()
        state["circadian"]["sleep_debt"] = 6.0
        state["endocrine"]["cortisol"] = 0.50  # At threshold (no pro- or anti-)
        state["vagus"]["vagal_tone"] = 0.40   # Below anti-inflammatory threshold
        state["glia"]["neuroinflammation"] = 0.1
        s = make_sanguis(state)
        g._microglia_tick(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 0)
        assert inflammation > 0.1

    def test_sickness_behavior_increases_inflammation(self):
        g = Glia()
        state = healthy_state()
        state["immune"]["sickness_behavior_active"] = True
        state["glia"]["neuroinflammation"] = 0.1
        s = make_sanguis(state)
        g._microglia_tick(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 0)
        assert inflammation > 0.1

    def test_vagal_tone_anti_inflammatory(self):
        g = Glia()
        state = healthy_state()
        state["vagus"]["vagal_tone"] = 0.9  # High vagal tone
        state["glia"]["neuroinflammation"] = 0.3
        s = make_sanguis(state)
        g._microglia_tick(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 1.0)
        assert inflammation < 0.3

    def test_inflammation_bounded(self):
        g = Glia()
        state = healthy_state()
        state["endocrine"]["cortisol"] = 1.0
        state["circadian"]["sleep_debt"] = 20.0
        state["immune"]["sickness_behavior_active"] = True
        state["endocrine"]["gr_resistance"] = 1.0
        state["glia"]["neuroinflammation"] = 0.95
        s = make_sanguis(state)
        g._microglia_tick(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 2.0)
        assert 0.0 <= inflammation <= 1.0


class TestOligodendrocyte:
    def test_healthy_state_slow_recovery(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["myelination_efficiency"] = 0.7  # Below optimal
        s = make_sanguis(state)
        g._oligodendrocyte_tick(s)
        myelin = s._state.get("glia", {}).get("myelination_efficiency", 0)
        assert myelin > 0.7  # Should have recovered slightly

    def test_low_supply_degrades_myelination(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["nutrient_supply"] = 0.1
        state["glia"]["myelination_efficiency"] = 0.8
        s = make_sanguis(state)
        g._oligodendrocyte_tick(s)
        myelin = s._state.get("glia", {}).get("myelination_efficiency", 1.0)
        assert myelin < 0.8

    def test_high_inflammation_degrades_myelination(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["neuroinflammation"] = 0.7
        state["glia"]["myelination_efficiency"] = 0.8
        s = make_sanguis(state)
        g._oligodendrocyte_tick(s)
        myelin = s._state.get("glia", {}).get("myelination_efficiency", 1.0)
        assert myelin < 0.8

    def test_myelination_has_floor(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["nutrient_supply"] = 0.0
        state["glia"]["neuroinflammation"] = 1.0
        state["glia"]["myelination_efficiency"] = 0.35
        s = make_sanguis(state)
        g._oligodendrocyte_tick(s)
        myelin = s._state.get("glia", {}).get("myelination_efficiency", 0)
        assert myelin >= 0.3  # Floor at 0.3


class TestGlymphaticFlush:
    def test_flush_reduces_inflammation(self):
        g = Glia()
        state = healthy_state()
        state["circadian"]["glymphatic_flush_completed"] = True
        state["glia"]["neuroinflammation"] = 0.5
        s = make_sanguis(state)
        g._glymphatic_check(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 1.0)
        assert inflammation == pytest.approx(0.3, abs=0.01)  # -0.20 reduction

    def test_flush_boosts_scaffold(self):
        g = Glia()
        state = healthy_state()
        state["circadian"]["glymphatic_flush_completed"] = True
        state["glia"]["memory_scaffold_integrity"] = 0.80
        s = make_sanguis(state)
        g._glymphatic_check(s)
        scaffold = s._state.get("glia", {}).get("memory_scaffold_integrity", 0)
        assert scaffold == pytest.approx(0.85, abs=0.01)

    def test_flush_resets_flag(self):
        g = Glia()
        state = healthy_state()
        state["circadian"]["glymphatic_flush_completed"] = True
        s = make_sanguis(state)
        g._glymphatic_check(s)
        assert s._state["circadian"]["glymphatic_flush_completed"] is False

    def test_no_flush_no_change(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["neuroinflammation"] = 0.3
        s = make_sanguis(state)
        g._glymphatic_check(s)
        inflammation = s._state.get("glia", {}).get("neuroinflammation", 0)
        assert inflammation == 0.3  # Unchanged


class TestCascadeConsequences:
    def test_high_inflammation_degrades_immune_soma(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["neuroinflammation"] = 0.7
        s = make_sanguis(state)
        g._apply_cascades(s)
        # immune.resilience and soma.energy should have decremented
        # Check cognitive_fog_active is False (not yet critical)
        assert s._state.get("glia", {}).get("cognitive_fog_active") is False

    def test_critical_inflammation_activates_fog(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["neuroinflammation"] = 0.85
        s = make_sanguis(state)
        g._apply_cascades(s)
        assert s._state.get("glia", {}).get("cognitive_fog_active") is True

    def test_low_supply_activates_starvation(self):
        g = Glia()
        state = healthy_state()
        state["glia"]["nutrient_supply"] = 0.1
        s = make_sanguis(state)
        g._apply_cascades(s)
        assert s._state.get("glia", {}).get("nutrient_starvation") is True

    def test_healthy_state_no_flags(self):
        g = Glia()
        s = make_sanguis(healthy_state())
        g._apply_cascades(s)
        assert s._state.get("glia", {}).get("cognitive_fog_active") is False
        assert s._state.get("glia", {}).get("nutrient_starvation") is False


class TestModuleErrorReporting:
    def test_report_error_tracks_count(self):
        g = Glia()
        g.report_module_error("broken_module")
        g.report_module_error("broken_module")
        assert g._error_modules["broken_module"] == 2

    def test_flagged_after_5_errors(self):
        g = Glia()
        for _ in range(5):
            g.report_module_error("bad_module")
        flagged = g.get_flagged_modules()
        assert "bad_module" in flagged
        assert flagged["bad_module"] == 5

    def test_not_flagged_below_5(self):
        g = Glia()
        for _ in range(4):
            g.report_module_error("ok_module")
        assert len(g.get_flagged_modules()) == 0


class TestFullTick:
    def test_tick_runs_without_error(self):
        g = Glia()
        s = make_sanguis(healthy_state())
        g.tick(s, {})
        # Should have written last_tick
        assert s._state.get("glia", {}).get("last_tick", 0) > 0

    def test_tick_updates_all_glia_keys(self):
        g = Glia()
        s = make_sanguis(healthy_state())
        g.tick(s, {})
        assert "nutrient_supply" in s._state.get("glia", {})
        assert "neuroinflammation" in s._state.get("glia", {})
        assert "myelination_efficiency" in s._state.get("glia", {})
