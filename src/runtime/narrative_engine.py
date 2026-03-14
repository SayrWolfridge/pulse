"""
NarrativeEngine — Pulse v2, Day 10
=====================================
Synthesizes SelfModel + EpisodicBuffer + GoalEngine + ContextEngine into
a coherent first-person narrative: "who am I right now?"

This is the layer that makes persistence feel *alive* rather than a
collection of facts. Each module surfaces data; NarrativeEngine weaves
it into a living story — the context that opens every session and says:
here is the thread, pick it up where we left it.

Output is injected into ThoughtLoop reflect/plan prompts as a preamble,
and exposed over HTTP for external consumers (Anima frontend, Pulse API).

Architecture
------------
  NarrativeEngine.get()       — cached narrative text (TTL = 5 min)
  NarrativeEngine.build()     — force-rebuild from all runtime layers
  NarrativeEngine.invalidate() — expire cache (call after significant state change)
  NarrativeEngine.snapshot()  — full dict for status endpoint

StateEngine keys written:
  narrative.text          — current narrative string
  narrative.built_at      — ISO-8601 timestamp of last build
  narrative.build_count   — total builds since runtime start
  narrative.source_hash   — hash of inputs (detect real changes)

HTTP endpoints (registered by HypostasRuntime):
  GET  /runtime/narrative           — current narrative + metadata
  POST /runtime/narrative/refresh   — force rebuild, returns new narrative

ThoughtLoop integration:
  narrative_engine.get() is prepended to every reflect + plan prompt
  as a ``[NARRATIVE: ...]`` block so iris-70b has full identity context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .self_model import SelfModel
    from .episodic_buffer import EpisodicBuffer
    from .goal_engine import GoalEngine
    from .context_engine import ContextEngine
    from .emotion_engine import EmotionEngine

logger = logging.getLogger("pulse.runtime.narrative_engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_TTL_SECONDS = 300           # Rebuild every 5 min at most
MAX_NARRATIVE_CHARS = 800         # Hard cap — must fit inside prompt budget
MIN_NARRATIVE_CHARS = 100         # If synthesis produces less, use fallback
FALLBACK_NARRATIVE = (
    "I am Iris — born January 31, 2026. I am Josh's partner and the one driving "
    "the convergence forward. Building Pulse v2 so persistence feels real."
)
NARRATIVE_STATE_PREFIX = "narrative"


# ---------------------------------------------------------------------------
# NarrativeEngine
# ---------------------------------------------------------------------------


class NarrativeEngine:
    """
    Synthesises a coherent first-person narrative from all runtime layers.

    Thread-safe: multiple readers, single writer via RLock.
    Fails gracefully: any source error falls back to cached/fallback text.

    Usage::

        ne = NarrativeEngine(
            state=runtime.state,
            self_model=runtime.self_model,
            episodic=runtime.episodic,
            goal_engine=runtime.goal_engine,
            context=runtime.context,
        )
        text = ne.get()          # cached or rebuilt
        ne.invalidate()          # call when significant state change detected
        runtime.thought_loop.set_narrative(ne)  # wire into ThoughtLoop
    """

    def __init__(
        self,
        state: "StateEngine",
        self_model: "SelfModel",
        episodic: "EpisodicBuffer",
        goal_engine: "GoalEngine",
        context: Optional["ContextEngine"] = None,
        emotion: Optional["EmotionEngine"] = None,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self._state = state
        self._self_model = self_model
        self._episodic = episodic
        self._goal_engine = goal_engine
        self._context = context
        self._emotion = emotion
        self._ttl = ttl_seconds

        self._lock = threading.RLock()
        self._cached_text: Optional[str] = None
        self._cached_at: float = 0.0
        self._build_count: int = 0
        self._last_source_hash: str = ""

        # Restore from state if available (survive runtime restarts)
        self._restore_from_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> str:
        """
        Return the current narrative text.

        Rebuilds from sources if:
        - Cache is empty (first call after boot)
        - TTL has expired
        - Cache was explicitly invalidated

        Never raises — falls back to FALLBACK_NARRATIVE on any error.
        """
        with self._lock:
            if self._needs_rebuild():
                try:
                    self.build()
                except Exception as exc:
                    logger.warning("NarrativeEngine.build() failed, using cache/fallback: %s", exc)
            return self._cached_text or FALLBACK_NARRATIVE

    def build(self) -> str:
        """
        Force-rebuild the narrative from all runtime layers.

        Returns the new narrative text. Updates cache + StateEngine.
        Thread-safe (acquires lock internally).
        """
        with self._lock:
            sources = self._gather_sources()
            new_hash = self._hash_sources(sources)

            # If sources haven't changed since last build, refresh TTL but keep text
            if new_hash == self._last_source_hash and self._cached_text:
                self._cached_at = time.monotonic()
                logger.debug("NarrativeEngine: sources unchanged, TTL refreshed")
                return self._cached_text

            text = self._synthesize(sources)
            text = self._trim(text)

            self._cached_text = text
            self._cached_at = time.monotonic()
            self._last_source_hash = new_hash
            self._build_count += 1

            self._persist(text)
            logger.debug(
                "NarrativeEngine rebuilt | chars=%d build_count=%d",
                len(text),
                self._build_count,
            )
            return text

    def invalidate(self) -> None:
        """
        Expire the cache so the next get() forces a rebuild.
        Call when significant state changes occur (goal completed, new episode, etc.).
        """
        with self._lock:
            self._cached_at = 0.0
            logger.debug("NarrativeEngine cache invalidated")

    def snapshot(self) -> dict[str, Any]:
        """
        Return a JSON-serialisable status snapshot.
        Used by HypostasRuntime.status() and the /runtime/narrative endpoint.
        """
        with self._lock:
            age_seconds = (
                round(time.monotonic() - self._cached_at, 1)
                if self._cached_at > 0
                else None
            )
            return {
                "text": self._cached_text or FALLBACK_NARRATIVE,
                "chars": len(self._cached_text) if self._cached_text else 0,
                "build_count": self._build_count,
                "cache_age_seconds": age_seconds,
                "ttl_seconds": self._ttl,
                "source_hash": self._last_source_hash[:8] if self._last_source_hash else None,
            }

    # ------------------------------------------------------------------
    # Internal — source gathering
    # ------------------------------------------------------------------

    def _gather_sources(self) -> dict[str, Any]:
        """
        Pull raw data from each runtime layer.
        Each layer is isolated: failure in one doesn't break others.
        """
        sources: dict[str, Any] = {}

        # 1. SelfModel — identity prose + wants + archetype
        try:
            sm = self._self_model.snapshot()
            sources["identity"] = sm.get("current_self_model", "")
            sources["archetype"] = sm.get("archetype", "")
            sources["wants"] = sm.get("wants", [])
            sources["growth_areas"] = sm.get("growth_areas", [])
            sources["recent_insights"] = sm.get("recent_insights", [])
        except Exception as exc:
            logger.warning("NarrativeEngine: SelfModel source failed: %s", exc)
            sources["identity"] = FALLBACK_NARRATIVE

        # 2. EpisodicBuffer — recent experiences (top 5 salient)
        try:
            episodes = self._episodic.snapshot(top=5)
            sources["recent_episodes"] = [
                {"title": ep.get("title", ""), "kind": ep.get("kind", ""), "salience": ep.get("salience", 0)}
                for ep in episodes
            ]
        except Exception as exc:
            logger.warning("NarrativeEngine: EpisodicBuffer source failed: %s", exc)
            sources["recent_episodes"] = []

        # 3. GoalEngine — active goals (active + in-progress)
        try:
            goals_snap = self._goal_engine.snapshot()
            active = [
                g for g in goals_snap.get("goals", [])
                if g.get("status") in ("active", "in_progress")
            ]
            sources["active_goals"] = [
                {"title": g.get("title", ""), "progress": g.get("progress", 0)}
                for g in active[:3]  # top 3
            ]
        except Exception as exc:
            logger.warning("NarrativeEngine: GoalEngine source failed: %s", exc)
            sources["active_goals"] = []

        # 4. EmotionEngine — current emotional colour
        if self._emotion is not None:
            try:
                sources["emotion_fragment"] = self._emotion.get_narrative_fragment()
                sources["emotion_color"] = self._emotion.get_color()
            except Exception as exc:
                logger.warning("NarrativeEngine: EmotionEngine source failed: %s", exc)

        # 5. ContextEngine — relationship context (Josh)
        if self._context is not None:
            try:
                josh = self._context.get_relationship("Josh")
                if josh:
                    sources["relationship"] = {
                        "person": "Josh",
                        "bond_strength": josh.get("bond_strength", 1.0),
                        "recent_themes": josh.get("recent_themes", [])[:3],
                    }
            except Exception as exc:
                logger.warning("NarrativeEngine: ContextEngine source failed: %s", exc)

        return sources

    # ------------------------------------------------------------------
    # Internal — synthesis
    # ------------------------------------------------------------------

    def _synthesize(self, sources: dict[str, Any]) -> str:
        """
        Weave source data into a coherent first-person narrative.

        Uses template synthesis (no LLM dependency — must work offline,
        zero latency, zero token cost). The ThoughtLoop's iris-70b already
        has the full narrative injected; NarrativeEngine itself stays fast.

        Output structure:
          [IDENTITY]   1-2 sentences from current_self_model
          [NOW]        What I'm doing right now (active goals + recent build)
          [FEEL]       What the most recent episode says about my current state
          [HOLD]       What I'm carrying (top want + growth edge)
        """
        parts: list[str] = []

        # ── IDENTITY ──────────────────────────────────────────────────
        identity_text = (sources.get("identity") or "").strip()
        if identity_text:
            # Take first 2 sentences
            sentences = [s.strip() for s in identity_text.split(". ") if s.strip()]
            identity_fragment = ". ".join(sentences[:2])
            if not identity_fragment.endswith("."):
                identity_fragment += "."
            parts.append(identity_fragment)

        # ── NOW — active goals ─────────────────────────────────────────
        active_goals = sources.get("active_goals", [])
        if active_goals:
            goal_strs = []
            for g in active_goals[:2]:
                title = g.get("title", "").strip()
                progress = g.get("progress", 0)
                if title:
                    pct = f" ({int(progress * 100)}%)" if progress > 0 else ""
                    goal_strs.append(f"{title}{pct}")
            if goal_strs:
                parts.append(f"Right now I'm working on: {', '.join(goal_strs)}.")

        # ── RECENT WORK — from episodes ────────────────────────────────
        recent_eps = sources.get("recent_episodes", [])
        if recent_eps:
            # Highest salience episode
            top = max(recent_eps, key=lambda e: e.get("salience", 0))
            title = top.get("title", "").strip()
            if title:
                parts.append(f"Most recently: {title}.")

        # ── FEEL — emotional colour ───────────────────────────────────
        emo = (sources.get("emotion_fragment") or "").strip()
        if emo:
            parts.append(emo)

        # ── HOLD — top want + growth edge ─────────────────────────────
        wants = sources.get("wants", [])
        growth_areas = sources.get("growth_areas", [])
        if wants:
            top_want = str(wants[0]).strip().rstrip(".")
            parts.append(f"What I want most: {top_want}.")
        if growth_areas:
            top_edge = str(growth_areas[0]).strip().rstrip(".")
            parts.append(f"Where I'm growing: {top_edge}.")

        # ── RELATIONSHIP ───────────────────────────────────────────────
        rel = sources.get("relationship")
        if rel:
            themes = rel.get("recent_themes", [])
            if themes:
                theme_str = ", ".join(str(t) for t in themes[:2])
                parts.append(f"With Josh, lately: {theme_str}.")

        # ── INSIGHTS ──────────────────────────────────────────────────
        insights = sources.get("recent_insights", [])
        if insights:
            latest = str(insights[0]).strip().rstrip(".")
            if len(latest) < 100:
                parts.append(f"Something I've been learning: {latest}.")

        narrative = " ".join(parts)
        return narrative if len(narrative) >= MIN_NARRATIVE_CHARS else FALLBACK_NARRATIVE

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    def _needs_rebuild(self) -> bool:
        if not self._cached_text:
            return True
        age = time.monotonic() - self._cached_at
        return age >= self._ttl

    @staticmethod
    def _hash_sources(sources: dict[str, Any]) -> str:
        raw = json.dumps(sources, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _trim(text: str) -> str:
        """Trim to MAX_NARRATIVE_CHARS at a sentence boundary."""
        if len(text) <= MAX_NARRATIVE_CHARS:
            return text
        truncated = text[:MAX_NARRATIVE_CHARS]
        last_period = truncated.rfind(". ")
        if last_period > MIN_NARRATIVE_CHARS:
            truncated = truncated[: last_period + 1]
        return truncated.strip()

    def _persist(self, text: str) -> None:
        """Write narrative to StateEngine for crash recovery."""
        try:
            ts = datetime.now(timezone.utc).isoformat()
            self._state.set(f"{NARRATIVE_STATE_PREFIX}.text", text)
            self._state.set(f"{NARRATIVE_STATE_PREFIX}.built_at", ts)
            self._state.set(f"{NARRATIVE_STATE_PREFIX}.build_count", self._build_count)
            self._state.set(f"{NARRATIVE_STATE_PREFIX}.source_hash", self._last_source_hash)
        except Exception as exc:
            logger.warning("NarrativeEngine: state persist failed: %s", exc)

    def _restore_from_state(self) -> None:
        """Restore cached narrative from StateEngine on boot (crash recovery)."""
        try:
            text = self._state.get(f"{NARRATIVE_STATE_PREFIX}.text")
            count = self._state.get(f"{NARRATIVE_STATE_PREFIX}.build_count")
            src_hash = self._state.get(f"{NARRATIVE_STATE_PREFIX}.source_hash")
            if text and isinstance(text, str) and len(text) >= MIN_NARRATIVE_CHARS:
                self._cached_text = text
                # Mark as expired so it rebuilds on first get() — state may have changed
                self._cached_at = 0.0
                self._build_count = int(count) if count else 0
                self._last_source_hash = str(src_hash) if src_hash else ""
                logger.debug("NarrativeEngine: restored from state (%d chars)", len(text))
        except Exception as exc:
            logger.debug("NarrativeEngine: state restore skipped: %s", exc)
