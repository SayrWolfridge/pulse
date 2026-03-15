"""DENDRITE — Social Graph. State via StateEngine under ``dendrite.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .relationship_graph import RelationshipGraph

PRIMARY = "josh"

class Dendrite:
    _KEY = "dendrite"
    def __init__(self, state: "StateEngine", relationships: "RelationshipGraph" = None) -> None:
        self._state = state
        self._relationships = relationships
        if self._state.get(f"{self._KEY}.people") is None:
            self._state.set(f"{self._KEY}.people", {
                PRIMARY: {"trust": 0.95, "interaction_count": 0, "last_interaction": 0,
                          "communication_style": "intimate", "emotional_valence": 0.8, "is_primary": True}
            })

    def record_interaction(self, person: str, valence: float = 0.0, style: str = None) -> dict:
        people = dict(self._state.get(f"{self._KEY}.people") or {})
        p_lower = person.lower()
        if p_lower not in people:
            people[p_lower] = {"trust": 0.3, "interaction_count": 0, "last_interaction": 0,
                               "communication_style": style or "casual", "emotional_valence": 0.5, "is_primary": False}
        p = people[p_lower]
        p["interaction_count"] = p.get("interaction_count", 0) + 1
        p["last_interaction"] = time.time()
        p["emotional_valence"] = max(0.0, min(1.0, p.get("emotional_valence", 0.5) * 0.8 + valence * 0.2))
        if valence > 0: p["trust"] = min(1.0, p.get("trust", 0.3) + 0.01)
        elif valence < -0.5: p["trust"] = max(0.0, p.get("trust", 0.3) - 0.05)
        if style: p["communication_style"] = style
        people[p_lower] = p
        self._state.set(f"{self._KEY}.people", people)
        return p

    def get_person(self, person: str) -> Optional[dict]:
        return (self._state.get(f"{self._KEY}.people") or {}).get(person.lower())

    def get_primary(self) -> dict:
        return (self._state.get(f"{self._KEY}.people") or {}).get(PRIMARY, {})

    def get_social_graph(self) -> dict:
        return dict(self._state.get(f"{self._KEY}.people") or {})

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        people = dict(self._state.get(f"{self._KEY}.people") or {})
        return {"total_people": len(people), "primary_trust": people.get(PRIMARY, {}).get("trust", 0)}
