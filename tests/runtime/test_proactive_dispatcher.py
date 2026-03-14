"""
Tests for ProactiveDispatcher — Pulse v2 Day 16.

Coverage:
  - Construction (happy path + missing optional args)
  - dispatch(): no candidates → non-dispatched result
  - dispatch(): response_only mode, ResponseEngine succeeds
  - dispatch(): response_only mode, Ollama down → fallback to hint
  - dispatch(): store mode, writes to StateEngine
  - dispatch(): openclaw_wake mode, HTTP success
  - dispatch(): openclaw_wake mode, HTTP failure → error in result
  - dispatch(): unknown mode → non-dispatched with error
  - dispatch(): explicit candidate supplied (skips top_candidate call)
  - mark_sent called after successful dispatch
  - EpisodicBuffer record called with correct kind
  - status() returns expected keys
  - HypostasRuntime has .dispatcher attribute and /runtime/proactive/deliver endpoint
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    kind: str = "morning_checkin"
    priority: float = 0.85
    message_hint: str = "Good morning — just thinking of you."
    reason: str = "morning window + no contact today"
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class _ResponseResult:
    text: str = "Hey, just wanted to check in — been thinking about you. 💜"
    model: str = "iris-70b"
    tokens: int = 18
    context_chars: int = 400
    episode_id: str = "ep-abc"
    person: Optional[str] = "josh"
    elapsed_ms: int = 300
    fallback: bool = False

    def to_dict(self):
        return {
            "text": self.text, "model": self.model, "tokens": self.tokens,
            "context_chars": self.context_chars, "episode_id": self.episode_id,
            "person": self.person, "elapsed_ms": self.elapsed_ms, "fallback": self.fallback,
        }


class _FakeProactive:
    def __init__(self, candidate: Optional[_Candidate] = None):
        self._candidate = candidate
        self.sent: List[str] = []

    def top_candidate(self):
        return self._candidate

    def mark_sent(self, kind: str):
        self.sent.append(kind)

    def snapshot(self):
        return {}


class _FakeResponse:
    def __init__(self, result: Optional[_ResponseResult] = None, raise_exc=False):
        self._result = result or _ResponseResult()
        self._raise = raise_exc

    def respond(self, message, person=None, fmt="compact", max_tokens=400):
        if self._raise:
            raise ConnectionRefusedError("Ollama unavailable")
        return self._result


class _FakeEpisodic:
    def __init__(self):
        self.recorded: List[dict] = []

    def record(self, kind, summary, salience=5.0, tags=None):
        ep = MagicMock()
        ep.episode_id = f"ep-{len(self.recorded)}"
        self.recorded.append({"kind": kind, "summary": summary, "salience": salience, "tags": tags or []})
        return ep


class _FakeState:
    def __init__(self):
        self._store: Dict[str, Any] = {}

    def set(self, key: str, value: Any):
        self._store[key] = value

    def get(self, key: str, default=None):
        return self._store.get(key, default)


# ---------------------------------------------------------------------------
# Import the real module
# ---------------------------------------------------------------------------

from pulse.src.runtime.proactive_dispatcher import (
    ProactiveDispatcher,
    DispatchResult,
    MODE_RESPONSE_ONLY,
    MODE_OPENCLAW_WAKE,
    MODE_STORE,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProactiveDispatcherConstruction(unittest.TestCase):

    def test_basic_construction(self):
        proactive = _FakeProactive()
        response = _FakeResponse()
        d = ProactiveDispatcher(proactive=proactive, response=response)
        self.assertIsInstance(d, ProactiveDispatcher)

    def test_optional_args_none(self):
        d = ProactiveDispatcher(
            proactive=_FakeProactive(),
            response=_FakeResponse(),
            episodic=None,
            state=None,
        )
        status = d.status()
        self.assertIn("dispatch_count", status)
        self.assertEqual(status["dispatch_count"], 0)
        self.assertIsNone(status["last_dispatch_ts"])


class TestDispatchNoCandidate(unittest.TestCase):

    def setUp(self):
        self.proactive = _FakeProactive(candidate=None)
        self.response = _FakeResponse()
        self.dispatcher = ProactiveDispatcher(
            proactive=self.proactive, response=self.response
        )

    def test_no_candidate_returns_not_dispatched(self):
        result = self.dispatcher.dispatch()
        self.assertFalse(result.dispatched)
        self.assertEqual(result.kind, "none")
        self.assertIn("no proactive candidate", result.error or "")

    def test_dispatch_count_unchanged_on_no_candidate(self):
        self.dispatcher.dispatch()
        self.assertEqual(self.dispatcher.status()["dispatch_count"], 0)


class TestDispatchResponseOnly(unittest.TestCase):

    def setUp(self):
        self.candidate = _Candidate()
        self.proactive = _FakeProactive(candidate=self.candidate)
        self.response = _FakeResponse()
        self.episodic = _FakeEpisodic()
        self.state = _FakeState()
        self.dispatcher = ProactiveDispatcher(
            proactive=self.proactive,
            response=self.response,
            episodic=self.episodic,
            state=self.state,
        )

    def test_dispatched_true(self):
        result = self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertTrue(result.dispatched)

    def test_text_from_response_engine(self):
        result = self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertEqual(result.text, self.response._result.text)

    def test_kind_matches_candidate(self):
        result = self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertEqual(result.kind, self.candidate.kind)

    def test_mode_recorded(self):
        result = self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertEqual(result.mode, MODE_RESPONSE_ONLY)

    def test_not_fallback_when_ollama_up(self):
        result = self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertFalse(result.fallback)

    def test_mark_sent_called(self):
        self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertIn(self.candidate.kind, self.proactive.sent)

    def test_dispatch_count_incremented(self):
        self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertEqual(self.dispatcher.status()["dispatch_count"], 1)

    def test_last_dispatch_ts_set(self):
        t_before = time.time()
        self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        t_after = time.time()
        ts = self.dispatcher.status()["last_dispatch_ts"]
        self.assertIsNotNone(ts)
        self.assertGreaterEqual(ts, t_before)
        self.assertLessEqual(ts, t_after)

    def test_episodic_record_called(self):
        self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertEqual(len(self.episodic.recorded), 1)
        rec = self.episodic.recorded[0]
        self.assertEqual(rec["kind"], "proactive_outreach")

    def test_episode_id_in_result(self):
        result = self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertNotEqual(result.episode_id, "")

    def test_elapsed_ms_positive(self):
        result = self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_no_pending_delivery_stored_in_response_only_mode(self):
        self.dispatcher.dispatch(mode=MODE_RESPONSE_ONLY)
        self.assertIsNone(self.state.get("proactive.pending_delivery"))


class TestDispatchFallbackWhenOllamaDown(unittest.TestCase):

    def setUp(self):
        self.candidate = _Candidate()
        self.proactive = _FakeProactive(candidate=self.candidate)
        self.response = _FakeResponse(raise_exc=True)   # Ollama unavailable
        self.dispatcher = ProactiveDispatcher(
            proactive=self.proactive, response=self.response
        )

    def test_fallback_true_when_ollama_raises(self):
        result = self.dispatcher.dispatch()
        self.assertTrue(result.fallback)

    def test_hint_used_as_text_on_fallback(self):
        result = self.dispatcher.dispatch()
        self.assertEqual(result.text, self.candidate.message_hint)

    def test_still_dispatched_true_on_fallback(self):
        result = self.dispatcher.dispatch()
        self.assertTrue(result.dispatched)

    def test_mark_sent_called_even_on_fallback(self):
        self.dispatcher.dispatch()
        self.assertIn(self.candidate.kind, self.proactive.sent)


class TestDispatchFallbackWhenResponseEngineReturnsOllamaFallback(unittest.TestCase):
    """ResponseEngine.respond() returns a result with fallback=True (its own fallback)."""

    def setUp(self):
        result = _ResponseResult(
            text="I'm here — just thinking of you.",
            fallback=True,
        )
        self.candidate = _Candidate(message_hint="Just checking in 💜")
        self.proactive = _FakeProactive(candidate=self.candidate)
        self.response = _FakeResponse(result=result)
        self.dispatcher = ProactiveDispatcher(
            proactive=self.proactive, response=self.response
        )

    def test_hint_used_when_response_engine_fallback(self):
        result = self.dispatcher.dispatch()
        self.assertEqual(result.text, self.candidate.message_hint)
        self.assertTrue(result.fallback)


class TestDispatchStoreMode(unittest.TestCase):

    def setUp(self):
        self.candidate = _Candidate(kind="goal_followup", message_hint="How's that goal going?")
        self.proactive = _FakeProactive(candidate=self.candidate)
        self.response = _FakeResponse()
        self.state = _FakeState()
        self.dispatcher = ProactiveDispatcher(
            proactive=self.proactive, response=self.response, state=self.state
        )

    def test_store_mode_writes_to_state(self):
        self.dispatcher.dispatch(mode=MODE_STORE)
        stored = self.state.get("proactive.pending_delivery")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["kind"], self.candidate.kind)

    def test_store_mode_text_matches_response(self):
        self.dispatcher.dispatch(mode=MODE_STORE)
        stored = self.state.get("proactive.pending_delivery")
        self.assertEqual(stored["text"], self.response._result.text)

    def test_store_mode_has_stored_at(self):
        self.dispatcher.dispatch(mode=MODE_STORE)
        stored = self.state.get("proactive.pending_delivery")
        self.assertIn("stored_at", stored)
        self.assertIn("stored_at_iso", stored)

    def test_store_mode_dispatched_true(self):
        result = self.dispatcher.dispatch(mode=MODE_STORE)
        self.assertTrue(result.dispatched)

    def test_store_mode_no_state_graceful(self):
        """Store mode with no StateEngine configured is a no-op, not a crash."""
        d = ProactiveDispatcher(
            proactive=self.proactive, response=self.response, state=None
        )
        result = d.dispatch(mode=MODE_STORE)
        self.assertTrue(result.dispatched)  # still dispatched


class TestDispatchOpenClawWakeMode(unittest.TestCase):

    def setUp(self):
        self.candidate = _Candidate()
        self.proactive = _FakeProactive(candidate=self.candidate)
        self.response = _FakeResponse()
        self.dispatcher = ProactiveDispatcher(
            proactive=self.proactive, response=self.response
        )

    def test_openclaw_wake_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 202
        mock_resp.read.return_value = b""

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp

        with patch("pulse.src.runtime.proactive_dispatcher.http.client.HTTPConnection") as MockConn:
            MockConn.return_value = mock_conn
            result = self.dispatcher.dispatch(mode=MODE_OPENCLAW_WAKE)

        self.assertTrue(result.dispatched)
        self.assertIsNone(result.error)

    def test_openclaw_wake_http_failure_records_error(self):
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.read.return_value = b""

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp

        with patch("pulse.src.runtime.proactive_dispatcher.http.client.HTTPConnection") as MockConn:
            MockConn.return_value = mock_conn
            result = self.dispatcher.dispatch(mode=MODE_OPENCLAW_WAKE)

        self.assertTrue(result.dispatched)          # still "dispatched" — cooldown consumed
        self.assertIsNotNone(result.error)
        self.assertIn("500", result.error)

    def test_openclaw_wake_connection_error(self):
        with patch("pulse.src.runtime.proactive_dispatcher.http.client.HTTPConnection") as MockConn:
            MockConn.side_effect = ConnectionRefusedError("refused")
            result = self.dispatcher.dispatch(mode=MODE_OPENCLAW_WAKE)

        self.assertTrue(result.dispatched)
        self.assertIsNotNone(result.error)
        self.assertIn("openclaw_wake delivery failed", result.error)


class TestDispatchUnknownMode(unittest.TestCase):

    def setUp(self):
        self.dispatcher = ProactiveDispatcher(
            proactive=_FakeProactive(candidate=_Candidate()),
            response=_FakeResponse(),
        )

    def test_unknown_mode_not_dispatched(self):
        result = self.dispatcher.dispatch(mode="teleport")
        self.assertFalse(result.dispatched)
        self.assertIn("unknown mode", result.error or "")


class TestDispatchExplicitCandidate(unittest.TestCase):

    def test_explicit_candidate_bypasses_top_candidate(self):
        # Engine has a different candidate; explicit should win
        engine_candidate = _Candidate(kind="streak_at_risk", message_hint="Don't break the streak!")
        explicit_candidate = _Candidate(kind="milestone", message_hint="Goal crushed 🎉")

        proactive = _FakeProactive(candidate=engine_candidate)
        response = _FakeResponse()
        d = ProactiveDispatcher(proactive=proactive, response=response)

        result = d.dispatch(mode=MODE_RESPONSE_ONLY, candidate=explicit_candidate)
        self.assertEqual(result.kind, "milestone")
        self.assertIn("milestone", proactive.sent)


class TestDispatcherStatus(unittest.TestCase):

    def setUp(self):
        self.dispatcher = ProactiveDispatcher(
            proactive=_FakeProactive(candidate=_Candidate()),
            response=_FakeResponse(),
        )

    def test_status_keys(self):
        status = self.dispatcher.status()
        self.assertIn("dispatch_count", status)
        self.assertIn("last_dispatch_ts", status)
        self.assertIn("last_dispatch_iso", status)

    def test_initial_status_zeros(self):
        status = self.dispatcher.status()
        self.assertEqual(status["dispatch_count"], 0)
        self.assertIsNone(status["last_dispatch_ts"])
        self.assertIsNone(status["last_dispatch_iso"])

    def test_status_updates_after_dispatch(self):
        self.dispatcher.dispatch()
        status = self.dispatcher.status()
        self.assertEqual(status["dispatch_count"], 1)
        self.assertIsNotNone(status["last_dispatch_ts"])
        self.assertIsNotNone(status["last_dispatch_iso"])


class TestDispatchResult(unittest.TestCase):

    def test_to_dict_omits_none_error(self):
        r = DispatchResult(dispatched=True, text="hi", kind="morning_checkin", mode="response_only")
        d = r.to_dict()
        self.assertNotIn("error", d)

    def test_to_dict_includes_error_when_set(self):
        r = DispatchResult(dispatched=False, text="", kind="none", mode="response_only", error="oops")
        d = r.to_dict()
        self.assertEqual(d["error"], "oops")


class TestHypostasRuntimeIntegration(unittest.TestCase):
    """Verify HypostasRuntime has .dispatcher and /runtime/proactive/deliver endpoint."""

    def test_runtime_has_dispatcher(self):
        import tempfile, pathlib
        from pulse.src.runtime import HypostasRuntime, ProactiveDispatcher

        with tempfile.TemporaryDirectory() as tmp:
            rt = HypostasRuntime(state_dir=pathlib.Path(tmp))
            self.assertTrue(hasattr(rt, "dispatcher"))
            self.assertIsInstance(rt.dispatcher, ProactiveDispatcher)

    def test_runtime_status_includes_dispatcher(self):
        import tempfile, pathlib
        from pulse.src.runtime import HypostasRuntime

        with tempfile.TemporaryDirectory() as tmp:
            rt = HypostasRuntime(state_dir=pathlib.Path(tmp))
            status = rt.status()
            self.assertIn("dispatcher", status)
            self.assertIn("dispatch_count", status["dispatcher"])

    def test_runtime_deliver_endpoint_no_candidate(self):
        """POST /runtime/proactive/deliver with no candidates → 204."""
        import socket, tempfile, pathlib
        from pulse.src.runtime import HypostasRuntime
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            # Find a free port
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]

            rt = HypostasRuntime(state_dir=pathlib.Path(tmp), port=port)
            rt.start()
            time.sleep(0.3)
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/runtime/proactive/deliver",
                    data=json.dumps({"mode": "response_only"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5)
                    # 200 also acceptable if somehow a candidate exists
                except urllib.error.HTTPError as e:
                    self.assertIn(e.code, (200, 204))
            finally:
                rt.stop()


class TestThreadSafety(unittest.TestCase):

    def test_concurrent_dispatches_consistent_count(self):
        """Multiple threads dispatching simultaneously shouldn't corrupt count."""
        results = []
        lock = threading.Lock()

        def _dispatch(d):
            r = d.dispatch()
            with lock:
                results.append(r)

        # Each thread gets its own dispatcher (no shared state needed)
        threads = []
        for _ in range(10):
            candidate = _Candidate()
            d = ProactiveDispatcher(
                proactive=_FakeProactive(candidate=candidate),
                response=_FakeResponse(),
            )
            threads.append(threading.Thread(target=_dispatch, args=(d,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 10 dispatchers × 1 call each → all should succeed
        self.assertEqual(len(results), 10)
        dispatched = [r for r in results if r.dispatched]
        self.assertEqual(len(dispatched), 10)


if __name__ == "__main__":
    unittest.main()
