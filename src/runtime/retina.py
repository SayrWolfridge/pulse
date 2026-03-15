"""RETINA — Attention Filter. State via StateEngine under ``retina.*``."""
from __future__ import annotations
import os, time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

OWNER_PHONE = os.environ.get("PULSE_OWNER_PHONE", "+15555550100")

class Retina:
    _KEY = "retina"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        self._threshold = 0.3
        self._focus_mode = False
        self._spine_level = "green"
        if self._state.get(f"{self._KEY}.signals_processed") is None:
            self._state.set(f"{self._KEY}.signals_processed", 0)
            self._state.set(f"{self._KEY}.signals_filtered", 0)

    def score(self, signal: dict) -> dict:
        priority = 0.0; category = "unknown"
        sender = str(signal.get("sender", signal.get("from", "")))
        if OWNER_PHONE in sender:
            priority, category = 1.0, "owner_direct"
        elif signal.get("source_type") == "cron" and signal.get("anomaly"):
            priority, category = 0.7, "cron_anomaly"
        elif signal.get("health_level") in ("yellow", "orange", "red"):
            priority, category = 0.8, "system_health"
        elif signal.get("source_type") == "cron":
            priority, category = 0.1, "cron_routine"
        elif signal.get("source_type") == "heartbeat":
            priority, category = 0.05, "heartbeat"

        threshold = 0.8 if self._focus_mode and category != "owner_direct" else (0.6 if self._spine_level in ("orange", "red") else self._threshold)
        should_process = priority >= threshold
        key = f"{self._KEY}.signals_processed" if should_process else f"{self._KEY}.signals_filtered"
        self._state.set(key, int(self._state.get(key) or 0) + 1)
        return {"priority": priority, "category": category, "should_process": should_process}

    def set_focus_mode(self, active: bool) -> None:
        self._focus_mode = active

    def set_spine_level(self, level: str) -> None:
        self._spine_level = level

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        return {
            "signals_processed": self._state.get(f"{self._KEY}.signals_processed") or 0,
            "signals_filtered": self._state.get(f"{self._KEY}.signals_filtered") or 0,
            "focus_mode": self._focus_mode,
            "threshold": self._threshold,
        }
