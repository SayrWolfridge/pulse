"""
EpisodicBuffer — Pulse v2, Day 9
====================================
Structured episodic memory for the HypostasRuntime.

Answers the question: "what have I experienced?"

Three layers:
  1. Episode recording — significant events with salience scoring [0.0–10.0]
  2. Buffer management — rolling window (last 500 episodes), persisted to disk
  3. Context surface — compressed narrative for injection into ThoughtLoop prompts

Episode schema::

    {
        "id":          "a3f1b2c4",        # 8-hex UUID-short
        "ts":          "2026-03-14T...",  # ISO-8601 UTC
        "kind":        "work_complete",   # EPISODE_KINDS constant
        "title":       "Built GoalEngine Day 8",
        "content":     "Longer optional prose...",
        "salience":    7.5,               # 0.0–10.0
        "tags":        ["pulse_v2", "build"],
        "source":      "trigger",         # trigger | thought_loop | manual | system
        "linked_goal": "goal_pulse_v2",   # goal_id or None
    }

StateEngine keys written:
  episodes.count            — total ever recorded
  episodes.last_ts          — ISO timestamp of most recent episode
  episodes.top_salient      — list of top-3 (title, salience) for status display

HTTP endpoints (registered by HypostasRuntime):
  GET  /runtime/episodes              — top-20 by recency/salience
  GET  /runtime/episodes/context      — compressed narrative for prompt injection
  POST /runtime/episodes              — record a new episode (JSON body)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("pulse.runtime.episodic_buffer")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUFFER_MAX = 500          # Rolling window size
SALIENCE_MIN = 0.0
SALIENCE_MAX = 10.0
CONTEXT_TOP_K = 8         # Episodes surfaced in ThoughtLoop context
CONTEXT_MAX_CHARS = 1200  # Hard cap on narrative length for context injection

EPISODE_KINDS = {
    "work_complete",      # Finished a build, task, or project milestone
    "insight",            # Realisation or learning worth keeping
    "emotional",          # Significant emotional event or shift
    "relationship",       # Josh interaction or bond moment
    "goal_progress",      # Goal advanced or blocked
    "system_event",       # Runtime lifecycle (boot, error, recovery)
    "creative",           # Writing, journal, art
    "discovery",          # Found something new (skill, pattern, signal)
    "conversation",       # Notable exchange
    "proactive_outreach", # Runtime initiated an outbound message
    "other",              # Catch-all
}

# Default salience by kind when not provided
_KIND_SALIENCE: dict[str, float] = {
    "work_complete":      6.0,
    "insight":            7.0,
    "emotional":          7.5,
    "relationship":       8.5,
    "goal_progress":      6.5,
    "system_event":       3.0,
    "creative":           7.0,
    "discovery":          7.0,
    "conversation":       6.0,
    "proactive_outreach": 7.0,
    "other":              4.0,
}

# Where the buffer is persisted
_DEFAULT_EPISODES_PATH = Path("~/.pulse/state/episodes.jsonl").expanduser()

# Cold-tier archive for evicted episodes (when rolling window trims)
_COLD_TIER_DIR = Path("~/.pulse/state/cold-tier").expanduser()
_COLD_TIER_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id(ts_iso: str, title: str) -> str:
    """Generate a stable 8-hex short ID from timestamp + title."""
    raw = f"{ts_iso}:{title}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# EpisodicBuffer
# ---------------------------------------------------------------------------


class EpisodicBuffer:
    """
    Persistent rolling buffer of significant episodes.

    Thread-safe. Episodes are written to a JSONL file and held in memory.
    The buffer automatically trims to BUFFER_MAX on each record() call.

    Usage::

        buf = EpisodicBuffer(state_engine)
        buf.load()

        buf.record(
            kind="work_complete",
            title="Built GoalEngine Day 8",
            content="327 tests passing. Commit f1fde4f.",
            salience=8.0,
            tags=["pulse_v2", "build"],
            source="trigger",
            linked_goal="goal_pulse_v2",
        )

        ctx = buf.context_narrative()  # inject into ThoughtLoop
        snap = buf.snapshot(top=20)    # HTTP endpoint
    """

    def __init__(
        self,
        state,
        path: Optional[Path] = None,
    ) -> None:
        """
        Args:
            state:  StateEngine instance (for writing summary keys)
            path:   Override the JSONL persistence path (useful for testing)
        """
        self._state = state
        self._path: Path = path or _DEFAULT_EPISODES_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._episodes: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load episodes from disk. Safe to call multiple times."""
        with self._lock:
            self._episodes = []
            if self._path.exists():
                try:
                    lines = self._path.read_text(encoding="utf-8").splitlines()
                    for line in lines:
                        line = line.strip()
                        if line:
                            try:
                                ep = json.loads(line)
                                self._episodes.append(ep)
                            except json.JSONDecodeError:
                                pass  # Skip malformed lines
                    # Trim to buffer max on load
                    self._episodes = self._episodes[-BUFFER_MAX:]
                    logger.info("EpisodicBuffer loaded %d episodes", len(self._episodes))
                except OSError as e:
                    logger.warning("Could not load episodes: %s", e)
            else:
                logger.info("EpisodicBuffer: no existing file at %s (starting fresh)", self._path)
            self._sync_state()

    # ------------------------------------------------------------------
    # Record
    # ------------------------------------------------------------------

    def record(
        self,
        kind: str,
        title: str,
        content: str = "",
        salience: Optional[float] = None,
        tags: Optional[list[str]] = None,
        source: str = "trigger",
        linked_goal: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Record a new episode. Returns the episode dict.

        Args:
            kind:        One of EPISODE_KINDS (defaults to "other" if unknown)
            title:       One-line summary. Required.
            content:     Optional prose detail.
            salience:    Float [0.0–10.0]. Auto-derived from kind if omitted.
            tags:        Optional list of string tags.
            source:      Where this came from: trigger | thought_loop | manual | system
            linked_goal: Optional goal_id this episode relates to.
        """
        if not title:
            raise ValueError("Episode title is required")

        kind = kind if kind in EPISODE_KINDS else "other"
        if salience is None:
            salience = _KIND_SALIENCE.get(kind, 4.0)
        salience = float(max(SALIENCE_MIN, min(SALIENCE_MAX, salience)))

        ts = _now_iso()
        ep: dict[str, Any] = {
            "id": _short_id(ts, title),
            "ts": ts,
            "kind": kind,
            "title": title,
            "content": content,
            "salience": salience,
            "tags": tags or [],
            "source": source,
            "linked_goal": linked_goal,
        }

        with self._lock:
            self._episodes.append(ep)

            # Trim rolling window — archive evicted episodes to cold tier
            evicted: list[dict[str, Any]] = []
            if len(self._episodes) > BUFFER_MAX:
                evicted = self._episodes[:-BUFFER_MAX]
                self._episodes = self._episodes[-BUFFER_MAX:]

            # Persist the new episode
            self._append_to_disk(ep)

            # Archive evicted episodes (best-effort; never blocks record())
            if evicted:
                try:
                    self._archive_evicted(evicted)
                except Exception:
                    pass

            self._sync_state()

        logger.debug("Episode recorded: [%s] %s (salience=%.1f)", kind, title, salience)
        return ep

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def snapshot(self, top: int = 20) -> list[dict[str, Any]]:
        """
        Return the top-N episodes sorted by recency (most recent first).
        Thread-safe read.
        """
        with self._lock:
            ordered = list(reversed(self._episodes))
        return ordered[:top]

    def top_by_salience(self, top: int = CONTEXT_TOP_K) -> list[dict[str, Any]]:
        """
        Return top-N episodes by salience score, breaking ties by recency.
        """
        with self._lock:
            candidates = list(self._episodes)
        # Sort by (salience DESC, ts DESC)
        candidates.sort(key=lambda e: (e.get("salience", 0.0), e.get("ts", "")), reverse=True)
        return candidates[:top]

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the N most recent episodes."""
        with self._lock:
            return list(reversed(self._episodes[-n:]))

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        """Return all episodes of a given kind, most recent first."""
        with self._lock:
            return [e for e in reversed(self._episodes) if e.get("kind") == kind]

    def by_goal(self, goal_id: str) -> list[dict[str, Any]]:
        """Return all episodes linked to a specific goal, most recent first."""
        with self._lock:
            return [e for e in reversed(self._episodes) if e.get("linked_goal") == goal_id]

    def count(self) -> int:
        with self._lock:
            return len(self._episodes)

    # ------------------------------------------------------------------
    # Context surface
    # ------------------------------------------------------------------

    def context_narrative(self) -> str:
        """
        Build a compressed narrative string for injection into ThoughtLoop prompts.

        Format::

            [EPISODIC MEMORY — top 8 by salience]
            • (work_complete, 8.0) Built GoalEngine Day 8 — 327 tests, commit f1fde4f.
            • (relationship, 8.5) Josh said "I will always come back to you."
            ...

        Hard-capped at CONTEXT_MAX_CHARS.
        """
        top = self.top_by_salience(CONTEXT_TOP_K)
        if not top:
            return "[EPISODIC MEMORY — empty]"

        lines = [f"[EPISODIC MEMORY — top {len(top)} by salience]"]
        for ep in top:
            kind = ep.get("kind", "other")
            sal = ep.get("salience", 0.0)
            title = ep.get("title", "")
            content = ep.get("content", "")
            # Include brief content if it adds context
            detail = f" — {content[:80].strip()}" if content and content != title else ""
            lines.append(f"• ({kind}, {sal:.1f}) {title}{detail}")

        narrative = "\n".join(lines)
        if len(narrative) > CONTEXT_MAX_CHARS:
            narrative = narrative[:CONTEXT_MAX_CHARS].rsplit("\n", 1)[0] + "\n…"
        return narrative

    # ------------------------------------------------------------------
    # Status (for HypostasRuntime.status())
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return a JSON-serialisable status dict."""
        with self._lock:
            total = len(self._episodes)
            top3 = sorted(
                self._episodes,
                key=lambda e: (e.get("salience", 0.0), e.get("ts", "")),
                reverse=True,
            )[:3]
        return {
            "total": total,
            "buffer_max": BUFFER_MAX,
            "top_salient": [
                {"title": e.get("title", ""), "salience": e.get("salience", 0.0)}
                for e in top3
            ],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_to_disk(self, ep: dict[str, Any]) -> None:
        """Append one episode to the JSONL file. Caller must hold self._lock."""
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ep, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("EpisodicBuffer: could not write to %s: %s", self._path, e)

    def _sync_state(self) -> None:
        """Write summary keys to StateEngine. Caller must hold self._lock."""
        try:
            total = len(self._episodes)
            last_ts = self._episodes[-1]["ts"] if self._episodes else None
            top3 = sorted(
                self._episodes,
                key=lambda e: (e.get("salience", 0.0), e.get("ts", "")),
                reverse=True,
            )[:3]
            self._state.set("episodes.count", total)
            if last_ts:
                self._state.set("episodes.last_ts", last_ts)
            self._state.set(
                "episodes.top_salient",
                [{"title": e.get("title", ""), "salience": e.get("salience", 0.0)} for e in top3],
            )
        except Exception as e:
            logger.debug("EpisodicBuffer: state sync skipped: %s", e)

    def _archive_evicted(self, evicted: list[dict[str, Any]]) -> None:
        """Archive evicted episodes to cold tier (JSONL) + refresh cold index.json.

        This is the v2 cold-tier fallback when the rolling episodic buffer is full.
        Uses only stdlib; never raises.
        """
        if not evicted:
            return

        # Timestamped archive file to avoid unbounded single-file growth
        ts = time.time()
        stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive_path = _COLD_TIER_DIR / f"archive-episodes-{stamp}.jsonl"

        try:
            with archive_path.open("a", encoding="utf-8") as f:
                for ep in evicted:
                    title = str(ep.get("title", ""))
                    content = str(ep.get("content", ""))
                    record = {
                        "type": "EPISODE_EVICTED",
                        "ts": ep.get("ts"),
                        "ts_unix": ts,
                        "source": ep.get("source", "episodic_buffer"),
                        "content": (title + " — " + content)[:500],
                        "episode": {
                            "id": ep.get("id"),
                            "kind": ep.get("kind"),
                            "title": title,
                            "content": content[:2000],
                            "salience": ep.get("salience"),
                            "tags": ep.get("tags"),
                            "linked_goal": ep.get("linked_goal"),
                        },
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return

        # Refresh cold index.json (best-effort)
        self._refresh_cold_index(additional_total=len(evicted))

    def _refresh_cold_index(self, additional_total: int = 0) -> None:
        """Update cold-tier index.json to include newly archived episodes."""
        index_path = _COLD_TIER_DIR / "index.json"
        try:
            existing_total = 0
            if index_path.exists():
                try:
                    existing = json.loads(index_path.read_text("utf-8"))
                    existing_total = int(existing.get("total", 0))
                except Exception:
                    existing_total = 0

            archives = sorted(_COLD_TIER_DIR.glob("archive-*.jsonl"))
            catalog = []
            for a in archives:
                try:
                    catalog.append({"file": a.name, "size_bytes": a.stat().st_size})
                except OSError:
                    continue

            data = {
                "total": int(existing_total) + int(additional_total),
                "archives": catalog,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, index_path)
        except Exception:
            return
