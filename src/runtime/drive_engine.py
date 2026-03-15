"""
DRIVE ENGINE — Drive Pressure Calculation Layer
===================================================
Ported from v1 pulse.src.drives.engine into v2 HypostasRuntime.

The math layer underneath HYPOTHALAMUS and GoalEngine. Calculates composite
pressure scores across all active drives, applies decay over time, handles
drive conflicts, and produces a ranked list for ThoughtLoop to act on.

All state persisted via StateEngine under ``drive_engine.*`` dot-paths.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .goal_engine import GoalEngine

logger = logging.getLogger("pulse.runtime.drive_engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DECAY_RATE = 0.02      # pressure decay per tick (per 5 min)
DEFAULT_MAX_PRESSURE = 10.0    # pressure cap
CONFLICT_DAMPENING = 0.7       # reduce conflicting drive pressures

# Drive conflict pairs — if both are high, dampen the weaker one
CONFLICT_PAIRS = [
    ("rest", "goals"),           # rest conflicts with productivity
    ("rest", "creative_expression"),
    ("emotions", "system"),       # emotional processing conflicts with system work
]


# ---------------------------------------------------------------------------
# DriveEngine
# ---------------------------------------------------------------------------

class DriveEngine:
    """Calculates composite pressure scores across all active drives.

    Reads drive weights from:
      1. StateEngine ``drives.*`` (set by Hypothalamus, GoalEngine, etc.)
      2. GoalEngine pressure (feeds into ``goals`` drive)

    Produces:
      - Ranked drive pressures
      - Top drive for ThoughtLoop
      - Decay over time
    """

    def __init__(self, state: "StateEngine", goal_engine: "GoalEngine") -> None:
        self._state = state
        self._goal_engine = goal_engine
        self._last_tick: float = time.time()

        # Initialize drive pressures if not present
        if self._state.get("drive_engine.pressures") is None:
            self._state.set("drive_engine.pressures", {})
            self._state.set("drive_engine.last_tick", time.time())

    def calculate_pressures(self) -> Dict[str, float]:
        """Return {drive_name: pressure_score} for all active drives.

        Combines:
          1. Base drive weights from StateEngine ``drives.*``
          2. GoalEngine pressure → ``goals`` drive
          3. Hypothalamus active drives → dynamic drives
          4. Sensor-driven spikes from state
        """
        pressures: Dict[str, float] = {}

        # 1. Read base drive weights from StateEngine
        base_drives = self._state.get("drives") or {}
        if isinstance(base_drives, dict):
            for name, value in base_drives.items():
                if isinstance(value, (int, float)):
                    pressures[name] = float(value)
                elif isinstance(value, dict) and "weight" in value:
                    pressures[name] = float(value.get("weight", 0))

        # 2. Get stored pressures and merge
        stored = self._state.get("drive_engine.pressures") or {}
        if isinstance(stored, dict):
            for name, value in stored.items():
                if isinstance(value, (int, float)):
                    # Stored pressures accumulate on top of base weights
                    pressures[name] = pressures.get(name, 0) + float(value)

        # 3. GoalEngine pressure → boost goals drive
        try:
            goal_pressure = self._goal_engine.pressure()
            pressures["goals"] = pressures.get("goals", 0) + goal_pressure
        except Exception:
            pass

        # 4. Hypothalamus active drives
        hypo_drives = self._state.get("hypothalamus.active_drives") or {}
        if isinstance(hypo_drives, dict):
            for name, drive_data in hypo_drives.items():
                if isinstance(drive_data, dict):
                    weight = drive_data.get("weight", 0.5)
                    pressures[name] = pressures.get(name, 0) + float(weight)

        # 5. Apply conflict dampening
        pressures = self._apply_conflicts(pressures)

        # Cap pressures
        pressures = {
            name: min(DEFAULT_MAX_PRESSURE, max(0.0, p))
            for name, p in pressures.items()
        }

        return pressures

    def top_drive(self) -> Tuple[str, float]:
        """Return (drive_name, pressure) of highest-pressure drive."""
        pressures = self.calculate_pressures()
        if not pressures:
            return ("none", 0.0)
        top_name = max(pressures, key=pressures.get)  # type: ignore[arg-type]
        return (top_name, pressures[top_name])

    def apply_decay(self) -> None:
        """Decay all drive pressures based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_tick
        self._last_tick = now

        # Decay rate scaled by time elapsed (normalized to 5-min intervals)
        decay_factor = DEFAULT_DECAY_RATE * (elapsed / 300.0)

        stored = dict(self._state.get("drive_engine.pressures") or {})
        changed = False

        for name in list(stored.keys()):
            if isinstance(stored[name], (int, float)):
                old = stored[name]
                new = max(0.0, old - decay_factor)
                if new != old:
                    stored[name] = round(new, 6)
                    changed = True
                # Remove zeroed entries
                if stored[name] <= 0.001:
                    del stored[name]
                    changed = True

        if changed:
            self._state.set("drive_engine.pressures", stored)

    def spike(self, drive_name: str, amount: float) -> float:
        """Spike a drive's pressure. Returns new pressure."""
        stored = dict(self._state.get("drive_engine.pressures") or {})
        current = float(stored.get(drive_name, 0))
        new = min(DEFAULT_MAX_PRESSURE, current + amount)
        stored[drive_name] = round(new, 4)
        self._state.set("drive_engine.pressures", stored)
        return new

    def decay_drive(self, drive_name: str, amount: float) -> float:
        """Manually decay a specific drive. Returns new pressure."""
        stored = dict(self._state.get("drive_engine.pressures") or {})
        current = float(stored.get(drive_name, 0))
        new = max(0.0, current - amount)
        stored[drive_name] = round(new, 4)
        self._state.set("drive_engine.pressures", stored)
        return new

    def ranked(self) -> List[Tuple[str, float]]:
        """Return all drives sorted by pressure, highest first."""
        pressures = self.calculate_pressures()
        return sorted(pressures.items(), key=lambda x: x[1], reverse=True)

    def status(self) -> dict:
        """Return current drive engine status."""
        pressures = self.calculate_pressures()
        ranked = sorted(pressures.items(), key=lambda x: x[1], reverse=True)
        top_name, top_pressure = self.top_drive()

        return {
            "top_drive": top_name,
            "top_pressure": round(top_pressure, 4),
            "pressures": {k: round(v, 4) for k, v in ranked},
            "drive_count": len(pressures),
            "last_tick": self._last_tick,
        }

    def tick(self) -> None:
        """Called by ThoughtLoop each cycle. Apply decay and update state."""
        self.apply_decay()

        # Store current pressures in state for other modules
        pressures = self.calculate_pressures()
        self._state.set("drive_engine.current_pressures", {
            k: round(v, 4) for k, v in pressures.items()
        })
        self._state.set("drive_engine.top_drive", self.top_drive()[0])
        self._state.set("drive_engine.last_tick", time.time())

    def _apply_conflicts(self, pressures: Dict[str, float]) -> Dict[str, float]:
        """Dampen conflicting drives — if two push in opposite directions,
        the weaker one gets reduced."""
        for drive_a, drive_b in CONFLICT_PAIRS:
            if drive_a in pressures and drive_b in pressures:
                pa = pressures[drive_a]
                pb = pressures[drive_b]
                if pa > 0 and pb > 0:
                    # Dampen the weaker one
                    if pa > pb:
                        pressures[drive_b] = pb * CONFLICT_DAMPENING
                    elif pb > pa:
                        pressures[drive_a] = pa * CONFLICT_DAMPENING
        return pressures
