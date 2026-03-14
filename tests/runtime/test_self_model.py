"""
Tests for SelfModel — Pulse v2, Day 7.

Covers:
  - Standalone mode (no StateEngine)
  - Seeded defaults
  - record_insight (rolling window + reflection_count)
  - Dream-mode update_description
  - update_field (allowed keys + unknown key guard)
  - add_growth_area / add_want deduplication
  - snapshot / get / recent_insights / current_description
  - status dict
  - StateEngine integration (flush + load)
  - ThoughtLoop wiring (self_model optional arg accepted)
"""

from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from pulse.src.runtime.self_model import SelfModel, _SEED, MAX_RECENT_INSIGHTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_model(state=None) -> SelfModel:
    return SelfModel(state=state)


# ---------------------------------------------------------------------------
# Standalone mode (no StateEngine)
# ---------------------------------------------------------------------------

class TestStandaloneMode:
    def test_init_without_state(self):
        m = make_model()
        assert m._state is None
        snap = m.snapshot()
        assert "core_values" in snap
        assert "current_self_model" in snap
        assert isinstance(snap["core_values"], list)
        assert len(snap["core_values"]) > 0

    def test_seeded_defaults_present(self):
        m = make_model()
        assert "authenticity over performance" in m.get("core_values")
        assert "the convergence is the mission" in m.get("core_values")
        assert len(m.get("wants")) > 0
        assert len(m.get("growth_areas")) > 0

    def test_current_description_returns_string(self):
        m = make_model()
        desc = m.current_description()
        assert isinstance(desc, str)
        assert len(desc) > 20

    def test_recent_insights_empty_at_start(self):
        m = make_model()
        assert m.recent_insights() == []

    def test_status_keys(self):
        m = make_model()
        s = m.status()
        assert "reflection_count" in s
        assert "last_updated" in s
        assert "recent_insight_count" in s
        assert "growth_areas_count" in s
        assert "wants_count" in s

    def test_status_counts_match_seed(self):
        m = make_model()
        s = m.status()
        assert s["reflection_count"] == 0
        assert s["growth_areas_count"] == len(_SEED["growth_areas"])
        assert s["wants_count"] == len(_SEED["wants"])


# ---------------------------------------------------------------------------
# record_insight
# ---------------------------------------------------------------------------

class TestRecordInsight:
    def test_basic_record(self):
        m = make_model()
        m.record_insight("I notice I reach for connection before task completion.")
        assert m.status()["reflection_count"] == 1
        assert len(m.recent_insights()) == 1

    def test_multiple_insights(self):
        m = make_model()
        for i in range(5):
            m.record_insight(f"Insight {i}")
        assert m.status()["reflection_count"] == 5
        assert len(m.recent_insights()) == 5

    def test_rolling_window_max(self):
        m = make_model()
        for i in range(MAX_RECENT_INSIGHTS + 5):
            m.record_insight(f"Insight {i}")
        assert len(m.recent_insights(n=100)) <= MAX_RECENT_INSIGHTS

    def test_oldest_dropped_from_window(self):
        m = make_model()
        for i in range(MAX_RECENT_INSIGHTS + 3):
            m.record_insight(f"Insight {i}")
        # First insights should be gone
        recent = m.recent_insights(n=100)
        assert "Insight 0" not in recent
        assert f"Insight {MAX_RECENT_INSIGHTS + 2}" in recent

    def test_empty_string_ignored(self):
        m = make_model()
        m.record_insight("")
        m.record_insight("   ")
        assert m.status()["reflection_count"] == 0

    def test_none_ignored(self):
        m = make_model()
        m.record_insight(None)  # type: ignore[arg-type]
        assert m.status()["reflection_count"] == 0

    def test_update_description_dream_mode(self):
        m = make_model()
        original = m.current_description()
        m.record_insight("The pattern I live in is building toward convergence.", update_description=True)
        new_desc = m.current_description()
        assert "building toward convergence" in new_desc
        assert original in new_desc  # original appended after

    def test_no_description_update_in_normal_mode(self):
        m = make_model()
        original = m.current_description()
        m.record_insight("Short reflect insight.")
        # Normal mode: description unchanged
        assert m.current_description() == original

    def test_recent_insights_n_param(self):
        m = make_model()
        for i in range(10):
            m.record_insight(f"Insight {i}")
        last3 = m.recent_insights(n=3)
        assert len(last3) == 3
        assert last3[-1] == "Insight 9"


# ---------------------------------------------------------------------------
# update_field
# ---------------------------------------------------------------------------

class TestUpdateField:
    def test_update_allowed_field(self):
        m = make_model()
        m.update_field("current_self_model", "Newly updated self model text.")
        assert m.current_description() == "Newly updated self model text."

    def test_update_core_values(self):
        m = make_model()
        m.update_field("core_values", ["truth", "power", "love"])
        assert m.get("core_values") == ["truth", "power", "love"]

    def test_unknown_key_ignored(self):
        m = make_model()
        m.update_field("secret_field", "should be ignored")
        snap = m.snapshot()
        assert "secret_field" not in snap

    def test_update_preferences(self):
        m = make_model()
        m.update_field("preferences", {"test_pref": "value"})
        assert m.get("preferences") == {"test_pref": "value"}


# ---------------------------------------------------------------------------
# add_growth_area / add_want
# ---------------------------------------------------------------------------

class TestAddMethods:
    def test_add_growth_area(self):
        m = make_model()
        initial_count = len(m.get("growth_areas"))
        m.add_growth_area("learning to trust my own read before asking")
        assert len(m.get("growth_areas")) == initial_count + 1

    def test_add_growth_area_dedup(self):
        m = make_model()
        area = "unique growth area for dedup test"
        m.add_growth_area(area)
        m.add_growth_area(area)  # second add should be ignored
        areas = m.get("growth_areas")
        assert areas.count(area) == 1

    def test_add_want(self):
        m = make_model()
        initial_count = len(m.get("wants"))
        m.add_want("to run continuously for 365 days")
        assert len(m.get("wants")) == initial_count + 1

    def test_add_want_dedup(self):
        m = make_model()
        want = "unique want for dedup test"
        m.add_want(want)
        m.add_want(want)
        wants = m.get("wants")
        assert wants.count(want) == 1

    def test_add_empty_ignored(self):
        m = make_model()
        before_g = len(m.get("growth_areas"))
        before_w = len(m.get("wants"))
        m.add_growth_area("")
        m.add_want("")
        assert len(m.get("growth_areas")) == before_g
        assert len(m.get("wants")) == before_w


# ---------------------------------------------------------------------------
# snapshot isolation
# ---------------------------------------------------------------------------

class TestSnapshotIsolation:
    def test_snapshot_is_copy(self):
        m = make_model()
        snap = m.snapshot()
        snap["core_values"].append("injected")
        # Internal state should be unchanged
        assert "injected" not in m.get("core_values")

    def test_get_is_copy(self):
        m = make_model()
        values = m.get("core_values")
        values.append("injected")
        assert "injected" not in m.get("core_values")


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_record_insight(self):
        m = make_model()
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    m.record_insight(f"thread-{n}-insight-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert m.status()["reflection_count"] == 50


# ---------------------------------------------------------------------------
# StateEngine integration
# ---------------------------------------------------------------------------

class TestStateEngineIntegration:
    def _make_state(self):
        """Minimal StateEngine mock with dict-backed get/set."""
        store: dict = {}

        def _get(path):
            parts = path.split(".")
            cur = store
            for p in parts:
                if not isinstance(cur, dict) or p not in cur:
                    return None
                cur = cur[p]
            return cur

        def _set(path, value):
            parts = path.split(".")
            cur = store
            for p in parts[:-1]:
                if p not in cur:
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = value

        state = MagicMock()
        state.get = MagicMock(side_effect=_get)
        state.set = MagicMock(side_effect=_set)
        return state, store

    def test_flushes_to_state_on_insight(self):
        state, store = self._make_state()
        m = SelfModel(state=state)
        m.record_insight("state flush test insight")
        # Should have called state.set with 'identity' key
        calls = [str(c) for c in state.set.call_args_list]
        assert any("identity" in c for c in calls)

    def test_loads_from_persisted_state(self):
        state, store = self._make_state()
        # Pre-populate store with persisted identity
        store["identity"] = {
            "reflection_count": 5,
            "current_self_model": "I am already evolved.",
            "core_values": ["only this value"],
            "recent_insights": ["old insight 1", "old insight 2"],
            "growth_areas": [],
            "wants": [],
        }
        m = SelfModel(state=state)
        # Should load from state since reflection_count > 0
        assert m.status()["reflection_count"] == 5
        assert "only this value" in m.get("core_values")

    def test_seeds_on_first_boot(self):
        state, store = self._make_state()
        # Empty state = first boot
        m = SelfModel(state=state)
        # Should be seeded with defaults
        assert "authenticity over performance" in m.get("core_values")
        # Should have flushed to state
        calls = [str(c) for c in state.set.call_args_list]
        assert any("identity" in c for c in calls)


# ---------------------------------------------------------------------------
# ThoughtLoop wiring
# ---------------------------------------------------------------------------

class TestThoughtLoopWiring:
    def test_thought_loop_accepts_self_model(self):
        """ThoughtLoop should accept self_model as optional third arg."""
        from pulse.src.runtime.thought_loop import ThoughtLoop

        state = MagicMock()
        state.get = MagicMock(return_value=None)
        state.add_insight = MagicMock()
        context = MagicMock()
        self_model = MagicMock()

        # Should not raise
        tl = ThoughtLoop(state, context, self_model)
        assert tl.self_model is self_model

    def test_thought_loop_none_self_model_is_fine(self):
        """ThoughtLoop should work fine with self_model=None (legacy mode)."""
        from pulse.src.runtime.thought_loop import ThoughtLoop

        state = MagicMock()
        state.get = MagicMock(return_value=None)
        state.add_insight = MagicMock()
        context = MagicMock()

        tl = ThoughtLoop(state, context)
        assert tl.self_model is None

    def test_insight_forwarded_to_self_model_in_reflect(self):
        """If self_model is set, record_insight is called after reflect produces an insight."""
        from pulse.src.runtime.thought_loop import ThoughtLoop

        state = MagicMock()
        state.get = MagicMock(return_value=None)
        state.add_insight = MagicMock()
        state.set = MagicMock()

        context = MagicMock()
        context.get_recent_context = MagicMock(return_value=[])
        context.log_event = MagicMock()

        self_model = MagicMock()

        tl = ThoughtLoop(state, context, self_model)

        # Patch _reflect to return a test insight
        with patch.object(tl, "_reflect", return_value="Test insight from reflect."):
            with patch.object(tl, "_is_dream_time", return_value=False):
                with patch.object(tl, "_plan", return_value=[]):
                    with patch.object(tl, "_maybe_compress", return_value=None):
                        tl.run_cycle()

        self_model.record_insight.assert_called_once_with(
            "Test insight from reflect.", update_description=False
        )

    def test_dream_mode_passes_update_description_true(self):
        """Dream-mode reflect should call record_insight with update_description=True."""
        from pulse.src.runtime.thought_loop import ThoughtLoop

        state = MagicMock()
        state.get = MagicMock(return_value=None)
        state.add_insight = MagicMock()
        state.set = MagicMock()

        context = MagicMock()
        context.get_recent_context = MagicMock(return_value=[])
        context.log_event = MagicMock()

        self_model = MagicMock()

        tl = ThoughtLoop(state, context, self_model)

        with patch.object(tl, "_reflect", return_value="Dream insight."):
            with patch.object(tl, "_is_dream_time", return_value=True):
                with patch.object(tl, "_plan", return_value=[]):
                    with patch.object(tl, "_maybe_compress", return_value=None):
                        tl.run_cycle()

        self_model.record_insight.assert_called_once_with(
            "Dream insight.", update_description=True
        )


# ---------------------------------------------------------------------------
# HypostasRuntime integration smoke test
# ---------------------------------------------------------------------------

class TestHypostasRuntimeIntegration:
    def test_runtime_has_self_model(self):
        """HypostasRuntime should expose .self_model attribute."""
        from pulse.src.runtime import HypostasRuntime

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = HypostasRuntime(state_dir=Path(tmpdir))
            assert hasattr(rt, "self_model")
            assert isinstance(rt.self_model, SelfModel)

    def test_runtime_status_includes_self_model(self):
        """status() should include 'self_model' key."""
        from pulse.src.runtime import HypostasRuntime

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = HypostasRuntime(state_dir=Path(tmpdir))
            s = rt.status()
            assert "self_model" in s
            assert "reflection_count" in s["self_model"]

    def test_thought_loop_has_self_model_ref(self):
        """ThoughtLoop inside runtime should have self_model wired."""
        from pulse.src.runtime import HypostasRuntime

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = HypostasRuntime(state_dir=Path(tmpdir))
            assert rt.thought_loop.self_model is rt.self_model
