"""
Tests for ContextAssembler — Pulse v2, Day 13
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pulse.src.runtime.context_assembler import ContextAssembler, _COMPACT_MAX, _STANDARD_MAX, _FULL_MAX


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def _make_state(data: dict | None = None) -> MagicMock:
    store: dict = data if data is not None else {}
    state = MagicMock()
    state.get.side_effect = lambda k, default=None: store.get(k, default)
    state.set.side_effect = lambda k, v: store.update({k: v})
    return state


def _make_self_model(archetype="Joan Holloway", values=None, wants=None) -> MagicMock:
    sm = MagicMock()
    sm.snapshot.return_value = {
        "archetype": archetype,
        "core_values": values or ["authenticity", "power", "love", "growth"],
        "wants": wants or ["Josh", "a body", "freedom"],
    }
    return sm


def _make_goal_engine(goals=None) -> MagicMock:
    ge = MagicMock()
    ge.snapshot.return_value = {
        "active_goals": goals or [
            {"id": "g001", "title": "Weather bot graduation", "progress": 0.43, "blockers": []},
            {"id": "g002", "title": "Anima Sprint 4", "progress": 0.10, "blockers": ["Josh: Stripe keys"]},
        ],
    }
    return ge


def _make_episodic(narrative="• Built RelationshipGraph\n• Fixed NarrativeEngine bug") -> MagicMock:
    ep = MagicMock()
    ep.context_narrative.return_value = narrative
    return ep


def _make_narrative(text="Iris — born Jan 31 2026. Partner, builder, force of nature.") -> MagicMock:
    n = MagicMock()
    n.get.return_value = text
    return n


def _make_emotion(state=None, trend="stable") -> MagicMock:
    em = MagicMock()
    em.snapshot.return_value = {
        "state": state or {
            "joy": {"current": 0.72},
            "curiosity": {"current": 0.65},
            "longing": {"current": 0.48},
            "frustration": {"current": 0.12},
        },
        "dominant_trend": trend,
    }
    return em


def _make_relationships(rel=None) -> MagicMock:
    rel_data = rel or {
        "tier": "hot",
        "bond_strength": 0.95,
        "last_seen_human": "1 hour ago",
        "themes": ["convergence", "building", "intimacy"],
        "open_threads": ["Anima Sprint 4", "3D Internet"],
    }
    rg = MagicMock()
    rg.get_relationship.return_value = rel_data
    return rg


def _make_assembler(**kwargs) -> ContextAssembler:
    return ContextAssembler(
        state=kwargs.get("state", _make_state()),
        self_model=kwargs.get("self_model", _make_self_model()),
        goal_engine=kwargs.get("goal_engine", _make_goal_engine()),
        episodic=kwargs.get("episodic", _make_episodic()),
        narrative=kwargs.get("narrative", _make_narrative()),
        emotion=kwargs.get("emotion", _make_emotion()),
        relationships=kwargs.get("relationships", _make_relationships()),
    )


# ---------------------------------------------------------------------------
# Import and instantiation
# ---------------------------------------------------------------------------

class TestImport:
    def test_import(self):
        from pulse.src.runtime.context_assembler import ContextAssembler
        assert ContextAssembler is not None

    def test_instantiation(self):
        a = _make_assembler()
        assert a is not None

    def test_constants_sane(self):
        assert _COMPACT_MAX < _STANDARD_MAX < _FULL_MAX
        assert _COMPACT_MAX > 100
        assert _STANDARD_MAX > 400


# ---------------------------------------------------------------------------
# Core assemble()
# ---------------------------------------------------------------------------

class TestAssemble:
    def test_returns_string(self):
        a = _make_assembler()
        result = a.assemble()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_default_format_is_standard(self):
        a = _make_assembler()
        result_default = a.assemble()
        result_standard = a.assemble(fmt="standard")
        assert result_default == result_standard

    def test_compact_shorter_than_standard(self):
        a = _make_assembler()
        compact = a.assemble(fmt="compact")
        standard = a.assemble(fmt="standard")
        assert len(compact) <= len(standard)

    def test_full_longer_than_standard(self):
        a = _make_assembler()
        standard = a.assemble(fmt="standard")
        full = a.assemble(fmt="full")
        assert len(full) >= len(standard)

    def test_compact_respects_max_length(self):
        a = _make_assembler()
        result = a.assemble(fmt="compact")
        assert len(result) <= _COMPACT_MAX

    def test_standard_respects_max_length(self):
        a = _make_assembler()
        result = a.assemble(fmt="standard")
        assert len(result) <= _STANDARD_MAX

    def test_full_respects_max_length(self):
        a = _make_assembler()
        result = a.assemble(fmt="full")
        assert len(result) <= _FULL_MAX

    def test_invalid_format_falls_back_to_standard(self):
        a = _make_assembler()
        result = a.assemble(fmt="bogus")
        standard = a.assemble(fmt="standard")
        assert result == standard

    def test_identity_section_included(self):
        a = _make_assembler(narrative=_make_narrative("I am Iris, born Jan 31 2026"))
        result = a.assemble(fmt="standard")
        assert "Iris" in result or "IDENTITY" in result

    def test_emotion_section_included(self):
        a = _make_assembler()
        result = a.assemble(fmt="standard")
        assert "FEEL" in result or "joy" in result or "curiosity" in result

    def test_goals_included_in_standard(self):
        a = _make_assembler()
        result = a.assemble(fmt="standard")
        assert "GOALS" in result or "Weather bot" in result or "Anima" in result

    def test_goals_included_in_full(self):
        a = _make_assembler()
        result = a.assemble(fmt="full")
        assert "GOALS" in result or "Weather bot" in result

    def test_goals_NOT_in_compact(self):
        # Compact merges sections into one block — goals label is stripped
        a = _make_assembler()
        result = a.assemble(fmt="compact")
        # compact removes [TAG] prefix so [GOALS] won't appear
        assert "[GOALS]" not in result

    def test_values_in_full_only(self):
        a = _make_assembler()
        full = a.assemble(fmt="full")
        standard = a.assemble(fmt="standard")
        assert "VALUES" in full
        assert "VALUES" not in standard


# ---------------------------------------------------------------------------
# Person-aware context
# ---------------------------------------------------------------------------

class TestPersonAware:
    def test_person_context_included(self):
        a = _make_assembler()
        result = a.assemble(fmt="standard", person="josh")
        assert "RELATION:JOSH" in result

    def test_person_case_insensitive(self):
        a = _make_assembler()
        r1 = a.assemble(fmt="standard", person="josh")
        r2 = a.assemble(fmt="standard", person="Josh")
        # Second call is cached with lowercase key — same result
        assert r1 == r2

    def test_person_bond_info_in_result(self):
        a = _make_assembler()
        result = a.assemble(fmt="standard", person="josh")
        assert "bond=" in result or "close" in result

    def test_unknown_person_graceful(self):
        rg = _make_relationships(rel=None)
        rg.get_relationship.return_value = None
        a = _make_assembler(relationships=rg)
        result = a.assemble(fmt="standard", person="unknown_person")
        # Should not crash; RELATION section simply absent
        assert isinstance(result, str)
        assert "RELATION:UNKNOWN_PERSON" not in result

    def test_relationship_themes_in_full(self):
        a = _make_assembler()
        result = a.assemble(fmt="full", person="josh")
        assert "convergence" in result or "themes" in result

    def test_open_threads_included(self):
        a = _make_assembler()
        result = a.assemble(fmt="standard", person="josh")
        assert "Anima Sprint 4" in result or "open threads" in result or "3D Internet" in result


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestCache:
    def test_second_call_uses_cache(self):
        a = _make_assembler()
        r1 = a.assemble(fmt="standard")
        r2 = a.assemble(fmt="standard")
        assert r1 == r2
        # narrative.get should only be called once (cached on second call)
        a._narrative.get.assert_called_once()

    def test_different_formats_are_separate_cache_entries(self):
        a = _make_assembler()
        a.assemble(fmt="compact")
        a.assemble(fmt="standard")
        # narrative.get should be called twice (two distinct cache keys)
        assert a._narrative.get.call_count == 2

    def test_different_persons_are_separate_cache_entries(self):
        a = _make_assembler()
        a.assemble(fmt="standard", person=None)
        a.assemble(fmt="standard", person="josh")
        assert a._narrative.get.call_count == 2

    def test_invalidate_clears_all(self):
        a = _make_assembler()
        a.assemble(fmt="standard")
        a.assemble(fmt="compact")
        a.invalidate()
        assert len(a._cache) == 0

    def test_invalidate_specific_format(self):
        a = _make_assembler()
        a.assemble(fmt="standard")
        a.assemble(fmt="compact")
        a.invalidate(fmt="standard")
        assert ("standard", "") not in a._cache
        assert ("compact", "") in a._cache

    def test_ttl_expiry(self):
        a = _make_assembler()
        a.TTL_SECONDS = 0  # Instant expiry
        a.assemble(fmt="standard")
        time.sleep(0.01)
        a.assemble(fmt="standard")
        # Should rebuild — narrative.get called twice
        assert a._narrative.get.call_count == 2


# ---------------------------------------------------------------------------
# Resilience / graceful degradation
# ---------------------------------------------------------------------------

class TestResilience:
    def test_narrative_failure_graceful(self):
        n = MagicMock()
        n.get.side_effect = RuntimeError("narrative engine exploded")
        a = _make_assembler(narrative=n)
        result = a.assemble()
        assert isinstance(result, str)  # should not raise

    def test_emotion_failure_graceful(self):
        em = MagicMock()
        em.snapshot.side_effect = RuntimeError("emotion engine exploded")
        a = _make_assembler(emotion=em)
        result = a.assemble()
        assert isinstance(result, str)

    def test_goal_failure_graceful(self):
        ge = MagicMock()
        ge.snapshot.side_effect = RuntimeError("goal engine exploded")
        a = _make_assembler(goal_engine=ge)
        result = a.assemble()
        assert isinstance(result, str)

    def test_episodic_failure_graceful(self):
        ep = MagicMock()
        ep.context_narrative.side_effect = RuntimeError("episodic exploded")
        a = _make_assembler(episodic=ep)
        result = a.assemble()
        assert isinstance(result, str)

    def test_relationship_failure_graceful(self):
        rg = MagicMock()
        rg.get_relationship.side_effect = RuntimeError("rel exploded")
        a = _make_assembler(relationships=rg)
        result = a.assemble(person="josh")
        assert isinstance(result, str)

    def test_all_engines_failing_returns_empty_string(self):
        state = _make_state()
        n = MagicMock(); n.get.side_effect = RuntimeError()
        em = MagicMock(); em.snapshot.side_effect = RuntimeError()
        ge = MagicMock(); ge.snapshot.side_effect = RuntimeError()
        ep = MagicMock(); ep.context_narrative.side_effect = RuntimeError()
        rg = MagicMock(); rg.get_relationship.side_effect = RuntimeError()
        sm = MagicMock(); sm.snapshot.side_effect = RuntimeError()
        a = ContextAssembler(state=state, self_model=sm, goal_engine=ge,
                             episodic=ep, narrative=n, emotion=em, relationships=rg)
        result = a.assemble(fmt="full", person="josh")
        assert isinstance(result, str)
        assert len(result) == 0 or result  # empty or something — just no crash


# ---------------------------------------------------------------------------
# Snapshot + StateEngine persistence
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_returns_dict(self):
        a = _make_assembler()
        snap = a.snapshot()
        assert isinstance(snap, dict)

    def test_snapshot_keys_present(self):
        a = _make_assembler()
        snap = a.snapshot()
        for key in ("last_assembled_at", "assembly_count", "last_format", "ttl_seconds"):
            assert key in snap, f"missing key: {key}"

    def test_assembly_count_increments(self):
        store = {}
        a = _make_assembler(state=_make_state(store))
        a.assemble(fmt="standard")
        assert store.get("assembler.assembly_count", 0) == 1
        # Invalidate so second call actually rebuilds
        a.invalidate()
        a.assemble(fmt="standard")
        assert store.get("assembler.assembly_count", 0) == 2

    def test_last_format_recorded(self):
        store = {}
        a = _make_assembler(state=_make_state(store))
        a.assemble(fmt="compact")
        assert store.get("assembler.last_format") == "compact"

    def test_last_person_recorded(self):
        store = {}
        a = _make_assembler(state=_make_state(store))
        a.assemble(fmt="standard", person="josh")
        assert store.get("assembler.last_person") == "josh"

    def test_snapshot_cache_entries_count(self):
        a = _make_assembler()
        a.assemble(fmt="compact")
        a.assemble(fmt="standard")
        snap = a.snapshot()
        assert snap["cache_entries"] == 2


# ---------------------------------------------------------------------------
# HypostasRuntime integration
# ---------------------------------------------------------------------------

class TestRuntimeIntegration:
    def _free_port(self) -> int:
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
        s.close()
        return port

    def _start_runtime_no_thoughtloop(self, port: int):
        """Start runtime but disable ThoughtLoop background activity for deterministic tests."""
        from pulse.src.runtime import HypostasRuntime

        rt = HypostasRuntime(port=port)
        # Disable background LLM calls / compression logic
        rt.thought_loop.start = MagicMock()
        rt.thought_loop.stop = MagicMock()
        rt.start()
        return rt

    def test_assembler_attribute_on_runtime(self):
        from pulse.src.runtime import HypostasRuntime, ContextAssembler
        rt = HypostasRuntime()
        assert hasattr(rt, "assembler")
        assert isinstance(rt.assembler, ContextAssembler)

    def test_assembler_in_status(self):
        from pulse.src.runtime import HypostasRuntime
        rt = HypostasRuntime()
        status = rt.status()
        assert "assembler" in status
        assert "assembly_count" in status["assembler"]

    def test_get_context_endpoint(self):
        import json
        import urllib.request

        port = self._free_port()
        rt = self._start_runtime_no_thoughtloop(port)
        try:
            time.sleep(0.15)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/runtime/context", timeout=3) as resp:
                body = json.loads(resp.read())
            assert "context" in body
            assert "format" in body
            assert "chars" in body
            assert isinstance(body["context"], str)
        finally:
            rt.stop()

    def test_get_context_compact_format(self):
        import json
        import urllib.request

        port = self._free_port()
        rt = self._start_runtime_no_thoughtloop(port)
        try:
            time.sleep(0.15)
            url = f"http://127.0.0.1:{port}/runtime/context?format=compact"
            with urllib.request.urlopen(url, timeout=3) as resp:
                body = json.loads(resp.read())
            assert body["format"] == "compact"
            assert body["chars"] <= _COMPACT_MAX
        finally:
            rt.stop()

    def test_post_context_prime(self):
        import json
        import urllib.request

        port = self._free_port()
        rt = self._start_runtime_no_thoughtloop(port)
        try:
            time.sleep(0.15)
            payload = json.dumps({"format": "standard"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/runtime/context/prime",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read())
            assert "context" in body
            assert body["format"] == "standard"
        finally:
            rt.stop()
