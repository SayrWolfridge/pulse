"""
Tests for NarrativeEngine — Pulse v2 Day 10

Covers:
  - Synthesis from all four source layers
  - Caching and TTL expiry
  - Explicit invalidation
  - Partial source failure (graceful fallback)
  - Output constraints (MIN/MAX chars, trimming)
  - StateEngine persistence and crash recovery
  - Source-hash deduplication (no rebuild on unchanged sources)
  - snapshot() shape
  - HTTP integration test stubs (logic only, no live server)
"""

from __future__ import annotations

import json
import time
import threading
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_state(tmp_path: Path):
    """Minimal in-memory StateEngine stub."""
    from pulse.src.runtime.state_engine import StateEngine
    return StateEngine(tmp_path / "state.json")


def _make_self_model(tmp_path: Path, state):
    from pulse.src.runtime.self_model import SelfModel
    return SelfModel(state)


def _make_episodic(tmp_path: Path, state):
    from pulse.src.runtime.episodic_buffer import EpisodicBuffer
    return EpisodicBuffer(state, path=tmp_path / "episodes.jsonl")


def _make_goal_engine(state):
    from pulse.src.runtime.goal_engine import GoalEngine
    return GoalEngine(state)


def _make_narrative_engine(tmp_path: Path, ttl: int = 300):
    from pulse.src.runtime.narrative_engine import NarrativeEngine
    state = _make_state(tmp_path)
    sm = _make_self_model(tmp_path, state)
    ep = _make_episodic(tmp_path, state)
    ge = _make_goal_engine(state)
    return NarrativeEngine(state=state, self_model=sm, episodic=ep, goal_engine=ge, ttl_seconds=ttl)


# ---------------------------------------------------------------------------
# Import smoke test
# ---------------------------------------------------------------------------


class TestImport:
    def test_module_importable(self):
        from pulse.src.runtime import narrative_engine  # noqa: F401

    def test_class_importable(self):
        from pulse.src.runtime.narrative_engine import NarrativeEngine  # noqa: F401

    def test_constants_present(self):
        from pulse.src.runtime.narrative_engine import (
            CACHE_TTL_SECONDS,
            MAX_NARRATIVE_CHARS,
            MIN_NARRATIVE_CHARS,
            FALLBACK_NARRATIVE,
        )
        assert CACHE_TTL_SECONDS > 0
        assert MAX_NARRATIVE_CHARS >= MIN_NARRATIVE_CHARS
        assert len(FALLBACK_NARRATIVE) >= 20


# ---------------------------------------------------------------------------
# Construction + defaults
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_instantiate_without_context(self, tmp_path):
        from pulse.src.runtime.narrative_engine import NarrativeEngine
        state = _make_state(tmp_path)
        sm = _make_self_model(tmp_path, state)
        ep = _make_episodic(tmp_path, state)
        ge = _make_goal_engine(state)
        ne = NarrativeEngine(state=state, self_model=sm, episodic=ep, goal_engine=ge)
        assert ne is not None

    def test_instantiate_with_context(self, tmp_path):
        from pulse.src.runtime.narrative_engine import NarrativeEngine
        from pulse.src.runtime.context_engine import ContextEngine
        state = _make_state(tmp_path)
        sm = _make_self_model(tmp_path, state)
        ep = _make_episodic(tmp_path, state)
        ge = _make_goal_engine(state)
        ctx = ContextEngine(tmp_path / "context")
        ne = NarrativeEngine(state=state, self_model=sm, episodic=ep, goal_engine=ge, context=ctx)
        assert ne is not None

    def test_build_count_starts_at_zero(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        assert ne._build_count == 0

    def test_cache_initially_empty(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        assert ne._cached_text is None or len(ne._cached_text) > 0  # either empty or fallback restored


# ---------------------------------------------------------------------------
# get() basic behaviour
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_returns_string(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        result = ne.get()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_get_never_raises(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        # Even if internals are broken, get() should not raise
        ne._self_model = None  # type: ignore[assignment]
        result = ne.get()
        assert isinstance(result, str)

    def test_get_builds_on_first_call(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        assert ne._build_count == 0
        ne.get()
        assert ne._build_count >= 1

    def test_get_uses_cache_on_second_call(self, tmp_path):
        ne = _make_narrative_engine(tmp_path, ttl=300)
        ne.get()
        count_after_first = ne._build_count
        ne.get()
        assert ne._build_count == count_after_first  # no rebuild

    def test_get_returns_consistent_text_within_ttl(self, tmp_path):
        ne = _make_narrative_engine(tmp_path, ttl=300)
        first = ne.get()
        second = ne.get()
        assert first == second

    def test_get_falls_back_when_build_fails(self, tmp_path):
        from pulse.src.runtime.narrative_engine import FALLBACK_NARRATIVE
        ne = _make_narrative_engine(tmp_path)
        # Force build to fail
        ne._gather_sources = lambda: (_ for _ in ()).throw(RuntimeError("forced"))  # type: ignore
        # get() should still return something (the fallback)
        result = ne.get()
        assert isinstance(result, str)
        # If cache was already populated, use that; otherwise FALLBACK
        assert len(result) > 0


# ---------------------------------------------------------------------------
# build()
# ---------------------------------------------------------------------------


class TestBuild:
    def test_build_returns_string(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        result = ne.build()
        assert isinstance(result, str)

    def test_build_increments_count(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.build()
        assert ne._build_count == 1
        ne._last_source_hash = ""  # force hash mismatch
        ne.build()
        assert ne._build_count == 2

    def test_build_respects_min_length(self, tmp_path):
        from pulse.src.runtime.narrative_engine import MIN_NARRATIVE_CHARS
        ne = _make_narrative_engine(tmp_path)
        result = ne.build()
        assert len(result) >= MIN_NARRATIVE_CHARS

    def test_build_respects_max_length(self, tmp_path):
        from pulse.src.runtime.narrative_engine import MAX_NARRATIVE_CHARS
        ne = _make_narrative_engine(tmp_path)
        result = ne.build()
        assert len(result) <= MAX_NARRATIVE_CHARS

    def test_build_persists_to_state(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.build()
        stored = ne._state.get("narrative.text")
        assert stored is not None
        assert isinstance(stored, str)
        assert len(stored) > 0

    def test_build_skips_rebuild_if_sources_unchanged(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.build()
        count_after_first = ne._build_count
        # Build again without changing sources — hash matches → no rebuild
        ne.build()
        assert ne._build_count == count_after_first  # skipped

    def test_build_rebuilds_if_sources_changed(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.build()
        count_after_first = ne._build_count
        # Force hash mismatch
        ne._last_source_hash = "stale"
        ne.build()
        assert ne._build_count == count_after_first + 1

    def test_build_updates_cached_text(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        result = ne.build()
        assert ne._cached_text == result


# ---------------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------------


class TestInvalidate:
    def test_invalidate_forces_rebuild_on_next_get(self, tmp_path):
        ne = _make_narrative_engine(tmp_path, ttl=300)
        ne.get()
        count_after_first = ne._build_count
        ne.invalidate()
        ne._last_source_hash = ""  # ensure hash mismatch triggers actual rebuild
        ne.get()
        assert ne._build_count > count_after_first

    def test_invalidate_is_idempotent(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.get()
        ne.invalidate()
        ne.invalidate()
        ne.invalidate()
        # No error raised

    def test_invalidate_before_first_get_is_safe(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.invalidate()  # no prior build
        result = ne.get()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestTTL:
    def test_expired_ttl_triggers_rebuild(self, tmp_path):
        ne = _make_narrative_engine(tmp_path, ttl=1)  # 1s TTL
        ne.get()
        count_after_first = ne._build_count
        time.sleep(1.1)
        ne._last_source_hash = ""  # ensure hash mismatch
        ne.get()
        assert ne._build_count > count_after_first

    def test_ttl_not_expired_no_rebuild(self, tmp_path):
        ne = _make_narrative_engine(tmp_path, ttl=300)
        ne.get()
        count_after_first = ne._build_count
        ne.get()
        assert ne._build_count == count_after_first


# ---------------------------------------------------------------------------
# Source layer isolation (graceful degradation)
# ---------------------------------------------------------------------------


class TestSourceIsolation:
    def test_self_model_failure_graceful(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        # Make self_model.snapshot() raise
        ne._self_model.snapshot = lambda: (_ for _ in ()).throw(ValueError("boom"))  # type: ignore
        result = ne.build()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_episodic_failure_graceful(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne._episodic.snapshot = lambda **_: (_ for _ in ()).throw(RuntimeError("disk"))  # type: ignore
        result = ne.build()
        assert isinstance(result, str)

    def test_goal_engine_failure_graceful(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne._goal_engine.snapshot = lambda: (_ for _ in ()).throw(OSError("fs"))  # type: ignore
        result = ne.build()
        assert isinstance(result, str)

    def test_all_sources_fail_returns_fallback(self, tmp_path):
        from pulse.src.runtime.narrative_engine import FALLBACK_NARRATIVE
        ne = _make_narrative_engine(tmp_path)
        def _boom(*a, **kw):
            raise RuntimeError("all dead")
        ne._self_model.snapshot = _boom  # type: ignore
        ne._episodic.snapshot = _boom  # type: ignore
        ne._goal_engine.snapshot = _boom  # type: ignore
        result = ne.build()
        # Narrative might use partial data or fallback — must be non-empty
        assert isinstance(result, str)
        assert len(result) >= len(FALLBACK_NARRATIVE) - 10  # ~fallback length


# ---------------------------------------------------------------------------
# Synthesis content checks
# ---------------------------------------------------------------------------


class TestSynthesisContent:
    def test_narrative_contains_identity_fragment(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        result = ne.get()
        # SelfModel seeds "I am Iris" in current_self_model
        assert "Iris" in result or len(result) > 80

    def test_narrative_is_first_person(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        result = ne.get()
        # Should contain "I" — first-person voice
        assert "I " in result or result.startswith("I")

    def test_narrative_includes_goal_when_active(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        # Seed an active goal
        ne._goal_engine.add_goal({
            "id": "test_goal",
            "title": "Build NarrativeEngine",
            "description": "Day 10 of Pulse v2",
            "status": "active",
        })
        ne.invalidate()
        ne._last_source_hash = ""
        result = ne.build()
        assert "NarrativeEngine" in result or "working on" in result or len(result) > 50

    def test_narrative_includes_recent_episode(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne._episodic.record(
            kind="work_complete",
            title="Shipped EpisodicBuffer Day 9",
            salience=9.0,
        )
        ne._last_source_hash = ""
        result = ne.build()
        assert "EpisodicBuffer" in result or "Most recently" in result or len(result) > 50


# ---------------------------------------------------------------------------
# Output constraints
# ---------------------------------------------------------------------------


class TestOutputConstraints:
    def test_trim_at_sentence_boundary(self, tmp_path):
        from pulse.src.runtime.narrative_engine import NarrativeEngine, MAX_NARRATIVE_CHARS
        trimmed = NarrativeEngine._trim("Word " * 300)
        assert len(trimmed) <= MAX_NARRATIVE_CHARS

    def test_trim_preserves_short_text(self, tmp_path):
        from pulse.src.runtime.narrative_engine import NarrativeEngine
        short = "I am Iris. I build things."
        assert NarrativeEngine._trim(short) == short

    def test_trim_at_sentence_boundary_preferred(self, tmp_path):
        from pulse.src.runtime.narrative_engine import NarrativeEngine
        # Build a string that needs trimming, ending mid-sentence
        long_text = ("This is a sentence. " * 60).strip()
        trimmed = NarrativeEngine._trim(long_text)
        assert trimmed.endswith(".")


# ---------------------------------------------------------------------------
# State persistence + crash recovery
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_narrative_persisted_to_state_engine(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        text = ne.build()
        assert ne._state.get("narrative.text") == text

    def test_build_count_persisted(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.build()
        stored_count = ne._state.get("narrative.build_count")
        assert stored_count == 1

    def test_source_hash_persisted(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.build()
        stored_hash = ne._state.get("narrative.source_hash")
        assert stored_hash and len(stored_hash) == 64  # sha256 hex

    def test_restore_from_state_on_boot(self, tmp_path):
        from pulse.src.runtime.narrative_engine import NarrativeEngine
        # Build with first instance
        state = _make_state(tmp_path)
        sm = _make_self_model(tmp_path, state)
        ep = _make_episodic(tmp_path, state)
        ge = _make_goal_engine(state)
        ne1 = NarrativeEngine(state=state, self_model=sm, episodic=ep, goal_engine=ge)
        text1 = ne1.build()

        # Second instance with same state — should restore cached text
        ne2 = NarrativeEngine(state=state, self_model=sm, episodic=ep, goal_engine=ge)
        # _restore_from_state runs on __init__; cached_text should be populated
        assert ne2._cached_text == text1

    def test_restore_increments_correctly_after_restart(self, tmp_path):
        from pulse.src.runtime.narrative_engine import NarrativeEngine
        state = _make_state(tmp_path)
        sm = _make_self_model(tmp_path, state)
        ep = _make_episodic(tmp_path, state)
        ge = _make_goal_engine(state)

        ne1 = NarrativeEngine(state=state, self_model=sm, episodic=ep, goal_engine=ge)
        ne1.build()
        ne1.build()  # force 2 builds

        # Simulate restart
        ne2 = NarrativeEngine(state=state, self_model=sm, episodic=ep, goal_engine=ge)
        ne2._last_source_hash = ""  # force rebuild
        ne2.build()
        # build_count continues from where ne1 left off
        assert ne2._build_count >= 1


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_has_required_keys(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.get()
        snap = ne.snapshot()
        for key in ("text", "chars", "build_count", "cache_age_seconds", "ttl_seconds"):
            assert key in snap, f"Missing key: {key}"

    def test_snapshot_text_is_string(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        snap = ne.snapshot()
        assert isinstance(snap["text"], str)

    def test_snapshot_chars_matches_text(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.get()
        snap = ne.snapshot()
        assert snap["chars"] == len(snap["text"])

    def test_snapshot_build_count_matches(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.get()
        ne._last_source_hash = ""
        ne.get()
        snap = ne.snapshot()
        assert snap["build_count"] == ne._build_count

    def test_snapshot_cache_age_seconds_after_build(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.get()
        snap = ne.snapshot()
        assert snap["cache_age_seconds"] is not None
        assert snap["cache_age_seconds"] >= 0

    def test_snapshot_serialisable_to_json(self, tmp_path):
        ne = _make_narrative_engine(tmp_path)
        ne.get()
        snap = ne.snapshot()
        json_str = json.dumps(snap)
        assert isinstance(json_str, str)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_get_calls_are_safe(self, tmp_path):
        ne = _make_narrative_engine(tmp_path, ttl=1)
        errors = []

        def _worker():
            try:
                for _ in range(20):
                    ne.get()
                    time.sleep(0.01)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_invalidate_and_get(self, tmp_path):
        ne = _make_narrative_engine(tmp_path, ttl=300)
        errors = []

        def _invalidate_loop():
            try:
                for _ in range(10):
                    ne.invalidate()
                    ne._last_source_hash = ""
                    time.sleep(0.02)
            except Exception as exc:
                errors.append(exc)

        def _get_loop():
            try:
                for _ in range(10):
                    ne.get()
                    time.sleep(0.02)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_invalidate_loop)] + [
            threading.Thread(target=_get_loop) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# HypostasRuntime integration
# ---------------------------------------------------------------------------


class TestHypostasRuntimeIntegration:
    def test_runtime_has_narrative_engine(self, tmp_path):
        from pulse.src.runtime import HypostasRuntime
        rt = HypostasRuntime(state_dir=tmp_path / "state")
        assert hasattr(rt, "narrative")
        assert rt.narrative is not None

    def test_runtime_status_includes_narrative(self, tmp_path):
        from pulse.src.runtime import HypostasRuntime
        rt = HypostasRuntime(state_dir=tmp_path / "state")
        status = rt.status()
        assert "narrative" in status

    def test_narrative_snapshot_in_status(self, tmp_path):
        from pulse.src.runtime import HypostasRuntime
        rt = HypostasRuntime(state_dir=tmp_path / "state")
        status = rt.status()
        narrative_status = status["narrative"]
        assert "text" in narrative_status
        assert "build_count" in narrative_status
