"""
Tests for GoalEngine — Pulse v2 Day 8
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pulse.src.runtime.goal_engine import (
    GoalEngine,
    _SEED_GOALS,
    _extract_blockers,
    _extract_note,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> GoalEngine:
    """GoalEngine with no StateEngine and no disk file (uses seed)."""
    import pulse.src.runtime.goal_engine as mod

    # Ensure local machine goals.json doesn't affect tests
    monkeypatch.setattr(mod, "_GOALS_JSON_CANDIDATES", [Path("/nonexistent/goals.json")])

    ge = GoalEngine(state=None)
    ge.load()
    return ge


@pytest.fixture()
def mock_state() -> MagicMock:
    state = MagicMock()
    state.set = MagicMock()
    state.get = MagicMock(return_value=None)
    return state


@pytest.fixture()
def engine_with_state(monkeypatch: pytest.MonkeyPatch, mock_state: MagicMock) -> GoalEngine:
    import pulse.src.runtime.goal_engine as mod

    monkeypatch.setattr(mod, "_GOALS_JSON_CANDIDATES", [Path("/nonexistent/goals.json")])

    ge = GoalEngine(state=mock_state)
    ge.load()
    return ge


@pytest.fixture()
def goals_json_file(tmp_path: Path) -> Path:
    """Write a minimal goals.json and return its path."""
    goals = {
        "goals": [
            {
                "id": "g001",
                "title": "Ship it",
                "priority": 1,
                "type": "business",
                "status": "active",
                "blocked_on": "deploy",
                "progress": [],
            },
            {
                "id": "g002",
                "title": "Clean up tech debt",
                "priority": 2,
                "type": "build",
                "status": "active",
                "blocked_on": None,
                "progress": [{"date": "2026-03-01", "note": "50% done"}],
            },
            {
                "id": "g003",
                "title": "Old completed goal",
                "priority": 3,
                "type": "general",
                "status": "completed",
                "progress": [],
            },
        ]
    }
    p = tmp_path / "goals.json"
    p.write_text(json.dumps(goals))
    return p


# ---------------------------------------------------------------------------
# Load / seed
# ---------------------------------------------------------------------------


class TestLoad:
    def test_seed_loads_when_no_file(self, engine: GoalEngine) -> None:
        assert engine._loaded is True
        assert len(engine._goals) == len(_SEED_GOALS)

    def test_seed_all_active_or_expected_status(self, engine: GoalEngine) -> None:
        statuses = {g["status"] for g in engine._goals}
        assert "active" in statuses

    def test_load_from_file(self, goals_json_file: Path) -> None:
        ge = GoalEngine(state=None)
        with patch.object(
            type(ge),
            "_GOALS_JSON_CANDIDATES" if hasattr(type(ge), "_GOALS_JSON_CANDIDATES") else "_read_goals_file",
        ):
            pass  # we'll test via _read_goals_file directly
        # Directly patch the candidates list
        ge2 = GoalEngine(state=None)
        import pulse.src.runtime.goal_engine as mod
        original = mod._GOALS_JSON_CANDIDATES
        mod._GOALS_JSON_CANDIDATES = [goals_json_file]
        try:
            ge2.load()
            assert ge2._loaded is True
            assert len(ge2._goals) == 3
        finally:
            mod._GOALS_JSON_CANDIDATES = original

    def test_loaded_flag_false_before_load(self) -> None:
        ge = GoalEngine()
        assert ge._loaded is False

    def test_normalise_fills_defaults(self) -> None:
        raw = [{"title": "Bare goal"}]
        result = GoalEngine._normalise(raw)
        assert len(result) == 1
        g = result[0]
        assert "id" in g
        assert g["status"] == "active"
        assert g["priority"] == 2
        assert g["blockers"] == []

    def test_normalise_skips_non_dicts(self) -> None:
        raw = [{"title": "Good"}, "not a dict", 42]
        result = GoalEngine._normalise(raw)
        assert len(result) == 1

    def test_file_with_list_format(self, tmp_path: Path) -> None:
        p = tmp_path / "goals_list.json"
        p.write_text(json.dumps([{"id": "x1", "title": "List format", "status": "active"}]))
        import pulse.src.runtime.goal_engine as mod
        original = mod._GOALS_JSON_CANDIDATES
        mod._GOALS_JSON_CANDIDATES = [p]
        try:
            ge = GoalEngine()
            ge.load()
            assert len(ge._goals) == 1
        finally:
            mod._GOALS_JSON_CANDIDATES = original


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestQueries:
    def test_active_goals_count(self, engine: GoalEngine) -> None:
        active = engine.active_goals
        assert len(active) >= 1
        assert all(g["status"] == "active" for g in active)

    def test_completed_goals_initially_empty_for_seed(self, engine: GoalEngine) -> None:
        # Seed goals are all active
        assert engine.completed_goals == []

    def test_blocked_goals_subset_of_active(self, engine: GoalEngine) -> None:
        blocked = engine.blocked_goals
        active = engine.active_goals
        for g in blocked:
            assert g in active
            assert g["blockers"]

    def test_unblocked_goals_have_no_blockers(self, engine: GoalEngine) -> None:
        for g in engine.unblocked_goals:
            assert not g["blockers"]

    def test_active_blocked_unblocked_partition(self, engine: GoalEngine) -> None:
        active = engine.active_goals
        blocked = engine.blocked_goals
        unblocked = engine.unblocked_goals
        # every active goal is either blocked or unblocked
        assert len(blocked) + len(unblocked) == len(active)


# ---------------------------------------------------------------------------
# Pressure
# ---------------------------------------------------------------------------


class TestPressure:
    def test_pressure_in_range(self, engine: GoalEngine) -> None:
        p = engine.pressure()
        assert 0.0 <= p <= 1.0

    def test_pressure_zero_with_no_goals(self) -> None:
        ge = GoalEngine()
        ge._goals = []
        ge._loaded = True
        assert ge.pressure() == 0.0

    def test_pressure_increases_with_blockers(self) -> None:
        ge = GoalEngine()
        ge._goals = [
            {"id": "a", "title": "No blocker", "priority": 1, "status": "active", "progress": 0.0, "blockers": []},
            {"id": "b", "title": "Has blocker", "priority": 1, "status": "active", "progress": 0.0, "blockers": ["dep"]},
        ]
        ge._loaded = True
        p = ge.pressure()
        # Both goals contribute; the blocked one adds extra
        assert p > 0.5

    def test_pressure_lower_for_completed_goals(self) -> None:
        """Completed goals do not contribute to pressure."""
        ge = GoalEngine()
        ge._goals = [
            {"id": "a", "title": "Active", "priority": 1, "status": "active", "progress": 0.0, "blockers": []},
        ]
        ge._loaded = True
        p_one = ge.pressure()

        ge2 = GoalEngine()
        ge2._goals = [
            {"id": "a", "title": "Done", "priority": 1, "status": "completed", "progress": 1.0, "blockers": []},
        ]
        ge2._loaded = True
        assert ge2.pressure() == 0.0
        assert p_one > 0.0

    def test_pressure_decreases_as_progress_increases(self) -> None:
        def make_engine(progress: float) -> GoalEngine:
            ge = GoalEngine()
            ge._goals = [
                {"id": "a", "title": "Goal", "priority": 1, "status": "active", "progress": progress, "blockers": []},
            ]
            ge._loaded = True
            return ge

        p0 = make_engine(0.0).pressure()
        p50 = make_engine(0.5).pressure()
        p90 = make_engine(0.9).pressure()
        assert p0 > p50 > p90


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


class TestMutations:
    def test_complete_goal(self, engine: GoalEngine) -> None:
        target = engine.active_goals[0]["id"]
        result = engine.complete_goal(target)
        assert result is True
        ids = [g["id"] for g in engine.completed_goals]
        assert target in ids

    def test_complete_nonexistent_goal(self, engine: GoalEngine) -> None:
        assert engine.complete_goal("does_not_exist") is False

    def test_update_progress(self, engine: GoalEngine) -> None:
        target = engine.active_goals[0]["id"]
        engine.update_progress(target, 0.75)
        for g in engine._goals:
            if g["id"] == target:
                assert g["progress"] == 0.75
                break

    def test_update_progress_clamps(self, engine: GoalEngine) -> None:
        target = engine.active_goals[0]["id"]
        engine.update_progress(target, 5.0)
        for g in engine._goals:
            if g["id"] == target:
                assert g["progress"] == 1.0
                break

    def test_update_progress_nonexistent(self, engine: GoalEngine) -> None:
        assert engine.update_progress("nope", 0.5) is False

    def test_add_blocker(self, engine: GoalEngine) -> None:
        target_id = engine.unblocked_goals[0]["id"] if engine.unblocked_goals else engine.active_goals[0]["id"]
        engine.add_blocker(target_id, "new_dependency")
        for g in engine._goals:
            if g["id"] == target_id:
                assert "new_dependency" in g["blockers"]
                break

    def test_add_blocker_no_duplicate(self, engine: GoalEngine) -> None:
        target_id = engine.active_goals[0]["id"]
        engine.add_blocker(target_id, "dep_x")
        engine.add_blocker(target_id, "dep_x")
        for g in engine._goals:
            if g["id"] == target_id:
                assert g["blockers"].count("dep_x") == 1
                break

    def test_remove_blocker(self, engine: GoalEngine) -> None:
        # First add it
        target_id = engine.active_goals[0]["id"]
        engine.add_blocker(target_id, "temp_blocker")
        result = engine.remove_blocker(target_id, "temp_blocker")
        assert result is True
        for g in engine._goals:
            if g["id"] == target_id:
                assert "temp_blocker" not in g["blockers"]
                break

    def test_add_goal(self, engine: GoalEngine) -> None:
        initial_count = len(engine.active_goals)
        engine.add_goal({"id": "new_001", "title": "Brand new goal", "priority": 2, "status": "active"})
        assert len(engine.active_goals) == initial_count + 1

    def test_add_goal_normalises(self, engine: GoalEngine) -> None:
        engine.add_goal({"title": "Minimal goal"})
        # Should still be added and normalised
        assert any(g["title"] == "Minimal goal" for g in engine._goals)


# ---------------------------------------------------------------------------
# for_plan()
# ---------------------------------------------------------------------------


class TestForPlan:
    def test_for_plan_returns_string(self, engine: GoalEngine) -> None:
        s = engine.for_plan()
        assert isinstance(s, str)
        assert len(s) > 0

    def test_for_plan_no_goals(self) -> None:
        ge = GoalEngine()
        ge._goals = []
        ge._loaded = True
        assert "No active goals" in ge.for_plan()

    def test_for_plan_includes_unblocked_highlight(self) -> None:
        ge = GoalEngine()
        ge._goals = [
            {"id": "a", "title": "Free goal", "priority": 1, "status": "active", "progress": 0.0, "blockers": []},
        ]
        ge._loaded = True
        plan = ge.for_plan()
        assert "Free goal" in plan
        assert "→" in plan

    def test_for_plan_includes_blocked_highlight(self) -> None:
        ge = GoalEngine()
        ge._goals = [
            {"id": "a", "title": "Stuck goal", "priority": 1, "status": "active", "progress": 0.1, "blockers": ["infra"]},
        ]
        ge._loaded = True
        plan = ge.for_plan()
        assert "Stuck goal" in plan
        assert "infra" in plan


# ---------------------------------------------------------------------------
# Snapshot / status
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_structure(self, engine: GoalEngine) -> None:
        snap = engine.snapshot()
        assert "loaded" in snap
        assert "active" in snap
        assert "completed" in snap
        assert "pressure" in snap

    def test_snapshot_pressure_matches_pressure_method(self, engine: GoalEngine) -> None:
        snap = engine.snapshot()
        assert snap["pressure"] == engine.pressure()

    def test_status_structure(self, engine: GoalEngine) -> None:
        s = engine.status()
        assert "loaded" in s
        assert "active_count" in s
        assert "blocked_count" in s
        assert "completed_count" in s
        assert "pressure" in s
        assert "top_goal" in s


# ---------------------------------------------------------------------------
# StateEngine integration
# ---------------------------------------------------------------------------


class TestStateSync:
    def test_state_set_called_after_load(self, engine_with_state: GoalEngine, mock_state: MagicMock) -> None:
        assert mock_state.set.called

    def test_state_pressure_key_set(self, engine_with_state: GoalEngine, mock_state: MagicMock) -> None:
        calls = [call[0][0] for call in mock_state.set.call_args_list]
        assert "goals.pressure" in calls

    def test_state_active_key_set(self, engine_with_state: GoalEngine, mock_state: MagicMock) -> None:
        calls = [call[0][0] for call in mock_state.set.call_args_list]
        assert "goals.active" in calls

    def test_complete_triggers_state_sync(self, engine_with_state: GoalEngine, mock_state: MagicMock) -> None:
        mock_state.reset_mock()
        goal_id = engine_with_state.active_goals[0]["id"]
        engine_with_state.complete_goal(goal_id)
        assert mock_state.set.called


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_blockers_list(self) -> None:
        assert _extract_blockers({"blockers": ["a", "b"]}) == ["a", "b"]

    def test_extract_blockers_string(self) -> None:
        assert _extract_blockers({"blocked_on": "deploy"}) == ["deploy"]

    def test_extract_blockers_empty_string(self) -> None:
        assert _extract_blockers({"blocked_on": ""}) == []

    def test_extract_blockers_none(self) -> None:
        assert _extract_blockers({}) == []

    def test_extract_note_direct(self) -> None:
        assert _extract_note({"note": "hello"}) == "hello"

    def test_extract_note_from_progress_list(self) -> None:
        g = {"progress": ["first entry", "latest entry"]}
        assert _extract_note(g) == "latest entry"

    def test_extract_note_from_progress_dict(self) -> None:
        g = {"progress": [{"date": "2026-01-01", "note": "dict entry"}]}
        assert _extract_note(g) == "dict entry"

    def test_extract_note_empty(self) -> None:
        assert _extract_note({}) == ""
