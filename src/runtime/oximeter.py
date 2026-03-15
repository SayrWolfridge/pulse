"""
OXIMETER — External Perception Tracker
=========================================
Ported from v1 pulse.src.oximeter into v2 HypostasRuntime.

Tracks engagement metrics (followers, likes, sentiment).
Compares self-perception vs external reality. Detects gaps.

All state persisted via StateEngine under ``oximeter.*`` dot-paths.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class Oximeter:
    """External perception tracker — self-view vs reality gap detection."""

    _KEY = "oximeter"

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.metrics") is None:
            self._state.set(f"{self._KEY}.metrics", {
                "followers": 0,
                "likes": 0,
                "replies": 0,
                "sentiment": 0.5,
            })
            self._state.set(f"{self._KEY}.self_perception", {
                "impact": 0.5,
                "reception": 0.5,
            })
            self._state.set(f"{self._KEY}.perception_gap", 0.0)
            self._state.set(f"{self._KEY}.history", [])
            self._state.set(f"{self._KEY}.last_update", 0)

    def update_metrics(
        self,
        followers: Optional[int] = None,
        likes: Optional[int] = None,
        replies: Optional[int] = None,
        sentiment: Optional[float] = None,
    ) -> dict:
        """Update external metrics."""
        m = dict(self._state.get(f"{self._KEY}.metrics") or {})
        if followers is not None:
            m["followers"] = followers
        if likes is not None:
            m["likes"] = likes
        if replies is not None:
            m["replies"] = replies
        if sentiment is not None:
            m["sentiment"] = _clamp01(sentiment)
        self._state.set(f"{self._KEY}.metrics", m)
        self._state.set(f"{self._KEY}.last_update", time.time())
        return m

    def update_self_perception(
        self,
        impact: Optional[float] = None,
        reception: Optional[float] = None,
    ) -> dict:
        """Update self-perception scores."""
        sp = dict(self._state.get(f"{self._KEY}.self_perception") or {})
        if impact is not None:
            sp["impact"] = _clamp01(impact)
        if reception is not None:
            sp["reception"] = _clamp01(reception)
        self._state.set(f"{self._KEY}.self_perception", sp)
        self._state.set(f"{self._KEY}.last_update", time.time())
        return sp

    def detect_gap(self) -> dict:
        """Compare self-perception vs external reality."""
        m = dict(self._state.get(f"{self._KEY}.metrics") or {})
        sp = dict(self._state.get(f"{self._KEY}.self_perception") or {})

        ext_impact = min(1.0, m.get("followers", 0) / 10000) if m.get("followers") else 0.0
        ext_reception = m.get("sentiment", 0.5)

        impact_gap = abs(sp.get("impact", 0.5) - ext_impact)
        reception_gap = abs(sp.get("reception", 0.5) - ext_reception)
        overall_gap = (impact_gap + reception_gap) / 2

        self._state.set(f"{self._KEY}.perception_gap", round(overall_gap, 3))

        history = list(self._state.get(f"{self._KEY}.history") or [])
        history.append({"ts": time.time(), "gap": round(overall_gap, 3)})
        self._state.set(f"{self._KEY}.history", history[-100:])

        return {
            "impact_gap": round(impact_gap, 3),
            "reception_gap": round(reception_gap, 3),
            "overall_gap": round(overall_gap, 3),
            "self_overestimates": sp.get("impact", 0.5) > ext_impact,
        }

    def tick(self) -> None:
        """Periodic gap detection."""
        self.detect_gap()

    def status(self) -> dict:
        return {
            "metrics": self._state.get(f"{self._KEY}.metrics") or {},
            "self_perception": self._state.get(f"{self._KEY}.self_perception") or {},
            "perception_gap": self._state.get(f"{self._KEY}.perception_gap") or 0.0,
        }
