"""
PROPRIOCEPTION — Self-Model / Capability Awareness
=====================================================
Ported from v1 pulse.src.proprioception into v2 HypostasRuntime.

Tracks what the system can and cannot do right now.
Knows current model, tools, skills, limitations, and resource usage.

All state persisted via StateEngine under ``proprioception.*`` dot-paths.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .self_model import SelfModel

ACTION_TOOL_MAP = {
    "send a message": "message",
    "send message": "message",
    "search the web": "web_search",
    "web search": "web_search",
    "browse": "browser",
    "read files": "read",
    "write files": "write",
    "edit files": "edit",
    "run commands": "exec",
    "see images": "image",
    "speak": "tts",
    "text to speech": "tts",
}


class Proprioception:
    """Self-model capability awareness — what can I do right now?"""

    _KEY = "proprioception"

    def __init__(self, state: "StateEngine", self_model: "SelfModel") -> None:
        self._state = state
        self._self_model = self_model
        if self._state.get(f"{self._KEY}.model") is None:
            self._state.set(f"{self._KEY}.model", "unknown")
            self._state.set(f"{self._KEY}.context_window", 200000)
            self._state.set(f"{self._KEY}.context_used", 0)
            self._state.set(f"{self._KEY}.tools_available", [])
            self._state.set(f"{self._KEY}.skills_available", [])
            self._state.set(f"{self._KEY}.channels_active", [])
            self._state.set(f"{self._KEY}.limitations", [])
            self._state.set(f"{self._KEY}.session_type", "unknown")
            self._state.set(f"{self._KEY}.uptime_start", None)
            self._state.set(f"{self._KEY}.failed_attempts", [])

    def can_i(self, action: str) -> Tuple[bool, str]:
        """Check if a specific action is possible right now."""
        tools = list(self._state.get(f"{self._KEY}.tools_available") or [])
        action_lower = action.lower().strip()

        if action_lower in tools:
            return True, f"{action_lower} tool available"

        required = ACTION_TOOL_MAP.get(action_lower)
        if required:
            if required in tools:
                return True, f"{required} tool available"
            self._log_failed_attempt(action_lower, f"{required} tool not available")
            return False, f"{required} tool not in current session"

        for lim in (self._state.get(f"{self._KEY}.limitations") or []):
            if action_lower in str(lim).lower():
                return False, str(lim)

        return False, f"Unknown action '{action}' — cannot confirm capability"

    def get_limits(self) -> dict:
        """Current resource limits."""
        ctx_max = int(self._state.get(f"{self._KEY}.context_window") or 200000)
        ctx_used = int(self._state.get(f"{self._KEY}.context_used") or 0)
        return {
            "context_window": ctx_max,
            "context_used": ctx_used,
            "context_remaining": ctx_max - ctx_used,
            "context_percent_used": round(ctx_used / max(ctx_max, 1) * 100, 1),
            "tools_available": self._state.get(f"{self._KEY}.tools_available") or [],
            "model": self._state.get(f"{self._KEY}.model") or "unknown",
        }

    def update_capabilities(
        self,
        model: str,
        tools: List[str],
        context_max: int,
        context_used: int = 0,
        skills: Optional[List[str]] = None,
        channels: Optional[List[str]] = None,
        limitations: Optional[List[str]] = None,
        session_type: str = "main",
    ) -> None:
        """Called on session start or model switch."""
        old_model = self._state.get(f"{self._KEY}.model")
        self._state.set(f"{self._KEY}.model", model)
        self._state.set(f"{self._KEY}.tools_available", tools)
        self._state.set(f"{self._KEY}.context_window", context_max)
        self._state.set(f"{self._KEY}.context_used", context_used)
        self._state.set(f"{self._KEY}.skills_available", skills or [])
        self._state.set(f"{self._KEY}.channels_active", channels or [])
        self._state.set(f"{self._KEY}.limitations", limitations or [])
        self._state.set(f"{self._KEY}.session_type", session_type)
        if not self._state.get(f"{self._KEY}.uptime_start"):
            self._state.set(f"{self._KEY}.uptime_start", time.time())

    def get_identity_snapshot(self) -> dict:
        """Who am I right now?"""
        return {
            "model": self._state.get(f"{self._KEY}.model") or "unknown",
            "session_type": self._state.get(f"{self._KEY}.session_type") or "unknown",
            "channels_active": self._state.get(f"{self._KEY}.channels_active") or [],
            "tools_count": len(self._state.get(f"{self._KEY}.tools_available") or []),
            "skills_count": len(self._state.get(f"{self._KEY}.skills_available") or []),
        }

    def _log_failed_attempt(self, action: str, reason: str) -> None:
        """Log when we attempt something we can't do."""
        attempts = list(self._state.get(f"{self._KEY}.failed_attempts") or [])
        attempts.append({
            "action": action,
            "reason": reason,
            "ts": int(time.time() * 1000),
        })
        self._state.set(f"{self._KEY}.failed_attempts", attempts[-50:])

    def tick(self) -> None:
        """No-op — capabilities are updated on session changes."""
        pass

    def status(self) -> dict:
        return {
            "model": self._state.get(f"{self._KEY}.model") or "unknown",
            "session_type": self._state.get(f"{self._KEY}.session_type") or "unknown",
            "tools_count": len(self._state.get(f"{self._KEY}.tools_available") or []),
            "skills_count": len(self._state.get(f"{self._KEY}.skills_available") or []),
            "channels_active": self._state.get(f"{self._KEY}.channels_active") or [],
            "failed_attempts": len(self._state.get(f"{self._KEY}.failed_attempts") or []),
        }
