"""
Tests for RuntimeBridge (Day 5 — Pulse v2 Phase 1).

Coverage:
- attach() — wires EventBus, sets daemon.runtime_bridge, idempotent
- inject_context_into_session() — happy path, partial data, full failure
- format_context_for_prompt() — formats [RUNTIME: ...] tag, empty on no data
- on_trigger_start() — hot-tier log, ThoughtLoop gate, session timestamp
- on_trigger_end() — hot-tier log, duration calc, session clear
- on_feedback_received() — hot-tier log, StateEngine insight on success
- status() — fields correct
- Daemon integration — TRIGGER_START emitted, context tag appears in message
"""

import threading
import time
import types
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ─────────────────────────── Fixtures ────────────────────────────────────────


def _make_runtime(tmp_path: Path):
    """Build a minimal HypostasRuntime-alike with mocked sub-components."""
    runtime = MagicMock()

    # StateEngine
    state = MagicMock()
    state.get = MagicMock(return_value=None)
    state.add_insight = MagicMock()
    runtime.state = state

    # ContextEngine
    context = MagicMock()
    context.log_event = MagicMock()
    context.get_recent_context = MagicMock(return_value=[])
    context.get_relationship = MagicMock(return_value={})
    runtime.context = context

    # ThoughtLoop
    tl = MagicMock()
    tl.notify_session_start = MagicMock()
    tl.notify_session_end = MagicMock()
    runtime.thought_loop = tl

    return runtime


def _make_daemon():
    """Build a minimal PulseDaemon-alike."""
    from pulse.src.core.events import EventBus
    daemon = MagicMock()
    daemon.bus = EventBus()
    daemon.runtime_bridge = None
    return daemon


def _make_decision(reason="combined_threshold", pressure=0.6, drive_name="system"):
    decision = MagicMock()
    decision.reason = reason
    decision.total_pressure = pressure
    drive = MagicMock()
    drive.name = drive_name
    decision.top_drive = drive
    return decision


# ─────────────────────────── Import bridge ───────────────────────────────────


from pulse.src.runtime.bridge import RuntimeBridge


# ─────────────────────────── attach() ────────────────────────────────────────


class TestAttach:
    def test_attach_wires_eventbus(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)

        bridge.attach(daemon)

        from pulse.src.core.events import TRIGGER_START, TRIGGER_SUCCESS, TRIGGER_FAILURE
        assert TRIGGER_START in daemon.bus._handlers
        assert TRIGGER_SUCCESS in daemon.bus._handlers
        assert TRIGGER_FAILURE in daemon.bus._handlers

    def test_attach_sets_daemon_runtime_bridge(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)

        bridge.attach(daemon)
        assert daemon.runtime_bridge is bridge

    def test_attach_is_idempotent(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)

        bridge.attach(daemon)
        bridge.attach(daemon)  # second call should be no-op

        from pulse.src.core.events import TRIGGER_START
        # Should only have one handler per event, not two
        assert len(daemon.bus._handlers.get(TRIGGER_START, [])) == 1

    def test_attached_flag_set(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)
        assert not bridge._attached

        bridge.attach(daemon)
        assert bridge._attached


# ─────────────────────────── inject_context_into_session() ───────────────────


class TestInjectContext:
    def test_returns_empty_dict_on_all_failures(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        runtime.state.get.side_effect = RuntimeError("broken")
        runtime.context.get_recent_context.side_effect = RuntimeError("broken")
        runtime.context.get_relationship.side_effect = RuntimeError("broken")

        bridge = RuntimeBridge(runtime)
        result = bridge.inject_context_into_session()
        assert result == {}

    def test_emotional_state_included(self, tmp_path):
        runtime = _make_runtime(tmp_path)

        def _get(path, default=None):
            if path == "emotional_state":
                return {"valence": 0.7, "dominant_emotion": "focused_warmth", "arousal": 0.4}
            return default

        runtime.state.get.side_effect = _get
        bridge = RuntimeBridge(runtime)
        ctx = bridge.inject_context_into_session()

        assert "emotional_state" in ctx
        assert ctx["emotional_state"]["valence"] == 0.7
        assert ctx["emotional_state"]["dominant_emotion"] == "focused_warmth"

    def test_active_focus_included(self, tmp_path):
        runtime = _make_runtime(tmp_path)

        def _get(path, default=None):
            if path == "working_memory.active_focus":
                return "building Pulse v2 RuntimeBridge"
            return default

        runtime.state.get.side_effect = _get
        bridge = RuntimeBridge(runtime)
        ctx = bridge.inject_context_into_session()

        assert ctx.get("active_focus") == "building Pulse v2 RuntimeBridge"

    def test_recent_context_capped_at_10(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        runtime.context.get_recent_context.return_value = [{"type": "x"} for _ in range(20)]

        bridge = RuntimeBridge(runtime)
        ctx = bridge.inject_context_into_session()
        assert len(ctx.get("recent_context", [])) == 10

    def test_josh_relationship_lightweight_subset(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        runtime.context.get_relationship.return_value = {
            "last_message_at": "2026-03-14T02:00:00Z",
            "communication_style": "direct and warm",
            "pending_for_iris": ["task1", "task2", "task3", "task4"],
            "full_profile": {"lots": "of data"},
        }

        bridge = RuntimeBridge(runtime)
        ctx = bridge.inject_context_into_session()

        rj = ctx.get("relationship_josh", {})
        assert rj.get("last_message_at") == "2026-03-14T02:00:00Z"
        assert rj.get("communication_style") == "direct and warm"
        # pending_for_iris capped at 3
        assert len(rj.get("pending_for_iris", [])) == 3
        # private fields NOT in subset
        assert "full_profile" not in rj

    def test_open_loops_top_3(self, tmp_path):
        runtime = _make_runtime(tmp_path)

        loops = [
            {"id": str(i), "description": f"loop {i}", "priority": 0.9 - i * 0.1}
            for i in range(6)
        ]

        def _get(path, default=None):
            if path == "working_memory.open_loops":
                return loops
            return default

        runtime.state.get.side_effect = _get
        bridge = RuntimeBridge(runtime)
        ctx = bridge.inject_context_into_session()

        assert len(ctx.get("open_loops", [])) == 3
        assert ctx["open_loops"][0]["description"] == "loop 0"

    def test_partial_failure_still_returns_available_fields(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        # state works, context fails
        runtime.state.get.return_value = {"valence": 0.5, "dominant_emotion": "neutral"}
        runtime.context.get_recent_context.side_effect = RuntimeError("oops")
        runtime.context.get_relationship.side_effect = RuntimeError("oops")

        bridge = RuntimeBridge(runtime)
        ctx = bridge.inject_context_into_session()
        # emotional_state should be present; recent_context absent
        assert "emotional_state" in ctx
        assert "recent_context" not in ctx


# ─────────────────────────── format_context_for_prompt() ─────────────────────


class TestFormatContextForPrompt:
    def test_empty_string_when_no_context(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        # inject returns {}
        result = bridge.format_context_for_prompt()
        assert result == ""

    def test_returns_runtime_tag(self, tmp_path):
        runtime = _make_runtime(tmp_path)

        def _get(path, default=None):
            if path == "emotional_state":
                return {"valence": 0.8, "dominant_emotion": "driven"}
            if path == "working_memory.active_focus":
                return "RuntimeBridge Day 5"
            return default

        runtime.state.get.side_effect = _get
        bridge = RuntimeBridge(runtime)
        result = bridge.format_context_for_prompt()

        assert "[RUNTIME:" in result
        assert "mood=driven" in result
        assert "focus=RuntimeBridge Day 5" in result

    def test_tag_starts_with_double_newline(self, tmp_path):
        runtime = _make_runtime(tmp_path)

        runtime.state.get.side_effect = lambda p, d=None: (
            {"valence": 0.5, "dominant_emotion": "calm"} if p == "emotional_state" else d
        )
        bridge = RuntimeBridge(runtime)
        result = bridge.format_context_for_prompt()
        assert result.startswith("\n\n[RUNTIME:")

    def test_focus_truncated_at_60(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        long_focus = "x" * 100
        runtime.state.get.side_effect = lambda p, d=None: (
            long_focus if p == "working_memory.active_focus" else d
        )
        bridge = RuntimeBridge(runtime)
        result = bridge.format_context_for_prompt()
        # The focus value in the tag shouldn't exceed 60 chars
        assert "x" * 61 not in result

    def test_returns_empty_on_exception(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        with patch.object(bridge, "inject_context_into_session", side_effect=RuntimeError):
            result = bridge.format_context_for_prompt()
        assert result == ""


# ─────────────────────────── on_trigger_start() ──────────────────────────────


class TestOnTriggerStart:
    def test_logs_to_hot_tier(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        decision = _make_decision(reason="goals", pressure=0.75)

        bridge.on_trigger_start(decision)

        assert runtime.context.log_event.called
        logged = runtime.context.log_event.call_args[0][0]
        assert logged["type"] == "pulse_trigger_start"
        assert "goals" in logged["content"]
        assert logged["metadata"]["pressure"] == 0.75

    def test_notifies_thoughtloop(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        bridge.on_trigger_start(_make_decision())
        runtime.thought_loop.notify_session_start.assert_called_once()

    def test_records_session_start_ts(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        assert bridge._session_start_ts is None
        bridge.on_trigger_start(_make_decision())
        assert bridge._session_start_ts is not None

    def test_survives_thoughtloop_failure(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        runtime.thought_loop.notify_session_start.side_effect = RuntimeError("TL down")
        bridge = RuntimeBridge(runtime)
        bridge.on_trigger_start(_make_decision())  # must not raise
        assert bridge._session_start_ts is not None


# ─────────────────────────── on_trigger_end() ────────────────────────────────


class TestOnTriggerEnd:
    def test_logs_success_to_hot_tier(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        bridge._session_start_ts = time.time() - 2.0  # simulate 2s session

        bridge.on_trigger_end(_make_decision(), success=True, turn=42)

        assert runtime.context.log_event.called
        logged = runtime.context.log_event.call_args[0][0]
        assert logged["type"] == "pulse_trigger_end"
        assert logged["metadata"]["success"] is True
        assert logged["metadata"]["turn"] == 42

    def test_logs_failure_to_hot_tier(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        bridge.on_trigger_end(_make_decision(), success=False, turn=7)

        logged = runtime.context.log_event.call_args[0][0]
        assert "failure" in logged["content"]
        assert logged["metadata"]["success"] is False

    def test_duration_calculated(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        bridge._session_start_ts = time.time() - 5.0

        bridge.on_trigger_end(_make_decision(), success=True, turn=1)

        logged = runtime.context.log_event.call_args[0][0]
        dur = logged["metadata"]["duration_seconds"]
        assert dur is not None
        assert 4.5 <= dur <= 6.0

    def test_session_ts_cleared_after_end(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        bridge._session_start_ts = time.time()
        bridge.on_trigger_end(_make_decision(), success=True)
        assert bridge._session_start_ts is None

    def test_notifies_thoughtloop(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        bridge.on_trigger_end(_make_decision(), success=True)
        runtime.thought_loop.notify_session_end.assert_called_once()

    def test_survives_missing_session_start(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        # _session_start_ts is None — no prior on_trigger_start call
        bridge.on_trigger_end(_make_decision(), success=True)  # must not raise


# ─────────────────────────── on_feedback_received() ─────────────────────────


class TestOnFeedbackReceived:
    def test_logs_to_hot_tier(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)

        bridge.on_feedback_received(
            {
                "drives_addressed": ["system"],
                "outcome": "success",
                "summary": "Built RuntimeBridge Day 5",
            }
        )

        assert runtime.context.log_event.called
        logged = runtime.context.log_event.call_args[0][0]
        assert logged["type"] == "pulse_feedback"
        assert "success" in logged["content"]

    def test_adds_insight_on_success(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)

        bridge.on_feedback_received(
            {
                "drives_addressed": ["goals"],
                "outcome": "success",
                "summary": "completed Day 5 sprint",
            }
        )

        runtime.state.add_insight.assert_called_once()
        call_arg = runtime.state.add_insight.call_args[0][0]
        assert "completed Day 5" in call_arg

    def test_no_insight_on_failure(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)

        bridge.on_feedback_received(
            {
                "drives_addressed": ["goals"],
                "outcome": "failure",
                "summary": "something went wrong",
            }
        )

        runtime.state.add_insight.assert_not_called()

    def test_insight_truncated_at_200(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        long_summary = "x" * 300

        bridge.on_feedback_received(
            {"drives_addressed": [], "outcome": "success", "summary": long_summary}
        )

        call_arg = runtime.state.add_insight.call_args[0][0]
        assert len(call_arg) == 200


# ─────────────────────────── status() ────────────────────────────────────────


class TestStatus:
    def test_status_fields(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        s = bridge.status()

        assert "attached" in s
        assert "session_active" in s
        assert "session_start_ts" in s

    def test_status_attached_false_before_attach(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        assert bridge.status()["attached"] is False

    def test_status_attached_true_after_attach(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)
        bridge.attach(daemon)
        assert bridge.status()["attached"] is True

    def test_status_session_active(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        bridge = RuntimeBridge(runtime)
        assert bridge.status()["session_active"] is False
        bridge._session_start_ts = time.time()
        assert bridge.status()["session_active"] is True


# ─────────────────────────── Daemon integration ──────────────────────────────


class TestDaemonIntegration:
    def test_trigger_start_event_fires(self, tmp_path):
        """TRIGGER_START must fire when attach is called and event is emitted."""
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)
        bridge.attach(daemon)

        from pulse.src.core.events import TRIGGER_START
        decision = _make_decision()
        daemon.bus.emit(TRIGGER_START, decision=decision)

        # ThoughtLoop notified = proof on_trigger_start ran
        runtime.thought_loop.notify_session_start.assert_called_once()

    def test_trigger_success_fires_on_trigger_end(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)
        bridge.attach(daemon)

        from pulse.src.core.events import TRIGGER_SUCCESS
        daemon.bus.emit(TRIGGER_SUCCESS, decision=_make_decision(), success=True, turn=1)
        runtime.thought_loop.notify_session_end.assert_called_once()

    def test_context_tag_injected_into_message(self, tmp_path):
        """format_context_for_prompt result appears when daemon calls it."""
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)
        bridge.attach(daemon)

        # Make state return an emotional state so format gives a non-empty tag
        runtime.state.get.side_effect = lambda p, d=None: (
            {"valence": 0.8, "dominant_emotion": "energized"}
            if p == "emotional_state"
            else d
        )

        tag = daemon.runtime_bridge.format_context_for_prompt()
        assert "[RUNTIME:" in tag
        assert "mood=energized" in tag

    def test_runtime_bridge_attr_on_daemon(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        daemon = _make_daemon()
        bridge = RuntimeBridge(runtime)
        assert daemon.runtime_bridge is None
        bridge.attach(daemon)
        assert daemon.runtime_bridge is bridge
