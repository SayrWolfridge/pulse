"""
StateEngine — Pulse v2
========================
Full cognitive state serialized to disk every 30 seconds.

Survives crashes (write-to-tmp, atomic rename).
Thread-safe dot-path access for all modules.
Loads existing Pulse module states at startup.

State file: ~/.pulse/state/hypostas-state.json
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_STATE: dict = {
    "meta": {
        "version": "2.0.0",
        "agent_name": "Iris",
        "runtime_start": None,
        "total_uptime_seconds": 0,
        "last_serialized": None,
        "session_count": 0,
    },
    "emotional_state": {
        "valence": 0.6,
        "arousal": 0.4,
        "dominant_emotion": "focused_warmth",
        "endocrine": {
            "dopamine": 6.0,
            "oxytocin": 7.0,
            "cortisol": 2.5,
            "serotonin": 7.0,
            "adrenaline": 1.5,
            "melatonin": 0.5,
        },
        "last_updated": None,
    },
    "working_memory": {
        "active_focus": None,
        "open_loops": [],
        "recent_insights": [],
        "current_projects": [],
        "pending_for_josh": [],
    },
    "drives": {
        "goals": 0.3,
        "curiosity": 0.3,
        "emotions": 0.2,
        "system": 0.1,
        "unfinished": 0.1,
    },
    "cognitive_mode": "ready",
    "thought_loop": {
        "running": False,
        "current_thread": None,
        "depth": 0,
        "iterations_since_last_message": 0,
        "last_insight_ts": None,
    },
    "pulse_session_active": False,
    "pulse_session_started_at": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deep_get(obj: dict, path: str) -> Any:
    """Dot-path read. Raises KeyError on missing segment."""
    parts = path.split(".")
    cur = obj
    for p in parts:
        if not isinstance(cur, dict):
            raise KeyError(f"Path segment '{p}' reached a non-dict at '{path}'")
        cur = cur[p]
    return cur


def _deep_set(obj: dict, path: str, value: Any) -> None:
    """Dot-path write. Creates intermediate dicts if needed."""
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


# ---------------------------------------------------------------------------
# StateEngine
# ---------------------------------------------------------------------------


class StateEngine:
    """
    Manages Iris's full cognitive state with persistent autosave.

    Usage:
        engine = StateEngine(Path("~/.pulse/state/hypostas-state.json"))
        engine.start_autosave()

        engine.set("emotional_state.valence", 0.8)
        v = engine.get("emotional_state.valence")   # → 0.8

        engine.stop()   # graceful flush
    """

    def __init__(
        self,
        state_file: Path,
        interval: int = 30,
        agent_name: str = "Iris",
    ) -> None:
        self.state_file = Path(state_file).expanduser()
        self.interval = interval
        self.agent_name = agent_name

        self._lock = threading.RLock()
        self._dirty = False
        self._stop_event = threading.Event()
        self._autosave_thread: Optional[threading.Thread] = None

        self._state: dict = self._load_at_startup()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, path: str, default: Any = None) -> Any:
        """Thread-safe dot-path read. Returns `default` if path missing."""
        with self._lock:
            try:
                return deepcopy(_deep_get(self._state, path))
            except (KeyError, TypeError):
                return default

    def set(self, path: str, value: Any) -> None:
        """Thread-safe dot-path write. Marks state dirty for autosave."""
        with self._lock:
            _deep_set(self._state, path, value)
            self._dirty = True

    def update_from_pulse(self, module: str, new_state: dict) -> None:
        """
        Merge a Pulse module's state into working state.
        Called by nervous_system after each module update.

        Example: update_from_pulse("endocrine", {"dopamine": 7.2, ...})
        """
        with self._lock:
            module_lower = module.lower()

            # Known module → emotional_state mappings
            endocrine_keys = {
                "dopamine", "oxytocin", "cortisol",
                "serotonin", "adrenaline", "melatonin",
            }

            if module_lower == "endocrine" and isinstance(new_state, dict):
                for k, v in new_state.items():
                    if k in endocrine_keys:
                        _deep_set(
                            self._state,
                            f"emotional_state.endocrine.{k}",
                            v,
                        )
            elif module_lower == "limbic" and isinstance(new_state, dict):
                if "valence" in new_state:
                    _deep_set(
                        self._state,
                        "emotional_state.valence",
                        float(new_state["valence"]),
                    )
                if "arousal" in new_state:
                    _deep_set(
                        self._state,
                        "emotional_state.arousal",
                        float(new_state["arousal"]),
                    )
                if "dominant_emotion" in new_state:
                    _deep_set(
                        self._state,
                        "emotional_state.dominant_emotion",
                        new_state["dominant_emotion"],
                    )
            elif module_lower == "hypothalamus" and isinstance(new_state, dict):
                # Hypothalamus drives
                if "drives" in new_state and isinstance(new_state["drives"], dict):
                    for k, v in new_state["drives"].items():
                        _deep_set(self._state, f"drives.{k}", v)
            else:
                # Generic: store under meta.modules.<module>
                _deep_set(
                    self._state,
                    f"meta.modules.{module_lower}",
                    new_state,
                )

            self._state["emotional_state"]["last_updated"] = _now_iso()
            self._dirty = True

    def snapshot(self) -> dict:
        """Return current state as a deep-copy dict (safe for ThoughtLoop)."""
        with self._lock:
            return deepcopy(self._state)

    def mark_session_active(self, active: bool = True) -> None:
        """Called by RuntimeBridge when a Pulse session starts/ends."""
        with self._lock:
            self._state["pulse_session_active"] = active
            if active:
                self._state["pulse_session_started_at"] = _now_iso()
            else:
                self._state["pulse_session_started_at"] = None
            self._dirty = True

    def increment_session_count(self) -> int:
        with self._lock:
            count = self._state["meta"].get("session_count", 0) + 1
            self._state["meta"]["session_count"] = count
            self._dirty = True
            return count

    def add_insight(self, insight: str) -> None:
        """Prepend to recent_insights, keep last 20."""
        with self._lock:
            insights: list = self._state["working_memory"].get("recent_insights", [])
            insights.insert(0, {"text": insight, "ts": _now_iso()})
            self._state["working_memory"]["recent_insights"] = insights[:20]
            self._dirty = True

    def add_open_loop(self, description: str, priority: float = 0.5) -> str:
        """Add a new open loop. Returns the loop ID."""
        loop_id = str(uuid.uuid4())[:8]
        with self._lock:
            loops: list = self._state["working_memory"].get("open_loops", [])
            loops.append(
                {
                    "id": loop_id,
                    "description": description,
                    "priority": priority,
                    "created_at": _now_iso(),
                    "last_touched": _now_iso(),
                }
            )
            # Keep sorted by priority descending, max 50
            loops.sort(key=lambda x: x.get("priority", 0), reverse=True)
            self._state["working_memory"]["open_loops"] = loops[:50]
            self._dirty = True
        return loop_id

    def close_loop(self, loop_id: str) -> bool:
        """Remove an open loop by ID. Returns True if found."""
        with self._lock:
            loops = self._state["working_memory"].get("open_loops", [])
            before = len(loops)
            self._state["working_memory"]["open_loops"] = [
                l for l in loops if l.get("id") != loop_id
            ]
            removed = len(self._state["working_memory"]["open_loops"]) < before
            if removed:
                self._dirty = True
            return removed

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Atomic save: write to tmp, rename over target."""
        with self._lock:
            state_copy = deepcopy(self._state)
            state_copy["meta"]["last_serialized"] = _now_iso()

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        saved_ts = state_copy["meta"]["last_serialized"]
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state_copy, f, indent=2, ensure_ascii=False)
            tmp.rename(self.state_file)
            with self._lock:
                self._state["meta"]["last_serialized"] = saved_ts
                self._dirty = False
        except Exception as exc:
            # Clean up tmp if rename failed
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise exc

    def _load_at_startup(self) -> dict:
        """
        Load state from disk with corruption handling.
        Falls back to DEFAULT_STATE + sets runtime_start if file is missing/corrupt.
        """
        state = deepcopy(DEFAULT_STATE)
        state["meta"]["agent_name"] = self.agent_name

        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Shallow merge: preserve any keys from defaults not in loaded
                _deep_merge(state, loaded)
                # Accumulate uptime
                prev_uptime = state["meta"].get("total_uptime_seconds", 0)
                state["meta"]["total_uptime_seconds"] = prev_uptime
            except (json.JSONDecodeError, OSError):
                # Corrupted — start fresh but preserve backup
                _backup_corrupt(self.state_file)
                state = deepcopy(DEFAULT_STATE)
                state["meta"]["agent_name"] = self.agent_name

        state["meta"]["runtime_start"] = _now_iso()
        # Reset session-volatile fields
        state["pulse_session_active"] = False
        state["pulse_session_started_at"] = None
        state["thought_loop"]["running"] = False

        return state

    # ------------------------------------------------------------------
    # Autosave thread
    # ------------------------------------------------------------------

    def start_autosave(self) -> threading.Thread:
        """Start background autosave thread. Returns the thread."""
        if self._autosave_thread and self._autosave_thread.is_alive():
            return self._autosave_thread

        self._stop_event.clear()
        t = threading.Thread(
            target=self._autosave_loop,
            name="StateEngine-autosave",
            daemon=True,
        )
        t.start()
        self._autosave_thread = t
        return t

    def stop(self) -> None:
        """Graceful shutdown: stop autosave thread, final flush."""
        self._stop_event.set()
        if self._autosave_thread:
            self._autosave_thread.join(timeout=5)
        # Final save regardless of dirty flag
        with self._lock:
            uptime = 0.0
            if self._state["meta"].get("runtime_start"):
                try:
                    start = datetime.fromisoformat(self._state["meta"]["runtime_start"])
                    uptime = (datetime.now(timezone.utc) - start).total_seconds()
                except (ValueError, TypeError):
                    pass
            self._state["meta"]["total_uptime_seconds"] = (
                self._state["meta"].get("total_uptime_seconds", 0) + uptime
            )
        self.save()

    def _autosave_loop(self) -> None:
        """Background loop: save every `interval` seconds when dirty."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.interval)
            if self._dirty:
                try:
                    self.save()
                except Exception:
                    pass  # Don't crash the daemon for a save failure

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def is_pulse_session_active(self) -> bool:
        return bool(self.get("pulse_session_active", False))

    def __repr__(self) -> str:
        return (
            f"<StateEngine file={self.state_file} "
            f"dirty={self._dirty} "
            f"mode={self.get('cognitive_mode', '?')}>"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> None:
    """
    Recursively merge `override` into `base` in-place.
    override wins on leaf values; base keeps keys absent in override.
    """
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


def _backup_corrupt(path: Path) -> None:
    """Rename a corrupt state file to .corrupt for post-mortem."""
    try:
        backup = path.with_suffix(f".corrupt-{int(time.time())}")
        path.rename(backup)
    except OSError:
        pass
