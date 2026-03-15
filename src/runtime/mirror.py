"""MIRROR — Bidirectional Modeling. State via StateEngine under ``mirror.*``."""
from __future__ import annotations
import hashlib, time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

WORKSPACE = Path.home() / ".openclaw" / "workspace"
JOSH_MODEL = WORKSPACE / "memory" / "self" / "josh_model.md"
IRIS_MODEL = WORKSPACE / "memory" / "self" / "iris_model.md"

class Mirror:
    _KEY = "mirror"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.iris_model_hash") is None:
            self._state.set(f"{self._KEY}.iris_model_hash", None)
            self._state.set(f"{self._KEY}.change_log", [])

    def _parse_md(self, path: Path) -> dict:
        if not path.exists(): return {}
        sections = {}; cur = None; lines = []
        for line in path.read_text().split("\n"):
            if line.strip().startswith("## "):
                if cur: sections[cur] = "\n".join(lines).strip()
                cur = line.strip()[3:].strip(); lines = []
            elif cur: lines.append(line)
        if cur: sections[cur] = "\n".join(lines).strip()
        return sections

    def get_josh_model(self) -> dict:
        return self._parse_md(JOSH_MODEL)

    def get_iris_model(self) -> dict:
        return self._parse_md(IRIS_MODEL)

    def check_updates(self) -> List[str]:
        if not IRIS_MODEL.exists(): return []
        current = hashlib.md5(IRIS_MODEL.read_bytes()).hexdigest()
        prev = self._state.get(f"{self._KEY}.iris_model_hash")
        if prev == current: return []
        self._state.set(f"{self._KEY}.iris_model_hash", current)
        sections = self.get_iris_model()
        changes = [f"Section '{s}' updated" for s in sections if sections[s].strip()]
        if changes:
            log = list(self._state.get(f"{self._KEY}.change_log") or [])
            log.append({"ts": int(time.time()*1000), "changes": changes})
            self._state.set(f"{self._KEY}.change_log", log[-50:])
        return changes

    def get_alignment_report(self) -> dict:
        return {"josh_view": self.get_iris_model(), "has_feedback": bool(self.get_iris_model())}

    def tick(self) -> None:
        self.check_updates()

    def status(self) -> dict:
        return {"josh_model_populated": bool(self.get_josh_model()), "iris_model_populated": bool(self.get_iris_model())}
