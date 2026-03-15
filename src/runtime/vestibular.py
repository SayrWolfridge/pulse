"""VESTIBULAR — Balance Monitor. State via StateEngine under ``vestibular.*``."""
from __future__ import annotations
from typing import TYPE_CHECKING, Dict, List
if TYPE_CHECKING:
    from .state_engine import StateEngine

HEALTHY_RANGES = {"building_shipping": (0.3, 0.7), "working_reflecting": (0.4, 0.8), "autonomy_collaboration": (0.3, 0.7)}

class Vestibular:
    _KEY = "vestibular"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.counters") is None:
            self._state.set(f"{self._KEY}.counters", {"building": 0, "shipping": 0, "working": 0, "reflecting": 0, "autonomy": 0, "collaboration": 0})
            self._state.set(f"{self._KEY}.imbalances", [])

    def record_activity(self, activity_type: str, count: int = 1) -> None:
        counters = dict(self._state.get(f"{self._KEY}.counters") or {})
        counters[activity_type] = counters.get(activity_type, 0) + count
        self._state.set(f"{self._KEY}.counters", counters)

    def check_balance(self) -> dict:
        c = dict(self._state.get(f"{self._KEY}.counters") or {})
        ratios = {}; imbalances = []
        for name, a, b in [("building_shipping", "building", "shipping"), ("working_reflecting", "working", "reflecting"), ("autonomy_collaboration", "autonomy", "collaboration")]:
            total = c.get(a, 0) + c.get(b, 0)
            ratio = c.get(a, 0) / total if total else 0.5
            ratios[name] = round(ratio, 3)
            lo, hi = HEALTHY_RANGES[name]
            if ratio < lo or ratio > hi:
                imbalances.append({"ratio": name, "value": ratio, "direction": f"too much {b}" if ratio < lo else f"too much {a}"})
        self._state.set(f"{self._KEY}.imbalances", imbalances)
        return {"ratios": ratios, "imbalances": imbalances, "healthy": len(imbalances) == 0}

    def tick(self) -> None:
        self.check_balance()

    def status(self) -> dict:
        return {"counters": self._state.get(f"{self._KEY}.counters"), "imbalances": self._state.get(f"{self._KEY}.imbalances") or []}
