"""
LIMBIC — Emotional Afterimages
================================
Ported from v1 pulse.src.limbic into v2 HypostasRuntime.

High-intensity emotions (intensity > 7 or |valence| > 2) leave decaying
afterimages with a 4-hour half-life. These color subsequent emotional
responses and feed into EmotionEngine.

All state via StateEngine under ``limbic.*``.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine

DEFAULT_HALF_LIFE_MS = 14_400_000  # 4 hours
DECAY_THRESHOLD = 0.5

# Emotional contagion keywords (from v1)
_CONTAGION_MAP = {
    "joy": (["happy", "excited", "amazing", "love it", "wonderful", "great news", "yay", "!!"], 1.5, 7.5),
    "frustration": (["frustrated", "annoyed", "ugh", "broken", "stupid", "hate", "angry"], -1.5, 7.5),
    "sadness": (["sad", "miss", "lonely", "heartbroken", "crying", "devastating"], -2.0, 8.0),
    "excitement": (["can't wait", "so excited", "incredible", "blown away", "pumped"], 2.0, 8.0),
    "anxiety": (["worried", "scared", "nervous", "anxious", "freaking out", "panic"], -1.0, 7.5),
    "warmth": (["thank you", "appreciate", "grateful", "means a lot", "love you"], 2.0, 7.5),
}


def _valence_to_emotion(valence: float, intensity: float) -> str:
    if valence > 1.5:
        return "elation" if intensity > 8 else "joy"
    elif valence > 0:
        return "excitement" if intensity > 7 else "warmth"
    elif valence > -1:
        return "unease" if intensity > 7 else "melancholy"
    else:
        return "anguish" if intensity > 8 else "frustration"


def _decayed_intensity(afterimage: dict, now_ms: Optional[int] = None) -> float:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    elapsed = now_ms - afterimage.get("created_at", now_ms)
    if elapsed <= 0:
        return afterimage.get("intensity", 0)
    half_life = afterimage.get("half_life_ms", DEFAULT_HALF_LIFE_MS)
    return afterimage["intensity"] * math.pow(0.5, elapsed / half_life)


class Limbic:
    """Emotional afterimage system — high-intensity emotions leave decaying residue."""

    _KEY = "limbic"

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        self._load_or_seed()

    def _load_or_seed(self) -> None:
        if self._state.get(f"{self._KEY}.afterimages") is None:
            self._state.set(f"{self._KEY}.afterimages", [])

    def _get_afterimages(self) -> List[dict]:
        return list(self._state.get(f"{self._KEY}.afterimages") or [])

    def _save_afterimages(self, images: List[dict]) -> None:
        self._state.set(f"{self._KEY}.afterimages", images)

    def record_emotion(self, valence: float, intensity: float, context: str) -> Optional[dict]:
        """Create afterimage if intensity > 7 or |valence| > 2."""
        if intensity <= 7 and abs(valence) <= 2:
            return None

        now_ms = int(time.time() * 1000)
        emotion = _valence_to_emotion(valence, intensity)
        afterimage = {
            "emotion": emotion,
            "valence": valence,
            "intensity": intensity,
            "context": context,
            "created_at": now_ms,
            "half_life_ms": DEFAULT_HALF_LIFE_MS,
            "last_milestone": 100,
        }
        images = self._get_afterimages()
        images.append(afterimage)
        self._save_afterimages(images)
        return afterimage

    def get_current_afterimages(self) -> List[dict]:
        """Return active afterimages with current decayed intensity."""
        images = self._get_afterimages()
        now_ms = int(time.time() * 1000)
        active = []

        for ai in images:
            current = _decayed_intensity(ai, now_ms)
            if current < DECAY_THRESHOLD:
                continue
            result = dict(ai)
            result["current_intensity"] = round(current, 2)
            active.append(result)

        # Save pruned list
        self._save_afterimages(active)
        return active

    def get_emotional_color(self) -> Optional[dict]:
        """Return dominant afterimage or None."""
        active = self.get_current_afterimages()
        if not active:
            return None
        return max(active, key=lambda a: a["current_intensity"])

    def detect_contagion(self, message_text: str, sender: str) -> Optional[dict]:
        """Detect emotional tone from messages, create resonance at 0.5x."""
        text_lower = message_text.lower()
        detected = None
        for emotion, (keywords, valence, intensity) in _CONTAGION_MAP.items():
            for kw in keywords:
                if kw in text_lower:
                    detected = (emotion, valence, intensity)
                    break
            if detected:
                break

        if not detected:
            return None

        emotion, valence, intensity = detected
        res_valence = valence * 0.5
        res_intensity = intensity * 0.5

        context = f"emotional contagion from {sender}: {emotion}"
        afterimage = self.record_emotion(res_valence, res_intensity, context)

        return {
            "detected_emotion": emotion,
            "sender": sender,
            "resonance_valence": res_valence,
            "resonance_intensity": res_intensity,
            "afterimage_created": afterimage is not None,
        }

    def tick(self) -> None:
        """Prune expired afterimages."""
        self.get_current_afterimages()

    def status(self) -> dict:
        active = self.get_current_afterimages()
        dominant = self.get_emotional_color()
        return {
            "active_afterimages": len(active),
            "dominant": dominant.get("emotion") if dominant else None,
            "afterimages": active[:5],
        }
