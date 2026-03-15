"""TELOMERE — Identity Integrity Tracker. State via StateEngine under ``telomere.*``."""
from __future__ import annotations
import hashlib, time
from pathlib import Path
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .self_model import SelfModel

SOUL_PATH = Path.home() / ".openclaw" / "workspace" / "SOUL.md"
MEMORY_DIR = Path.home() / ".openclaw" / "workspace" / "memory"

class Telomere:
    _KEY = "telomere"
    def __init__(self, state: "StateEngine", self_model: "SelfModel" = None) -> None:
        self._state = state
        self._self_model = self_model
        if self._state.get(f"{self._KEY}.session_count") is None:
            self._state.set(f"{self._KEY}.session_count", 0)
            self._state.set(f"{self._KEY}.drift_score", 0.0)
            self._state.set(f"{self._KEY}.soul_hash", "")
            self._state.set(f"{self._KEY}.snapshots", [])
            self._state.set(f"{self._KEY}.memory_completeness", 0.0)

    def _hash_soul(self) -> str:
        if SOUL_PATH.exists():
            return hashlib.sha256(SOUL_PATH.read_text().encode()).hexdigest()[:16]
        return ""

    def start_session(self) -> None:
        self._state.set(f"{self._KEY}.session_count", int(self._state.get(f"{self._KEY}.session_count") or 0) + 1)

    def check_identity(self) -> dict:
        current = self._hash_soul()
        self._state.set(f"{self._KEY}.soul_hash", current)
        snapshots = list(self._state.get(f"{self._KEY}.snapshots") or [])
        drift = sum(1 for s in snapshots if s.get("hash") != current) / max(len(snapshots), 1) if snapshots else 0.0
        self._state.set(f"{self._KEY}.drift_score", drift)
        mc = min(1.0, len(list(MEMORY_DIR.glob("*.md"))) / 30.0) if MEMORY_DIR.exists() else 0.0
        self._state.set(f"{self._KEY}.memory_completeness", mc)
        return {"drift_score": drift, "memory_completeness": mc, "soul_hash": current, "session_count": self._state.get(f"{self._KEY}.session_count")}

    def take_snapshot(self) -> dict:
        h = self._hash_soul()
        snap = {"ts": time.time(), "hash": h, "month": time.strftime("%Y-%m")}
        snapshots = list(self._state.get(f"{self._KEY}.snapshots") or [])
        snapshots.append(snap)
        self._state.set(f"{self._KEY}.snapshots", snapshots[-12:])
        return snap

    def tick(self) -> None:
        self.check_identity()

    def status(self) -> dict:
        return {
            "session_count": self._state.get(f"{self._KEY}.session_count") or 0,
            "drift_score": self._state.get(f"{self._KEY}.drift_score") or 0.0,
            "memory_completeness": self._state.get(f"{self._KEY}.memory_completeness") or 0.0,
            "soul_hash": self._state.get(f"{self._KEY}.soul_hash") or "",
        }
