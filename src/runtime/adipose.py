"""ADIPOSE — Token/Energy Budgeting. State via StateEngine under ``adipose.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict
if TYPE_CHECKING:
    from .state_engine import StateEngine

DEFAULT_ALLOC = {"conversation": 0.60, "crons": 0.25, "reserve": 0.15}

class Adipose:
    _KEY = "adipose"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.daily_budget") is None:
            self._state.set(f"{self._KEY}.daily_budget", 1_000_000)
            self._state.set(f"{self._KEY}.usage", {"conversation": 0, "crons": 0, "reserve": 0})
            self._recalc()

    def _recalc(self) -> None:
        total = int(self._state.get(f"{self._KEY}.daily_budget") or 1_000_000)
        self._state.set(f"{self._KEY}.budgets", {k: int(total * v) for k, v in DEFAULT_ALLOC.items()})

    def allocate(self, category: str, tokens: int) -> bool:
        budgets = dict(self._state.get(f"{self._KEY}.budgets") or {})
        usage = dict(self._state.get(f"{self._KEY}.usage") or {})
        if usage.get(category, 0) + tokens > budgets.get(category, 0):
            return False
        usage[category] = usage.get(category, 0) + tokens
        self._state.set(f"{self._KEY}.usage", usage)
        return True

    def get_remaining(self, category: str) -> int:
        budgets = dict(self._state.get(f"{self._KEY}.budgets") or {})
        usage = dict(self._state.get(f"{self._KEY}.usage") or {})
        return max(0, budgets.get(category, 0) - usage.get(category, 0))

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        budgets = dict(self._state.get(f"{self._KEY}.budgets") or {})
        usage = dict(self._state.get(f"{self._KEY}.usage") or {})
        cats = {}
        for c in ["conversation", "crons", "reserve"]:
            b = budgets.get(c, 0)
            u = usage.get(c, 0)
            cats[c] = {"budget": b, "used": u, "remaining": max(0, b - u), "pct": round(u/b*100, 1) if b else 0}
        return {"daily_budget": self._state.get(f"{self._KEY}.daily_budget"), "categories": cats}
