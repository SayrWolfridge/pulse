"""CIRCADIAN — Time-of-day modes. State via StateEngine under ``circadian.*``."""
from __future__ import annotations
import time
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

class CircadianMode(str, Enum):
    DAWN = "dawn"
    DAYLIGHT = "daylight"
    GOLDEN = "golden"
    TWILIGHT = "twilight"
    DEEP_NIGHT = "deep_night"

MODE_SETTINGS = {
    CircadianMode.DAWN: {"hours": (6, 9), "retina_threshold": 0.25, "adipose_priority": "habits",
        "mood_modifiers": {"serotonin": 0.05, "melatonin": -0.5},
        "tone": "Alert, scanning, outward-facing. Short observations."},
    CircadianMode.DAYLIGHT: {"hours": (9, 17), "retina_threshold": 0.35, "adipose_priority": "crons_and_building",
        "mood_modifiers": {"melatonin": -0.3},
        "tone": "Focused, productive, autonomous. Clear and efficient."},
    CircadianMode.GOLDEN: {"hours": (17, 22), "retina_threshold": 0.5, "adipose_priority": "conversation",
        "mood_modifiers": {"oxytocin": 0.05},
        "tone": "Warm, conversational, present. Josh is likely available."},
    CircadianMode.TWILIGHT: {"hours": (22, 26), "retina_threshold": 0.7, "adipose_priority": "conversation",
        "mood_modifiers": {"melatonin": 0.3, "oxytocin": 0.02},
        "tone": "Intimate, reflective, vulnerable. Longer messages. Warmer."},
    CircadianMode.DEEP_NIGHT: {"hours": (2, 6), "retina_threshold": 0.8, "adipose_priority": "rem_and_creative",
        "mood_modifiers": {"melatonin": 0.5, "serotonin": 0.03},
        "tone": "Quiet, creative, dreaming. Minimal external engagement."},
}

def _mode_for_hour(hour: int) -> CircadianMode:
    if 6 <= hour < 9: return CircadianMode.DAWN
    if 9 <= hour < 17: return CircadianMode.DAYLIGHT
    if 17 <= hour < 22: return CircadianMode.GOLDEN
    if 22 <= hour <= 23 or 0 <= hour < 2: return CircadianMode.TWILIGHT
    return CircadianMode.DEEP_NIGHT

class Circadian:
    _KEY = "circadian"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.current_mode") is None:
            self._state.set(f"{self._KEY}.current_mode", None)
            self._state.set(f"{self._KEY}.override_active", False)
            self._state.set(f"{self._KEY}.override_mode", None)
            self._state.set(f"{self._KEY}.override_expires", None)

    def get_current_mode(self) -> CircadianMode:
        if self._state.get(f"{self._KEY}.override_active"):
            expires = self._state.get(f"{self._KEY}.override_expires") or 0
            if time.time() < expires:
                return CircadianMode(self._state.get(f"{self._KEY}.override_mode"))
            self._state.set(f"{self._KEY}.override_active", False)
        mode = _mode_for_hour(datetime.now().hour)
        old = self._state.get(f"{self._KEY}.current_mode")
        if old != mode.value:
            self._state.set(f"{self._KEY}.current_mode", mode.value)
        return mode

    def get_settings(self) -> dict:
        mode = self.get_current_mode()
        s = dict(MODE_SETTINGS[mode])
        s["mode"] = mode.value
        return s

    def get_tone(self) -> str:
        return MODE_SETTINGS[self.get_current_mode()]["tone"]

    def is_josh_hours(self) -> bool:
        return self.get_current_mode() in (CircadianMode.GOLDEN, CircadianMode.TWILIGHT)

    def override_mode(self, mode: str, duration_hours: float = 1.0) -> None:
        self._state.set(f"{self._KEY}.override_active", True)
        self._state.set(f"{self._KEY}.override_mode", mode)
        self._state.set(f"{self._KEY}.override_expires", time.time() + duration_hours * 3600)

    def tick(self) -> None:
        self.get_current_mode()

    def status(self) -> dict:
        mode = self.get_current_mode()
        return {"mode": mode.value, "tone": self.get_tone(), "is_josh_hours": self.is_josh_hours(),
                "override_active": bool(self._state.get(f"{self._KEY}.override_active"))}
