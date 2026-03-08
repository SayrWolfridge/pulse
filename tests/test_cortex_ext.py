"""Tests for CORTEX_EXT — Active Learning / Knowledge Gap Identification."""

import time
from pathlib import Path

import pytest

from pulse.src import cortexext


def _patch_state_paths(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cortexext, "_DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        cortexext, "_DEFAULT_STATE_FILE", tmp_path / "cortexext-state.json"
    )


class TestCortexExtBasics:
    def test_default_state_shape(self):
        s = cortexext._default_state()
        assert s["total_scans"] == 0
        assert s["last_run"] == 0
        assert s["gaps"] == []
        assert s["history"] == []

    def test_should_run(self):
        assert not cortexext.should_run(0)
        assert not cortexext.should_run(1)
        assert cortexext.should_run(cortexext.LOOP_INTERVAL)
        assert cortexext.should_run(cortexext.LOOP_INTERVAL * 2)

    def test_load_save_roundtrip(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)
        s = cortexext._default_state()
        s["total_scans"] = 7
        cortexext._save_state(s)
        loaded = cortexext._load_state()
        assert loaded["total_scans"] == 7


class TestCortexExtScan:
    def test_scan_creates_gap_and_broadcasts(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)

        # Fake THALAMUS
        entries = [
            {
                "ts": int(time.time() * 1000),
                "source": "spine",
                "type": "health",
                "salience": 0.9,
                "data": {"status": "red", "error": "disk full"},
            }
        ]
        monkeypatch.setattr(cortexext.thalamus, "read_recent", lambda n=200: entries)

        appended = []
        monkeypatch.setattr(
            cortexext.thalamus, "append", lambda e: appended.append(e) or e
        )

        summary = cortexext.run_scan(loop_count=cortexext.LOOP_INTERVAL, recent_n=10)
        assert summary["new_gaps"] == 1
        assert summary["broadcasts"] >= 1
        assert len(appended) >= 1

        status = cortexext.get_status()
        assert status["gap_count"] == 1
        assert status["top_gaps"][0]["count"] >= 1

    def test_scan_escalates_on_repeat(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)

        entries = [
            {
                "ts": int(time.time() * 1000),
                "source": "cli",
                "type": "error",
                "salience": 0.8,
                "data": {"error": "traceback"},
            }
        ]
        monkeypatch.setattr(cortexext.thalamus, "read_recent", lambda n=200: entries)

        appended = []
        monkeypatch.setattr(
            cortexext.thalamus, "append", lambda e: appended.append(e) or e
        )

        # Run multiple scans to hit escalation count
        for i in range(cortexext.ESCALATION_COUNT):
            cortexext.run_scan(
                loop_count=cortexext.LOOP_INTERVAL * (i + 1), recent_n=10
            )

        # Should have at least one escalation broadcast
        assert any(e.get("type") == "learning_gap_escalated" for e in appended)

    def test_scan_ignores_startup_noise(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)

        entries = [
            {
                "ts": int(time.time() * 1000),
                "source": "nervous_system",
                "type": "startup",
                "salience": 0.5,
                "data": {"errors": ["some error-like text"], "modules_failed": 2},
            }
        ]
        monkeypatch.setattr(cortexext.thalamus, "read_recent", lambda n=200: entries)

        appended = []
        monkeypatch.setattr(
            cortexext.thalamus, "append", lambda e: appended.append(e) or e
        )

        summary = cortexext.run_scan(loop_count=cortexext.LOOP_INTERVAL, recent_n=10)
        assert summary["new_gaps"] == 0
        assert summary["broadcasts"] == 0
        assert cortexext.get_status()["gap_count"] == 0


class TestCortexExtResolve:
    """Tests for the resolve_gap() mechanism."""

    def test_resolve_unknown_gap_returns_false(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(cortexext.thalamus, "append", lambda e: e)
        result = cortexext.resolve_gap("nonexistent:gap", reason="test")
        assert result is False

    def test_resolve_existing_gap_returns_true(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)

        # Seed a gap via scan
        entries = [
            {
                "ts": int(time.time() * 1000),
                "source": "nephron",
                "type": "filter_cycle",
                "salience": 0.8,
                "data": {"errors": ["AttributeError: list has no .get"]},
            }
        ]
        monkeypatch.setattr(cortexext.thalamus, "read_recent", lambda n=200: entries)
        appended = []
        monkeypatch.setattr(
            cortexext.thalamus, "append", lambda e: appended.append(e) or e
        )

        cortexext.run_scan(loop_count=cortexext.LOOP_INTERVAL, recent_n=10)

        # Gap should exist and be open
        status_before = cortexext.get_status()
        assert status_before["gap_count"] == 1
        assert status_before["resolved_count"] == 0

        # Resolve it
        gap_id = cortexext._gap_id("nephron:filter_cycle")
        result = cortexext.resolve_gap(gap_id, reason="Fixed in commit 51000ec: handle list format")
        assert result is True

        # Verify gap_count drops and resolved_count rises
        status_after = cortexext.get_status()
        assert status_after["gap_count"] == 0
        assert status_after["resolved_count"] == 1

    def test_resolved_gap_not_escalated_on_rescan(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)

        entries = [
            {
                "ts": int(time.time() * 1000),
                "source": "nephron",
                "type": "filter_cycle",
                "salience": 0.8,
                "data": {"errors": ["stale error"]},
            }
        ]
        monkeypatch.setattr(cortexext.thalamus, "read_recent", lambda n=200: entries)
        appended = []
        monkeypatch.setattr(
            cortexext.thalamus, "append", lambda e: appended.append(e) or e
        )

        # Run until escalation threshold
        for i in range(cortexext.ESCALATION_COUNT + 1):
            cortexext.run_scan(
                loop_count=cortexext.LOOP_INTERVAL * (i + 1), recent_n=10
            )

        # Mark resolved
        gap_id = cortexext._gap_id("nephron:filter_cycle")
        cortexext.resolve_gap(gap_id, reason="bug fixed")
        appended.clear()  # Reset broadcast capture

        # Run more scans — resolved gap should not produce escalation broadcasts
        for i in range(cortexext.ESCALATION_COUNT + 2):
            cortexext.run_scan(
                loop_count=cortexext.LOOP_INTERVAL * (cortexext.ESCALATION_COUNT + i + 2),
                recent_n=10,
            )

        escalations = [e for e in appended if e.get("type") == "learning_gap_escalated"]
        assert len(escalations) == 0, "Resolved gap should not produce escalation broadcasts"

    def test_resolve_emits_thalamus_event(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)

        # Seed a gap
        entries = [
            {
                "ts": int(time.time() * 1000),
                "source": "spine",
                "type": "health",
                "salience": 0.9,
                "data": {"status": "red", "error": "disk full"},
            }
        ]
        monkeypatch.setattr(cortexext.thalamus, "read_recent", lambda n=200: entries)
        appended = []
        monkeypatch.setattr(
            cortexext.thalamus, "append", lambda e: appended.append(e) or e
        )

        cortexext.run_scan(loop_count=cortexext.LOOP_INTERVAL, recent_n=10)
        appended.clear()

        gap_id = cortexext._gap_id("spine:health:red")
        cortexext.resolve_gap(gap_id, reason="disk expanded")

        resolution_events = [e for e in appended if e.get("type") == "learning_gap_resolved"]
        assert len(resolution_events) == 1
        assert resolution_events[0]["data"]["gap_id"] == gap_id
        assert "disk expanded" in resolution_events[0]["data"]["reason"]

    def test_get_status_includes_resolved_count(self, tmp_path, monkeypatch):
        _patch_state_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(cortexext.thalamus, "read_recent", lambda n=200: [])
        monkeypatch.setattr(cortexext.thalamus, "append", lambda e: e)
        status = cortexext.get_status()
        assert "resolved_count" in status
        assert isinstance(status["resolved_count"], int)
