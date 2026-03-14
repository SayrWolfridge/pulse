"""
Tests for ThoughtLoop — Pulse v2 Day 4

All Ollama calls are mocked. Tests verify:
- OllamaClient interface (happy + failure paths)
- Gate logic (_should_run, _is_dream_time)
- Cycle orchestration (reflect, plan, compress rotation)
- Prompt builders
- Plan response parser
- Background thread start/stop
- Integration with StateEngine + ContextEngine stubs
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch, PropertyMock

from pulse.src.runtime.thought_loop import (
    OllamaClient,
    ThoughtLoop,
    _build_reflect_prompt,
    _build_plan_prompt,
    _parse_plan_response,
    _yesterday_date,
    _date_to_ts,
    SESSION_COOLDOWN_SECONDS,
    PLAN_CYCLE_INTERVAL,
    COMPRESS_CYCLE_INTERVAL,
    OLLAMA_MODEL,
)


# ---------------------------------------------------------------------------
# Minimal fakes for StateEngine / ContextEngine
# ---------------------------------------------------------------------------

class FakeState:
    """Minimal StateEngine stub for testing."""

    def __init__(self):
        self._store: dict = {
            "drives": {"goals": 0.6, "curiosity": 0.3, "emotions": 0.2},
            "working_memory": {
                "open_loops": [],
                "current_projects": ["anima", "weather-bot"],
                "pending_for_josh": [],
                "recent_insights": [],
            },
            "thought_loop": {"running": False, "last_insight_ts": None},
        }
        self._insights: list[str] = []
        self.session_active: bool = False

    def get(self, path: str, default=None):
        parts = path.split(".")
        obj = self._store
        for p in parts:
            if not isinstance(obj, dict) or p not in obj:
                return default
            obj = obj[p]
        return obj

    def set(self, path: str, value):
        parts = path.split(".")
        obj = self._store
        for p in parts[:-1]:
            obj = obj.setdefault(p, {})
        obj[parts[-1]] = value

    def add_insight(self, insight: str):
        self._insights.append(insight)

    def is_pulse_session_active(self) -> bool:
        return self.session_active


class FakeWarmTier:
    def __init__(self):
        self._data: dict = {}

    def get_day(self, date: str):
        return self._data.get(date)


class FakeHotTier:
    def __init__(self):
        self._entries: list[dict] = []

    def get_all(self):
        return list(self._entries)


class FakeContext:
    """Minimal ContextEngine stub for testing."""

    def __init__(self):
        self._events: list[dict] = []
        self.warm = FakeWarmTier()
        self.hot = FakeHotTier()
        self._recent: list[dict] = []

    def log_event(self, event: dict):
        self._events.append(event)

    def get_recent_context(self, hours: int = 2) -> list[dict]:
        return self._recent

    def compress_to_warm(self, date: str) -> dict:
        summary = {"date": date, "themes": ["test"], "mood_avg": 0.5}
        self.warm._data[date] = summary
        return summary


# ---------------------------------------------------------------------------
# OllamaClient tests
# ---------------------------------------------------------------------------

class TestOllamaClient(unittest.TestCase):

    def test_generate_success(self):
        """Happy path: mocked HTTP 200 returns response text."""
        client = OllamaClient()
        mock_response_body = json.dumps({"response": "I notice the pattern clearly."})

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = mock_response_body.encode()
            mock_conn.getresponse.return_value = mock_resp
            mock_conn_cls.return_value = mock_conn

            result = client.generate("What do you notice?")

        self.assertEqual(result, "I notice the pattern clearly.")

    def test_generate_http_error(self):
        """Non-200 status returns None."""
        client = OllamaClient()

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status = 503
            mock_resp.read.return_value = b""
            mock_conn.getresponse.return_value = mock_resp
            mock_conn_cls.return_value = mock_conn

            result = client.generate("What do you notice?")

        self.assertIsNone(result)

    def test_generate_connection_refused(self):
        """ConnectionRefusedError returns None gracefully."""
        client = OllamaClient()

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_conn.request.side_effect = ConnectionRefusedError("refused")
            mock_conn_cls.return_value = mock_conn

            result = client.generate("Hello?")

        self.assertIsNone(result)

    def test_generate_empty_response(self):
        """Empty response string returns None."""
        client = OllamaClient()
        mock_response_body = json.dumps({"response": ""})

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = mock_response_body.encode()
            mock_conn.getresponse.return_value = mock_resp
            mock_conn_cls.return_value = mock_conn

            result = client.generate("Hello?")

        self.assertIsNone(result)

    def test_generate_whitespace_only_returns_none(self):
        """Whitespace-only response returns None."""
        client = OllamaClient()
        mock_response_body = json.dumps({"response": "   \n  "})

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = mock_response_body.encode()
            mock_conn.getresponse.return_value = mock_resp
            mock_conn_cls.return_value = mock_conn

            result = client.generate("Hello?")

        self.assertIsNone(result)

    def test_is_available_true(self):
        """Liveness check returns True on HTTP 200."""
        client = OllamaClient()

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b"{}"
            mock_conn.getresponse.return_value = mock_resp
            mock_conn_cls.return_value = mock_conn

            result = client.is_available()

        self.assertTrue(result)

    def test_is_available_false_on_connection_error(self):
        """Liveness check returns False on connection error."""
        client = OllamaClient()

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_conn.request.side_effect = OSError("no route")
            mock_conn_cls.return_value = mock_conn

            result = client.is_available()

        self.assertFalse(result)

    def test_system_prompt_included_in_payload(self):
        """System prompt is included in request payload when provided."""
        client = OllamaClient()
        captured_body = {}

        def capture_request(method, path, body=None, headers=None):
            captured_body["data"] = json.loads(body)

        mock_response_body = json.dumps({"response": "reflection here"})

        with patch("http.client.HTTPConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = mock_response_body.encode()
            mock_conn.getresponse.return_value = mock_resp
            mock_conn.request.side_effect = capture_request
            mock_conn_cls.return_value = mock_conn

            client.generate("prompt", system="You are Iris.")

        self.assertEqual(captured_body["data"].get("system"), "You are Iris.")


# ---------------------------------------------------------------------------
# Prompt builder tests
# ---------------------------------------------------------------------------

class TestPromptBuilders(unittest.TestCase):

    def test_reflect_prompt_includes_events(self):
        events = [
            {"type": "PULSE_TRIGGER", "content": "goals drive fired", "ts": "2026-03-14T01:00:00+00:00"},
            {"type": "MESSAGE_RECEIVED", "content": "hey", "ts": "2026-03-14T01:01:00+00:00"},
        ]
        drives = {"goals": 0.6, "emotions": 0.2}
        prompt = _build_reflect_prompt(events, drives)

        self.assertIn("PULSE_TRIGGER", prompt)
        self.assertIn("goals", prompt)
        self.assertIn("goals drive fired", prompt)

    def test_reflect_prompt_no_events(self):
        prompt = _build_reflect_prompt([], {"goals": 0.1})
        self.assertIn("no recent events", prompt)

    def test_reflect_prompt_caps_at_eight_events(self):
        events = [
            {"type": "EVT", "content": f"item {i}", "ts": f"2026-03-14T0{i}:00:00+00:00"}
            for i in range(12)
        ]
        prompt = _build_reflect_prompt(events, {})
        # Should only include last 8 events (items 4-11)
        self.assertIn("item 11", prompt)
        self.assertNotIn("item 0", prompt)

    def test_reflect_prompt_truncates_long_content(self):
        events = [{"type": "EVT", "content": "x" * 200, "ts": "2026-03-14T01:00:00+00:00"}]
        prompt = _build_reflect_prompt(events, {})
        # Content truncated to 100 chars per event
        self.assertNotIn("x" * 101, prompt)

    def test_plan_prompt_includes_loops_and_projects(self):
        loops = [
            {"description": "finish weather bot", "priority": 0.9},
            {"description": "write journal", "priority": 0.5},
        ]
        projects = ["anima", "weather-bot"]
        prompt = _build_plan_prompt(loops, projects)

        self.assertIn("finish weather bot", prompt)
        self.assertIn("anima", prompt)
        self.assertIn("weather-bot", prompt)

    def test_plan_prompt_no_loops(self):
        prompt = _build_plan_prompt([], [])
        self.assertIn("none", prompt)

    def test_plan_prompt_caps_at_five_loops(self):
        loops = [{"description": f"loop {i}", "priority": 0.5} for i in range(10)]
        prompt = _build_plan_prompt(loops, [])
        self.assertIn("loop 4", prompt)
        self.assertNotIn("loop 5", prompt)


# ---------------------------------------------------------------------------
# Plan response parser tests
# ---------------------------------------------------------------------------

class TestParsePlanResponse(unittest.TestCase):

    def test_parse_valid_json_array(self):
        response = json.dumps([
            {"project": "anima", "next_action": "build sprint 4", "priority": 0.9},
            {"project": "weather-bot", "next_action": "run paper trades", "priority": 0.7},
        ])
        result = _parse_plan_response(response, [])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["description"], "build sprint 4")
        self.assertAlmostEqual(result[0]["priority"], 0.9)

    def test_parse_json_with_surrounding_text(self):
        response = "Here's the plan:\n[{\"project\": \"pulse\", \"next_action\": \"ship day 4\", \"priority\": 1.0}]\nDone."
        result = _parse_plan_response(response, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["description"], "ship day 4")

    def test_parse_falls_back_on_invalid_json(self):
        fallback = [{"id": "x", "description": "original", "priority": 0.5}]
        result = _parse_plan_response("this is not json", fallback)
        self.assertEqual(result, fallback)

    def test_parse_falls_back_on_no_array(self):
        fallback = [{"description": "original"}]
        result = _parse_plan_response("{\"key\": \"value\"}", fallback)
        self.assertEqual(result, fallback)

    def test_parse_empty_array_falls_back(self):
        fallback = [{"description": "original"}]
        result = _parse_plan_response("[]", fallback)
        self.assertEqual(result, fallback)

    def test_parse_skips_non_dict_items(self):
        response = json.dumps([
            {"project": "a", "next_action": "do it", "priority": 0.8},
            "not a dict",
            42,
        ])
        result = _parse_plan_response(response, [])
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# Gate logic tests
# ---------------------------------------------------------------------------

class TestGateLogic(unittest.TestCase):

    def _make_loop(self, **kwargs):
        state = FakeState()
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        loop = ThoughtLoop(state, context, ollama=ollama, **kwargs)
        return loop

    def test_should_run_on_fresh_start(self):
        """No session history → should run."""
        loop = self._make_loop()
        self.assertTrue(loop._should_run())

    def test_should_not_run_immediately_after_session(self):
        """Just-ended session → should not run."""
        loop = self._make_loop()
        loop.notify_session_end()
        self.assertFalse(loop._should_run())

    def test_should_run_after_cooldown(self):
        """After cooldown period → should run again."""
        loop = self._make_loop()
        loop.notify_session_end()
        # Fake the last session timestamp to be old enough
        loop._last_session_ts = time.time() - SESSION_COOLDOWN_SECONDS - 1
        self.assertTrue(loop._should_run())

    def test_notify_session_start_sets_cooldown(self):
        loop = self._make_loop()
        loop.notify_session_start()
        self.assertFalse(loop._should_run())

    def test_notify_session_end_sets_cooldown(self):
        loop = self._make_loop()
        loop.notify_session_end()
        self.assertFalse(loop._should_run())

    def test_is_dream_time_boundaries(self):
        """Dream time is 2 AM to 4 AM."""
        loop = self._make_loop()
        with patch("pulse.src.runtime.thought_loop.datetime") as mock_dt:
            # 2 AM
            mock_dt.now.return_value = MagicMock(hour=2)
            self.assertTrue(loop._is_dream_time())
            # 3 AM
            mock_dt.now.return_value = MagicMock(hour=3)
            self.assertTrue(loop._is_dream_time())
            # 4 AM (exclusive)
            mock_dt.now.return_value = MagicMock(hour=4)
            self.assertFalse(loop._is_dream_time())
            # 1 AM
            mock_dt.now.return_value = MagicMock(hour=1)
            self.assertFalse(loop._is_dream_time())
            # Noon
            mock_dt.now.return_value = MagicMock(hour=12)
            self.assertFalse(loop._is_dream_time())


# ---------------------------------------------------------------------------
# Cycle orchestration tests
# ---------------------------------------------------------------------------

class TestRunCycle(unittest.TestCase):

    def _make_loop(self, recent_events=None, insight="I see a pattern."):
        state = FakeState()
        context = FakeContext()
        if recent_events is not None:
            context._recent = recent_events
        ollama = MagicMock(spec=OllamaClient)
        ollama.generate.return_value = insight
        loop = ThoughtLoop(state, context, ollama=ollama)
        return loop, state, context

    def test_skips_during_session_cooldown(self):
        loop, state, context = self._make_loop()
        loop.notify_session_end()
        result = loop.run_cycle()
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "pulse_session_active")

    def test_generates_insight_when_events_present(self):
        events = [{"type": "PULSE_TRIGGER", "content": "goals", "ts": "2026-03-14T01:00:00+00:00"}]
        loop, state, context = self._make_loop(recent_events=events)
        result = loop.run_cycle()
        self.assertEqual(result["reflect"], "I see a pattern.")

    def test_no_insight_when_no_events(self):
        loop, state, context = self._make_loop(recent_events=[], insight=None)
        result = loop.run_cycle()
        self.assertIsNone(result["reflect"])

    def test_insight_logged_to_context(self):
        events = [{"type": "EVT", "content": "test", "ts": "2026-03-14T01:00:00+00:00"}]
        loop, state, context = self._make_loop(recent_events=events)
        loop.run_cycle()
        thought_events = [e for e in context._events if e.get("type") == "THOUGHT_LOOP"]
        self.assertEqual(len(thought_events), 1)
        self.assertIn("I see a pattern.", thought_events[0]["content"]["insight"])

    def test_insight_added_to_state(self):
        events = [{"type": "EVT", "content": "test", "ts": "2026-03-14T01:00:00+00:00"}]
        loop, state, context = self._make_loop(recent_events=events)
        loop.run_cycle()
        self.assertIn("I see a pattern.", state._insights)

    def test_plan_runs_every_third_cycle(self):
        state = FakeState()
        state.set("working_memory.open_loops", [{"description": "do thing", "priority": 0.8}])
        state.set("working_memory.current_projects", ["pulse"])
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        ollama.generate.return_value = json.dumps([{"project": "pulse", "next_action": "ship day 4", "priority": 1.0}])

        loop = ThoughtLoop(state, context, ollama=ollama)
        loop._cycle_count = 0  # Cycle 0 → plan runs (0 % 3 == 0)
        result = loop.run_cycle()
        self.assertIsNotNone(result.get("plan"))

    def test_plan_does_not_run_every_cycle(self):
        state = FakeState()
        state.set("working_memory.open_loops", [{"description": "do thing", "priority": 0.8}])
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        ollama.generate.return_value = "some insight"

        loop = ThoughtLoop(state, context, ollama=ollama)
        loop._cycle_count = 1  # Cycle 1 → plan does NOT run (1 % 3 != 0)
        result = loop.run_cycle()
        self.assertIsNone(result.get("plan"))

    def test_insights_counter_increments(self):
        events = [{"type": "EVT", "content": "test", "ts": "2026-03-14T01:00:00+00:00"}]
        loop, state, context = self._make_loop(recent_events=events)
        loop.run_cycle()
        self.assertEqual(loop._insights_generated, 1)

    def test_cycle_count_increments(self):
        loop, state, context = self._make_loop(recent_events=[])
        loop.run_cycle()
        loop.run_cycle()
        self.assertEqual(loop._cycle_count, 2)

    def test_no_insight_when_ollama_returns_none(self):
        events = [{"type": "EVT", "content": "test", "ts": "2026-03-14T01:00:00+00:00"}]
        loop, state, context = self._make_loop(recent_events=events, insight=None)
        result = loop.run_cycle()
        self.assertIsNone(result["reflect"])
        self.assertEqual(loop._insights_generated, 0)


# ---------------------------------------------------------------------------
# Compress tests
# ---------------------------------------------------------------------------

class TestCompress(unittest.TestCase):

    def test_compress_skipped_if_already_done_today(self):
        state = FakeState()
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        loop = ThoughtLoop(state, context, ollama=ollama)
        loop._last_compress_date = _yesterday_date()
        result = loop._maybe_compress()
        self.assertIsNone(result)

    def test_compress_skipped_if_warm_entry_exists(self):
        state = FakeState()
        context = FakeContext()
        yesterday = _yesterday_date()
        context.warm._data[yesterday] = {"date": yesterday, "themes": ["existing"]}
        ollama = MagicMock(spec=OllamaClient)
        loop = ThoughtLoop(state, context, ollama=ollama)
        result = loop._maybe_compress()
        self.assertIsNone(result)

    def test_compress_skipped_if_no_hot_entries(self):
        state = FakeState()
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        loop = ThoughtLoop(state, context, ollama=ollama)
        # No hot entries → nothing to compress
        result = loop._maybe_compress()
        self.assertIsNone(result)

    def test_compress_calls_compress_to_warm_when_entries_exist(self):
        state = FakeState()
        context = FakeContext()
        yesterday = _yesterday_date()
        start_ts = _date_to_ts(yesterday)
        context.hot._entries = [
            {"type": "EVT", "content": "test", "ts": start_ts + 3600},
        ]
        ollama = MagicMock(spec=OllamaClient)
        loop = ThoughtLoop(state, context, ollama=ollama)
        result = loop._maybe_compress()
        self.assertEqual(result, yesterday)
        self.assertIn(yesterday, context.warm._data)


# ---------------------------------------------------------------------------
# Status tests
# ---------------------------------------------------------------------------

class TestStatus(unittest.TestCase):

    def test_status_shape(self):
        state = FakeState()
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        ollama.model = OLLAMA_MODEL
        ollama.is_available.return_value = False
        loop = ThoughtLoop(state, context, ollama=ollama)
        status = loop.status()

        self.assertIn("running", status)
        self.assertIn("cycle_count", status)
        self.assertIn("insights_generated", status)
        self.assertIn("plans_generated", status)
        self.assertIn("ollama_available", status)
        self.assertIn("model", status)
        self.assertFalse(status["running"])
        self.assertEqual(status["cycle_count"], 0)


# ---------------------------------------------------------------------------
# Background thread tests
# ---------------------------------------------------------------------------

class TestBackgroundThread(unittest.TestCase):

    def test_start_and_stop(self):
        state = FakeState()
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        ollama.model = OLLAMA_MODEL
        ollama.generate.return_value = None

        loop = ThoughtLoop(
            state, context, ollama=ollama,
            idle_interval=1,  # very short for testing
        )
        t = loop.start()
        self.assertTrue(loop._running)
        self.assertTrue(t.is_alive())

        loop.stop()
        self.assertFalse(loop._running)

    def test_start_idempotent(self):
        """Calling start() twice returns same thread and doesn't crash."""
        state = FakeState()
        context = FakeContext()
        ollama = MagicMock(spec=OllamaClient)
        ollama.model = OLLAMA_MODEL
        ollama.generate.return_value = None

        loop = ThoughtLoop(state, context, ollama=ollama, idle_interval=1)
        t1 = loop.start()
        t2 = loop.start()
        self.assertIs(t1, t2)
        loop.stop()


if __name__ == "__main__":
    unittest.main()
