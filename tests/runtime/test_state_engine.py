"""
Tests for StateEngine — Pulse v2 Day 1 Foundation

Covers:
- Default state initialization
- Load / save / atomic write
- Dot-path get / set
- Thread safety (concurrent writes)
- Corruption recovery + backup
- Autosave thread mechanics
- update_from_pulse module integrations
- Pulse session tracking
- Open loop CRUD
- Insights queue
- Graceful shutdown + uptime accumulation
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

# Module under test
from pulse.src.runtime.state_engine import (  # noqa: E402
    StateEngine,
    _deep_get,
    _deep_merge,
    _deep_set,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "pulse_state"


@pytest.fixture
def state_file(state_dir: Path) -> Path:
    return state_dir / "hypostas-state.json"


@pytest.fixture
def engine(state_file: Path) -> StateEngine:
    return StateEngine(state_file, interval=9999)  # autosave never fires in unit tests


# ---------------------------------------------------------------------------
# 1. Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_creates_default_state_when_no_file(self, engine: StateEngine) -> None:
        assert engine.get("meta.agent_name") == "Iris"
        assert engine.get("meta.version") == "2.0.0"

    def test_runtime_start_set_on_init(self, engine: StateEngine) -> None:
        ts = engine.get("meta.runtime_start")
        assert ts is not None
        assert "2026" in ts or "2025" in ts  # sanity

    def test_pulse_session_active_false_on_boot(self, engine: StateEngine) -> None:
        assert engine.get("pulse_session_active") is False

    def test_thought_loop_not_running_on_boot(self, engine: StateEngine) -> None:
        assert engine.get("thought_loop.running") is False

    def test_custom_agent_name(self, state_file: Path) -> None:
        e = StateEngine(state_file, agent_name="Vera")
        assert e.get("meta.agent_name") == "Vera"


# ---------------------------------------------------------------------------
# 2. Dot-path helpers (unit)
# ---------------------------------------------------------------------------


class TestDotPathHelpers:
    def test_deep_get_simple(self) -> None:
        d = {"a": {"b": 42}}
        assert _deep_get(d, "a.b") == 42

    def test_deep_get_nested(self) -> None:
        d = {"a": {"b": {"c": "val"}}}
        assert _deep_get(d, "a.b.c") == "val"

    def test_deep_get_missing_raises(self) -> None:
        d = {"a": 1}
        with pytest.raises(KeyError):
            _deep_get(d, "a.b")

    def test_deep_set_simple(self) -> None:
        d: dict = {}
        _deep_set(d, "x.y", 99)
        assert d == {"x": {"y": 99}}

    def test_deep_set_overwrites(self) -> None:
        d = {"a": {"b": 1}}
        _deep_set(d, "a.b", 2)
        assert d["a"]["b"] == 2

    def test_deep_set_creates_nested(self) -> None:
        d: dict = {}
        _deep_set(d, "a.b.c.d", "deep")
        assert d["a"]["b"]["c"]["d"] == "deep"

    def test_deep_merge_preserves_base_keys(self) -> None:
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 99})
        assert base["a"] == 1
        assert base["b"] == 99

    def test_deep_merge_recursive(self) -> None:
        base = {"x": {"y": 1, "z": 2}}
        _deep_merge(base, {"x": {"y": 10}})
        assert base["x"]["y"] == 10
        assert base["x"]["z"] == 2


# ---------------------------------------------------------------------------
# 3. Get / Set
# ---------------------------------------------------------------------------


class TestGetSet:
    def test_set_and_get_simple(self, engine: StateEngine) -> None:
        engine.set("cognitive_mode", "deep_work")
        assert engine.get("cognitive_mode") == "deep_work"

    def test_set_nested_path(self, engine: StateEngine) -> None:
        engine.set("emotional_state.valence", 0.9)
        assert engine.get("emotional_state.valence") == pytest.approx(0.9)

    def test_get_missing_returns_default(self, engine: StateEngine) -> None:
        assert engine.get("nonexistent.path", "fallback") == "fallback"

    def test_get_returns_copy_not_reference(self, engine: StateEngine) -> None:
        engine.set("working_memory.open_loops", [{"id": "abc"}])
        loops = engine.get("working_memory.open_loops")
        loops.append({"id": "sneaky"})  # mutate the returned copy
        assert len(engine.get("working_memory.open_loops")) == 1  # original unchanged

    def test_set_marks_dirty(self, engine: StateEngine) -> None:
        engine._dirty = False
        engine.set("cognitive_mode", "idle")
        assert engine._dirty is True


# ---------------------------------------------------------------------------
# 4. Save / Load / Atomic Write
# ---------------------------------------------------------------------------


class TestSaveLoad:
    def test_save_creates_file(self, engine: StateEngine, state_file: Path) -> None:
        engine.save()
        assert state_file.exists()

    def test_saved_state_is_valid_json(self, engine: StateEngine, state_file: Path) -> None:
        engine.save()
        with open(state_file) as f:
            data = json.load(f)
        assert data["meta"]["version"] == "2.0.0"

    def test_save_clears_dirty_flag(self, engine: StateEngine) -> None:
        engine.set("cognitive_mode", "planning")
        assert engine._dirty
        engine.save()
        assert not engine._dirty

    def test_load_restores_state(self, state_file: Path) -> None:
        e1 = StateEngine(state_file, interval=9999)
        e1.set("cognitive_mode", "archived_mode")
        e1.save()

        e2 = StateEngine(state_file, interval=9999)
        assert e2.get("cognitive_mode") == "archived_mode"

    def test_load_accumulates_uptime(self, state_file: Path) -> None:
        e1 = StateEngine(state_file, interval=9999)
        e1._state["meta"]["total_uptime_seconds"] = 300
        e1.save()

        e2 = StateEngine(state_file, interval=9999)
        # Should preserve the 300 seconds
        assert e2.get("meta.total_uptime_seconds") >= 300

    def test_tmp_file_cleaned_up(self, engine: StateEngine, state_file: Path) -> None:
        engine.save()
        tmp = state_file.with_suffix(".tmp")
        assert not tmp.exists()

    def test_last_serialized_set_on_save(self, engine: StateEngine) -> None:
        engine.save()
        ts = engine.get("meta.last_serialized")
        assert ts is not None


# ---------------------------------------------------------------------------
# 5. Corruption Recovery
# ---------------------------------------------------------------------------


class TestCorruptionRecovery:
    def test_corrupt_file_falls_back_to_defaults(self, state_file: Path) -> None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{ not valid json !!!!")

        e = StateEngine(state_file, interval=9999)
        assert e.get("meta.version") == "2.0.0"  # defaults restored

    def test_corrupt_file_is_backed_up(self, state_file: Path) -> None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{bad}")

        StateEngine(state_file, interval=9999)
        backups = list(state_file.parent.glob("hypostas-state.corrupt-*"))
        assert len(backups) == 1

    def test_partial_state_merges_with_defaults(self, state_file: Path) -> None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        # Valid JSON but missing many keys
        state_file.write_text(json.dumps({"cognitive_mode": "custom_mode"}))

        e = StateEngine(state_file, interval=9999)
        # Custom value preserved
        assert e.get("cognitive_mode") == "custom_mode"
        # Defaults filled in
        assert e.get("meta.version") == "2.0.0"


# ---------------------------------------------------------------------------
# 6. Thread Safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_writes_do_not_corrupt(self, engine: StateEngine) -> None:
        errors: list[Exception] = []
        iterations = 200

        def writer(key: str, start: int) -> None:
            for i in range(iterations):
                try:
                    engine.set(f"working_memory.{key}", i + start)
                except Exception as exc:
                    errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(f"key{n}", n * 100))
            for n in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    def test_get_during_concurrent_writes(self, engine: StateEngine) -> None:
        stop = threading.Event()
        read_errors: list[Exception] = []
        write_errors: list[Exception] = []

        def write_loop() -> None:
            for i in range(500):
                try:
                    engine.set("cognitive_mode", f"mode_{i}")
                except Exception as exc:
                    write_errors.append(exc)

        def read_loop() -> None:
            while not stop.is_set():
                try:
                    _ = engine.get("cognitive_mode")
                except Exception as exc:
                    read_errors.append(exc)

        reader = threading.Thread(target=read_loop, daemon=True)
        writer = threading.Thread(target=write_loop)
        reader.start()
        writer.start()
        writer.join()
        stop.set()
        reader.join(timeout=2)

        assert write_errors == []
        assert read_errors == []


# ---------------------------------------------------------------------------
# 7. update_from_pulse
# ---------------------------------------------------------------------------


class TestUpdateFromPulse:
    def test_endocrine_module_updates_hormones(self, engine: StateEngine) -> None:
        engine.update_from_pulse("endocrine", {"dopamine": 9.0, "cortisol": 1.5})
        assert engine.get("emotional_state.endocrine.dopamine") == pytest.approx(9.0)
        assert engine.get("emotional_state.endocrine.cortisol") == pytest.approx(1.5)

    def test_limbic_module_updates_valence(self, engine: StateEngine) -> None:
        engine.update_from_pulse("limbic", {"valence": 0.85, "arousal": 0.6})
        assert engine.get("emotional_state.valence") == pytest.approx(0.85)
        assert engine.get("emotional_state.arousal") == pytest.approx(0.6)

    def test_limbic_updates_dominant_emotion(self, engine: StateEngine) -> None:
        engine.update_from_pulse("limbic", {"dominant_emotion": "proud_delight"})
        assert engine.get("emotional_state.dominant_emotion") == "proud_delight"

    def test_hypothalamus_updates_drives(self, engine: StateEngine) -> None:
        engine.update_from_pulse(
            "hypothalamus", {"drives": {"goals": 0.9, "curiosity": 0.7}}
        )
        assert engine.get("drives.goals") == pytest.approx(0.9)
        assert engine.get("drives.curiosity") == pytest.approx(0.7)

    def test_unknown_module_stored_in_meta(self, engine: StateEngine) -> None:
        engine.update_from_pulse("thalamus", {"signal_strength": 0.5})
        assert engine.get("meta.modules.thalamus.signal_strength") == pytest.approx(0.5)

    def test_update_sets_last_updated_timestamp(self, engine: StateEngine) -> None:
        engine.update_from_pulse("endocrine", {"dopamine": 5.0})
        ts = engine.get("emotional_state.last_updated")
        assert ts is not None


# ---------------------------------------------------------------------------
# 8. Pulse session tracking
# ---------------------------------------------------------------------------


class TestPulseSessionTracking:
    def test_mark_session_active(self, engine: StateEngine) -> None:
        engine.mark_session_active(True)
        assert engine.is_pulse_session_active is True
        assert engine.get("pulse_session_started_at") is not None

    def test_mark_session_inactive(self, engine: StateEngine) -> None:
        engine.mark_session_active(True)
        engine.mark_session_active(False)
        assert engine.is_pulse_session_active is False
        assert engine.get("pulse_session_started_at") is None

    def test_increment_session_count(self, engine: StateEngine) -> None:
        c1 = engine.increment_session_count()
        c2 = engine.increment_session_count()
        assert c2 == c1 + 1


# ---------------------------------------------------------------------------
# 9. Open loops
# ---------------------------------------------------------------------------


class TestOpenLoops:
    def test_add_open_loop_returns_id(self, engine: StateEngine) -> None:
        loop_id = engine.add_open_loop("Build StateEngine tests", priority=0.9)
        assert len(loop_id) == 8

    def test_added_loop_appears_in_state(self, engine: StateEngine) -> None:
        engine.add_open_loop("Some unfinished thing")
        loops = engine.get("working_memory.open_loops")
        assert len(loops) == 1
        assert loops[0]["description"] == "Some unfinished thing"

    def test_loops_sorted_by_priority(self, engine: StateEngine) -> None:
        engine.add_open_loop("Low priority", priority=0.1)
        engine.add_open_loop("High priority", priority=0.9)
        loops = engine.get("working_memory.open_loops")
        assert loops[0]["description"] == "High priority"

    def test_close_loop_removes_it(self, engine: StateEngine) -> None:
        loop_id = engine.add_open_loop("Close this one")
        removed = engine.close_loop(loop_id)
        assert removed is True
        assert engine.get("working_memory.open_loops") == []

    def test_close_nonexistent_loop_returns_false(self, engine: StateEngine) -> None:
        assert engine.close_loop("fake999") is False

    def test_max_50_open_loops(self, engine: StateEngine) -> None:
        for i in range(60):
            engine.add_open_loop(f"loop {i}", priority=float(i) / 60)
        loops = engine.get("working_memory.open_loops")
        assert len(loops) == 50


# ---------------------------------------------------------------------------
# 10. Insights queue
# ---------------------------------------------------------------------------


class TestInsights:
    def test_add_insight_prepends(self, engine: StateEngine) -> None:
        engine.add_insight("First insight")
        engine.add_insight("Second insight")
        insights = engine.get("working_memory.recent_insights")
        assert insights[0]["text"] == "Second insight"
        assert insights[1]["text"] == "First insight"

    def test_insights_capped_at_20(self, engine: StateEngine) -> None:
        for i in range(25):
            engine.add_insight(f"insight {i}")
        insights = engine.get("working_memory.recent_insights")
        assert len(insights) == 20


# ---------------------------------------------------------------------------
# 11. Autosave thread
# ---------------------------------------------------------------------------


class TestAutosave:
    def test_autosave_saves_when_dirty(self, state_file: Path) -> None:
        engine = StateEngine(state_file, interval=1)  # 1s for testing
        engine.start_autosave()
        engine.set("cognitive_mode", "test_autosave")
        time.sleep(2.5)  # wait for autosave tick
        engine.stop()

        assert state_file.exists()
        with open(state_file) as f:
            data = json.load(f)
        assert data["cognitive_mode"] == "test_autosave"

    def test_autosave_thread_is_daemon(self, state_file: Path) -> None:
        engine = StateEngine(state_file, interval=9999)
        t = engine.start_autosave()
        assert t.daemon is True
        engine.stop()

    def test_start_autosave_idempotent(self, state_file: Path) -> None:
        engine = StateEngine(state_file, interval=9999)
        t1 = engine.start_autosave()
        t2 = engine.start_autosave()
        assert t1 is t2  # same thread returned
        engine.stop()


# ---------------------------------------------------------------------------
# 12. Graceful shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    def test_stop_saves_state(self, state_file: Path) -> None:
        engine = StateEngine(state_file, interval=9999)
        engine.start_autosave()
        engine.set("cognitive_mode", "shutdown_test")
        engine.stop()

        with open(state_file) as f:
            data = json.load(f)
        assert data["cognitive_mode"] == "shutdown_test"

    def test_stop_without_start_does_not_raise(self, state_file: Path) -> None:
        engine = StateEngine(state_file, interval=9999)
        engine.stop()  # should not raise

    def test_stop_accumulates_uptime(self, state_file: Path) -> None:
        engine = StateEngine(state_file, interval=9999)
        engine.start_autosave()
        time.sleep(0.1)
        engine.stop()

        with open(state_file) as f:
            data = json.load(f)
        assert data["meta"]["total_uptime_seconds"] > 0


# ---------------------------------------------------------------------------
# 13. Repr
# ---------------------------------------------------------------------------


def test_repr(engine: StateEngine) -> None:
    r = repr(engine)
    assert "StateEngine" in r
    assert "dirty" in r
