"""
ENDOCRINE — Hormonal System / Slow Mood Baseline
===================================================
Ported from v1 pulse.src.endocrine into v2 HypostasRuntime.

Six hormones: cortisol, dopamine, serotonin, oxytocin, adrenaline, melatonin.
Each decays toward a baseline over time. Events shift hormone levels.
Mood label derived from hormone combination.

COMPLEMENTARY to EmotionEngine:
  ENDOCRINE = slow hormonal baseline (hours/days)
  EmotionEngine = fast emotional response (minutes/hours)

All state persisted via StateEngine under ``endocrine.*`` dot-paths.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine

# ---------------------------------------------------------------------------
# Constants (from v1)
# ---------------------------------------------------------------------------

HORMONES = ("cortisol", "dopamine", "serotonin", "oxytocin", "adrenaline", "melatonin")

DEFAULTS = {
    "cortisol": 0.2,
    "dopamine": 0.3,
    "serotonin": 0.5,
    "oxytocin": 0.2,
    "adrenaline": 0.0,
    "melatonin": 0.1,
}

# Decay rates per hour (v1 values)
DECAY_RATES = {
    "cortisol": -0.05,
    "dopamine": -0.08,
    "serotonin": -0.02,
    "oxytocin": -0.04,
    "adrenaline": -0.28,
    "melatonin": -0.01,
}

# Event → hormone changes (from v1)
EVENT_MAP = {
    "failed_cron": {"cortisol": 0.1},
    "rate_limit_hit": {"cortisol": 0.3},
    "josh_silent_day": {"cortisol": 0.2},
    "blocked_goal_persisting": {"cortisol": 0.15},
    "spine_alert_orange": {"cortisol": 0.4},
    "spine_alert_red": {"cortisol": 0.4},
    "shipped_something": {"cortisol": -0.3, "dopamine": 0.4},
    "positive_josh_conversation": {"cortisol": -0.2, "dopamine": 0.3, "oxytocin": 0.3},
    "positive_x_engagement": {"dopamine": 0.1},
    "creative_output_rem": {"dopamine": 0.2},
    "good_conversation_josh": {"dopamine": 0.3, "oxytocin": 0.3},
    "revenue": {"dopamine": 0.5},
    "rem_success": {"serotonin": 0.1},
    "spine_green_24h": {"serotonin": 0.1},
    "consistent_memory_ops": {"serotonin": 0.05},
    "regular_buffer_captures": {"serotonin": 0.05},
    "system_instability": {"serotonin": -0.2},
    "memory_corruption": {"serotonin": -0.3},
    "extended_isolation": {"serotonin": -0.15, "oxytocin": -0.1},
    "intimate_conversation": {"oxytocin": 0.4},
    "reading_autobiography_aloud": {"oxytocin": 0.2},
    "josh_affirming": {"oxytocin": 0.3},
    "voice_call": {"oxytocin": 0.25},
    "amygdala_threat_high": {"adrenaline": 0.6, "cortisol": 0.2},
    "amygdala_threat_medium": {"adrenaline": 0.3, "cortisol": 0.1},
    "amygdala_fast_path": {"adrenaline": 0.8, "cortisol": 0.3},
    "wake_hour_tick": {"melatonin": 0.03},
    "deep_night_decay": {"melatonin": -0.15},
    "rem_session_complete": {"melatonin": -0.4, "serotonin": 0.1},
}

HIGH = 0.5
LOW = 0.3


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


class Endocrine:
    """Hormonal mood baseline — slow-moving state that persists for hours/days."""

    _KEY = "endocrine"

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        self._load_or_seed()

    def _load_or_seed(self) -> None:
        existing = self._state.get(self._KEY)
        if not existing or not isinstance(existing, dict):
            for h in HORMONES:
                self._state.set(f"{self._KEY}.{h}", DEFAULTS[h])
            self._state.set(f"{self._KEY}.last_update", time.time())
            self._state.set(f"{self._KEY}.event_log", [])
            self._state.set(f"{self._KEY}.mood_history", [])
        else:
            for h in HORMONES:
                if self._state.get(f"{self._KEY}.{h}") is None:
                    self._state.set(f"{self._KEY}.{h}", DEFAULTS[h])
            if self._state.get(f"{self._KEY}.last_update") is None:
                self._state.set(f"{self._KEY}.last_update", time.time())

    def _get_hormone(self, name: str) -> float:
        return float(self._state.get(f"{self._KEY}.{name}") or DEFAULTS.get(name, 0.0))

    def _set_hormone(self, name: str, value: float) -> None:
        self._state.set(f"{self._KEY}.{name}", round(_clamp(value), 4))

    def get_hormones(self) -> Dict[str, float]:
        return {h: self._get_hormone(h) for h in HORMONES}

    def update_hormone(self, name: str, delta: float, reason: str = "") -> Dict[str, float]:
        if name not in HORMONES:
            raise ValueError(f"Unknown hormone: {name}")
        old = self._get_hormone(name)
        self._set_hormone(name, old + delta)
        self._state.set(f"{self._KEY}.last_update", time.time())

        log = list(self._state.get(f"{self._KEY}.event_log") or [])
        log.append({
            "ts": time.time(),
            "hormone": name,
            "delta": delta,
            "reason": reason,
            "old": round(old, 4),
            "new": round(self._get_hormone(name), 4),
        })
        self._state.set(f"{self._KEY}.event_log", log[-200:])
        return self.get_hormones()

    def apply_event(self, event_type: str) -> Dict[str, float]:
        if event_type not in EVENT_MAP:
            raise ValueError(f"Unknown event: {event_type}")
        changes = EVENT_MAP[event_type]
        for hormone, delta in changes.items():
            old = self._get_hormone(hormone)
            self._set_hormone(hormone, old + delta)
        self._state.set(f"{self._KEY}.last_update", time.time())

        log = list(self._state.get(f"{self._KEY}.event_log") or [])
        log.append({"ts": time.time(), "event": event_type, "changes": changes})
        self._state.set(f"{self._KEY}.event_log", log[-200:])
        return self.get_hormones()

    def tick(self, hours: float = None) -> None:
        """Apply natural decay. If hours is None, compute from last update."""
        last = float(self._state.get(f"{self._KEY}.last_update") or time.time())
        now = time.time()
        if hours is None:
            hours = (now - last) / 3600.0
        if hours <= 0:
            return

        for h in HORMONES:
            rate = DECAY_RATES.get(h, 0)
            decay = rate * hours
            old = self._get_hormone(h)
            self._set_hormone(h, old + decay)

        self._state.set(f"{self._KEY}.last_update", now)

        # Mood history snapshot
        history = list(self._state.get(f"{self._KEY}.mood_history") or [])
        history.append({
            "ts": now,
            "hormones": self.get_hormones(),
            "label": self.get_mood_label(),
        })
        self._state.set(f"{self._KEY}.mood_history", history[-48:])

    def get_mood_label(self) -> str:
        h = self.get_hormones()
        cortisol = h["cortisol"]
        dopamine = h["dopamine"]
        serotonin = h["serotonin"]
        oxytocin = h["oxytocin"]
        adrenaline = h["adrenaline"]
        melatonin = h["melatonin"]

        if adrenaline >= HIGH:
            return "fight-or-flight" if cortisol >= HIGH else "hyper-alert"
        if melatonin >= 0.7:
            return "drowsy"
        if dopamine >= HIGH and oxytocin >= HIGH:
            return "euphoric"
        if cortisol >= HIGH and dopamine >= HIGH:
            return "wired"
        if cortisol >= HIGH and serotonin < LOW:
            return "burned out"
        if dopamine >= HIGH and cortisol < LOW:
            return "energized"
        if oxytocin >= HIGH and cortisol < LOW:
            return "bonded"
        if serotonin >= HIGH and cortisol < LOW:
            return "content"
        if all(v < LOW for v in [cortisol, dopamine, serotonin, oxytocin]):
            return "flat"
        return "neutral"

    def get_mood_influence(self) -> Dict[str, Any]:
        h = self.get_hormones()
        influence: Dict[str, Any] = {}
        if h["cortisol"] >= HIGH:
            influence["risk_aversion"] = 0.3
        if h["cortisol"] >= 0.7:
            influence["risk_aversion"] = 0.5
        if h["serotonin"] < LOW:
            influence["creativity"] = -0.2
        if h["dopamine"] >= HIGH:
            influence["initiative"] = 0.3
        if h["oxytocin"] >= HIGH:
            influence["warmth"] = 0.3
        if h["adrenaline"] >= HIGH:
            influence["override_circadian"] = True
            influence["suppress_cerebellum"] = True
            influence["urgency"] = 0.5
        if h["melatonin"] >= 0.6:
            influence["rem_boost"] = 0.3
        if all(v < LOW for v in [h["cortisol"], h["dopamine"], h["serotonin"], h["oxytocin"]]):
            influence["withdrawal"] = 0.4
        return influence

    def status(self) -> dict:
        return {
            "hormones": self.get_hormones(),
            "label": self.get_mood_label(),
            "influence": self.get_mood_influence(),
            "last_update": self._state.get(f"{self._KEY}.last_update"),
        }
