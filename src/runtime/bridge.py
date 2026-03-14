"""
RuntimeBridge — Connects Pulse v2 runtime to the existing Pulse daemon.

The bridge is the backward-compatibility layer. The existing daemon continues
to run exactly as before; the bridge adds:
  - Hot-tier logging of every trigger start/end and feedback event
  - ThoughtLoop session gate notification (backs off during active sessions)
  - Context injection: enriches trigger messages with current cognitive state

Usage (from HypostasRuntime.start()):
    bridge = RuntimeBridge(self)
    bridge.attach(daemon)   # wires into daemon EventBus, idempotent

The daemon gains an optional .runtime_bridge attribute. _trigger_turn() calls
bridge.format_context_for_prompt() to append a [RUNTIME: ...] tag.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pulse.src.runtime import HypostasRuntime

logger = logging.getLogger("pulse.runtime.bridge")


class RuntimeBridge:
    """
    Connects HypostasRuntime (StateEngine + ContextEngine + ThoughtLoop) to
    the existing PulseDaemon via its EventBus.

    All hooks are additive — daemon behaviour is unchanged when bridge is absent.
    Attach with bridge.attach(daemon); detach is not required (process lifetime).
    """

    def __init__(self, runtime: "HypostasRuntime"):
        self.runtime = runtime
        self._attached = False
        self._session_start_ts: Optional[float] = None
        self._daemon = None

    # ─────────────────────────── ATTACH / DETACH ─────────────────────────────

    def attach(self, daemon) -> None:
        """
        Wire bridge hooks into an existing PulseDaemon.

        - Subscribes to TRIGGER_START, TRIGGER_SUCCESS, TRIGGER_FAILURE on bus
        - Registers self on daemon.runtime_bridge for context injection calls
        - Idempotent: safe to call multiple times
        """
        if self._attached:
            return

        from pulse.src.core.events import TRIGGER_START, TRIGGER_SUCCESS, TRIGGER_FAILURE

        daemon.bus.on(TRIGGER_START, self.on_trigger_start)
        daemon.bus.on(TRIGGER_SUCCESS, self.on_trigger_end)
        daemon.bus.on(TRIGGER_FAILURE, self.on_trigger_end)

        # Let daemon reference us for context injection in _trigger_turn
        daemon.runtime_bridge = self
        self._daemon = daemon
        self._attached = True

        logger.info("RuntimeBridge attached to PulseDaemon ✓")

    # ──────────────────────────── CONTEXT INJECTION ──────────────────────────

    def inject_context_into_session(self) -> dict:
        """
        Returns a context package for injection into Pulse session prompts.

        Called by daemon._trigger_turn() to enrich trigger messages with the
        current cognitive state + recent context. Returns {} on any failure so
        it never blocks a trigger.
        """
        try:
            ctx: dict = {}

            # Emotional state from StateEngine
            try:
                es = self.runtime.state.get("emotional_state")
                if es:
                    ctx["emotional_state"] = {
                        "valence": es.get("valence"),
                        "dominant_emotion": es.get("dominant_emotion"),
                    }
            except Exception as e:
                logger.debug(f"emotional_state unavailable: {e}")

            # Active working focus
            try:
                focus = self.runtime.state.get("working_memory.active_focus")
                if focus:
                    ctx["active_focus"] = str(focus)
            except Exception as e:
                logger.debug(f"active_focus unavailable: {e}")

            # Recent hot-tier events (last 2 h, capped at 10)
            try:
                recent = self.runtime.context.get_recent_context(hours=2)
                if recent:
                    ctx["recent_context"] = recent[:10]
            except Exception as e:
                logger.debug(f"recent_context unavailable: {e}")

            # Josh relationship — lightweight subset only
            try:
                josh = self.runtime.context.get_relationship("josh")
                if josh:
                    ctx["relationship_josh"] = {
                        "last_message_at": josh.get("last_message_at"),
                        "communication_style": josh.get("communication_style"),
                        "pending_for_iris": (josh.get("pending_for_iris") or [])[:3],
                    }
            except Exception as e:
                logger.debug(f"relationship_josh unavailable: {e}")

            # Open loops — top 3 by priority
            try:
                loops = self.runtime.state.get("working_memory.open_loops")
                if loops:
                    ctx["open_loops"] = [
                        {
                            "description": l.get("description"),
                            "priority": l.get("priority"),
                        }
                        for l in loops[:3]
                        if isinstance(l, dict)
                    ]
            except Exception as e:
                logger.debug(f"open_loops unavailable: {e}")

            return ctx

        except Exception as e:
            logger.warning(f"inject_context_into_session failed: {e}")
            return {}

    def format_context_for_prompt(self) -> str:
        """
        Returns a formatted [RUNTIME: ...] tag for appending to trigger messages.

        Keeps the tag short — one line, three fields max. Empty string when
        there's nothing meaningful to add (so trigger messages stay clean).
        """
        try:
            ctx = self.inject_context_into_session()
            if not ctx:
                return ""

            parts = []

            if "emotional_state" in ctx:
                es = ctx["emotional_state"]
                emotion = es.get("dominant_emotion") or ""
                valence = es.get("valence")
                if emotion:
                    v_str = f",v={valence:.2f}" if valence is not None else ""
                    parts.append(f"mood={emotion}{v_str}")

            if "active_focus" in ctx:
                parts.append(f"focus={ctx['active_focus'][:60]}")

            if "open_loops" in ctx and ctx["open_loops"]:
                desc = (ctx["open_loops"][0].get("description") or "")[:60]
                if desc:
                    parts.append(f"top_loop={desc}")

            if not parts:
                return ""

            return "\n\n[RUNTIME: " + " | ".join(parts) + "]"

        except Exception as e:
            logger.warning(f"format_context_for_prompt failed: {e}")
            return ""

    # ─────────────────────────── EVENT HANDLERS ──────────────────────────────

    def on_trigger_start(self, decision, **kwargs) -> None:
        """
        Called when TRIGGER_START fires (before webhook dispatch).

        - Marks ThoughtLoop session gate (backs off for 2 min)
        - Logs trigger_start event to hot tier
        - Records session start timestamp for duration tracking
        """
        try:
            self._session_start_ts = time.time()

            # Gate ThoughtLoop
            try:
                self.runtime.thought_loop.notify_session_start()
            except Exception as e:
                logger.debug(f"ThoughtLoop.notify_session_start failed: {e}")

            # Log to hot tier
            try:
                reason = getattr(decision, "reason", "unknown")
                pressure = getattr(decision, "total_pressure", 0.0)
                top_drive = (
                    decision.top_drive.name
                    if hasattr(decision, "top_drive") and decision.top_drive
                    else None
                )
                self.runtime.context.log_event(
                    {
                        "type": "pulse_trigger_start",
                        "content": (
                            f"Trigger fired: {reason} "
                            f"(pressure={pressure:.2f}, drive={top_drive})"
                        ),
                        "metadata": {
                            "reason": reason,
                            "pressure": round(pressure, 4),
                            "top_drive": top_drive,
                        },
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "source": "pulse_daemon",
                    }
                )
            except Exception as e:
                logger.debug(f"Hot-tier log in on_trigger_start failed: {e}")

        except Exception as e:
            logger.warning(f"on_trigger_start error: {e}")

    def on_trigger_end(self, decision, success: bool, turn: int = 0, **kwargs) -> None:
        """
        Called on TRIGGER_SUCCESS or TRIGGER_FAILURE.

        - Clears ThoughtLoop session gate
        - Logs trigger_end event (with duration) to hot tier
        """
        try:
            # Clear ThoughtLoop gate
            try:
                self.runtime.thought_loop.notify_session_end()
            except Exception as e:
                logger.debug(f"ThoughtLoop.notify_session_end failed: {e}")

            # Calculate session duration
            duration_s: Optional[float] = None
            if self._session_start_ts is not None:
                duration_s = round(time.time() - self._session_start_ts, 1)
                self._session_start_ts = None

            # Log to hot tier
            try:
                reason = getattr(decision, "reason", "unknown")
                pressure = getattr(decision, "total_pressure", 0.0)
                outcome = "success" if success else "failure"
                self.runtime.context.log_event(
                    {
                        "type": "pulse_trigger_end",
                        "content": (
                            f"Trigger {outcome}: {reason} "
                            f"(turn #{turn}, duration={duration_s}s)"
                        ),
                        "metadata": {
                            "reason": reason,
                            "pressure": round(pressure, 4),
                            "success": success,
                            "turn": turn,
                            "duration_seconds": duration_s,
                        },
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "source": "pulse_daemon",
                    }
                )
            except Exception as e:
                logger.debug(f"Hot-tier log in on_trigger_end failed: {e}")

        except Exception as e:
            logger.warning(f"on_trigger_end error: {e}")

    def on_feedback_received(self, feedback: dict) -> None:
        """
        Called after the agent posts a feedback payload.

        - Logs to hot tier
        - Adds summary as an insight to StateEngine (if outcome=success)
        """
        try:
            drives_addressed = feedback.get("drives_addressed", [])
            outcome = feedback.get("outcome", "success")
            summary = feedback.get("summary", "")

            # Hot tier
            try:
                self.runtime.context.log_event(
                    {
                        "type": "pulse_feedback",
                        "content": f"Feedback ({outcome}): {summary[:120]}",
                        "metadata": {
                            "drives_addressed": drives_addressed,
                            "outcome": outcome,
                        },
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "source": "pulse_agent",
                    }
                )
            except Exception as e:
                logger.debug(f"Hot-tier log in on_feedback_received failed: {e}")

            # Insight into StateEngine on success
            if summary and outcome == "success":
                try:
                    self.runtime.state.add_insight(summary[:200])
                except Exception as e:
                    logger.debug(f"StateEngine.add_insight failed: {e}")

        except Exception as e:
            logger.warning(f"on_feedback_received error: {e}")

    # ────────────────────────────── STATUS ───────────────────────────────────

    def status(self) -> dict:
        """Return bridge status dict for health / diagnostics."""
        return {
            "attached": self._attached,
            "session_active": self._session_start_ts is not None,
            "session_start_ts": self._session_start_ts,
        }
