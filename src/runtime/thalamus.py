"""
THALAMUS — Intra-Agent Broadcast Bus
=======================================
Ported from v1 pulse.src.thalamus into v2 HypostasRuntime.

Lightweight in-memory event bus for intra-agent communication.
Complements AURA (inter-agent) — THALAMUS is within one agent.

Append-only ring buffer. Every module can write, every module can read.
All state persisted via StateEngine under ``thalamus.*`` dot-paths.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine

MAX_ENTRIES = 500


class Thalamus:
    """Central nervous system bus — intra-agent broadcast layer."""

    _KEY = "thalamus"

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.entries") is None:
            self._state.set(f"{self._KEY}.entries", [])
            self._state.set(f"{self._KEY}.total_appended", 0)
            self._state.set(f"{self._KEY}.last_append_ts", 0)

    def append(self, entry: dict) -> dict:
        """Append an entry to the broadcast bus. Adds timestamp if missing.

        Entry format: {"source": str, "type": str, "salience": float, "data": dict}
        """
        if "ts" not in entry:
            entry["ts"] = int(time.time() * 1000)

        entries = list(self._state.get(f"{self._KEY}.entries") or [])
        entries.append(entry)
        # Ring buffer — keep last MAX_ENTRIES
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        self._state.set(f"{self._KEY}.entries", entries)

        total = int(self._state.get(f"{self._KEY}.total_appended") or 0) + 1
        self._state.set(f"{self._KEY}.total_appended", total)
        self._state.set(f"{self._KEY}.last_append_ts", entry["ts"])
        return entry

    def read_recent(self, n: int = 10) -> List[dict]:
        """Return last N entries."""
        entries = list(self._state.get(f"{self._KEY}.entries") or [])
        return entries[-n:]

    def read_since(self, epoch_ms: int) -> List[dict]:
        """Return all entries since given timestamp (ms)."""
        entries = list(self._state.get(f"{self._KEY}.entries") or [])
        return [e for e in entries if e.get("ts", 0) >= epoch_ms]

    def read_by_source(self, source: str, n: int = 10) -> List[dict]:
        """Return last N entries from a specific source."""
        entries = list(self._state.get(f"{self._KEY}.entries") or [])
        filtered = [e for e in entries if e.get("source") == source]
        return filtered[-n:]

    def read_by_type(self, entry_type: str, n: int = 10) -> List[dict]:
        """Return last N entries of a specific type."""
        entries = list(self._state.get(f"{self._KEY}.entries") or [])
        filtered = [e for e in entries if e.get("type") == entry_type]
        return filtered[-n:]

    def tick(self) -> None:
        """No-op for now — bus doesn't need periodic maintenance."""
        pass

    def status(self) -> dict:
        entries = list(self._state.get(f"{self._KEY}.entries") or [])
        total = int(self._state.get(f"{self._KEY}.total_appended") or 0)

        # Count by source for summary
        sources: Dict[str, int] = {}
        for e in entries[-50:]:
            src = e.get("source", "unknown")
            sources[src] = sources.get(src, 0) + 1

        return {
            "entry_count": len(entries),
            "total_appended": total,
            "last_append_ts": self._state.get(f"{self._KEY}.last_append_ts"),
            "recent_sources": sources,
        }
