"""CEREBELLUM — Habit Automation. State via StateEngine under ``cerebellum.*``."""
from __future__ import annotations
import hashlib, time
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, List, Optional, Tuple
if TYPE_CHECKING:
    from .state_engine import StateEngine

class Cerebellum:
    _KEY = "cerebellum"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.task_history") is None:
            self._state.set(f"{self._KEY}.task_history", {})
            self._state.set(f"{self._KEY}.graduated_tasks", {})
            self._state.set(f"{self._KEY}.savings", {"total": 0, "today": 0, "today_date": ""})

    def track_execution(self, task_name: str, input_hash: str, output_pattern: str, tokens_used: int) -> None:
        history = dict(self._state.get(f"{self._KEY}.task_history") or {})
        entries = list(history.get(task_name, []))
        entries.append({"input_hash": input_hash, "output_hash": hashlib.sha256(output_pattern.encode()).hexdigest()[:16],
                        "output_pattern": output_pattern, "tokens_used": tokens_used, "ts": int(time.time()*1000)})
        history[task_name] = entries[-10:]
        self._state.set(f"{self._KEY}.task_history", history)

    def detect_habits(self, min_reps: int = 5, threshold: float = 0.85) -> List[dict]:
        history = dict(self._state.get(f"{self._KEY}.task_history") or {})
        graduated = dict(self._state.get(f"{self._KEY}.graduated_tasks") or {})
        candidates = []
        for task, execs in history.items():
            if task in graduated or len(execs) < min_reps: continue
            recent = execs[-min_reps:]
            patterns = [e["output_pattern"] for e in recent]
            sims = [SequenceMatcher(None, patterns[0], p).ratio() for p in patterns[1:]]
            avg = sum(sims) / len(sims) if sims else 0
            if avg >= threshold:
                candidates.append({"task_name": task, "similarity": round(avg, 3), "executions": len(execs)})
        return candidates

    def should_use_habit(self, task_name: str) -> Tuple[bool, Optional[str]]:
        graduated = dict(self._state.get(f"{self._KEY}.graduated_tasks") or {})
        g = graduated.get(task_name)
        return (True, g.get("script_path")) if g else (False, None)

    def record_savings(self, tokens: int) -> None:
        s = dict(self._state.get(f"{self._KEY}.savings") or {"total": 0, "today": 0})
        s["total"] = s.get("total", 0) + tokens
        self._state.set(f"{self._KEY}.savings", s)

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        return {
            "graduated_tasks": len(dict(self._state.get(f"{self._KEY}.graduated_tasks") or {})),
            "tracked_tasks": len(dict(self._state.get(f"{self._KEY}.task_history") or {})),
            "savings": self._state.get(f"{self._KEY}.savings"),
        }
