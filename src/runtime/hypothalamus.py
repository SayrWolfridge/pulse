"""
HYPOTHALAMUS — Meta-Drive Layer
==================================
Ported from v1 pulse.src.hypothalamus into v2 HypostasRuntime.

Listens for recurring need signals from other modules. When enough
signals converge from different sources, HYPOTHALAMUS births a new drive.
Drives at weight floor for 30 days get retired.

All state persisted via StateEngine under ``hypothalamus.*`` dot-paths.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .goal_engine import GoalEngine

SIGNAL_THRESHOLD = 3
RETIREMENT_DAYS = 30
WEIGHT_FLOOR = 0.1
REDUCED_THRESHOLD_NEEDS = {"connection", "social", "belonging", "companionship"}


class Hypothalamus:
    """Meta-drive layer — births new drives from recurring signals."""

    _KEY = "hypothalamus"

    def __init__(self, state: "StateEngine", goal_engine: "GoalEngine") -> None:
        self._state = state
        self._goal_engine = goal_engine
        if self._state.get(f"{self._KEY}.pending_signals") is None:
            self._state.set(f"{self._KEY}.pending_signals", {})
            self._state.set(f"{self._KEY}.active_drives", {})
            self._state.set(f"{self._KEY}.retired_drives", [])
            self._state.set(f"{self._KEY}.last_scan", 0)

    def record_need_signal(self, need_name: str, source_module: str) -> dict:
        """Record a need signal from a module. May birth a drive."""
        now = time.time()
        pending = dict(self._state.get(f"{self._KEY}.pending_signals") or {})

        if need_name not in pending:
            pending[need_name] = {
                "modules": [],
                "first_seen": now,
                "last_seen": now,
                "count": 0,
            }

        p = pending[need_name]
        if source_module not in p["modules"]:
            p["modules"].append(source_module)
        p["last_seen"] = now
        p["count"] = p.get("count", 0) + 1

        result = {
            "need": need_name,
            "module_count": len(p["modules"]),
            "birthed": False,
        }

        # Check threshold
        threshold = 2 if need_name in REDUCED_THRESHOLD_NEEDS else SIGNAL_THRESHOLD
        age_hours = (now - p["first_seen"]) / 3600
        count_escalation = p["count"] >= 50 and age_hours >= 1.0

        active_drives = dict(self._state.get(f"{self._KEY}.active_drives") or {})

        if (len(p["modules"]) >= threshold or count_escalation) and need_name not in active_drives:
            active_drives[need_name] = {
                "weight": 1.0,
                "born_ts": now,
                "last_active_ts": now,
                "source_modules": list(p["modules"]),
                "at_floor_since": None,
            }
            del pending[need_name]
            result["birthed"] = True
            self._state.set(f"{self._KEY}.active_drives", active_drives)

            # Feed new drive type into GoalEngine
            try:
                self._goal_engine.add_open_loop(
                    f"[HYPO] New drive birthed: {need_name}",
                    priority=0.7,
                )
            except Exception:
                pass

        self._state.set(f"{self._KEY}.pending_signals", pending)
        return result

    def scan_drives(self) -> dict:
        """Periodic scan: decay weights, retire stale drives."""
        now = time.time()
        active_drives = dict(self._state.get(f"{self._KEY}.active_drives") or {})
        retired_list = list(self._state.get(f"{self._KEY}.retired_drives") or [])
        retired_names = []

        for name, drive in list(active_drives.items()):
            if not isinstance(drive, dict):
                continue
            age_days = (now - drive.get("born_ts", now)) / 86400
            if age_days > 7:
                drive["weight"] = max(WEIGHT_FLOOR, drive.get("weight", 1.0) - 0.01)

            if drive.get("weight", 1.0) <= WEIGHT_FLOOR:
                if drive.get("at_floor_since") is None:
                    drive["at_floor_since"] = now
                elif (now - drive["at_floor_since"]) / 86400 >= RETIREMENT_DAYS:
                    retired_names.append(name)
            else:
                drive["at_floor_since"] = None

        for name in retired_names:
            drive = active_drives.pop(name)
            retired_list.append({
                "name": name,
                "retired_ts": now,
                "lifespan_days": round((now - drive.get("born_ts", now)) / 86400, 1),
            })

        self._state.set(f"{self._KEY}.active_drives", active_drives)
        self._state.set(f"{self._KEY}.retired_drives", retired_list[-50:])
        self._state.set(f"{self._KEY}.last_scan", now)

        return {
            "active_drives": len(active_drives),
            "retired": retired_names,
            "pending_signals": len(self._state.get(f"{self._KEY}.pending_signals") or {}),
        }

    def reinforce_drive(self, drive_name: str, amount: float = 0.1) -> None:
        """Reinforce an active drive's weight."""
        active = dict(self._state.get(f"{self._KEY}.active_drives") or {})
        if drive_name in active:
            drive = active[drive_name]
            drive["weight"] = min(1.0, drive.get("weight", 0) + amount)
            drive["last_active_ts"] = time.time()
            drive["at_floor_since"] = None
            self._state.set(f"{self._KEY}.active_drives", active)

    def get_active_drives(self) -> dict:
        """Return all active drives."""
        return dict(self._state.get(f"{self._KEY}.active_drives") or {})

    def tick(self) -> None:
        """Periodic scan."""
        self.scan_drives()

    def status(self) -> dict:
        active = dict(self._state.get(f"{self._KEY}.active_drives") or {})
        pending = dict(self._state.get(f"{self._KEY}.pending_signals") or {})
        retired = list(self._state.get(f"{self._KEY}.retired_drives") or [])

        return {
            "active_drives": len(active),
            "pending_signals": len(pending),
            "retired_total": len(retired),
            "drives": {k: round(v.get("weight", 0), 3) for k, v in active.items() if isinstance(v, dict)},
        }
