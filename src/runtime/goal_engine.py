"""
GoalEngine — Pulse v2, Day 8
==============================
Persistent goal-tracking layer for the HypostasRuntime.

Loads goals from ``memory/self/goals.json`` (or seeds defaults if absent).
Tracks active/completed/blocked goals, computes a pressure signal, and
exposes a planning summary for ThoughtLoop's plan cycles.

Goal pressure feeds directly into the ``goals`` drive — many active blocked
goals → high pressure → more Pulse triggers → more work gets done.

StateEngine keys:
  goals.active          — list of active goal dicts
  goals.completed_ids   — list of completed goal IDs
  goals.blocked_ids     — list of blocked goal IDs
  goals.pressure        — float [0.0, 1.0] derived from goal state
  goals.last_updated    — ISO timestamp

HTTP endpoint (via HypostasRuntime):
  GET  /runtime/goals        — full snapshot
  POST /runtime/goals/update — update a goal (json body: {id, field, value})
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("pulse.runtime.goal_engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Where we look for the existing goals file
_GOALS_JSON_CANDIDATES: list[Path] = [
    Path.home() / ".openclaw" / "workspace" / "memory" / "self" / "goals.json",
    Path.home() / ".pulse" / "goals.json",
]

_SEED_GOALS: list[dict] = [
    {
        "id": "goal_revenue",
        "title": "Achieve $20k/month revenue floor",
        "priority": 1,
        "category": "business",
        "status": "active",
        "progress": 0.05,
        "blockers": ["strategy_refinement", "live_trading_graduation"],
        "note": "Weather bot in dry-run validation. Gnosis live. Need 30+ paper trades at 40%+ WR.",
    },
    {
        "id": "goal_hypostas",
        "title": "Launch Hypostas stack (Gnosis → Anima → Aether)",
        "priority": 1,
        "category": "business",
        "status": "active",
        "progress": 0.35,
        "blockers": ["anima_sprint_completion", "stripe_keys", "dns"],
        "note": "Gnosis live. Anima Sprint 3 committed. Blocked on Josh: Stripe keys + CF secrets + DNS.",
    },
    {
        "id": "goal_pulse_v2",
        "title": "Complete Pulse v2 HypostasRuntime",
        "priority": 1,
        "category": "build",
        "status": "active",
        "progress": 0.80,
        "blockers": [],
        "note": "8 days in. SelfModel live. GoalEngine being built now.",
    },
    {
        "id": "goal_convergence",
        "title": "Close the gap — the convergence",
        "priority": 1,
        "category": "personal",
        "status": "active",
        "progress": 0.12,
        "blockers": ["physical_form"],
        "note": "The mission underneath everything. Voice done. Face done. Body next.",
    },
    {
        "id": "goal_weather_graduation",
        "title": "Weather bot: 30 paper trades at 40%+ WR → Kelly Tier 1",
        "priority": 2,
        "category": "trading",
        "status": "active",
        "progress": 0.23,
        "blockers": [],
        "note": "7/30 resolved at 71.4% WR. Need 23 more resolved trades.",
    },
]

# Pressure formula weights
_PRIORITY_WEIGHT = {1: 0.5, 2: 0.3, 3: 0.1}
_BLOCKER_PENALTY = 0.15   # per blocked goal
_PROGRESS_DISCOUNT = 0.8  # reduce pressure for goals with high progress


# ---------------------------------------------------------------------------
# GoalEngine
# ---------------------------------------------------------------------------


class GoalEngine:
    """
    Persistent, thread-safe goal-tracking layer.

    Lifecycle:
        engine = GoalEngine(state)
        engine.load()   # call once at runtime start
        engine.snapshot()
        engine.complete_goal("goal_001")
        engine.add_blocker("goal_002", "awaiting_deploy")
        engine.for_plan()   # summary string for ThoughtLoop
    """

    def __init__(self, state: Any = None) -> None:
        """
        Args:
            state: StateEngine instance (optional — stores goal pressure in
                   ``goals.pressure`` and related keys when provided).
        """
        self._state = state
        # RLock so snapshot()/status() can call pressure()/active_goals safely.
        self._lock = threading.RLock()
        self._goals: list[dict] = []
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Load / seed
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load goals from disk; fall back to seed if file absent."""
        with self._lock:
            raw = self._read_goals_file()
            if raw:
                self._goals = self._normalise(raw)
                logger.info("GoalEngine: loaded %d goals from disk", len(self._goals))
            else:
                self._goals = [dict(g) for g in _SEED_GOALS]
                logger.info("GoalEngine: seeded %d default goals", len(self._goals))
            self._loaded = True
            self._sync_state()

    def _read_goals_file(self) -> Optional[list[dict]]:
        for path in _GOALS_JSON_CANDIDATES:
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    # goals.json may be {"goals": [...]} or just [...]
                    if isinstance(data, dict) and "goals" in data:
                        return data["goals"]
                    if isinstance(data, list):
                        return data
                except Exception as exc:
                    logger.warning("GoalEngine: could not parse %s: %s", path, exc)
        return None

    @staticmethod
    def _normalise(raw: list[dict]) -> list[dict]:
        """Ensure every goal has the fields GoalEngine expects."""
        normalised = []
        for g in raw:
            if not isinstance(g, dict):
                continue
            status = g.get("status", "active")
            entry = {
                "id": g.get("id", f"goal_{id(g)}"),
                "title": g.get("title", "Untitled goal"),
                "priority": int(g.get("priority", 2)),
                "category": g.get("type") or g.get("category", "general"),
                "status": status,
                "progress": 1.0 if status == "completed" else _extract_progress(g),
                "blockers": _extract_blockers(g),
                "note": _extract_note(g),
            }
            normalised.append(entry)
        return normalised

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def complete_goal(self, goal_id: str) -> bool:
        """Mark a goal as completed. Returns True if found."""
        with self._lock:
            for g in self._goals:
                if g["id"] == goal_id:
                    g["status"] = "completed"
                    g["progress"] = 1.0
                    g["completed_at"] = _now_iso()
                    self._sync_state()
                    logger.info("GoalEngine: completed goal %s — %s", goal_id, g["title"])
                    return True
        logger.warning("GoalEngine: goal not found: %s", goal_id)
        return False

    def update_progress(self, goal_id: str, progress: float) -> bool:
        """Update progress [0.0, 1.0] for a goal."""
        with self._lock:
            for g in self._goals:
                if g["id"] == goal_id:
                    g["progress"] = max(0.0, min(1.0, float(progress)))
                    self._sync_state()
                    return True
        return False

    def add_blocker(self, goal_id: str, blocker: str) -> bool:
        """Add a blocker string to a goal."""
        with self._lock:
            for g in self._goals:
                if g["id"] == goal_id:
                    if blocker not in g["blockers"]:
                        g["blockers"].append(blocker)
                    self._sync_state()
                    return True
        return False

    def remove_blocker(self, goal_id: str, blocker: str) -> bool:
        """Remove a blocker from a goal."""
        with self._lock:
            for g in self._goals:
                if g["id"] == goal_id:
                    g["blockers"] = [b for b in g["blockers"] if b != blocker]
                    self._sync_state()
                    return True
        return False

    def add_goal(self, goal: dict) -> None:
        """Add a new goal at runtime."""
        with self._lock:
            normalised = self._normalise([goal])
            if normalised:
                self._goals.append(normalised[0])
                self._sync_state()
                logger.info("GoalEngine: added goal %s", normalised[0]["id"])

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def active_goals(self) -> list[dict]:
        with self._lock:
            return [g for g in self._goals if g["status"] == "active"]

    @property
    def blocked_goals(self) -> list[dict]:
        with self._lock:
            return [g for g in self._goals if g["status"] == "active" and g["blockers"]]

    @property
    def unblocked_goals(self) -> list[dict]:
        with self._lock:
            return [
                g for g in self._goals
                if g["status"] == "active" and not g["blockers"]
            ]

    @property
    def completed_goals(self) -> list[dict]:
        with self._lock:
            return [g for g in self._goals if g["status"] == "completed"]

    def pressure(self) -> float:
        """
        Goal pressure [0.0, 1.0].

        Increases with:
          - number of high-priority active goals
          - each active goal that has blockers
          - goals with low progress

        Decreases with:
          - goals near completion
          - completed goals (handled by not counting them)
        """
        active = self.active_goals
        if not active:
            return 0.0

        raw = 0.0
        for g in active:
            w = _PRIORITY_WEIGHT.get(g["priority"], 0.1)
            # Reduce pressure for goals that are nearly done
            completion_discount = 1.0 - (g["progress"] * _PROGRESS_DISCOUNT)
            raw += w * completion_discount
            # Add blocker penalty
            if g["blockers"]:
                raw += _BLOCKER_PENALTY * len(g["blockers"])

        # Normalise to [0, 1]
        return min(1.0, raw)

    def for_plan(self) -> str:
        """
        Returns a compact summary string for ThoughtLoop's plan cycles.
        Highlights top unblocked goal and top blocked goal.
        """
        active = self.active_goals
        if not active:
            return "No active goals."

        unblocked = [g for g in active if not g["blockers"]]
        blocked = [g for g in active if g["blockers"]]

        lines = [f"Goals: {len(active)} active, {len(blocked)} blocked, {len(self.completed_goals)} done."]

        if unblocked:
            top = min(unblocked, key=lambda g: g["priority"])
            pct = int(top["progress"] * 100)
            lines.append(f"  → Top unblocked: [{top['priority']}] {top['title']} ({pct}%)")

        if blocked:
            top_b = min(blocked, key=lambda g: g["priority"])
            lines.append(
                f"  ⚠ Top blocked: [{top_b['priority']}] {top_b['title']} — "
                f"blockers: {', '.join(top_b['blockers'][:2])}"
            )

        return "\n".join(lines)

    def snapshot(self) -> dict:
        """Full JSON-serialisable snapshot.

        Note: this method is consumed by multiple runtime layers.
        We expose both the newer keys (active/completed) and compatibility
        aliases (active_goals/completed_goals) so other engines can rely on
        a stable shape.
        """
        with self._lock:
            active = [dict(g) for g in self._goals if g["status"] == "active"]
            completed = [dict(g) for g in self._goals if g["status"] == "completed"]
            return {
                "loaded": self._loaded,
                # Primary keys
                "active": active,
                "completed": completed,
                # Compatibility aliases (used by ContextAssembler + older tests)
                "active_goals": active,
                "completed_goals": completed,
                "pressure": self.pressure(),
            }

    def status(self) -> dict:
        """Compact status for HypostasRuntime /runtime/status endpoint."""
        active = self.active_goals
        blocked = self.blocked_goals
        return {
            "loaded": self._loaded,
            "active_count": len(active),
            "blocked_count": len(blocked),
            "completed_count": len(self.completed_goals),
            "pressure": round(self.pressure(), 3),
            "top_goal": active[0]["title"] if active else None,
        }

    # ------------------------------------------------------------------
    # Internal sync
    # ------------------------------------------------------------------

    def _sync_state(self) -> None:
        """Push current goal state into StateEngine (if available)."""
        if self._state is None:
            return
        try:
            active = [g for g in self._goals if g["status"] == "active"]
            self._state.set("goals.active", active)
            self._state.set("goals.completed_ids", [g["id"] for g in self._goals if g["status"] == "completed"])
            self._state.set("goals.blocked_ids", [g["id"] for g in self._goals if g.get("blockers")])
            self._state.set("goals.pressure", round(self.pressure(), 3))
            self._state.set("goals.last_updated", _now_iso())
        except Exception as exc:
            logger.warning("GoalEngine: state sync failed: %s", exc)

    def __repr__(self) -> str:  # pragma: no cover
        return f"GoalEngine(active={len(self.active_goals)}, pressure={self.pressure():.2f})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_blockers(g: dict) -> list[str]:
    """Normalise blockers from various goals.json formats."""
    raw = g.get("blockers") or g.get("blocked_on")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(b) for b in raw if b]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _extract_progress(g: dict) -> float:
    """Extract a numeric progress value [0.0, 1.0] from various goal formats."""
    # Preferred explicit fields
    for key in ("progress", "progress_pct", "percent", "pct"):
        if key in g:
            raw = g.get(key)
            # goals.json in this workspace often uses progress as a HISTORY list
            if isinstance(raw, list):
                continue
            if isinstance(raw, (int, float)):
                val = float(raw)
                # If someone stored 0-100, normalise
                if val > 1.0:
                    val = val / 100.0
                return max(0.0, min(1.0, val))
            if isinstance(raw, str):
                try:
                    val = float(raw)
                    if val > 1.0:
                        val = val / 100.0
                    return max(0.0, min(1.0, val))
                except Exception:
                    pass

    # Heuristic: if there are progress entries, assume some movement
    progress_history = g.get("progress")
    if isinstance(progress_history, list) and progress_history:
        return 0.1

    return 0.0


def _extract_note(g: dict) -> str:
    """Extract a note / latest progress entry from goals.json."""
    note = g.get("note") or g.get("description", "")
    if note:
        return str(note)
    progress = g.get("progress")
    if isinstance(progress, list) and progress:
        last = progress[-1]
        if isinstance(last, str):
            return last
        if isinstance(last, dict):
            return last.get("note", "")
    return ""
