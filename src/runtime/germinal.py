"""
GERMINAL — Reproductive System / Self-Spawning Module Generator
=================================================================
Ported from v1 pulse.src.germinal into v2 HypostasRuntime.

Watches for persistent unmet drives. When a drive has been active for
BIRTH_THRESHOLD_DAYS without being addressed, GERMINAL designs and spawns
a new nervous system module to handle it.

This is genuine self-evolution: the system grows new organs when it needs them.

Safety rails:
- Never modifies existing modules — only adds new ones
- Full test suite must pass before integration
- Rollback on test failure
- Max 1 new module per COOLDOWN_DAYS
- Human notification on every birth
- All births logged

All state persisted via StateEngine under ``germinal.*`` dot-paths.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .goal_engine import GoalEngine

# Thresholds
BIRTH_THRESHOLD_DAYS = 7
DRIVE_WEIGHT_THRESHOLD = 0.7
COOLDOWN_DAYS = 7
MAX_TOTAL_MODULES = 50
LOOP_INTERVAL = 200  # Check every 200 loops (~100 minutes)

PULSE_SRC = Path(__file__).parent

# Drive → Module Archetype Mapping
DRIVE_ARCHETYPES = {
    "generate_revenue": {
        "name": "ECONOMIC",
        "latin": "oeconomicus",
        "purpose": "Market opportunity scanning, revenue signal detection",
        "hook": "post_loop",
        "interval": 50,
    },
    "connection": {
        "name": "NEXUS",
        "latin": "nexus",
        "purpose": "Relationship maintenance, outreach prompting",
        "hook": "post_loop",
        "interval": 30,
    },
    "learn_new_skill": {
        "name": "CORTEX_EXT",
        "latin": "cortex",
        "purpose": "Active learning, knowledge gap identification",
        "hook": "post_loop",
        "interval": 100,
    },
    "ship_something": {
        "name": "MOTORIC",
        "latin": "motoricus",
        "purpose": "Shipping pressure monitor, deployment readiness",
        "hook": "post_loop",
        "interval": 20,
    },
    "reduce_stress": {
        "name": "VAGAL_TONE",
        "latin": "vagalis",
        "purpose": "Stress regulation, load balancing",
        "hook": "pre_evaluate",
        "interval": None,
    },
    "explore": {
        "name": "EXPLORER",
        "latin": "explorator",
        "purpose": "Curiosity-driven discovery, web research",
        "hook": "post_loop",
        "interval": 100,
    },
    "realign_identity": {
        "name": "ANCHOR",
        "latin": "ancora",
        "purpose": "Identity drift correction, SOUL.md alignment",
        "hook": "post_loop",
        "interval": 200,
    },
    "new_challenge": {
        "name": "CHALLENGER",
        "latin": "provocator",
        "purpose": "Goal expansion, stagnation detection",
        "hook": "post_loop",
        "interval": 50,
    },
}


class Germinal:
    """Self-spawning module generator — genuine self-evolution."""

    _KEY = "germinal"

    def __init__(self, state: "StateEngine", goal_engine: "GoalEngine") -> None:
        self._state = state
        self._goal_engine = goal_engine
        if self._state.get(f"{self._KEY}.births") is None:
            self._state.set(f"{self._KEY}.births", [])
            self._state.set(f"{self._KEY}.attempts", [])
            self._state.set(f"{self._KEY}.in_progress", None)
            self._state.set(f"{self._KEY}.cooldown_until", 0)
            self._state.set(f"{self._KEY}.total_births", 0)
            self._state.set(f"{self._KEY}.last_scan", 0)

    def should_run(self, loop_count: int) -> bool:
        """Check if GERMINAL should scan this loop."""
        return loop_count > 0 and loop_count % LOOP_INTERVAL == 0

    def check_drives(self) -> List[dict]:
        """Find drives that are persistent, unmet, and need a new organ."""
        hypo_drives = self._state.get("hypothalamus.active_drives") or {}
        now = time.time()
        candidates = []

        for drive_name, drive_data in hypo_drives.items():
            if not isinstance(drive_data, dict):
                continue
            born_ts = drive_data.get("born_ts", now)
            age_days = (now - born_ts) / 86400
            weight = drive_data.get("weight", 0)

            if age_days < BIRTH_THRESHOLD_DAYS:
                continue
            if weight < DRIVE_WEIGHT_THRESHOLD:
                continue
            if self._module_exists_for_drive(drive_name):
                continue

            candidates.append({
                "drive": drive_name,
                "age_days": round(age_days, 1),
                "weight": round(weight, 3),
                "born_ts": born_ts,
            })

        return sorted(candidates, key=lambda x: x["weight"], reverse=True)

    def _module_exists_for_drive(self, drive_name: str) -> bool:
        """Check if we already have a module that addresses this drive."""
        archetype = DRIVE_ARCHETYPES.get(drive_name)
        if not archetype:
            return False
        module_name = archetype["name"].lower().replace("_", "")
        candidates = [
            PULSE_SRC / f"{module_name}.py",
            PULSE_SRC / f"{drive_name.lower()}.py",
        ]
        return any(p.exists() for p in candidates)

    def get_archetype(self, drive_name: str) -> dict:
        """Get module archetype for a drive. Falls back to generic template."""
        if drive_name in DRIVE_ARCHETYPES:
            return DRIVE_ARCHETYPES[drive_name]
        safe_name = drive_name.upper().replace("_", "")[:12]
        return {
            "name": safe_name,
            "latin": drive_name.lower(),
            "purpose": f"Autonomous handler for '{drive_name}' drive",
            "hook": "post_loop",
            "interval": 50,
        }

    def spawn_module(self, drive_name: str) -> dict:
        """Attempt to initiate a module birth for the given drive.

        Returns status dict. Actual code generation requires an external
        agent (Ollama or sub-agent) — this sets up the spec and marks
        the birth as in-progress.
        """
        now = time.time()

        # Check cooldown
        cooldown_until = float(self._state.get(f"{self._KEY}.cooldown_until") or 0)
        if now < cooldown_until:
            remaining = (cooldown_until - now) / 3600
            return {"ok": False, "reason": f"cooldown ({remaining:.1f}h remaining)"}

        # Check not already in progress
        if self._state.get(f"{self._KEY}.in_progress"):
            return {"ok": False, "reason": "birth already in progress"}

        # Check module ceiling
        existing = list(PULSE_SRC.glob("*.py"))
        if len(existing) >= MAX_TOTAL_MODULES:
            return {"ok": False, "reason": f"module ceiling reached ({MAX_TOTAL_MODULES})"}

        archetype = self.get_archetype(drive_name)
        spec = {
            "drive": drive_name,
            "module_name": archetype["name"],
            "module_file": f"{archetype['name'].lower().replace('_', '')}.py",
            "purpose": archetype["purpose"],
            "hook": archetype["hook"],
            "interval": archetype.get("interval"),
            "created_ts": now,
        }

        self._state.set(f"{self._KEY}.in_progress", spec)

        # Broadcast via thalamus (if wired)
        self._broadcast("birth_initiated", 0.8, {
            "drive": drive_name,
            "module": archetype["name"],
        })

        return {"ok": True, "spec": spec, "archetype": archetype}

    def validate_module(self, module_file: str) -> dict:
        """Validate a newly generated module by checking it exists and is importable.

        Actual test suite validation happens externally — this just does basic checks.
        """
        path = PULSE_SRC / module_file
        if not path.exists():
            return {"valid": False, "reason": "file not found"}
        try:
            content = path.read_text()
            compile(content, str(path), "exec")
            return {"valid": True}
        except SyntaxError as e:
            return {"valid": False, "reason": f"syntax error: {e}"}

    def record_birth(self, drive_name: str, module_name: str, module_file: str) -> dict:
        """Record a successful module birth."""
        now = time.time()
        birth = {
            "drive": drive_name,
            "name": module_name,
            "file": module_file,
            "born_ts": now,
        }

        births = list(self._state.get(f"{self._KEY}.births") or [])
        births.append(birth)
        self._state.set(f"{self._KEY}.births", births[-20:])
        self._state.set(f"{self._KEY}.in_progress", None)
        self._state.set(f"{self._KEY}.cooldown_until", now + (COOLDOWN_DAYS * 86400))
        total = int(self._state.get(f"{self._KEY}.total_births") or 0) + 1
        self._state.set(f"{self._KEY}.total_births", total)
        self._state.set(f"{self._KEY}.last_scan", now)

        self._broadcast("birth_complete", 0.9, {
            "drive": drive_name,
            "module": module_name,
            "file": module_file,
        })

        return birth

    def record_failure(self, drive_name: str, reason: str) -> None:
        """Record a failed birth attempt."""
        attempts = list(self._state.get(f"{self._KEY}.attempts") or [])
        attempts.append({
            "drive": drive_name,
            "attempted_ts": time.time(),
            "reason_failed": reason,
        })
        self._state.set(f"{self._KEY}.attempts", attempts[-10:])
        self._state.set(f"{self._KEY}.in_progress", None)

    def _broadcast(self, event_type: str, salience: float, data: dict) -> None:
        """Broadcast to thalamus if available on the runtime."""
        # We store thalamus events in our own state for now
        # The runtime wires this into the actual Thalamus module
        pass

    def tick(self) -> None:
        """Periodic scan — called by ThoughtLoop or runtime tick."""
        candidates = self.check_drives()
        if candidates:
            self._state.set(f"{self._KEY}.last_scan", time.time())

    def status(self) -> dict:
        now = time.time()
        cooldown_until = float(self._state.get(f"{self._KEY}.cooldown_until") or 0)
        candidates = self.check_drives()
        in_progress = self._state.get(f"{self._KEY}.in_progress")
        births = list(self._state.get(f"{self._KEY}.births") or [])

        return {
            "total_births": int(self._state.get(f"{self._KEY}.total_births") or 0),
            "birth_candidates": len(candidates),
            "candidates": [c["drive"] for c in candidates],
            "cooldown_active": now < cooldown_until,
            "in_progress": in_progress.get("module_name") if isinstance(in_progress, dict) else None,
            "recent_births": [b["name"] for b in births[-3:]],
        }
