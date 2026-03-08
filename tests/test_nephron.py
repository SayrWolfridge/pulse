"""Tests for NEPHRON — Excretory System / Memory Pruning."""

import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from pulse.src import nephron


class TestNephronBasics:
    def test_default_state(self):
        state = nephron._default_state()
        assert state["total_cycles"] == 0
        assert state["total_pruned"] == 0
        assert state["last_run"] == 0
        assert state["history"] == []

    def test_should_run(self):
        assert not nephron.should_run(0)
        assert not nephron.should_run(1)
        assert not nephron.should_run(50)
        assert not nephron.should_run(99)
        assert nephron.should_run(100)
        assert nephron.should_run(200)
        assert nephron.should_run(300)

    def test_get_status(self):
        status = nephron.get_status()
        assert "total_cycles" in status
        assert "total_pruned" in status
        assert "last_run" in status

    def test_filter_all_runs(self):
        results = nephron.filter_all()
        assert "pruned" in results
        assert "errors" in results
        assert "timestamp" in results

    def test_state_persists_after_filter(self):
        nephron.filter_all()
        status = nephron.get_status()
        assert status["total_cycles"] >= 1

    def test_thalamus_pruning(self):
        """Test that THALAMUS bus gets trimmed when too large."""
        thalamus_file = nephron._DEFAULT_STATE_DIR / "thalamus.jsonl"

        # Create oversized file
        original = thalamus_file.read_text() if thalamus_file.exists() else ""
        try:
            lines = [
                json.dumps({"ts": i, "source": "test", "type": "test"})
                for i in range(600)
            ]
            thalamus_file.write_text("\n".join(lines) + "\n")

            pruned = nephron._prune_thalamus()
            assert pruned == 100  # 600 - 500

            remaining = thalamus_file.read_text().strip().split("\n")
            assert len(remaining) == 500
        finally:
            # Restore original
            thalamus_file.write_text(original)

    def test_thalamus_no_pruning_needed(self):
        """No pruning when under threshold."""
        thalamus_file = nephron._DEFAULT_STATE_DIR / "thalamus.jsonl"
        original = thalamus_file.read_text() if thalamus_file.exists() else ""
        try:
            lines = [json.dumps({"ts": i}) for i in range(100)]
            thalamus_file.write_text("\n".join(lines) + "\n")
            assert nephron._prune_thalamus() == 0
        finally:
            thalamus_file.write_text(original)

    def test_endocrine_history_pruning(self):
        """Trim mood_history when over 48."""
        endo_file = nephron._DEFAULT_STATE_DIR / "endocrine-state.json"
        if not endo_file.exists():
            return

        original = endo_file.read_text()
        try:
            data = json.loads(original)
            # Temporarily inflate
            data["mood_history"] = [{"ts": i, "label": "test"} for i in range(60)]
            endo_file.write_text(json.dumps(data))

            pruned = nephron._prune_endocrine_history()
            assert pruned == 12  # 60 - 48

            after = json.loads(endo_file.read_text())
            assert len(after["mood_history"]) == 48
        finally:
            endo_file.write_text(original)

    def test_engram_list_format(self):
        """_prune_engrams handles raw list format (not dict-wrapped)."""
        engram_file = nephron._DEFAULT_STATE_DIR / "engram-store.json"
        original = engram_file.read_text() if engram_file.exists() else None
        now_ms = time.time() * 1000
        old_ms = (time.time() - 200 * 86400) * 1000  # 200 days ago → should prune

        test_memories = [
            # Recent high-emotion → keep
            {
                "id": "keep-001",
                "event": "recent event",
                "emotion": {"valence": 0.5, "intensity": 0.8, "label": "joy"},
                "location": "test",
                "timestamp": now_ms,
                "sensory": {},
                "associations": [],
                "recall_count": 0,
                "last_recalled": None,
            },
            # Old low-emotion → prune
            {
                "id": "prune-001",
                "event": "old boring event",
                "emotion": {"valence": 0.0, "intensity": 0.05, "label": "neutral"},
                "location": "test",
                "timestamp": old_ms,
                "sensory": {},
                "associations": [],
                "recall_count": 0,
                "last_recalled": None,
            },
        ]
        try:
            engram_file.write_text(json.dumps(test_memories, indent=2))
            pruned = nephron._prune_engrams()
            assert pruned == 1, f"Expected 1 pruned, got {pruned}"
            remaining = json.loads(engram_file.read_text())
            assert isinstance(remaining, list), "Output must remain a list"
            assert len(remaining) == 1
            assert remaining[0]["id"] == "keep-001"
        finally:
            if original is not None:
                engram_file.write_text(original)
            elif engram_file.exists():
                engram_file.unlink()

    def test_engram_no_attributeerror_on_list(self):
        """filter_all() must not produce errors from engram list format."""
        results = nephron.filter_all()
        engram_errors = [e for e in results.get("errors", []) if "engram" in e.lower()]
        assert engram_errors == [], f"Unexpected engram errors: {engram_errors}"

    def test_chronicle_no_recent_pruning(self):
        """Recent chronicle entries should not be pruned."""
        chronicle_file = nephron._DEFAULT_STATE_DIR / "chronicle.jsonl"
        if not chronicle_file.exists():
            return

        original = chronicle_file.read_text()
        try:
            lines = [
                json.dumps({"ts": time.time() - 100, "event": "test"}) for _ in range(5)
            ]
            chronicle_file.write_text("\n".join(lines) + "\n")
            assert nephron._prune_chronicle() == 0
        finally:
            chronicle_file.write_text(original)
