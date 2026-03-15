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

    def update_field(self, field: str, value: Any) -> dict:
        """Update a single field without full capture."""
        valid = {"decisions", "action_items", "emotional_state", "open_threads", "topic", "key_context"}
        if field not in valid:
            raise KeyError(f"Unknown buffer field: {field}")
        self._state.set(f"{self._KEY}.{field}", value)
        self._state.set(f"{self._KEY}.captured_at", int(time.time() * 1000))
        return self.get_buffer()

    def rotate(self) -> Optional[str]:
        """Archive current buffer and start fresh. Returns archive key or None."""
        buf = self.get_buffer()
        if not buf.get("captured_at"):
            return None
        archive_key = f"{self._KEY}.archive.{int(time.time())}"
        self._state.set(archive_key, buf)
        # Reset
        self._state.set(f"{self._KEY}.captured_at", 0)
        self._state.set(f"{self._KEY}.decisions", [])
        self._state.set(f"{self._KEY}.action_items", [])
        self._state.set(f"{self._KEY}.emotional_state", {"valence": 0.0, "intensity": 0.0, "context": ""})
        self._state.set(f"{self._KEY}.open_threads", [])
        self._state.set(f"{self._KEY}.topic", "")
        self._state.set(f"{self._KEY}.key_context", "")
        return archive_key

    def auto_capture(self, messages: List[Dict]) -> dict:
        """Given recent message dicts, automatically extract working memory.

        Each message should have at least: {"role": str, "content": str}
        """
        decisions: List[str] = []
        action_items: List[str] = []
        open_threads: List[str] = []
        valence = 0.0
        intensity = 0.0

        decision_markers = ["decided", "decision", "let's go with", "agreed", "we'll do"]
        action_markers = ["todo", "need to", "should do", "next step", "let me", "i'll"]
        question_markers = ["?", "what about", "how do we", "should we"]

        for msg in messages:
            content = str(msg.get("content", "")).lower()
            for marker in decision_markers:
                if marker in content:
                    for sentence in msg.get("content", "").replace("\n", ". ").split(". "):
                        if marker in sentence.lower():
                            decisions.append(sentence.strip().rstrip("."))
                            break
                    break
            for marker in action_markers:
                if marker in content:
                    for sentence in msg.get("content", "").replace("\n", ". ").split(". "):
                        if marker in sentence.lower():
                            action_items.append(sentence.strip().rstrip("."))
                            break
                    break
            for marker in question_markers:
                if marker in content:
                    for sentence in msg.get("content", "").replace("\n", ". ").split(". "):
                        if marker in sentence.lower() or "?" in sentence:
                            open_threads.append(sentence.strip().rstrip("."))
                            break
                    break

        decisions = list(dict.fromkeys(decisions))[:10]
        action_items = list(dict.fromkeys(action_items))[:10]
        open_threads = list(dict.fromkeys(open_threads))[:10]
        summary = " ".join(str(m.get("content", ""))[:200] for m in messages[-5:])[:500]

        return self.capture(
            summary=summary,
            decisions=decisions,
            action_items=action_items,
            emotional_state={"valence": 0.0, "intensity": 0.0, "context": "neutral"},
            open_threads=open_threads,
        )

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
