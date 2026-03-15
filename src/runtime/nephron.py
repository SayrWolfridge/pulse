"""NEPHRON — Memory Pruning. State via StateEngine under ``nephron.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .context_engine import ContextEngine

LOOP_INTERVAL = 100

class Nephron:
    _KEY = "nephron"
    def __init__(self, state: "StateEngine", context: "ContextEngine" = None) -> None:
        self._state = state
        self._context = context
        if self._state.get(f"{self._KEY}.total_cycles") is None:
            self._state.set(f"{self._KEY}.total_cycles", 0)
            self._state.set(f"{self._KEY}.total_pruned", 0)
            self._state.set(f"{self._KEY}.last_run", 0)

    def should_run(self, loop_count: int) -> bool:
        return loop_count > 0 and loop_count % LOOP_INTERVAL == 0

    def filter_all(self) -> dict:
        pruned = {}
        # Prune engram store if > 1000
        store = list(self._state.get("engram.store") or [])
        if len(store) > 1000:
            store.sort(key=lambda e: e.get("emotion", {}).get("intensity", 0))
            removed = len(store) - 1000
            self._state.set("engram.store", store[removed:])
            pruned["engrams"] = removed
        # Prune endocrine event_log if > 200
        log = list(self._state.get("endocrine.event_log") or [])
        if len(log) > 200:
            removed = len(log) - 200
            self._state.set("endocrine.event_log", log[removed:])
            pruned["endocrine_log"] = removed
        # Prune limbic afterimages if > 50
        images = list(self._state.get("limbic.afterimages") or [])
        if len(images) > 50:
            removed = len(images) - 50
            self._state.set("limbic.afterimages", images[removed:])
            pruned["afterimages"] = removed

        total = sum(pruned.values())
        self._state.set(f"{self._KEY}.total_cycles", int(self._state.get(f"{self._KEY}.total_cycles") or 0) + 1)
        self._state.set(f"{self._KEY}.total_pruned", int(self._state.get(f"{self._KEY}.total_pruned") or 0) + total)
        self._state.set(f"{self._KEY}.last_run", time.time())
        return {"pruned": pruned, "total": total}

    def tick(self) -> None:
        pass  # called via should_run / filter_all

    def status(self) -> dict:
        return {
            "total_cycles": self._state.get(f"{self._KEY}.total_cycles") or 0,
            "total_pruned": self._state.get(f"{self._KEY}.total_pruned") or 0,
            "last_run": self._state.get(f"{self._KEY}.last_run"),
        }
