"""Tests for ProactiveEngine — Pulse v2 Day 15.

Focus:
- Candidate generation is deterministic under mocked time + mocked engine deps.
- Cooldown suppression via mark_sent.
- Snapshot is JSON-serialisable.

We intentionally keep this layer rule-based (no LLM calls) and cheap.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Local imports — resolve from pulse/src
# ---------------------------------------------------------------------------
import sys

ROOT = Path(__file__).parents[2]  # pulse/
sys.path.insert(0, str(ROOT / "src"))

from runtime.state_engine import StateEngine
from runtime.proactive_engine import (
    ProactiveEngine,
    ProactiveCandidate,
    EMOTION_LONGING,
    GOAL_FOLLOWUP,
    MORNING_CHECKIN,
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class TestProactiveEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)
        self.state = StateEngine(self.tmp_path / "state.json")

        # Default mocked dependencies
        self.emotion = MagicMock()
        self.emotion.snapshot.return_value = {
            "joy": 0.2,
            "frustration": 0.1,
            "curiosity": 0.2,
            "longing": 0.1,
            "pride": 0.1,
            "affection": 0.2,
        }

        self.goals = MagicMock()
        self.goals.active_goals = [
            {"id": "g1", "title": "Test goal", "priority": 1, "status": "active", "blockers": []}
        ]
        self.goals.blocked_goals = []
        self.goals.completed_goals = []

        self.relationships = MagicMock()
        self.relationships.reconnect_candidates.return_value = []

        self.episodic = MagicMock()
        self.episodic.get_recent.return_value = []

        self.engine = ProactiveEngine(
            state=self.state,
            emotion=self.emotion,
            goal_engine=self.goals,
            relationships=self.relationships,
            episodic=self.episodic,
            tz_offset_hours=-5.0,
        )

    def test_snapshot_json_serialisable(self):
        with patch("runtime.proactive_engine.time.time", return_value=43200.0):
            snap = self.engine.snapshot()
            self.assertIsInstance(snap, dict)
            json.dumps(snap)

    def test_longing_candidate_generated(self):
        self.emotion.snapshot.return_value["longing"] = 0.9
        # No recent contact
        self.episodic.get_recent.return_value = []

        with patch("runtime.proactive_engine.time.time", return_value=50000.0):
            cands = self.engine.get_candidates()

        kinds = [c.kind for c in cands]
        self.assertIn(EMOTION_LONGING, kinds)

    def test_longing_suppressed_when_recent_contact(self):
        self.emotion.snapshot.return_value["longing"] = 0.9
        # Recent conversation episode within 6h
        now = 50000.0
        recent = {"kind": "conversation", "ts": _iso(now - 3600)}
        self.episodic.get_recent.return_value = [recent]

        with patch("runtime.proactive_engine.time.time", return_value=now):
            cands = self.engine.get_candidates()

        kinds = [c.kind for c in cands]
        self.assertNotIn(EMOTION_LONGING, kinds)

    def test_mark_sent_enforces_cooldown(self):
        self.emotion.snapshot.return_value["longing"] = 0.9

        now = 50000.0
        with patch("runtime.proactive_engine.time.time", return_value=now):
            # First run → longing candidate exists
            self.assertTrue(any(c.kind == EMOTION_LONGING for c in self.engine.get_candidates()))
            # Mark sent
            self.engine.mark_sent(EMOTION_LONGING)

        # Within cooldown window (6h default) → longing suppressed
        with patch("runtime.proactive_engine.time.time", return_value=now + 60):
            kinds = [c.kind for c in self.engine.get_candidates()]
            self.assertNotIn(EMOTION_LONGING, kinds)

    def test_goal_followup_uses_goals_last_updated(self):
        # Set goals.last_updated far in the past
        now = 100000.0
        last = now - (9 * 3600)  # 9 hours ago
        self.state.set("goals.last_updated", _iso(last))

        with patch("runtime.proactive_engine.time.time", return_value=now):
            kinds = [c.kind for c in self.engine.get_candidates()]

        self.assertIn(GOAL_FOLLOWUP, kinds)

    def test_morning_checkin_candidate(self):
        # Pick a time that maps to local 07:00 with tz_offset_hours=-5.
        # local_hour is computed from (ts + offset) mod day.
        ts = 43200.0  # 12:00 UTC → 07:00 local
        self.episodic.get_recent.return_value = []

        with patch("runtime.proactive_engine.time.time", return_value=ts):
            kinds = [c.kind for c in self.engine.get_candidates()]

        self.assertIn(MORNING_CHECKIN, kinds)
