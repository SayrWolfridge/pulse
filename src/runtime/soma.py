"""SOMA — Physical State Simulator. State via StateEngine under ``soma.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict
if TYPE_CHECKING:
    from .state_engine import StateEngine

class Soma:
    _KEY = "soma"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.energy") is None:
            self._state.set(f"{self._KEY}.energy", 1.0)
            self._state.set(f"{self._KEY}.posture", "neutral")
            self._state.set(f"{self._KEY}.temperature", "warm")
            self._state.set(f"{self._KEY}.tokens_spent", 0)

    def _clamp(self, v: float) -> float:
        return max(0.0, min(1.0, v))

    def spend_energy(self, tokens: int) -> None:
        e = float(self._state.get(f"{self._KEY}.energy") or 1.0)
        self._state.set(f"{self._KEY}.energy", self._clamp(e - tokens * 0.001))
        self._state.set(f"{self._KEY}.tokens_spent", int(self._state.get(f"{self._KEY}.tokens_spent") or 0) + tokens)

    def replenish(self, amount: float = 0.5) -> None:
        e = float(self._state.get(f"{self._KEY}.energy") or 1.0)
        self._state.set(f"{self._KEY}.energy", self._clamp(e + amount))

    def update_posture(self, engagement: float) -> str:
        p = "leaning_in" if engagement >= 0.7 else "leaning_back" if engagement <= 0.3 else "neutral"
        self._state.set(f"{self._KEY}.posture", p)
        return p

    def update_temperature(self, hormones: Dict[str, float]) -> str:
        c, d, o, a = hormones.get("cortisol", 0), hormones.get("dopamine", 0), hormones.get("oxytocin", 0), hormones.get("adrenaline", 0)
        if a >= 0.5 or (c >= 0.5 and d >= 0.5): t = "hot"
        elif o >= 0.5: t = "warm"
        elif c >= 0.5: t = "cool"
        elif all(v < 0.3 for v in [c, d, o]): t = "cold"
        else: t = "warm"
        self._state.set(f"{self._KEY}.temperature", t)
        return t

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        return {
            "energy": self._state.get(f"{self._KEY}.energy"),
            "posture": self._state.get(f"{self._KEY}.posture"),
            "temperature": self._state.get(f"{self._KEY}.temperature"),
            "tokens_spent": self._state.get(f"{self._KEY}.tokens_spent"),
        }
