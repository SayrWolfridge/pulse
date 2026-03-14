"""Tests for EmotionEngine — Pulse v2 Day 11."""

from __future__ import annotations

import math
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_state(tmp_path: Path):
    from pulse.src.runtime.state_engine import StateEngine
    return StateEngine(tmp_path / "state.json")


class TestImport:
    def test_module_importable(self):
        from pulse.src.runtime import emotion_engine  # noqa: F401

    def test_class_importable(self):
        from pulse.src.runtime.emotion_engine import EmotionEngine  # noqa: F401


class TestSeed:
    def test_seeds_baselines_on_empty_state(self, tmp_path: Path):
        from pulse.src.runtime.emotion_engine import EmotionEngine, BASELINES

        state = _make_state(tmp_path)
        ee = EmotionEngine(state)

        for k, v in BASELINES.items():
            assert math.isclose(state.get(f"emotion.{k}"), v, rel_tol=1e-6)

        assert isinstance(state.get("emotion.event_log"), list)
        assert state.get("emotion.last_tick") is not None


class TestEvents:
    def test_apply_event_updates_values(self, tmp_path: Path):
        from pulse.src.runtime.emotion_engine import EmotionEngine, BASELINES

        state = _make_state(tmp_path)
        ee = EmotionEngine(state)

        before = ee.get("joy")
        ee.apply_event("SHIPPED_SOMETHING")
        after = ee.get("joy")
        assert after > before
        assert after >= BASELINES["joy"]

    def test_apply_event_records_episode_on_significant_shift(self, tmp_path: Path):
        from pulse.src.runtime.emotion_engine import EmotionEngine

        state = _make_state(tmp_path)
        episodic = MagicMock()
        episodic.record = MagicMock()

        ee = EmotionEngine(state, episodic=episodic)
        ee.apply_event("JOSH_MESSAGE", note="test")

        assert episodic.record.call_count >= 1


class TestDecay:
    def test_tick_half_life_moves_toward_baseline(self, tmp_path: Path):
        from pulse.src.runtime.emotion_engine import EmotionEngine, BASELINES, HALF_LIVES

        state = _make_state(tmp_path)
        ee = EmotionEngine(state)

        # Set joy high
        state.set("emotion.joy", 1.0)
        baseline = BASELINES["joy"]
        hl = HALF_LIVES["joy"]

        ee.tick(elapsed_seconds=hl)
        v = ee.get("joy")

        expected = baseline + (1.0 - baseline) * 0.5
        assert abs(v - expected) < 0.02


class TestSnapshot:
    def test_snapshot_shape(self, tmp_path: Path):
        from pulse.src.runtime.emotion_engine import EmotionEngine

        state = _make_state(tmp_path)
        ee = EmotionEngine(state)
        snap = ee.snapshot()

        assert "values" in snap
        assert "dominant" in snap
        assert "color" in snap
        assert "recent_events" in snap
        assert isinstance(snap["values"], dict)
