"""SPINE — System Health Monitor. State via StateEngine under ``spine.*``."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Dict, List
if TYPE_CHECKING:
    from .state_engine import StateEngine

LEVELS = ["green", "yellow", "orange", "red"]
LEVEL_ORDER = {l: i for i, l in enumerate(LEVELS)}

class Spine:
    _KEY = "spine"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.status") is None:
            self._state.set(f"{self._KEY}.status", "green")
            self._state.set(f"{self._KEY}.alerts", [])
            self._state.set(f"{self._KEY}.paused_crons", [])

    def check_token_usage(self, total: int, budget: int = 100000) -> dict:
        pct = total / max(budget, 1)
        level = "red" if pct > 0.95 else "orange" if pct > 0.85 else "yellow" if pct > 0.70 else "green"
        if level != "green":
            self._upsert_alert("token_usage", level, {"pct": round(pct, 3)})
        else:
            self._remove_alert("token_usage")
        return {"level": level, "pct": round(pct, 3)}

    def check_context_size(self, current: int, maximum: int) -> dict:
        pct = current / max(maximum, 1)
        level = "red" if pct > 0.95 else "orange" if pct > 0.90 else "yellow" if pct > 0.80 else "green"
        if level != "green":
            self._upsert_alert("context_size", level, {"pct": round(pct, 3)})
        else:
            self._remove_alert("context_size")
        return {"level": level, "pct": round(pct, 4)}

    def check_provider_health(self, provider: str, latency_ms: int, success: bool) -> dict:
        level = "green"
        if not success: level = "red"
        elif latency_ms > 10000: level = "red"
        elif latency_ms > 5000: level = "orange"
        if level != "green":
            self._upsert_alert(f"provider:{provider}", level, {"latency_ms": latency_ms})
        else:
            self._remove_alert(f"provider:{provider}")
        return {"level": level, "provider": provider}

    def _upsert_alert(self, source: str, level: str, data: dict = None) -> None:
        alerts = list(self._state.get(f"{self._KEY}.alerts") or [])
        alerts = [a for a in alerts if a.get("source") != source]
        alerts.append({"source": source, "level": level, "ts": int(time.time()*1000), **(data or {})})
        self._state.set(f"{self._KEY}.alerts", alerts)
        self._update_overall()

    def _remove_alert(self, source: str) -> None:
        alerts = list(self._state.get(f"{self._KEY}.alerts") or [])
        self._state.set(f"{self._KEY}.alerts", [a for a in alerts if a.get("source") != source])
        self._update_overall()

    def _update_overall(self) -> None:
        alerts = list(self._state.get(f"{self._KEY}.alerts") or [])
        if not alerts:
            self._state.set(f"{self._KEY}.status", "green")
            self._state.set(f"{self._KEY}.paused_crons", [])
            return
        worst = max(LEVEL_ORDER.get(a.get("level", "green"), 0) for a in alerts)
        self._state.set(f"{self._KEY}.status", LEVELS[worst])

    def tick(self) -> None:
        self._update_overall()

    def status(self) -> dict:
        return {
            "status": self._state.get(f"{self._KEY}.status") or "green",
            "alerts": list(self._state.get(f"{self._KEY}.alerts") or []),
            "paused_crons": list(self._state.get(f"{self._KEY}.paused_crons") or []),
        }
