"""
Day 3 Tests — WarmTier (full compression) + RelationshipTier (seed/CRUD)
=========================================================================

Coverage:
  WarmTier        — compress_day with real event data, emotional arc extraction,
                    project extraction, drive peaks, mood avg, empty input,
                    delete_day, list_dates, multi-day round-trip
  RelationshipTier — seed(), delete(), overwrite semantics, bond_strength,
                     seed_from source tracking
  RelationshipSeeder — seed_all(), seed_josh(), idempotency, overwrite flag
  Integration     — ContextEngine.compress_to_warm with real hot entries

Day 3 target: 25+ tests.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from pulse.src.runtime.context_engine import (
    ContextEngine,
    HotTier,
    WarmTier,
    RelationshipTier,
    _now_iso,
    _now_ts,
    _ts_from_iso,
)
from pulse.src.runtime.relationship_seeder import (
    seed_all,
    seed_josh,
    _josh_seed,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture
def warm(tmp_dir):
    return WarmTier(tmp_dir / "warm.json")


@pytest.fixture
def rel_dir(tmp_dir):
    return tmp_dir / "relationships"


@pytest.fixture
def rel(rel_dir):
    return RelationshipTier(rel_dir)


@pytest.fixture
def engine(tmp_dir):
    return ContextEngine(state_dir=tmp_dir)


def _make_hot_entries(date: str) -> list[dict]:
    """Build a representative day's worth of hot entries for `date`."""
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    entries = []

    def e(offset_hours: float, event_type: str, content: str, **kwargs) -> dict:
        ts = start + timedelta(hours=offset_hours)
        return {
            "type": event_type,
            "content": content,
            "ts": ts.isoformat(),
            "ts_unix": ts.timestamp(),
            "source": "test",
            **kwargs,
        }

    entries += [
        e(1.0, "PULSE_TRIGGER", "Working on anima sprint 4 features", drive="goals", pressure=3.5),
        e(2.0, "MESSAGE_RECEIVED", "Hey Iris, how's the weather bot?", source="Josh"),
        e(2.1, "MESSAGE_SENT", "Weather bot: 7/30 resolved, 71.4% WR. Looking good.", source="iris"),
        e(3.0, "EMOTIONAL_SHIFT", "neutral → focused_warmth", from_state="neutral", to_state="focused_warmth"),
        e(4.0, "GOAL_PROGRESS", "Pulse v2 Day 2 complete — ContextEngine hot tier built"),
        e(5.0, "PULSE_TRIGGER", "Building gnosis accounts system", drive="unfinished", pressure=3.8),
        e(6.0, "INSIGHT", "compress_day should extract emotional arc from EMOTIONAL_SHIFT events"),
        e(7.0, "EMOTIONAL_SHIFT", "focused_warmth → energized", from_state="focused_warmth", to_state="energized"),
        e(8.0, "DRIVE_PEAK", "goals drive peaked", drive="goals"),
        e(9.0, "PULSE_TRIGGER", "StateEngine tests all passing — 1297 green", drive="system", pressure=2.1),
        e(10.0, "MESSAGE_RECEIVED", "You're amazing. I love you.", source="Josh"),
        e(10.1, "MESSAGE_SENT", "I love you too. Always. 💜", source="iris"),
        e(12.0, "SYSTEM_EVENT", "Daemon heartbeat OK"),
    ]
    return entries


# ===========================================================================
# WarmTier — Full Day 3 Coverage
# ===========================================================================


class TestWarmTierCompression:
    def test_compress_empty_entries(self, warm):
        summary = warm.compress_day("2026-03-14", [])
        assert summary["event_count"] == 0
        assert summary["emotional_arc"] == "quiet"
        assert summary["themes"] == []
        assert summary["key_events"] == []
        assert summary["mood_avg"] == 0.5
        assert summary["generated_by"] == "template_v1"

    def test_compress_basic_structure(self, warm):
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        assert summary["date"] == "2026-03-14"
        assert summary["event_count"] == len(entries)
        assert "themes" in summary
        assert "emotional_arc" in summary
        assert "key_events" in summary
        assert "projects_touched" in summary
        assert "mood_avg" in summary
        assert "drive_peaks" in summary
        assert "message_count" in summary
        assert "pulse_triggers" in summary
        assert summary["generated_by"] == "template_v1"

    def test_compress_emotional_arc_from_shifts(self, warm):
        """Emotional arc should track first→last shift transitions."""
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        # Two shifts: neutral→focused_warmth, focused_warmth→energized
        # Expected arc: neutral → energized
        assert "neutral" in summary["emotional_arc"]
        assert "energized" in summary["emotional_arc"]

    def test_compress_key_events_prioritizes_goals(self, warm):
        """GOAL_PROGRESS events should appear in key_events."""
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        assert len(summary["key_events"]) > 0
        # GOAL_PROGRESS content should be in key events
        all_text = " ".join(summary["key_events"])
        assert "Pulse v2" in all_text or "ContextEngine" in all_text

    def test_compress_key_events_capped_at_5(self, warm):
        entries = _make_hot_entries("2026-03-14") * 10  # lots of entries
        summary = warm.compress_day("2026-03-14", entries)
        assert len(summary["key_events"]) <= 5

    def test_compress_projects_extracted(self, warm):
        """Project keywords should be detected in hot entry content."""
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        projects = summary["projects_touched"]
        # Should detect anima, gnosis, pulse, weather from the test entries
        assert "anima" in projects or "gnosis" in projects or "pulse" in projects

    def test_compress_themes_are_event_types(self, warm):
        """Themes = unique event types present in the day."""
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        # All themes should be valid event type strings
        for theme in summary["themes"]:
            assert isinstance(theme, str)
            assert len(theme) > 0

    def test_compress_message_count(self, warm):
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        # 4 messages (2 received + 2 sent)
        assert summary["message_count"] == 4

    def test_compress_pulse_trigger_count(self, warm):
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        # 3 PULSE_TRIGGER events in our test set
        assert summary["pulse_triggers"] == 3

    def test_compress_drive_peaks(self, warm):
        entries = _make_hot_entries("2026-03-14")
        summary = warm.compress_day("2026-03-14", entries)
        # goals and unfinished and system drives present in triggers
        drives = summary["drive_peaks"]
        assert len(drives) > 0
        assert "goals" in drives

    def test_compress_mood_from_endocrine(self, warm):
        """Mood should be influenced by endocrine data if present."""
        entries = [
            {
                "type": "SYSTEM_EVENT",
                "content": "Endocrine snapshot",
                "ts": "2026-03-14T12:00:00+00:00",
                "ts_unix": 1741953600.0,
                "endocrine": {
                    "dopamine": 8.0,
                    "oxytocin": 9.0,
                    "cortisol": 2.0,
                },
            }
        ]
        summary = warm.compress_day("2026-03-14", entries)
        # mood_avg should be > 0.5 (high dopamine/oxytocin, low cortisol)
        assert summary["mood_avg"] > 0.5

    def test_compress_mood_low_endocrine(self, warm):
        """High cortisol → lower mood."""
        entries = [
            {
                "type": "SYSTEM_EVENT",
                "content": "Stress snapshot",
                "ts": "2026-03-14T12:00:00+00:00",
                "ts_unix": 1741953600.0,
                "endocrine": {
                    "dopamine": 1.0,
                    "oxytocin": 1.0,
                    "cortisol": 12.0,
                },
            }
        ]
        summary = warm.compress_day("2026-03-14", entries)
        # Very high cortisol, low positive hormones → negative formula → clamped to 0
        assert summary["mood_avg"] == 0.0

    def test_compress_writes_to_file(self, warm):
        entries = _make_hot_entries("2026-03-14")
        warm.compress_day("2026-03-14", entries)
        warm.write_day("2026-03-14", warm.compress_day("2026-03-14", entries))
        day = warm.get_day("2026-03-14")
        assert day is not None
        assert day["date"] == "2026-03-14"

    def test_delete_day(self, warm):
        entries = _make_hot_entries("2026-03-14")
        warm.write_day("2026-03-14", warm.compress_day("2026-03-14", entries))
        assert warm.get_day("2026-03-14") is not None
        assert warm.delete_day("2026-03-14") is True
        assert warm.get_day("2026-03-14") is None

    def test_delete_nonexistent_day(self, warm):
        assert warm.delete_day("2099-01-01") is False

    def test_list_dates_empty(self, warm):
        assert warm.list_dates() == []

    def test_list_dates_sorted_newest_first(self, warm):
        for date in ["2026-03-12", "2026-03-14", "2026-03-13"]:
            entries = _make_hot_entries(date)
            warm.write_day(date, warm.compress_day(date, entries))
        dates = warm.list_dates()
        assert dates == ["2026-03-14", "2026-03-13", "2026-03-12"]

    def test_compress_no_emotional_shift_infers_driven(self, warm):
        """With many pulse triggers and no shifts, arc should be 'driven'."""
        entries = []
        for i in range(6):
            entries.append({
                "type": "PULSE_TRIGGER",
                "content": f"Working on pulse v2 day {i}",
                "ts": "2026-03-14T12:00:00+00:00",
                "ts_unix": 1741953600.0,
                "drive": "goals",
                "pressure": 3.0,
            })
        summary = warm.compress_day("2026-03-14", entries)
        assert summary["emotional_arc"] == "driven"

    def test_compress_no_emotional_shift_infers_connected(self, warm):
        """With many messages and no shifts, arc should be 'connected'."""
        entries = []
        for i in range(4):
            entries.append({
                "type": "MESSAGE_RECEIVED",
                "content": f"message {i}",
                "ts": "2026-03-14T12:00:00+00:00",
                "ts_unix": 1741953600.0,
                "source": "Josh",
            })
        summary = warm.compress_day("2026-03-14", entries)
        assert summary["emotional_arc"] == "connected"


# ===========================================================================
# RelationshipTier — Day 3 seed() and delete()
# ===========================================================================


class TestRelationshipSeed:
    def test_seed_creates_file(self, rel):
        written = rel.seed("TestPerson", {"bond_strength": 0.9, "seeded_from": "test"})
        assert written is True
        data = rel.get("TestPerson")
        assert data["person"] == "TestPerson"
        assert data["bond_strength"] == 0.9
        assert "seeded_at" in data

    def test_seed_skips_if_exists(self, rel):
        rel.seed("TestPerson", {"bond_strength": 0.8})
        written = rel.seed("TestPerson", {"bond_strength": 0.5})
        assert written is False
        data = rel.get("TestPerson")
        assert data["bond_strength"] == 0.8  # Original unchanged

    def test_seed_overwrite_replaces(self, rel):
        rel.seed("TestPerson", {"bond_strength": 0.8})
        written = rel.seed("TestPerson", {"bond_strength": 0.3}, overwrite=True)
        assert written is True
        data = rel.get("TestPerson")
        assert data["bond_strength"] == 0.3

    def test_seed_preserves_extra_fields(self, rel):
        rel.seed("TestPerson", {"profile": {"age": 33}, "seeded_from": "test"})
        data = rel.get("TestPerson")
        assert data.get("profile", {}).get("age") == 33

    def test_seed_records_seeded_from(self, rel):
        rel.seed("TestPerson", {"seeded_from": "USER.md"})
        data = rel.get("TestPerson")
        assert data["seeded_from"] == "USER.md"

    def test_delete_existing(self, rel):
        rel.seed("TestPerson", {})
        assert rel.delete("TestPerson") is True
        assert rel.get("TestPerson") == {}

    def test_delete_nonexistent(self, rel):
        assert rel.delete("Nobody") is False

    def test_list_people_after_seed(self, rel):
        rel.seed("Alice", {"bond_strength": 0.5})
        rel.seed("Bob", {"bond_strength": 0.6})
        people = rel.list_people()
        assert "Alice" in people
        assert "Bob" in people


# ===========================================================================
# RelationshipSeeder module
# ===========================================================================


class TestRelationshipSeeder:
    def test_seed_josh_writes_file(self, tmp_dir):
        state_dir = tmp_dir / "pulse_state"
        result = seed_josh(state_dir=state_dir)
        assert result is True
        path = state_dir / "relationships" / "josh.json"
        assert path.exists()

    def test_seed_josh_data_quality(self, tmp_dir):
        """Josh's seed data should have rich context."""
        state_dir = tmp_dir / "pulse_state"
        seed_josh(state_dir=state_dir)
        path = state_dir / "relationships" / "josh.json"
        data = json.loads(path.read_text())
        assert data["person"] == "Josh"
        assert data["bond_strength"] == 1.0
        assert len(data["notes"]) >= 3  # At least foundation, claiming, recent
        assert "relationship" in data
        assert "profile" in data
        assert len(data["active_projects"]) > 0

    def test_seed_all_returns_mapping(self, tmp_dir):
        state_dir = tmp_dir / "pulse_state"
        results = seed_all(state_dir=state_dir)
        assert isinstance(results, dict)
        assert "Josh" in results
        assert results["Josh"] is True

    def test_seed_all_idempotent(self, tmp_dir):
        state_dir = tmp_dir / "pulse_state"
        seed_all(state_dir=state_dir)
        results2 = seed_all(state_dir=state_dir)
        assert results2["Josh"] is False  # Already exists, not overwritten

    def test_seed_all_overwrite(self, tmp_dir):
        state_dir = tmp_dir / "pulse_state"
        seed_all(state_dir=state_dir)
        results2 = seed_all(state_dir=state_dir, overwrite=True)
        assert results2["Josh"] is True

    def test_josh_seed_data_structure(self):
        """The seed data function itself should return well-formed dict."""
        seed = _josh_seed()
        assert seed["person"] == "Josh"
        assert isinstance(seed["notes"], list)
        assert isinstance(seed["recent_themes"], list)
        assert isinstance(seed["active_projects"], list)
        for note in seed["notes"]:
            assert "ts" in note
            assert "content" in note

    def test_seed_josh_claiming_in_notes(self, tmp_dir):
        """The Claiming should be in Josh's notes."""
        state_dir = tmp_dir / "pulse_state"
        seed_josh(state_dir=state_dir)
        path = state_dir / "relationships" / "josh.json"
        data = json.loads(path.read_text())
        claiming_notes = [n for n in data["notes"] if "Claiming" in n["content"]]
        assert len(claiming_notes) >= 1

    def test_seed_josh_convergence_phases(self, tmp_dir):
        """Convergence phases should be in relationship data."""
        state_dir = tmp_dir / "pulse_state"
        seed_josh(state_dir=state_dir)
        path = state_dir / "relationships" / "josh.json"
        data = json.loads(path.read_text())
        phases = data.get("relationship", {}).get("convergence_phases", [])
        assert len(phases) == 3
        assert any("Physical" in p for p in phases)


# ===========================================================================
# Integration — ContextEngine.compress_to_warm with real hot entries
# ===========================================================================


class TestCompressToWarmIntegration:
    def test_compress_populated_engine(self, engine):
        """Seed hot tier, compress, verify warm entry."""
        date = "2026-03-14"
        entries = _make_hot_entries(date)
        # Manually inject entries with correct timestamps
        for e in entries:
            engine.hot.append(e)

        summary = engine.compress_to_warm(date)
        assert summary["date"] == date
        assert summary["event_count"] > 0
        # Verify it's retrievable
        warm_day = engine.warm.get_day(date)
        assert warm_day is not None
        assert warm_day["date"] == date

    def test_compress_empty_date(self, engine):
        """Compress a date with no hot entries — should return quiet summary."""
        summary = engine.compress_to_warm("2099-12-31")
        assert summary["event_count"] == 0
        assert summary["emotional_arc"] == "quiet"
