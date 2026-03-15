"""BUFFER — Working Memory Capture. State via StateEngine under ``buffer.*``."""
from __future__ import annotations
import time, uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

class Buffer:
    _KEY = "buffer"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.captured_at") is None:
            self._state.set(f"{self._KEY}.captured_at", 0)
            self._state.set(f"{self._KEY}.decisions", [])
            self._state.set(f"{self._KEY}.action_items", [])
            self._state.set(f"{self._KEY}.emotional_state", {"valence": 0.0, "intensity": 0.0, "context": ""})
            self._state.set(f"{self._KEY}.open_threads", [])
            self._state.set(f"{self._KEY}.topic", "")
            self._state.set(f"{self._KEY}.key_context", "")

    def capture(self, summary: str, decisions: list, action_items: list,
                emotional_state: dict, open_threads: list, topic: str = "") -> dict:
        self._state.set(f"{self._KEY}.captured_at", int(time.time() * 1000))
        self._state.set(f"{self._KEY}.key_context", summary)
        self._state.set(f"{self._KEY}.decisions", decisions)
        self._state.set(f"{self._KEY}.action_items", action_items)
        self._state.set(f"{self._KEY}.emotional_state", emotional_state)
        self._state.set(f"{self._KEY}.open_threads", open_threads)
        self._state.set(f"{self._KEY}.topic", topic)
        return self.get_buffer()

    def get_buffer(self) -> dict:
        return {
            "captured_at": self._state.get(f"{self._KEY}.captured_at"),
            "topic": self._state.get(f"{self._KEY}.topic"),
            "decisions": self._state.get(f"{self._KEY}.decisions"),
            "action_items": self._state.get(f"{self._KEY}.action_items"),
            "emotional_state": self._state.get(f"{self._KEY}.emotional_state"),
            "open_threads": self._state.get(f"{self._KEY}.open_threads"),
            "key_context": self._state.get(f"{self._KEY}.key_context"),
        }

    def get_compact_summary(self, max_tokens: int = 500) -> str:
        buf = self.get_buffer()
        if not buf.get("captured_at"): return ""
        parts = []
        if buf.get("topic"): parts.append(f"Topic: {buf['topic']}")
        if buf.get("key_context"): parts.append(f"Context: {buf['key_context']}")
        if buf.get("decisions"): parts.append("Decisions: " + "; ".join(buf["decisions"]))
        if buf.get("action_items"): parts.append("Actions: " + "; ".join(buf["action_items"]))
        if buf.get("open_threads"): parts.append("Threads: " + "; ".join(buf["open_threads"]))
        return "\n".join(parts)[:max_tokens * 4]

    def tick(self) -> None:
        pass

    def status(self) -> dict:
        buf = self.get_buffer()
        return {
            "has_capture": bool(buf.get("captured_at")),
            "topic": buf.get("topic", ""),
            "decisions": len(buf.get("decisions") or []),
            "action_items": len(buf.get("action_items") or []),
            "open_threads": len(buf.get("open_threads") or []),
        }
