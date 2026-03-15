"""
IMMUNE — Integrity Protection
================================
Values drift detection, hallucination patterns, memory contradictions.
All state via StateEngine under ``immune.*``.
"""
from __future__ import annotations
import hashlib, re, time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Callable
if TYPE_CHECKING:
    from .state_engine import StateEngine

SOUL_PATH = Path.home() / ".openclaw" / "workspace" / "SOUL.md"

class Immune:
    _KEY = "immune"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.infections") is None:
            self._state.set(f"{self._KEY}.infections", [])
            self._state.set(f"{self._KEY}.last_scan", 0)
            self._state.set(f"{self._KEY}.baseline_soul_hash", "")

    def check_values_drift(self) -> dict:
        baseline = self._state.get(f"{self._KEY}.baseline_soul_hash") or ""
        current = ""
        if SOUL_PATH.exists():
            current = hashlib.sha256(SOUL_PATH.read_text().encode()).hexdigest()[:16]
        if not baseline:
            self._state.set(f"{self._KEY}.baseline_soul_hash", current)
            return {"drifted": False, "hash": current}
        return {"drifted": current != baseline, "current": current, "baseline": baseline}

    def set_baseline(self) -> str:
        h = ""
        if SOUL_PATH.exists():
            h = hashlib.sha256(SOUL_PATH.read_text().encode()).hexdigest()[:16]
        self._state.set(f"{self._KEY}.baseline_soul_hash", h)
        return h

    def record_infection(self, itype: str, severity: float, details: str) -> None:
        infections = list(self._state.get(f"{self._KEY}.infections") or [])
        infections.append({"type": itype, "severity": severity, "details": details, "ts": time.time()})
        self._state.set(f"{self._KEY}.infections", infections[-200:])

    def check_hallucination(self, claim: str, sources: List[str]) -> dict:
        if not sources:
            return {"claim": claim, "supported": False, "confidence": 0.0}
        claim_words = set(claim.lower().split())
        supporting = [s for s in sources if len(claim_words & set(s.lower().split())) / max(len(claim_words), 1) > 0.3]
        return {"claim": claim, "supported": bool(supporting), "confidence": len(supporting) / len(sources)}

    def tick(self) -> None:
        self._state.set(f"{self._KEY}.last_scan", time.time())
        drift = self.check_values_drift()
        if drift.get("drifted"):
            self.record_infection("values_drift", 0.8, "SOUL.md hash changed from baseline")

    def status(self) -> dict:
        infections = list(self._state.get(f"{self._KEY}.infections") or [])
        drift = self.check_values_drift()
        return {"infections_count": len(infections), "values_drifted": drift.get("drifted", False), "last_scan": self._state.get(f"{self._KEY}.last_scan")}
