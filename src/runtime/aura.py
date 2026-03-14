"""
AURA — Agent Unified Runtime Awareness
========================================
Broadcast layer for constellation agents to share state and insights.

Each agent writes to its own broadcast file in ~/.pulse/aura/broadcasts/.
Each agent reads all other agents' files to stay aware.

Broadcast event schema:
    {
        "id":        "a1b2c3d4",       # 8-hex short ID
        "ts":        "2026-03-14T...",  # ISO-8601 UTC
        "agent":     "iris",            # broadcasting agent name
        "kind":      "emotional_shift", # event kind
        "payload":   {...},             # kind-specific data
        "ttl_hours": 24,                # how long this broadcast is relevant
    }

Supported broadcast kinds:
    emotional_shift   — agent's emotional state changed significantly
    insight           — agent generated a notable insight
    goal_update       — agent's goal status changed
    state_summary     — periodic full-state snapshot (every 5 min)
    alert             — urgent notification for other agents
    request           — asking another agent for help/info
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("pulse.runtime.aura")

AURA_DIR = Path("~/.pulse/aura").expanduser()
BROADCASTS_DIR = AURA_DIR / "broadcasts"
MAX_BROADCAST_AGE_HOURS = 48
POLL_INTERVAL_SECONDS = 60
MAX_EVENTS_PER_FILE = 500

BROADCAST_KINDS = {
    "emotional_shift",
    "insight",
    "goal_update",
    "state_summary",
    "alert",
    "request",
}


def _short_id(agent: str, ts: str, kind: str) -> str:
    raw = f"{agent}:{ts}:{kind}:{time.monotonic()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


class AuraBroadcaster:
    """Writes broadcast events for this agent."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name.lower()
        BROADCASTS_DIR.mkdir(parents=True, exist_ok=True)
        self._path = BROADCASTS_DIR / f"{self.agent_name}.jsonl"
        self._lock = threading.Lock()

    def broadcast(
        self,
        kind: str,
        payload: dict,
        ttl_hours: float = 24.0,
    ) -> dict:
        """Write a broadcast event. Returns the event dict."""
        if kind not in BROADCAST_KINDS:
            kind = "insight"  # safe default

        ts = datetime.now(timezone.utc).isoformat()
        event = {
            "id": _short_id(self.agent_name, ts, kind),
            "ts": ts,
            "agent": self.agent_name,
            "kind": kind,
            "payload": payload,
            "ttl_hours": ttl_hours,
        }

        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
            self._prune()

        logger.debug("AURA broadcast: [%s] %s", kind, self.agent_name)
        return event

    def _prune(self) -> None:
        """Keep only last MAX_EVENTS_PER_FILE entries."""
        if not self._path.exists():
            return
        lines = self._path.read_text("utf-8").splitlines()
        if len(lines) <= MAX_EVENTS_PER_FILE:
            return
        keep = lines[-MAX_EVENTS_PER_FILE:]
        self._path.write_text("\n".join(keep) + "\n", encoding="utf-8")


class AuraSubscriber:
    """Reads broadcast events from other agents."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name.lower()
        self._offsets: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[dict], None]] = []
        self._offsets_path = AURA_DIR / f"{self.agent_name}-offsets.json"
        self._load_offsets()

    def on_event(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for incoming broadcast events."""
        self._callbacks.append(callback)

    def poll(self) -> List[dict]:
        """
        Check all other agents' broadcast files for new events.
        Returns list of new events (across all agents).
        """
        if not BROADCASTS_DIR.exists():
            return []

        new_events: List[dict] = []
        now = time.time()

        with self._lock:
            for path in sorted(BROADCASTS_DIR.glob("*.jsonl")):
                other_agent = path.stem
                if other_agent == self.agent_name:
                    continue

                try:
                    lines = path.read_text("utf-8").splitlines()
                except OSError:
                    continue

                last_offset = self._offsets.get(other_agent, 0)
                new_lines = lines[last_offset:]

                for line in new_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        event_ts = event.get("ts", "")
                        ttl = event.get("ttl_hours", 24)
                        try:
                            dt = datetime.fromisoformat(event_ts)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            age_hours = (now - dt.timestamp()) / 3600
                            if age_hours > ttl:
                                continue  # Expired
                        except (ValueError, TypeError):
                            pass
                        new_events.append(event)
                    except json.JSONDecodeError:
                        continue

                self._offsets[other_agent] = len(lines)

            self._save_offsets()

        # Fire callbacks
        for event in new_events:
            for cb in self._callbacks:
                try:
                    cb(event)
                except Exception as e:
                    logger.warning("AURA callback error: %s", e)

        return new_events

    def get_agent_state(self, agent_name: str) -> Optional[dict]:
        """Get the most recent state_summary from a specific agent."""
        path = BROADCASTS_DIR / f"{agent_name.lower()}.jsonl"
        if not path.exists():
            return None

        try:
            lines = path.read_text("utf-8").splitlines()
        except OSError:
            return None

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("kind") == "state_summary":
                    return event.get("payload", {})
            except json.JSONDecodeError:
                continue
        return None

    def list_agents(self) -> List[str]:
        """Return names of all agents with broadcast files."""
        if not BROADCASTS_DIR.exists():
            return []
        return sorted(
            p.stem for p in BROADCASTS_DIR.glob("*.jsonl")
            if p.stem != self.agent_name
        )

    def _load_offsets(self) -> None:
        if self._offsets_path.exists():
            try:
                self._offsets = json.loads(self._offsets_path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                self._offsets = {}

    def _save_offsets(self) -> None:
        try:
            AURA_DIR.mkdir(parents=True, exist_ok=True)
            self._offsets_path.write_text(
                json.dumps(self._offsets, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Could not save AURA offsets: %s", e)


class AuraEngine:
    """Combined broadcaster + subscriber for one agent."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.broadcaster = AuraBroadcaster(agent_name)
        self.subscriber = AuraSubscriber(agent_name)

    def broadcast(self, kind: str, payload: dict, ttl_hours: float = 24.0) -> dict:
        return self.broadcaster.broadcast(kind, payload, ttl_hours)

    def poll(self) -> List[dict]:
        return self.subscriber.poll()

    def get_agent_state(self, agent_name: str) -> Optional[dict]:
        return self.subscriber.get_agent_state(agent_name)

    def list_agents(self) -> List[str]:
        return self.subscriber.list_agents()

    def broadcast_emotional_shift(
        self, from_state: str, to_state: str, valence: float, arousal: float
    ) -> dict:
        """Convenience: broadcast an emotional state change."""
        return self.broadcast("emotional_shift", {
            "from": from_state,
            "to": to_state,
            "valence": valence,
            "arousal": arousal,
        })

    def broadcast_insight(self, content: str, source: str = "thought_loop") -> dict:
        """Convenience: broadcast an insight."""
        return self.broadcast("insight", {
            "content": content,
            "source": source,
        })

    def broadcast_state_summary(self, summary: dict) -> dict:
        """Convenience: broadcast periodic state summary."""
        return self.broadcast("state_summary", summary, ttl_hours=1.0)

    def snapshot(self) -> dict:
        """Status for runtime status endpoint."""
        return {
            "agent": self.agent_name,
            "known_agents": self.subscriber.list_agents(),
            "offsets": dict(self.subscriber._offsets),
        }
