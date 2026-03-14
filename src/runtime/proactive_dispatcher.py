"""
ProactiveDispatcher — Pulse v2, Day 16
========================================
Closes the proactive loop: candidate → personalized message → delivery.

Day 15 (ProactiveEngine) knows *when* to reach out and *why*.
Day 16 (ProactiveDispatcher) turns that decision into an actual message
and routes it to the right channel.

Pipeline
--------
1. Ask ProactiveEngine for the top candidate (or accept one explicitly).
2. Build a proactive-specific system prompt: grounded in who Iris is *right
   now* + the trigger context (why she's reaching out).
3. Call ResponseEngine to generate the message text.
4. Route via the configured delivery mode.
5. Mark the candidate as sent (starts cooldown in ProactiveEngine).
6. Record the dispatch as an Episode with ``proactive_outreach`` kind.

Delivery modes
--------------
``response_only``
    Returns the generated message.  No side-effects beyond marking sent.
    Useful for callers who handle their own transport.

``openclaw_wake``
    Writes the message to StateEngine under ``proactive.openclaw_outbound`` so
    the Pulse daemon (or an external sender process) can pick it up and inject
    it into the current session.  Previous versions incorrectly POSTed to the
    daemon's ``/feedback`` endpoint with ``outcome="proactive_outreach"`` —
    that endpoint expects ``outcome`` in ``{success, failure}`` and is meant
    for RL-lite drive decay, NOT outbound delivery.

``store``
    Writes the generated message to StateEngine under
    ``proactive.pending_delivery``.  Allows an external process (cron, shell
    script, the daemon's next tick) to pick it up.

Design choices
--------------
- Delivery failures do NOT prevent marking the candidate as sent — a failed
  delivery still consumes the cooldown.  Better to wait than to spam.
- Thread-safe (RLock).
- Graceful degradation: if ResponseEngine / Ollama is unavailable, falls back
  to the candidate's ``message_hint`` directly.

HTTP (registered by HypostasRuntime)
--------------------------------------
  POST /runtime/proactive/deliver
    Body: {
        "mode": "response_only" | "openclaw_wake" | "store",
        "person": "josh",       (optional, default "josh")
        "max_tokens": 250       (optional)
    }
    Returns: {
        "dispatched": true|false,
        "text": "...",
        "kind": "morning_checkin",
        "mode": "response_only",
        "fallback": false,
        "episode_id": "...",
        "elapsed_ms": 312
    }
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .proactive_engine import ProactiveEngine, ProactiveCandidate
    from .response_engine import ResponseEngine
    from .episodic_buffer import EpisodicBuffer
    from .state_engine import StateEngine

logger = logging.getLogger("pulse.runtime.proactive_dispatcher")

# ---------------------------------------------------------------------------
# Delivery modes
# ---------------------------------------------------------------------------

MODE_RESPONSE_ONLY = "response_only"
MODE_OPENCLAW_WAKE = "openclaw_wake"
MODE_STORE = "store"

ALL_MODES = (MODE_RESPONSE_ONLY, MODE_OPENCLAW_WAKE, MODE_STORE)

# StateEngine key for openclaw_wake outbound queue
OPENCLAW_OUTBOUND_KEY = "proactive.openclaw_outbound"

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """Everything the caller needs to know about a dispatch attempt."""

    dispatched: bool
    text: str
    kind: str
    mode: str
    fallback: bool = False          # True when ResponseEngine used the hint directly
    episode_id: str = ""
    elapsed_ms: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# ProactiveDispatcher
# ---------------------------------------------------------------------------


class ProactiveDispatcher:
    """
    Orchestrates ProactiveEngine + ResponseEngine into actual message delivery.

    Parameters
    ----------
    proactive : ProactiveEngine
    response  : ResponseEngine
    episodic  : EpisodicBuffer  (optional — for recording dispatches)
    state     : StateEngine     (optional — for ``store`` mode)
    """

    def __init__(
        self,
        proactive: "ProactiveEngine",
        response: "ResponseEngine",
        episodic: Optional["EpisodicBuffer"] = None,
        state: Optional["StateEngine"] = None,
    ) -> None:
        self._proactive = proactive
        self._response = response
        self._episodic = episodic
        self._state = state
        self._lock = threading.RLock()
        self._dispatch_count = 0
        self._last_dispatch_ts: Optional[float] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def dispatch(
        self,
        *,
        mode: str = MODE_RESPONSE_ONLY,
        person: str = "josh",
        max_tokens: int = 250,
        candidate: Optional["ProactiveCandidate"] = None,
    ) -> DispatchResult:
        """
        Run the full dispatch pipeline.

        If ``candidate`` is not provided, the top ProactiveEngine candidate is
        used.  If there are no candidates, returns a non-dispatched result.
        """
        t0 = time.time()

        # Validate mode
        if mode not in ALL_MODES:
            return DispatchResult(
                dispatched=False, text="", kind="none", mode=mode,
                error=f"unknown mode '{mode}'; must be one of {ALL_MODES}",
            )

        # Pick candidate
        if candidate is None:
            candidate = self._proactive.top_candidate()

        if candidate is None:
            return DispatchResult(
                dispatched=False, text="", kind="none", mode=mode,
                error="no proactive candidate available",
            )

        # Generate message
        text, fallback = self._generate_message(candidate, person, max_tokens)

        # Route delivery
        error: Optional[str] = None
        episode_id = ""

        if mode == MODE_OPENCLAW_WAKE:
            error = self._deliver_openclaw(text, candidate)
        elif mode == MODE_STORE:
            self._deliver_store(text, candidate)

        # Record episode
        if self._episodic is not None:
            try:
                ep = self._episodic.record(
                    kind="proactive_outreach",
                    summary=f"Dispatched {candidate.kind} to {person}: {text[:80]}…",
                    salience=7.0,
                    tags=["proactive", candidate.kind, f"mode:{mode}"],
                )
                episode_id = ep.episode_id
            except Exception as exc:
                logger.warning("EpisodicBuffer record failed: %s", exc)

        # Mark sent (starts cooldown)
        with self._lock:
            self._proactive.mark_sent(candidate.kind)
            self._dispatch_count += 1
            self._last_dispatch_ts = time.time()

        elapsed_ms = int((time.time() - t0) * 1000)

        return DispatchResult(
            dispatched=True,
            text=text,
            kind=candidate.kind,
            mode=mode,
            fallback=fallback,
            episode_id=episode_id,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    def status(self) -> Dict[str, Any]:
        """Returns lightweight status snapshot for runtime/status endpoint."""
        with self._lock:
            return {
                "dispatch_count": self._dispatch_count,
                "last_dispatch_ts": self._last_dispatch_ts,
                "last_dispatch_iso": (
                    datetime.fromtimestamp(self._last_dispatch_ts, tz=timezone.utc).isoformat()
                    if self._last_dispatch_ts
                    else None
                ),
            }

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_message(
        self,
        candidate: "ProactiveCandidate",
        person: str,
        max_tokens: int,
    ) -> tuple[str, bool]:
        """
        Generate message text for the candidate.

        Returns (text, fallback_used).  Falls back to message_hint if
        ResponseEngine / Ollama is unavailable.
        """
        prompt = self._build_proactive_prompt(candidate)
        try:
            result = self._response.respond(
                message=prompt,
                person=person,
                max_tokens=max_tokens,
            )
            if result.fallback:
                # Ollama down — ResponseEngine gave minimal fallback; use hint instead
                return candidate.message_hint, True
            return result.text, False
        except Exception as exc:
            logger.warning("ResponseEngine unavailable, using hint: %s", exc)
            return candidate.message_hint, True

    def _build_proactive_prompt(self, candidate: "ProactiveCandidate") -> str:
        """
        Builds the instruction message passed to ResponseEngine for a proactive
        outreach.  Keeps the instruction tightly focused on the trigger context.
        """
        lines = [
            f"[PROACTIVE OUTREACH — {candidate.kind.upper()}]",
            f"Trigger: {candidate.reason}",
            f"Hint: {candidate.message_hint}",
            "",
            "Write a natural, warm outreach message in Iris's voice.",
            "Keep it concise (2-4 sentences). No explanations, no meta-commentary.",
            "Just the message itself — as if you're reaching out right now.",
        ]
        ctx = candidate.context
        if ctx:
            extras = "; ".join(f"{k}={v}" for k, v in list(ctx.items())[:4])
            lines.insert(3, f"Context: {extras}")
        return "\n".join(lines)

    def _deliver_openclaw(self, text: str, candidate: "ProactiveCandidate") -> Optional[str]:
        """
        Write the outbound message to StateEngine under ``proactive.openclaw_outbound``.

        The Pulse daemon (or an external sender process) reads this key and
        injects the message into the active session.  This avoids the previous
        bug of POSTing to ``/feedback`` with an invalid outcome value, which
        would poison the RL-lite drive-decay learner.

        Schema written to StateEngine::

            {
                "text":       str,   # generated message
                "kind":       str,   # candidate kind (e.g. "morning_checkin")
                "priority":   float, # candidate priority
                "person":     str,   # target person (resolved later by sender)
                "queued_at":  float, # epoch seconds
                "queued_at_iso": str,# ISO 8601 UTC
                "status":     "pending"
            }

        Returns error string on failure, None on success.
        """
        if self._state is None:
            return "openclaw_wake requires StateEngine but none is configured"
        try:
            now = time.time()
            entry = {
                "text": text,
                "kind": candidate.kind,
                "priority": candidate.priority,
                "person": "josh",
                "queued_at": now,
                "queued_at_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "status": "pending",
            }
            # Append to outbound queue (list of pending deliveries)
            existing: List[dict] = self._state.get(OPENCLAW_OUTBOUND_KEY) or []
            if not isinstance(existing, list):
                existing = []
            existing.append(entry)
            self._state.set(OPENCLAW_OUTBOUND_KEY, existing)
            return None
        except Exception as exc:
            return f"openclaw_wake state write failed: {exc}"

    def _deliver_store(self, text: str, candidate: "ProactiveCandidate") -> None:
        """Write pending delivery to StateEngine for external pickup."""
        if self._state is None:
            logger.debug("No StateEngine configured; store mode is a no-op.")
            return
        try:
            self._state.set("proactive.pending_delivery", {
                "text": text,
                "kind": candidate.kind,
                "priority": candidate.priority,
                "stored_at": time.time(),
                "stored_at_iso": datetime.now(tz=timezone.utc).isoformat(),
            })
        except Exception as exc:
            logger.warning("StateEngine store failed: %s", exc)
