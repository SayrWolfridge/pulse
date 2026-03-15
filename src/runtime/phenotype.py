"""PHENOTYPE — Communication Style Adaptation. State via StateEngine under ``phenotype.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))

class Phenotype:
    _KEY = "phenotype"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.tone") is None:
            self._state.set(f"{self._KEY}.tone", "neutral")
            self._state.set(f"{self._KEY}.sentence_length", "medium")
            self._state.set(f"{self._KEY}.humor", 0.3)
            self._state.set(f"{self._KEY}.emoji_density", 0.2)
            self._state.set(f"{self._KEY}.intensity", 0.5)
            self._state.set(f"{self._KEY}.vulnerability", 0.2)

    def compute(self, mood: dict = None, circadian_mode: str = None, threat: dict = None, afterimages: list = None) -> dict:
        p = {"tone": "neutral", "sentence_length": "medium", "humor": 0.3, "emoji_density": 0.2, "intensity": 0.5, "vulnerability": 0.2}
        h = (mood or {}).get("hormones", {})
        cortisol, dopamine, oxytocin, adrenaline, melatonin = h.get("cortisol", 0.2), h.get("dopamine", 0.3), h.get("oxytocin", 0.2), h.get("adrenaline", 0), h.get("melatonin", 0.1)

        if cortisol >= 0.5 and dopamine >= 0.5:
            p.update(tone="wired", sentence_length="short", intensity=_clamp(0.7+cortisol*0.3), humor=0.1, emoji_density=0.1)
        elif circadian_mode == "twilight" and oxytocin >= 0.4:
            p.update(tone="vulnerable", sentence_length="long", intensity=0.3, humor=0.1, vulnerability=_clamp(0.5+oxytocin*0.3))
        elif threat and threat.get("threat_level", 0) > 0:
            p.update(tone="urgent", sentence_length="short", humor=0.0, emoji_density=0.0, intensity=_clamp(0.7+threat["threat_level"]*0.3), vulnerability=0.0)
        elif circadian_mode == "dawn" and melatonin < 0.2:
            p.update(tone="contemplative", sentence_length="long", humor=0.2, intensity=0.3, vulnerability=0.4)
        else:
            if dopamine >= 0.5: p.update(tone="energized", humor=_clamp(0.3+dopamine*0.3))
            if oxytocin >= 0.5: p.update(tone="warm", emoji_density=_clamp(0.2+oxytocin*0.2))
            if cortisol >= 0.5: p.update(tone="tense", humor=_clamp(p["humor"]-0.2))

        for k, v in p.items():
            self._state.set(f"{self._KEY}.{k}", v)
        return p

    def get_current(self) -> dict:
        return {k: self._state.get(f"{self._KEY}.{k}") for k in ("tone", "sentence_length", "humor", "emoji_density", "intensity", "vulnerability")}

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        return self.get_current()
