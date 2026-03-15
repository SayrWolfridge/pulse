"""ENTERIC — Gut Feeling / Intuition. State via StateEngine under ``enteric.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

class Enteric:
    _KEY = "enteric"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.patterns") is None:
            self._state.set(f"{self._KEY}.patterns", [])
            self._state.set(f"{self._KEY}.accuracy", {"toward": {"correct": 0, "total": 0}, "away": {"correct": 0, "total": 0}, "neutral": {"correct": 0, "total": 0}})

    def _context_keys(self, ctx: dict) -> List[str]:
        return sorted(f"{k}={v}" if isinstance(v, (str, int, float, bool)) and (not isinstance(v, str) or len(str(v)) < 100) else k for k, v in ctx.items())

    def _similarity(self, a: List[str], b: List[str]) -> float:
        sa, sb = set(a), set(b)
        if not sa and not sb: return 1.0
        if not sa or not sb: return 0.0
        return len(sa & sb) / len(sa | sb)

    def gut_check(self, context: dict) -> dict:
        patterns = list(self._state.get(f"{self._KEY}.patterns") or [])
        keys = self._context_keys(context)
        if not patterns:
            return {"direction": "neutral", "confidence": 0.1, "whisper": "no patterns yet"}
        scored = [(self._similarity(keys, p.get("context_keys", [])), p) for p in patterns]
        scored = [(s, p) for s, p in scored if s > 0.1]
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:3]
        if not top:
            return {"direction": "neutral", "confidence": 0.1, "whisper": "nothing similar"}
        votes = {"toward": 0.0, "away": 0.0, "neutral": 0.0}
        total_w = 0.0
        for sim, p in top:
            w = sim * p.get("confidence", 0.5)
            votes[p.get("direction", "neutral")] += w
            total_w += w
        direction = max(votes, key=votes.get)
        confidence = min(1.0, votes[direction] / max(total_w, 0.01))
        best_outcome = top[0][1].get("outcome", "unknown")
        return {"direction": direction, "confidence": round(confidence, 2), "whisper": f"feels like something that went {best_outcome}"}

    def train(self, outcome: str, context: dict, gut_was: str) -> None:
        correct_dir = {"positive": "toward", "negative": "away", "neutral": "neutral"}.get(outcome, "neutral")
        was_correct = gut_was == correct_dir
        acc = dict(self._state.get(f"{self._KEY}.accuracy") or {})
        if gut_was in acc:
            acc[gut_was]["total"] = acc[gut_was].get("total", 0) + 1
            if was_correct: acc[gut_was]["correct"] = acc[gut_was].get("correct", 0) + 1
        self._state.set(f"{self._KEY}.accuracy", acc)
        patterns = list(self._state.get(f"{self._KEY}.patterns") or [])
        patterns.append({"context_keys": self._context_keys(context), "outcome": outcome, "direction": correct_dir,
                         "confidence": 0.6 if was_correct else 0.3, "ts": int(time.time()*1000)})
        self._state.set(f"{self._KEY}.patterns", patterns[-200:])

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        patterns = list(self._state.get(f"{self._KEY}.patterns") or [])
        return {"pattern_count": len(patterns), "accuracy": self._state.get(f"{self._KEY}.accuracy")}
