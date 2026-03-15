"""ENGRAM — Episodic Memory Indexing. State via StateEngine under ``engram.*``."""
from __future__ import annotations
import time, uuid
from typing import TYPE_CHECKING, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .episodic_buffer import EpisodicBuffer

class Engram:
    _KEY = "engram"
    def __init__(self, state: "StateEngine", episodic: Optional["EpisodicBuffer"] = None) -> None:
        self._state = state
        self._episodic = episodic
        if self._state.get(f"{self._KEY}.store") is None:
            self._state.set(f"{self._KEY}.store", [])

    def encode(self, event: str, emotion: dict, location: str, sensory: dict = None) -> dict:
        engram = {
            "id": str(uuid.uuid4())[:8],
            "event": event,
            "emotion": {"valence": float(emotion.get("valence", 0)), "intensity": float(emotion.get("intensity", 0)), "label": str(emotion.get("label", "neutral"))},
            "location": location,
            "timestamp": time.time() * 1000,
            "sensory": sensory or {"voice": False, "image": False, "text_tone": "neutral"},
            "recall_count": 0,
        }
        store = list(self._state.get(f"{self._KEY}.store") or [])
        store.append(engram)
        if len(store) > 1000:
            store.sort(key=lambda e: e.get("emotion", {}).get("intensity", 0))
            store = store[len(store)-1000:]
        self._state.set(f"{self._KEY}.store", store)
        return engram

    def recall(self, query: str, n: int = 5) -> List[dict]:
        store = list(self._state.get(f"{self._KEY}.store") or [])
        keywords = query.lower().split()
        if not keywords: return []
        scored = []
        for e in store:
            text = f"{e.get('event', '')} {e.get('emotion', {}).get('label', '')}".lower()
            matches = sum(1 for kw in keywords if kw in text)
            if matches:
                scored.append((matches / len(keywords), e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:n]]

    def recall_by_place(self, location: str, limit: int = 5) -> List[dict]:
        store = list(self._state.get(f"{self._KEY}.store") or [])
        return [e for e in store if e.get("location") == location][-limit:]

    def get_places(self) -> Dict[str, int]:
        store = list(self._state.get(f"{self._KEY}.store") or [])
        places: Dict[str, int] = {}
        for e in store:
            loc = e.get("location", "unknown")
            places[loc] = places.get(loc, 0) + 1
        return places

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        store = list(self._state.get(f"{self._KEY}.store") or [])
        return {"total_engrams": len(store), "places": self.get_places()}
