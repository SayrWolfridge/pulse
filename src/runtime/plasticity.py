"""PLASTICITY — Drive Weight Evolution. State via StateEngine under ``plasticity.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Any
if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .goal_engine import GoalEngine

class Plasticity:
    _KEY = "plasticity"
    def __init__(self, state: "StateEngine", goal_engine: "GoalEngine" = None) -> None:
        self._state = state
        self._goal_engine = goal_engine
        if self._state.get(f"{self._KEY}.history") is None:
            self._state.set(f"{self._KEY}.history", {})
            self._state.set(f"{self._KEY}.eval_count", 0)

    def record_evaluation(self, drive_name: str, success: bool, quality: float, context: str = "") -> Optional[dict]:
        history = dict(self._state.get(f"{self._KEY}.history") or {})
        records = list(history.get(drive_name, []))
        records.append({"ts": time.time(), "success": success, "quality": max(0, min(1, quality)), "context": context})
        history[drive_name] = records[-100:]
        self._state.set(f"{self._KEY}.history", history)
        count = int(self._state.get(f"{self._KEY}.eval_count") or 0) + 1
        self._state.set(f"{self._KEY}.eval_count", count)
        if count % 10 == 0:
            return self.evolve()
        return None

    def evolve(self, current_weights: Dict[str, float] = None) -> dict:
        history = dict(self._state.get(f"{self._KEY}.history") or {})
        changes = []
        for drive, records in history.items():
            if len(records) < 3: continue
            successes = sum(1 for r in records if r["success"])
            tp_rate = successes / len(records)
            avg_q = sum(r["quality"] for r in records) / len(records)
            composite = 0.4 * tp_rate + 0.3 * avg_q + 0.3 * (1 - (1 - tp_rate))
            if 0.4 <= composite <= 0.6: continue
            delta = min(0.1, max(-0.1, (composite - 0.5) * 0.5))
            if current_weights and drive in current_weights:
                old = current_weights[drive]
                new = max(0.3, min(3.0, old + delta))
                if new != old:
                    changes.append({"drive": drive, "before": round(old, 4), "after": round(new, 4), "delta": round(delta, 4)})
        return {"changes": changes, "eval_count": self._state.get(f"{self._KEY}.eval_count")}

    def get_summary(self) -> dict:
        history = dict(self._state.get(f"{self._KEY}.history") or {})
        drives = {}
        for name, records in history.items():
            if not records: continue
            s = sum(1 for r in records if r["success"])
            drives[name] = {"total": len(records), "success_rate": round(s / len(records), 3),
                           "avg_quality": round(sum(r["quality"] for r in records) / len(records), 3)}
        return {"eval_count": self._state.get(f"{self._KEY}.eval_count"), "drives": drives}

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        return self.get_summary()
