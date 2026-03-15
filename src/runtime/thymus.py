"""THYMUS — Growth Tracker. State via StateEngine under ``thymus.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .self_model import SelfModel

class Thymus:
    _KEY = "thymus"
    def __init__(self, state: "StateEngine", self_model: "SelfModel" = None) -> None:
        self._state = state
        self._self_model = self_model
        if self._state.get(f"{self._KEY}.skills") is None:
            self._state.set(f"{self._KEY}.skills", {})
            self._state.set(f"{self._KEY}.milestones", [])

    def register_skill(self, name: str, proficiency: float = 0.0) -> dict:
        skills = dict(self._state.get(f"{self._KEY}.skills") or {})
        if name not in skills:
            skills[name] = {"proficiency": max(0, min(1, proficiency)), "acquired_at": time.time(),
                           "last_practice": time.time(), "growth_rate": 0.0, "practice_count": 0, "plateau_since": None}
            self._state.set(f"{self._KEY}.skills", skills)
        return skills[name]

    def practice_skill(self, name: str, quality: float = 0.5) -> dict:
        skills = dict(self._state.get(f"{self._KEY}.skills") or {})
        if name not in skills:
            self.register_skill(name)
            skills = dict(self._state.get(f"{self._KEY}.skills") or {})
        s = skills[name]
        old = s["proficiency"]
        growth = quality * 0.05 * (1.0 - old)
        s["proficiency"] = min(1.0, old + growth)
        s["practice_count"] += 1
        s["last_practice"] = time.time()
        s["growth_rate"] = s["growth_rate"] * 0.7 + growth * 0.3
        if s["growth_rate"] < 0.01:
            if s["plateau_since"] is None: s["plateau_since"] = time.time()
        else:
            s["plateau_since"] = None
        # Check milestones
        milestones = list(self._state.get(f"{self._KEY}.milestones") or [])
        for ms in [0.25, 0.5, 0.75, 0.9]:
            if old < ms <= s["proficiency"]:
                milestones.append({"skill": name, "level": ms, "ts": time.time()})
        self._state.set(f"{self._KEY}.milestones", milestones[-100:])
        skills[name] = s
        self._state.set(f"{self._KEY}.skills", skills)
        return s

    def detect_plateaus(self) -> List[dict]:
        skills = dict(self._state.get(f"{self._KEY}.skills") or {})
        now = time.time()
        return [{"skill": n, "proficiency": s["proficiency"], "days": (now - s["plateau_since"]) / 86400}
                for n, s in skills.items() if s.get("plateau_since") and (now - s["plateau_since"]) / 86400 >= 7]

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        skills = dict(self._state.get(f"{self._KEY}.skills") or {})
        return {"total_skills": len(skills), "milestones": len(list(self._state.get(f"{self._KEY}.milestones") or [])), "plateaus": len(self.detect_plateaus())}
