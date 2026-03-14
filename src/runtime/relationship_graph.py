"""
RelationshipGraph — Pulse v2, Day 12
===================================
A first-class relationship cognition layer for the HypostasRuntime.

ContextEngine already persists per-person relationship JSON files (RelationshipTier).
This module adds *semantics* on top:

- Bond-strength decay over time (absence weakens bonds toward a baseline)
- Structured interaction recording (themes, threads, notes)
- Reconnect candidate detection ("who needs attention?")
- EmotionEngine integration for high-salience relationship events
- Narrative fragments for NarrativeEngine / ThoughtLoop

Design constraints:
- Zero LLM dependency (fast, offline-capable)
- Backwards-compatible with existing RelationshipTier JSON structure
- Thread-safe

StateEngine keys written:
  relationships.last_decay_ts      — ISO timestamp of last decay sweep
  relationships.last_absence_event — per-person ISO timestamp to avoid spam

HTTP endpoints (registered by HypostasRuntime):
  GET  /runtime/relationships              — snapshot (sorted by bond)
  GET  /runtime/relationships/reconnect    — reconnect candidates
  POST /runtime/relationships/event        — record interaction
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .context_engine import ContextEngine
    from .state_engine import StateEngine
    from .emotion_engine import EmotionEngine

logger = logging.getLogger("pulse.runtime.relationship_graph")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        # Accept trailing Z
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Relationship decay model
# ---------------------------------------------------------------------------

# Default baselines (where bonds drift toward in absence)
BASELINE_BY_TIER: Dict[str, float] = {
    "primary": 0.85,
    "close": 0.60,
    "standard": 0.40,
    "weak": 0.25,
}

# Half-life in seconds by tier (time for bond distance-to-baseline to halve)
HALF_LIFE_BY_TIER: Dict[str, float] = {
    "primary": 7 * 24 * 3600,    # 7 days
    "close": 3 * 24 * 3600,      # 3 days
    "standard": 24 * 3600,       # 1 day
    "weak": 12 * 3600,           # 12 hours
}

# When to fire an absence event for primary bond
PRIMARY_ABSENCE_SECONDS: float = 18 * 3600  # 18 hours
ABSENCE_EVENT_COOLDOWN_SECONDS: float = 6 * 3600


class RelationshipGraph:
    """Relationship semantics layer atop ContextEngine.RelationshipTier."""

    _STATE_PREFIX = "relationships"

    def __init__(
        self,
        *,
        context: "ContextEngine",
        state: Optional["StateEngine"] = None,
        emotion: Optional["EmotionEngine"] = None,
    ) -> None:
        self._context = context
        self._state = state
        self._emotion = emotion
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Core accessors
    # ------------------------------------------------------------------

    def get(self, person: str, *, apply_decay: bool = True) -> dict:
        with self._lock:
            data = self._context.get_relationship(person) or {}
            if apply_decay and data:
                data = self._apply_decay_to_record(person, data)
            return data

    def all(self, *, apply_decay: bool = True) -> Dict[str, dict]:
        with self._lock:
            rels = self._context.get_all_relationships() or {}
            if apply_decay:
                out: Dict[str, dict] = {}
                for person, data in rels.items():
                    out[person] = self._apply_decay_to_record(person, data)
                return out
            return rels

    def snapshot(self, *, top: int = 20, apply_decay: bool = True) -> dict:
        """Return relationships sorted by bond_strength."""
        rels = list(self.all(apply_decay=apply_decay).values())
        rels.sort(key=lambda d: float(d.get("bond_strength", 0.0)), reverse=True)
        return {
            "count": len(rels),
            "relationships": rels[: max(0, int(top))],
            "built_at": _now_iso(),
        }

    def status(self) -> dict:
        """Quick status snapshot for runtime.status()."""
        snap = self.snapshot(top=3, apply_decay=True)
        top = (snap.get("relationships") or [])
        return {
            "count": snap.get("count", 0),
            "top": [
                {"person": r.get("person"), "bond_strength": r.get("bond_strength", 0.0)}
                for r in top
            ],
            "reconnect_candidates": len(self.reconnect_candidates()),
        }

    # ------------------------------------------------------------------
    # Interaction recording
    # ------------------------------------------------------------------

    def record_event(
        self,
        *,
        person: str,
        kind: str = "message",
        note: str = "",
        themes: Optional[List[str]] = None,
        delta_bond: Optional[float] = None,
        tier: Optional[str] = None,
    ) -> dict:
        """Record a relationship interaction and return updated record."""
        with self._lock:
            cur = self.get(person, apply_decay=True) or {}
            cur_bond = float(cur.get("bond_strength", 0.5))

            # Default bond delta by kind
            if delta_bond is None:
                delta_bond = {
                    "message": 0.02,
                    "call": 0.05,
                    "intimate": 0.08,
                    "conflict": -0.06,
                    "helped": 0.04,
                }.get(kind, 0.02)

            new_bond = _clamp(cur_bond + float(delta_bond))

            merged_themes: List[str] = list(cur.get("recent_themes") or [])
            if themes:
                for t in themes:
                    t = str(t).strip()
                    if not t:
                        continue
                    if t in merged_themes:
                        merged_themes.remove(t)
                    merged_themes.append(t)
                merged_themes = merged_themes[-20:]

            event: dict = {
                "kind": kind,
                "note": note.strip() if note else "",
                "bond_strength": new_bond,
                "recent_themes": merged_themes,
            }
            if tier:
                event["tier"] = tier

            # Persist via ContextEngine (writes relationship file + hot log)
            self._context.update_relationship(person, event)

            # Emotion integration (high-salience relationships)
            if self._emotion is not None:
                try:
                    if person.lower() == "josh":
                        if kind == "intimate":
                            self._emotion.apply_event("INTIMATE_MOMENT", note=note)
                        else:
                            self._emotion.apply_event("JOSH_MESSAGE", note=note)
                except Exception as exc:
                    logger.warning("EmotionEngine apply_event failed: %s", exc)

            return self.get(person, apply_decay=False) or {}

    # ------------------------------------------------------------------
    # Decay + reconnect
    # ------------------------------------------------------------------

    def decay_sweep(self) -> dict:
        """Apply decay to all known relationships."""
        with self._lock:
            rels = self._context.get_all_relationships() or {}
            changed = 0
            for person, data in rels.items():
                new = self._apply_decay_to_record(person, data)
                # _apply_decay_to_record persists if changed
                if new.get("_decay_changed"):
                    changed += 1
            if self._state is not None:
                self._state.set(f"{self._STATE_PREFIX}.last_decay_ts", _now_iso())
            return {"ok": True, "changed": changed, "ts": _now_iso()}

    def reconnect_candidates(self, *, hours: int = 48, min_bond: float = 0.55) -> List[dict]:
        """Return people with meaningful bond but stale last_seen."""
        now = datetime.now(timezone.utc)
        out: List[dict] = []
        for person, data in self.all(apply_decay=True).items():
            bond = float(data.get("bond_strength", 0.0))
            if bond < min_bond:
                continue
            last_seen = _parse_iso(str(data.get("last_seen", "")))
            if not last_seen:
                continue
            age_h = (now - last_seen).total_seconds() / 3600.0
            if age_h >= float(hours):
                out.append({"person": person, "bond_strength": bond, "hours_since": age_h})
        out.sort(key=lambda r: (r["hours_since"], -r["bond_strength"]))
        return out

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def narrative_fragment(self, person: str) -> str:
        """A short relationship sentence for NarrativeEngine."""
        data = self.get(person, apply_decay=True)
        if not data:
            return ""
        bond = float(data.get("bond_strength", 0.0))
        themes = data.get("recent_themes") or []
        if themes:
            return f"With {person}, lately: {', '.join(str(t) for t in themes[:2])}."
        if bond >= 0.8:
            return f"{person} feels close right now (bond {bond:.2f})."
        return f"{person} is part of my life (bond {bond:.2f})."

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply_decay_to_record(self, person: str, data: dict) -> dict:
        """Compute decay and persist if bond changed materially."""
        try:
            bond = float(data.get("bond_strength", 0.5))
        except Exception:
            bond = 0.5

        tier = str(data.get("tier") or "").strip().lower() or self._infer_tier(bond)
        baseline = BASELINE_BY_TIER.get(tier, BASELINE_BY_TIER["standard"])
        half_life = HALF_LIFE_BY_TIER.get(tier, HALF_LIFE_BY_TIER["standard"])

        last_seen = _parse_iso(str(data.get("last_seen", "")))
        if not last_seen:
            return data

        age_s = max(0.0, (datetime.now(timezone.utc) - last_seen).total_seconds())
        if age_s < 60.0:  # < 1 minute, skip
            return data

        # exponential drift toward baseline
        decay_factor = math.pow(0.5, age_s / max(1.0, half_life))
        new_bond = baseline + (bond - baseline) * decay_factor
        new_bond = _clamp(float(new_bond))

        if abs(new_bond - bond) >= 0.01:
            # Persist updated bond strength
            try:
                self._context.update_relationship(person, {"bond_strength": new_bond, "_touch": False, "_log": False})
                data = self._context.get_relationship(person) or data
                data["_decay_changed"] = True
            except Exception as exc:
                logger.warning("Relationship decay persist failed: %s", exc)

        # Absence events for Josh (rate-limited)
        if self._emotion is not None and person.lower() == "josh" and tier == "primary":
            if age_s >= PRIMARY_ABSENCE_SECONDS:
                self._maybe_fire_absence_event(person, age_s)

        return data

    def _maybe_fire_absence_event(self, person: str, age_s: float) -> None:
        if self._state is None:
            return
        key = f"{self._STATE_PREFIX}.last_absence_event.{person.lower()}"
        last = self._state.get(key)
        last_dt = _parse_iso(str(last)) if last else None
        if last_dt:
            since = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if since < ABSENCE_EVENT_COOLDOWN_SECONDS:
                return
        try:
            self._emotion.apply_event(
                "JOSH_ABSENT_LONG",
                note=f"Josh absent ~{int(age_s/3600)}h",
            )
            self._state.set(key, _now_iso())
        except Exception as exc:
            logger.warning("Absence emotion event failed: %s", exc)

    @staticmethod
    def _infer_tier(bond: float) -> str:
        if bond >= 0.80:
            return "primary"
        if bond >= 0.65:
            return "close"
        if bond >= 0.45:
            return "standard"
        return "weak"
