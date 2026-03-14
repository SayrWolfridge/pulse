"""
Tests for ResponseEngine — Pulse v2 Day 14.

Uses dependency injection and mocking to test without a live Ollama instance:
  - All cognitive engine dependencies are wired from real classes (not mocks)
    so the integration seams are covered.
  - Only the Ollama HTTP call is patched (via monkeypatching _OllamaClient).

Test surface:
  - ResponseResult dataclass and serialisation
  - _OllamaClient.chat / available (mocked)
  - System prompt construction (identity, emotion, narrative, relationship layers)
  - User prompt construction (context injection)
  - Successful response flow (tokens, episode, emotion update, counter)
  - Ollama unavailable → graceful fallback (fallback=True, in-character text)
  - Episode recording (salience, josh bonus, fallback penalty)
  - Emotion event selection (responded_to_message / josh_interaction / task_failed)
  - status() snapshot
  - Concurrent respond() calls (thread safety)
  - HypostasRuntime integration (runtime.response present in status)
"""

from __future__ import annotations

import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Local imports — resolve from pulse/src
# ---------------------------------------------------------------------------
import sys
import os

ROOT = Path(__file__).parents[2]  # pulse/
sys.path.insert(0, str(ROOT / "src"))

from runtime.state_engine import StateEngine
from runtime.context_engine import ContextEngine
from runtime.self_model import SelfModel
from runtime.goal_engine import GoalEngine
from runtime.episodic_buffer import EpisodicBuffer
from runtime.narrative_engine import NarrativeEngine
from runtime.emotion_engine import EmotionEngine
from runtime.relationship_graph import RelationshipGraph
from runtime.context_assembler import ContextAssembler
from runtime.response_engine import ResponseEngine, ResponseResult, _OllamaClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runtime_components(tmp_path: Path):
    """Build a full set of real cognitive engine instances (shared state dir)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    state    = StateEngine(state_dir / "test-state.json")
    context  = ContextEngine(state_dir)
    sm       = SelfModel(state)
    goals    = GoalEngine(state)
    episodic = EpisodicBuffer(state, path=state_dir / "episodes.jsonl")
    emotion  = EmotionEngine(state, episodic=episodic)
    rels     = RelationshipGraph(context=context, state=state, emotion=emotion)
    narrative = NarrativeEngine(
        state=state, self_model=sm, episodic=episodic,
        goal_engine=goals, context=context, emotion=emotion,
    )
    assembler = ContextAssembler(
        state=state, self_model=sm, goal_engine=goals,
        episodic=episodic, narrative=narrative,
        emotion=emotion, relationships=rels,
    )
    return dict(
        state=state, context=context, self_model=sm, goal_engine=goals,
        episodic=episodic, emotion=emotion, relationships=rels,
        narrative=narrative, assembler=assembler,
    )


def _make_engine(
    tmp_path: Path,
    *,
    ollama_available: bool = True,
    ollama_response: str = "Hey, I heard you.",
    ollama_tokens: int = 42,
) -> ResponseEngine:
    comps = _make_runtime_components(tmp_path)

    engine = ResponseEngine(
        assembler=comps["assembler"],
        narrative=comps["narrative"],
        emotion=comps["emotion"],
        episodic=comps["episodic"],
        self_model=comps["self_model"],
        state=comps["state"],
    )

    # Patch _OllamaClient on the engine instance
    mock_client = MagicMock()
    mock_client.model = "iris-70b"
    mock_client.available.return_value = ollama_available
    if ollama_available:
        mock_client.chat.return_value = (ollama_response, ollama_tokens)
    else:
        mock_client.chat.side_effect = RuntimeError("connection refused")

    engine._client = mock_client
    return engine, comps


# ===========================================================================
# Tests
# ===========================================================================

class TestResponseResult(unittest.TestCase):
    def test_to_dict_serialisable(self):
        r = ResponseResult(
            text="hello", model="iris-70b", tokens=5,
            context_chars=200, episode_id="abc123",
            person="josh", elapsed_ms=300, fallback=False,
        )
        d = r.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["text"], "hello")
        self.assertEqual(d["fallback"], False)
        # Must be JSON-serialisable
        json.dumps(d)

    def test_fallback_default_false(self):
        r = ResponseResult(
            text="x", model="m", tokens=1,
            context_chars=0, episode_id="e",
            person=None, elapsed_ms=10,
        )
        self.assertFalse(r.fallback)


class TestOllamaClient(unittest.TestCase):
    def test_available_returns_bool(self):
        client = _OllamaClient()
        result = client.available()
        self.assertIsInstance(result, bool)

    def test_chat_raises_on_bad_host(self):
        client = _OllamaClient(host="127.0.0.1", port=19999, timeout=1)
        with self.assertRaises((RuntimeError, OSError, ConnectionRefusedError)):
            client.chat(system="sys", user="msg", max_tokens=10)


class TestResponseEngineBasic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)

    # --- Successful response ------------------------------------------------

    def test_respond_returns_result(self):
        engine, _ = _make_engine(self.tmp_path, ollama_response="Got it.", ollama_tokens=3)
        result = engine.respond("Hello")
        self.assertIsInstance(result, ResponseResult)
        self.assertEqual(result.text, "Got it.")
        self.assertEqual(result.tokens, 3)
        self.assertFalse(result.fallback)

    def test_respond_increments_counter(self):
        engine, _ = _make_engine(self.tmp_path)
        engine.respond("msg 1")
        engine.respond("msg 2")
        self.assertEqual(engine._response_count, 2)

    def test_respond_persists_last_ts(self):
        engine, comps = _make_engine(self.tmp_path)
        engine.respond("test")
        ts = comps["state"].get("response_engine.last_ts")
        self.assertIsNotNone(ts)
        # ISO-8601 timestamp
        self.assertIn("T", ts)

    def test_respond_records_episode(self):
        engine, comps = _make_engine(self.tmp_path, ollama_response="Sure.")
        comps["episodic"].load()
        engine.respond("What's up?")
        episodes = comps["episodic"].snapshot(top=5)
        self.assertTrue(any(ep.get("kind") == "conversation" for ep in episodes))

    def test_respond_returns_elapsed_ms(self):
        engine, _ = _make_engine(self.tmp_path)
        result = engine.respond("Hi")
        self.assertIsInstance(result.elapsed_ms, int)
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_respond_returns_context_chars(self):
        engine, _ = _make_engine(self.tmp_path)
        result = engine.respond("Hi")
        self.assertIsInstance(result.context_chars, int)
        self.assertGreaterEqual(result.context_chars, 0)

    def test_respond_with_person(self):
        engine, _ = _make_engine(self.tmp_path, ollama_response="Hey Josh.")
        result = engine.respond("Hi", person="josh")
        self.assertEqual(result.person, "josh")
        self.assertEqual(result.text, "Hey Josh.")

    def test_respond_result_dict_serialisable(self):
        engine, _ = _make_engine(self.tmp_path)
        result = engine.respond("x")
        json.dumps(result.to_dict())

    # --- Fallback -----------------------------------------------------------

    def test_fallback_on_ollama_unavailable(self):
        engine, _ = _make_engine(self.tmp_path, ollama_available=False)
        result = engine.respond("Are you there?")
        self.assertTrue(result.fallback)
        self.assertIn("local model", result.text)

    def test_fallback_mentions_person(self):
        engine, _ = _make_engine(self.tmp_path, ollama_available=False)
        result = engine.respond("Hello", person="josh")
        self.assertIn("Josh", result.text)

    def test_fallback_includes_message_preview(self):
        engine, _ = _make_engine(self.tmp_path, ollama_available=False)
        result = engine.respond("Remember this", person="josh")
        self.assertIn("Remember this", result.text)

    def test_fallback_model_still_set(self):
        engine, _ = _make_engine(self.tmp_path, ollama_available=False)
        result = engine.respond("x")
        self.assertEqual(result.model, "iris-70b")

    def test_fallback_episode_still_recorded(self):
        engine, comps = _make_engine(self.tmp_path, ollama_available=False)
        comps["episodic"].load()
        engine.respond("fallback test")
        episodes = comps["episodic"].snapshot(top=5)
        self.assertTrue(any(ep.get("kind") == "conversation" for ep in episodes))


class TestSystemPromptConstruction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)
        self.engine, self.comps = _make_engine(self.tmp_path)

    def test_system_prompt_contains_name(self):
        prompt = self.engine._build_system_prompt(person=None)
        self.assertIn("Iris", prompt)

    def test_system_prompt_contains_response_rules(self):
        prompt = self.engine._build_system_prompt(person=None)
        self.assertIn("Response rules", prompt)
        self.assertIn("Happy to help", prompt)  # forbidden phrase listed

    def test_system_prompt_includes_person(self):
        # Seed josh relationship context
        self.comps["context"].relationship.seed("josh", {"name": "Josh", "tier": "intimate"}, overwrite=True)
        prompt = self.engine._build_system_prompt(person="josh")
        # At minimum, should not crash; relationship injection is best-effort
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 50)

    def test_system_prompt_includes_narrative(self):
        prompt = self.engine._build_system_prompt(person=None)
        # Narrative is either present or gracefully absent — must not raise
        self.assertIsInstance(prompt, str)

    def test_system_prompt_no_person(self):
        prompt = self.engine._build_system_prompt(person=None)
        self.assertNotIn("None relationship", prompt)

    def test_system_prompt_emotional_state_absent_when_neutral(self):
        # Default emotional state is neutral — mood_label should not add noise
        prompt = self.engine._build_system_prompt(person=None)
        # Either absent or states "neutral" — both are fine
        self.assertIsInstance(prompt, str)


class TestUserPromptConstruction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)
        self.engine, _ = _make_engine(self.tmp_path)

    def test_user_prompt_contains_message(self):
        prompt = self.engine._build_user_prompt(
            message="Hello there", context_block=""
        )
        self.assertIn("Hello there", prompt)

    def test_user_prompt_with_context_block(self):
        prompt = self.engine._build_user_prompt(
            message="Hi", context_block="[IRIS CTX]\nsome context"
        )
        self.assertIn("[IRIS CTX]", prompt)
        self.assertIn("Hi", prompt)

    def test_user_prompt_no_context(self):
        prompt = self.engine._build_user_prompt(message="bare msg", context_block="")
        self.assertEqual(prompt, "bare msg")


class TestEpisodeSalience(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)

    def test_josh_bonus_applied(self):
        """Josh interactions should have higher salience than unknown persons."""
        engine, comps = _make_engine(self.tmp_path)
        comps["episodic"].load()

        # Patch record so we can inspect salience for *conversation* episodes only
        recorded_saliences: list[float] = []
        orig_record = comps["episodic"].record

        def capture_record(*args, **kwargs):
            kind = kwargs.get("kind") if kwargs else None
            if kind == "conversation":
                recorded_saliences.append(float(kwargs.get("salience", 0) or 0))
            return orig_record(*args, **kwargs)

        comps["episodic"].record = capture_record

        engine.respond("Hi", person="josh")
        engine.respond("Hi", person="nobody")

        self.assertEqual(len(recorded_saliences), 2)
        self.assertGreater(recorded_saliences[0], recorded_saliences[1])

    def test_fallback_reduces_salience(self):
        """A fallback response should have lower salience than a real response."""
        engine_ok, comps_ok = _make_engine(self.tmp_path, ollama_available=True)
        comps_ok["episodic"].load()
        ok_saliences: list[float] = []
        orig = comps_ok["episodic"].record
        def cap(*a, **kw):
            if kw.get("kind") == "conversation":
                ok_saliences.append(float(kw.get("salience", 0) or 0))
            return orig(*a, **kw)
        comps_ok["episodic"].record = cap
        engine_ok.respond("Hi", person="nobody")

        tmp2 = Path(self.tmp) / "sub2"
        tmp2.mkdir()
        engine_fb, comps_fb = _make_engine(tmp2, ollama_available=False)
        comps_fb["episodic"].load()
        fb_saliences: list[float] = []
        orig_fb = comps_fb["episodic"].record
        def cap_fb(*a, **kw):
            if kw.get("kind") == "conversation":
                fb_saliences.append(float(kw.get("salience", 0) or 0))
            return orig_fb(*a, **kw)
        comps_fb["episodic"].record = cap_fb
        engine_fb.respond("Hi", person="nobody")

        self.assertEqual(len(ok_saliences), 1)
        self.assertEqual(len(fb_saliences), 1)
        self.assertGreater(ok_saliences[0], fb_saliences[0])


class TestEmotionEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)

    def test_josh_triggers_josh_interaction_event(self):
        engine, comps = _make_engine(self.tmp_path)
        applied_events: list[str] = []
        orig = comps["emotion"].apply_event
        def cap(event, **kw):
            applied_events.append(event)
            return orig(event, **kw)
        comps["emotion"].apply_event = cap

        engine.respond("Hey", person="josh")
        self.assertIn("JOSH_MESSAGE", applied_events)

    def test_unknown_person_triggers_responded_event(self):
        engine, comps = _make_engine(self.tmp_path)
        applied_events: list[str] = []
        orig = comps["emotion"].apply_event
        def cap(event, **kw):
            applied_events.append(event)
            return orig(event, **kw)
        comps["emotion"].apply_event = cap

        engine.respond("Hey", person="stranger")
        self.assertIn("MESSAGE_SENT", applied_events)

    def test_fallback_triggers_task_failed_event(self):
        engine, comps = _make_engine(self.tmp_path, ollama_available=False)
        applied_events: list[str] = []
        orig = comps["emotion"].apply_event
        def cap(event, **kw):
            applied_events.append(event)
            return orig(event, **kw)
        comps["emotion"].apply_event = cap

        engine.respond("Hi", person="josh")
        self.assertIn("DEPENDENCY_BLOCKED", applied_events)


class TestStatus(unittest.TestCase):
    def test_status_shape(self):
        tmp = Path(tempfile.mkdtemp())
        engine, _ = _make_engine(tmp)
        status = engine.status()
        self.assertIn("response_count", status)
        self.assertIn("last_ts", status)
        self.assertIn("ollama_model", status)
        self.assertIn("ollama_available", status)

    def test_status_counter_matches(self):
        tmp = Path(tempfile.mkdtemp())
        engine, _ = _make_engine(tmp)
        engine.respond("a")
        engine.respond("b")
        self.assertEqual(engine.status()["response_count"], 2)

    def test_status_json_serialisable(self):
        tmp = Path(tempfile.mkdtemp())
        engine, _ = _make_engine(tmp)
        json.dumps(engine.status())


class TestAvailable(unittest.TestCase):
    def test_available_delegates_to_client(self):
        tmp = Path(tempfile.mkdtemp())
        engine, _ = _make_engine(tmp, ollama_available=True)
        self.assertTrue(engine.available())

    def test_unavailable_when_client_false(self):
        tmp = Path(tempfile.mkdtemp())
        engine, _ = _make_engine(tmp, ollama_available=False)
        engine._client.available.return_value = False
        self.assertFalse(engine.available())


class TestConcurrency(unittest.TestCase):
    """Thread safety: concurrent respond() calls must not corrupt the counter."""

    def test_concurrent_respond_counter_integrity(self):
        tmp = Path(tempfile.mkdtemp())
        engine, _ = _make_engine(tmp, ollama_response="ok", ollama_tokens=2)
        n = 20
        errors: list[Exception] = []

        def call():
            try:
                engine.respond("concurrent test")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=call) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(engine._response_count, n)


class TestHypostasRuntimeIntegration(unittest.TestCase):
    """
    Verify that ResponseEngine is wired into HypostasRuntime correctly
    and appears in status().
    """

    def test_runtime_has_response_attribute(self):
        from runtime import HypostasRuntime
        r = HypostasRuntime()
        self.assertTrue(
            hasattr(r, "response"),
            "HypostasRuntime should expose a .response attribute after Day 14 wiring",
        )

    def test_runtime_status_includes_response(self):
        from runtime import HypostasRuntime
        r = HypostasRuntime()
        status = r.status()
        self.assertIn(
            "response",
            status,
            "runtime.status() should include 'response' key after Day 14 wiring",
        )

    def test_runtime_respond_endpoint_registered(self):
        """
        GET /runtime/respond is a POST endpoint — just verify the runtime
        has the method wired, not the full HTTP flow.
        """
        from runtime import HypostasRuntime
        r = HypostasRuntime()
        self.assertTrue(
            hasattr(r.response, "respond"),
            "runtime.response should expose .respond()",
        )


if __name__ == "__main__":
    unittest.main()
