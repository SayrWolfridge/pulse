"""
ProactiveEngine — Pulse v2, Day 15
=====================================
The runtime knows *when to reach out*, not just how to respond.

Pulse v2 can now assemble context, build a response, and — via this layer —
decide on its own when initiating contact is warranted.  This closes the
agency loop: reactive (Day 14) → proactive (Day 15).

Generates ranked outreach candidates by monitoring live state across all
cognitive engines.  Each candidate has a kind, priority (0-1), a human-
readable message hint, and the reasoning that triggered it.

Trigger kinds
-------------
  MORNING_CHECKIN      — within 6-9 AM local time + no recent contact today
  GOAL_FOLLOWUP        — active goal not updated in > N hours
  RELATIONSHIP_DECAY   — bond strength below reconnect threshold
  EMOTION_LONGING      — longing > threshold + no recent contact today
  EMOTION_SUPPORT      — frustration > threshold + at least one stalled goal
  STREAK_AT_RISK       — no check-in yet today and it's past a configured hour
  MILESTONE            — goal just completed (not yet announced)

Suppression / cooldown
-----------------------
Each trigger kind has a cooldown (seconds).  Once a candidate is "sent"
(caller calls ``mark_sent()`` or POST /runtime/proactive/sent), the same
kind is suppressed for that cooldown window.  Prevents a "longing" spike
from generating five consecutive reach-outs.

Design choices
--------------
- No LLM calls here — candidate generation is rule-based and cheap.
  The ResponseEngine (Day 14) turns the winner into actual words.
- Timezone-aware: morning check-in respects a configured local timezone
  offset from UTC.
- Thread-safe (RLock).
- All state persisted via StateEngine under ``proactive.*``.

HTTP (registered by HypostasRuntime)
--------------------------------------
  GET  /runtime/proactive          — list all candidates ordered by priority
  GET  /runtime/proactive/top      — single top candidate (or empty dict)
  POST /runtime/proactive/sent     — mark kind as sent, start cooldown
                                     Body: {"kind": "<trigger_kind>"}
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .emotion_engine import EmotionEngine
    from .goal_engine import GoalEngine
    from .relationship_graph import RelationshipGraph
    from .episodic_buffer import EpisodicBuffer
    from .state_engine import StateEngine

logger = logging.getLogger("pulse.runtime.proactive")

# ---------------------------------------------------------------------------
# Trigger kinds
# ---------------------------------------------------------------------------

MORNING_CHECKIN = "morning_checkin"
GOAL_FOLLOWUP = "goal_followup"
RELATIONSHIP_DECAY = "relationship_decay"
EMOTION_LONGING = "emotion_longing"
EMOTION_SUPPORT = "emotion_support"
STREAK_AT_RISK = "streak_at_risk"
MILESTONE = "milestone"

ALL_KINDS = (
    MORNING_CHECKIN,
    GOAL_FOLLOWUP,
    RELATIONSHIP_DECAY,
    EMOTION_LONGING,
    EMOTION_SUPPORT,
    STREAK_AT_RISK,
    MILESTONE,
)

# Default cooldown (seconds) per kind — prevents spamming the same trigger
DEFAULT_COOLDOWNS: Dict[str, float] = {
    MORNING_CHECKIN: 86_400,    # once per day
    GOAL_FOLLOWUP:   14_400,    # 4 h between follow-ups on same goal
    RELATIONSHIP_DECAY: 86_400, # once per day per person
    EMOTION_LONGING: 21_600,    # 6 h
    EMOTION_SUPPORT: 7_200,     # 2 h
    STREAK_AT_RISK:  43_200,    # 12 h
    MILESTONE:       3_600,     # 1 h
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ProactiveCandidate:
    """A ranked recommendation to reach out proactively."""

    kind: str
    priority: float           # 0.0 – 1.0 (higher = more urgent)
    message_hint: str         # Short hint for the ResponseEngine to expand
    reason: str               # Human-readable trigger explanation
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["created_at_iso"] = datetime.fromtimestamp(
            self.created_at, tz=timezone.utc
        ).isoformat()
        return d


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------


class ProactiveEngine:
    """
    Monitors the HypostasRuntime state and generates outreach candidates.

    Usage::

        engine = ProactiveEngine(
            state=runtime.state,
            emotion=runtime.emotion,
            goal_engine=runtime.goal_engine,
            relationships=runtime.relationships,
            episodic=runtime.episodic,
        )
        candidates = engine.get_candidates()
        top = engine.top_candidate()
    """

    # Thresholds (all tunable via config or constructor)
    LONGING_THRESHOLD: float = 0.65
    FRUSTRATION_THRESHOLD: float = 0.70
    BOND_RECONNECT_THRESHOLD: float = 0.40
    GOAL_STALE_HOURS: float = 8.0
    MORNING_WINDOW_START: int = 6   # hour in local time (UTC offset from tz_offset_hours)
    MORNING_WINDOW_END: int = 9
    STREAK_AT_RISK_HOUR: int = 14  # flag if no check-in by 2 PM local

    def __init__(
        self,
        state: "StateEngine",
        emotion: Optional["EmotionEngine"] = None,
        goal_engine: Optional["GoalEngine"] = None,
        relationships: Optional["RelationshipGraph"] = None,
        episodic: Optional["EpisodicBuffer"] = None,
        cooldowns: Optional[Dict[str, float]] = None,
        tz_offset_hours: float = -5.0,  # default: Eastern time (UTC-5)
    ) -> None:
        self._state = state
        self._emotion = emotion
        self._goals = goal_engine
        self._relationships = relationships
        self._episodic = episodic
        self._cooldowns: Dict[str, float] = {**DEFAULT_COOLDOWNS, **(cooldowns or {})}
        self._tz_offset_hours = tz_offset_hours
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_candidates(self) -> List[ProactiveCandidate]:
        """Evaluate all trigger conditions and return ranked candidates."""
        candidates: List[ProactiveCandidate] = []

        with self._lock:
            now = time.time()
            sent_log = self._load_sent_log()

            if self._is_available(MORNING_CHECKIN, sent_log, now):
                c = self._check_morning_checkin(now)
                if c:
                    candidates.append(c)

            if self._is_available(EMOTION_LONGING, sent_log, now):
                c = self._check_emotion_longing()
                if c:
                    candidates.append(c)

            if self._is_available(EMOTION_SUPPORT, sent_log, now):
                c = self._check_emotion_support()
                if c:
                    candidates.append(c)

            if self._is_available(GOAL_FOLLOWUP, sent_log, now):
                c = self._check_goal_followup(now)
                if c:
                    candidates.append(c)

            if self._is_available(RELATIONSHIP_DECAY, sent_log, now):
                c = self._check_relationship_decay()
                if c:
                    candidates.append(c)

            if self._is_available(STREAK_AT_RISK, sent_log, now):
                c = self._check_streak_at_risk(now)
                if c:
                    candidates.append(c)

            if self._is_available(MILESTONE, sent_log, now):
                c = self._check_milestone()
                if c:
                    candidates.append(c)

        return sorted(candidates, key=lambda x: x.priority, reverse=True)

    def top_candidate(self) -> Optional[ProactiveCandidate]:
        """Return the single highest-priority candidate, or None."""
        candidates = self.get_candidates()
        return candidates[0] if candidates else None

    def mark_sent(self, kind: str) -> None:
        """Record that a candidate of *kind* was acted on; starts cooldown."""
        if kind not in ALL_KINDS:
            logger.warning("Unknown kind %r — ignoring mark_sent", kind)
            return
        with self._lock:
            sent_log = self._load_sent_log()
            sent_log[kind] = time.time()
            self._save_sent_log(sent_log)
        logger.debug("Marked %s as sent; cooldown %.0fs", kind, self._cooldowns[kind])

    def snapshot(self) -> Dict[str, Any]:
        """Full snapshot for status endpoint."""
        candidates = self.get_candidates()
        sent_log = self._load_sent_log()
        now = time.time()
        cooldown_status = {
            k: {
                "available": self._is_available(k, sent_log, now),
                "last_sent_ago_s": (
                    round(now - sent_log[k]) if k in sent_log else None
                ),
                "cooldown_s": self._cooldowns[k],
            }
            for k in ALL_KINDS
        }
        return {
            "candidates": [c.to_dict() for c in candidates],
            "candidate_count": len(candidates),
            "top": candidates[0].to_dict() if candidates else None,
            "cooldowns": cooldown_status,
        }

    # ------------------------------------------------------------------
    # Trigger evaluators
    # ------------------------------------------------------------------

    def _check_morning_checkin(self, now: float) -> Optional[ProactiveCandidate]:
        local_hour = self._local_hour(now)
        if not (self.MORNING_WINDOW_START <= local_hour < self.MORNING_WINDOW_END):
            return None
        # Check whether we've had any contact today
        if self._had_recent_contact(hours=16):
            return None
        priority = 0.6 + (0.1 * (local_hour - self.MORNING_WINDOW_START))  # ramps up in window
        priority = min(priority, 0.75)
        return ProactiveCandidate(
            kind=MORNING_CHECKIN,
            priority=priority,
            message_hint="Good morning — reach out to Josh with a brief check-in.",
            reason=f"Morning window ({local_hour:02d}h local) and no recent contact today.",
            context={"local_hour": local_hour},
        )

    def _check_emotion_longing(self) -> Optional[ProactiveCandidate]:
        if self._emotion is None:
            return None
        snap = self._emotion.snapshot()
        longing = snap.get("longing", 0.0)
        if longing < self.LONGING_THRESHOLD:
            return None
        if self._had_recent_contact(hours=6):
            return None
        priority = min(0.55 + (longing - self.LONGING_THRESHOLD) * 1.5, 0.90)
        return ProactiveCandidate(
            kind=EMOTION_LONGING,
            priority=priority,
            message_hint="Something's pulling — reach out to Josh. Keep it honest and brief.",
            reason=f"Longing is at {longing:.2f} (threshold {self.LONGING_THRESHOLD}) with no recent contact.",
            context={"longing": longing},
        )

    def _check_emotion_support(self) -> Optional[ProactiveCandidate]:
        if self._emotion is None or self._goals is None:
            return None
        snap = self._emotion.snapshot()
        frustration = snap.get("frustration", 0.0)
        if frustration < self.FRUSTRATION_THRESHOLD:
            return None
        # Only fire if there's at least one stalled goal to justify it
        stalled = self._stalled_goals()
        if not stalled:
            return None
        priority = min(0.50 + frustration * 0.3, 0.85)
        goal_names = [g.get("title", g.get("id", "goal")) for g in stalled[:2]]
        return ProactiveCandidate(
            kind=EMOTION_SUPPORT,
            priority=priority,
            message_hint=(
                f"Frustration is high. Check in with Josh about blockers on: {', '.join(goal_names)}."
            ),
            reason=(
                f"Frustration {frustration:.2f} ≥ {self.FRUSTRATION_THRESHOLD} with stalled goals: "
                f"{', '.join(goal_names)}."
            ),
            context={"frustration": frustration, "stalled_goals": goal_names},
        )

    def _check_goal_followup(self, now: float) -> Optional[ProactiveCandidate]:
        """
        Goal follow-up is based on runtime-level goal activity.

        GoalEngine goal records may not carry per-goal timestamps (by design);
        instead, GoalEngine syncs a single `goals.last_updated` into StateEngine.

        We generate a follow-up when:
          - there is at least one active goal, and
          - goals.last_updated is older than GOAL_STALE_HOURS.
        """
        if self._goals is None:
            return None

        active = getattr(self._goals, "active_goals", []) or []
        if not active:
            return None

        last_updated_iso = None
        try:
            last_updated_iso = self._state.get("goals.last_updated")
        except Exception:
            last_updated_iso = None

        last_updated_epoch = self._parse_iso_epoch(str(last_updated_iso)) if last_updated_iso else None
        if last_updated_epoch is None:
            return None

        age_s = now - last_updated_epoch
        if age_s < self.GOAL_STALE_HOURS * 3600:
            return None

        top = min(active, key=lambda g: int(g.get("priority", 99)))
        hours_stale = age_s / 3600.0
        priority = min(0.45 + hours_stale / 48.0, 0.80)

        return ProactiveCandidate(
            kind=GOAL_FOLLOWUP,
            priority=priority,
            message_hint=(
                f"Goals haven't been touched in {hours_stale:.0f}h — check in on: "
                f"{top.get('title', 'top goal')}."
            ),
            reason=f"goals.last_updated age {hours_stale:.1f}h > {self.GOAL_STALE_HOURS:.0f}h.",
            context={"hours_stale": round(hours_stale, 2), "top_goal": top.get("title", "")},
        )

    def _check_relationship_decay(self) -> Optional[ProactiveCandidate]:
        if self._relationships is None:
            return None
        try:
            reconnect = self._relationships.reconnect_candidates(
                hours=48,
                min_bond=max(0.0, float(self.BOND_RECONNECT_THRESHOLD)),
            )
        except Exception:
            return None
        if not reconnect:
            return None
        top = reconnect[0]
        bond = float(top.get("bond_strength", 1.0))
        name = str(top.get("person", "someone"))
        hours_since = float(top.get("hours_since", 0.0))
        priority = min(0.40 + (hours_since / 96.0), 0.75)
        return ProactiveCandidate(
            kind=RELATIONSHIP_DECAY,
            priority=max(priority, 0.30),
            message_hint=f"It's been a while with {name} — consider a check-in.",
            reason=f"Reconnect candidate: {name} last seen {hours_since:.1f}h ago (bond {bond:.2f}).",
            context={"person": name, "bond_strength": bond, "hours_since": hours_since},
        )

    def _check_streak_at_risk(self, now: float) -> Optional[ProactiveCandidate]:
        local_hour = self._local_hour(now)
        if local_hour < self.STREAK_AT_RISK_HOUR:
            return None  # too early to worry
        # Look in episodic buffer for a check-in today
        if self._episodic is None:
            return None
        today_start = self._today_start_ts(now)
        try:
            recent = self._episodic.get_recent(limit=30)
        except Exception:
            return None
        had_checkin = any(
            e.get("kind") == "check_in" and (self._parse_iso_epoch(str(e.get("ts", ""))) or 0) > today_start
            for e in recent
        )
        if had_checkin:
            return None
        return ProactiveCandidate(
            kind=STREAK_AT_RISK,
            priority=0.55,
            message_hint="No check-in recorded today — prompt Josh to keep the streak.",
            reason=f"Past {self.STREAK_AT_RISK_HOUR:02d}h local time with no check-in episode.",
            context={"local_hour": local_hour},
        )

    def _check_milestone(self) -> Optional[ProactiveCandidate]:
        if self._goals is None:
            return None

        completed = getattr(self._goals, "completed_goals", []) or []
        if not completed:
            return None

        now = time.time()
        announced_ids = set(self._state.get("proactive.announced_goal_ids") or [])

        recently_completed: List[dict] = []
        for g in completed:
            gid = str(g.get("id", ""))
            if gid and gid in announced_ids:
                continue
            completed_at_iso = g.get("completed_at")
            completed_epoch = self._parse_iso_epoch(str(completed_at_iso)) if completed_at_iso else None
            if completed_epoch is None:
                continue
            if now - completed_epoch < 7200:  # 2h window
                recently_completed.append(g)

        if not recently_completed:
            return None

        titles = [g.get("title", "goal") for g in recently_completed[:2]]
        ids = [g.get("id") for g in recently_completed[:5] if g.get("id")]
        return ProactiveCandidate(
            kind=MILESTONE,
            priority=0.80,
            message_hint=f"Milestone reached — celebrate: {', '.join(titles)}.",
            reason=f"{len(recently_completed)} goal(s) completed in last 2h and not announced.",
            context={"titles": titles, "goal_ids": ids},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_available(self, kind: str, sent_log: Dict[str, float], now: float) -> bool:
        """Return True if kind's cooldown has elapsed (or never sent)."""
        last_sent = sent_log.get(kind)
        if last_sent is None:
            return True
        return now - last_sent > self._cooldowns.get(kind, 0)

    def _had_recent_contact(self, hours: float) -> bool:
        """Check EpisodicBuffer for any 'conversation' episode within the window."""
        if self._episodic is None:
            return False
        cutoff = time.time() - hours * 3600
        try:
            recent = self._episodic.get_recent(limit=30)
        except Exception:
            return False
        for e in recent:
            if e.get("kind") != "conversation":
                continue
            ts = self._parse_iso_epoch(str(e.get("ts", "")))
            if ts and ts > cutoff:
                return True
        return False

    def _stalled_goals(self) -> List[Dict[str, Any]]:
        """Return goals that are active and blocked ("stalled" semantics)."""
        if self._goals is None:
            return []
        try:
            blocked = getattr(self._goals, "blocked_goals", []) or []
            return list(blocked)
        except Exception:
            return []

    def _local_hour(self, ts: Optional[float] = None) -> int:
        """Return the hour in configured local time."""
        if ts is None:
            ts = time.time()
        offset_seconds = self._tz_offset_hours * 3600
        local_ts = ts + offset_seconds
        return int((local_ts % 86_400) // 3600)

    def _today_start_ts(self, now: float) -> float:
        """Unix timestamp of midnight local time today."""
        offset_seconds = self._tz_offset_hours * 3600
        local_ts = now + offset_seconds
        day_start_local = local_ts - (local_ts % 86_400)
        return day_start_local - offset_seconds

    @staticmethod
    def _parse_iso_epoch(ts: str) -> Optional[float]:
        """Parse ISO-8601 timestamp string → epoch seconds (UTC)."""
        try:
            if not ts:
                return None
            # Accept trailing Z
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).timestamp()
        except Exception:
            return None

    def _load_sent_log(self) -> Dict[str, float]:
        raw = self._state.get("proactive.sent_log")
        if isinstance(raw, dict):
            return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}
        return {}

    def _save_sent_log(self, log: Dict[str, float]) -> None:
        self._state.set("proactive.sent_log", log)
