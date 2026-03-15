"""VAGUS — Silence Detector. State via StateEngine under ``vagus.*``."""
from __future__ import annotations
import time
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

SOURCES = ["josh", "markets", "agents", "crons"]

class Vagus:
    _KEY = "vagus"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.timestamps") is None:
            self._state.set(f"{self._KEY}.timestamps", {})

    def update_timestamp(self, source: str) -> None:
        ts = self._state.get(f"{self._KEY}.timestamps") or {}
        ts[source] = int(time.time() * 1000)
        self._state.set(f"{self._KEY}.timestamps", ts)

    def _is_sleep(self) -> bool:
        h = datetime.now().hour
        return h >= 23 or h < 8

    def check_silence(self) -> List[dict]:
        ts_map = self._state.get(f"{self._KEY}.timestamps") or {}
        now_ms = int(time.time() * 1000)
        silences = []
        for source in SOURCES:
            last = ts_map.get(source)
            if last is None: continue
            hours = (now_ms - last) / 3_600_000
            sig = self._significance(source, hours)
            if sig > 0:
                silences.append({"source": source, "duration_hours": round(hours, 2), "significance": round(sig, 3)})
        return silences

    def _significance(self, source: str, hours: float) -> float:
        if source == "josh":
            if self._is_sleep(): return 0.0
            if hours < 2: return 0.0
            return min((hours - 2) / 6.0, 1.0)
        if source == "markets":
            return 0.8 if hours >= 1 else 0.0
        if source == "crons":
            return 0.5 if hours >= 2 else 0.0
        if source == "agents":
            return min(hours / 8.0, 0.7) if hours >= 1 else 0.0
        return 0.0

    def josh_silence_hours(self) -> float:
        ts_map = self._state.get(f"{self._KEY}.timestamps") or {}
        last = ts_map.get("josh")
        if last is None: return 0.0
        return (int(time.time() * 1000) - last) / 3_600_000

    def tick(self) -> None:
        pass  # check_silence is called on-demand

    def status(self) -> dict:
        silences = self.check_silence()
        return {"silences": silences, "josh_silence_hours": round(self.josh_silence_hours(), 2)}
