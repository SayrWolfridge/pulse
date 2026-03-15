"""CALLOSUM — Logic-Emotion Bridge. State via StateEngine under ``callosum.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .emotion_engine import EmotionEngine

class Callosum:
    _KEY = "callosum"
    def __init__(self, state: "StateEngine", emotion: "EmotionEngine" = None) -> None:
        self._state = state
        self._emotion = emotion
        if self._state.get(f"{self._KEY}.insights") is None:
            self._state.set(f"{self._KEY}.insights", [])
            self._state.set(f"{self._KEY}.integration_history", [])
            self._state.set(f"{self._KEY}.bridge_count", 0)

    def bridge(self) -> dict:
        """Run reconciliation: compare logical state vs emotional state."""
        # Get emotional state
        emotional = "unknown"
        if self._emotion:
            try:
                snap = self._emotion.snapshot()
                emotional = f"mood: {snap.get('color', 'unknown')}, dominant: {', '.join(snap.get('dominant', []))}"
            except Exception:
                pass

        # Get logical state from working memory
        loops = self._state.get("working_memory.open_loops") or []
        logical = f"{len(loops)} open loops" if loops else "quiet — no open loops"

        # Get gut signal
        gut = "neutral"
        try:
            enteric_patterns = list(self._state.get("enteric.patterns") or [])
            if enteric_patterns:
                gut = enteric_patterns[-1].get("direction", "neutral")
        except Exception:
            pass

        # Calculate integration score
        score = 0.5
        if gut == "toward": score += 0.15
        elif gut == "away": score -= 0.15
        if "content" in emotional.lower() or "energized" in emotional.lower(): score += 0.15
        if "stressed" in emotional.lower() or "frustrated" in emotional.lower(): score -= 0.1
        if loops: score += 0.1
        score = max(0.0, min(1.0, score))

        # Detect split
        split = False; tension = ""
        if gut == "away" and loops:
            split = True; tension = "Logical side active but gut says pull away"
        elif "frustrat" in emotional.lower() and loops:
            split = True; tension = f"Emotional state ({emotional}) conflicts with active processing"

        insight = {
            "ts": time.time()*1000, "logical": logical, "emotional": emotional,
            "gut": gut, "split": split, "tension": tension,
            "integration_score": round(score, 3),
        }
        insights = list(self._state.get(f"{self._KEY}.insights") or [])
        insights.append(insight)
        self._state.set(f"{self._KEY}.insights", insights[-50:])
        history = list(self._state.get(f"{self._KEY}.integration_history") or [])
        history.append({"ts": time.time()*1000, "score": score})
        self._state.set(f"{self._KEY}.integration_history", history[-100:])
        self._state.set(f"{self._KEY}.bridge_count", int(self._state.get(f"{self._KEY}.bridge_count") or 0) + 1)
        return insight

    def get_integration_score(self) -> float:
        history = list(self._state.get(f"{self._KEY}.integration_history") or [])
        if not history: return 0.5
        recent = history[-10:]
        return sum(h["score"] for h in recent) / len(recent)

    def tick(self) -> None:
        # Run bridge every 10th call (managed by ThoughtLoop)
        pass

    def status(self) -> dict:
        return {
            "bridge_count": self._state.get(f"{self._KEY}.bridge_count") or 0,
            "integration_score": round(self.get_integration_score(), 3),
            "latest_split": any(i.get("split") for i in (list(self._state.get(f"{self._KEY}.insights") or []))[-3:]),
        }
