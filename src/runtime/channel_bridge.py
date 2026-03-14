"""
ChannelBridge — Pulse v2 Day 17
================================

Connects the internal HypostasRuntime (ResponseEngine + ProactiveDispatcher)
with the outside world (Signal / Discord / Telegram / webhooks).

Design goals
------------
- **Bidirectional**: inbound messages can be responded to; outbound messages can
  be delivered.
- **Extensible**: channel backends are pluggable via ``register_channel``.
- **Safe by default**: if no delivery handler exists, we return the response
  text to the caller rather than crashing.
- **Stdlib-only**: no external deps.

This implementation intentionally keeps delivery handlers generic. In OpenClaw
production, an external process can register a handler that uses OpenClaw's
native message routing. For local testing, ``LocalHandler`` captures messages.

HTTP endpoints are registered by HypostasRuntime:
  POST /runtime/bridge/receive
  POST /runtime/bridge/send
  GET  /runtime/bridge/status
  POST /runtime/bridge/proactive
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Protocol, Union

logger = logging.getLogger("pulse.runtime.channel_bridge")


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


class ChannelHandler(Protocol):
    def send(
        self,
        message: str,
        *,
        person: Optional[str] = None,
        channel: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> bool: ...


HandlerLike = Union[ChannelHandler, Callable[..., bool]]


# ---------------------------------------------------------------------------
# Built-in local handler
# ---------------------------------------------------------------------------


@dataclass
class LocalMessage:
    message: str
    person: Optional[str]
    channel: str
    ts: float


class LocalHandler:
    """In-memory handler for tests and local dev."""

    def __init__(self) -> None:
        self.sent: list[LocalMessage] = []
        self._lock = threading.Lock()

    def send(
        self,
        message: str,
        *,
        person: Optional[str] = None,
        channel: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> bool:
        ch = channel or "local"
        with self._lock:
            self.sent.append(LocalMessage(message=message, person=person, channel=ch, ts=time.time()))
        return True


# ---------------------------------------------------------------------------
# OpenClaw handler — writes outbound messages to StateEngine for pickup
# ---------------------------------------------------------------------------

# StateEngine key where outbound messages are queued.
OPENCLAW_OUTBOUND_KEY = "proactive.openclaw_outbound"


class OpenClawHandler:
    """Channel handler that queues outbound messages in StateEngine.

    In OpenClaw production, direct outbound delivery is not possible from
    within the Pulse runtime — the daemon owns the transport layer.  This
    handler writes each outbound message to ``proactive.openclaw_outbound``
    (a list of pending entries) so that the daemon or an external sender
    process can pick them up and inject them into the active session.

    Schema for each queued entry::

        {
            "text":          str,   # message body
            "person":        str,   # target recipient
            "channel":       str,   # originating channel name
            "meta":          dict,  # caller-supplied metadata (kind, etc.)
            "queued_at":     float, # epoch seconds
            "queued_at_iso": str,   # ISO 8601 UTC
            "status":        "pending"
        }

    External sender process responsibilities:
      1. Read ``proactive.openclaw_outbound`` from StateEngine.
      2. For each entry with ``status == "pending"``, deliver via the
         daemon's native transport (e.g. Signal, Discord, webhook).
      3. Update ``status`` to ``"sent"`` or ``"failed"`` after delivery.

    If no StateEngine is available, ``send()`` returns ``False`` and logs a
    warning — it does NOT crash.
    """

    def __init__(self, state: Any) -> None:
        self._state = state
        self._lock = threading.Lock()

    def send(
        self,
        message: str,
        *,
        person: Optional[str] = None,
        channel: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> bool:
        if self._state is None:
            logger.warning("OpenClawHandler: no StateEngine configured; cannot queue message")
            return False
        try:
            now = time.time()
            entry = {
                "text": message,
                "person": person or "unknown",
                "channel": channel or "openclaw",
                "meta": meta or {},
                "queued_at": now,
                "queued_at_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "status": "pending",
            }
            with self._lock:
                existing = self._state.get(OPENCLAW_OUTBOUND_KEY) or []
                if not isinstance(existing, list):
                    existing = []
                existing.append(entry)
                self._state.set(OPENCLAW_OUTBOUND_KEY, existing)
            return True
        except Exception as exc:
            logger.warning("OpenClawHandler.send failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# ChannelBridge
# ---------------------------------------------------------------------------


@dataclass
class BridgeStats:
    inbound_count: int = 0
    outbound_count: int = 0
    inbound_failures: int = 0
    outbound_failures: int = 0
    last_inbound_ts: Optional[float] = None
    last_outbound_ts: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.last_inbound_ts:
            d["last_inbound_iso"] = datetime.fromtimestamp(self.last_inbound_ts, tz=timezone.utc).isoformat()
        else:
            d["last_inbound_iso"] = None
        if self.last_outbound_ts:
            d["last_outbound_iso"] = datetime.fromtimestamp(self.last_outbound_ts, tz=timezone.utc).isoformat()
        else:
            d["last_outbound_iso"] = None
        return d


class ChannelBridge:
    """Bidirectional router between runtime cognition and delivery channels."""

    def __init__(
        self,
        runtime: Any,
        config: Optional[dict] = None,
    ) -> None:
        self.runtime = runtime
        self.config = config or {}
        self._lock = threading.RLock()
        self._handlers: Dict[str, HandlerLike] = {}
        self._local = LocalHandler()
        self._stats = BridgeStats()

        # Default channels always available.
        self.register_channel("local", self._local)

        # OpenClaw handler: queues outbound messages in StateEngine for
        # pickup by the daemon or an external sender process.
        state = getattr(runtime, "state", None)
        if state is not None:
            self._openclaw = OpenClawHandler(state)
            self.register_channel("openclaw", self._openclaw)
        else:
            self._openclaw = None

    # ------------------------------------------------------------------
    # Registration / routing
    # ------------------------------------------------------------------

    def register_channel(self, name: str, handler: HandlerLike) -> None:
        """Register or replace a channel handler."""
        if not name or not isinstance(name, str):
            raise ValueError("channel name must be a non-empty string")
        with self._lock:
            self._handlers[name] = handler

    def handlers(self) -> list[str]:
        with self._lock:
            return sorted(self._handlers.keys())

    def get_preferred_channel(self, person: Optional[str]) -> str:
        """Resolve preferred channel for a person. Defaults to 'local'."""
        if not person:
            return "local"
        try:
            mapping = self.runtime.state.get("channel_bridge.preferred_channels") or {}
            if isinstance(mapping, dict) and person in mapping:
                return str(mapping[person])
        except Exception:
            pass
        return str(self.config.get("default_channel", "local"))

    def _call_handler(
        self,
        handler: HandlerLike,
        message: str,
        *,
        person: Optional[str],
        channel: str,
        meta: Optional[dict] = None,
    ) -> bool:
        if callable(handler) and not hasattr(handler, "send"):
            return bool(handler(message=message, person=person, channel=channel, meta=meta))
        if hasattr(handler, "send"):
            return bool(handler.send(message, person=person, channel=channel, meta=meta))
        raise TypeError("handler must be callable or have a .send() method")

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def receive(
        self,
        message: str,
        *,
        person: str = "josh",
        channel: str = "local",
        fmt: str = "compact",
        max_tokens: int = 400,
    ) -> str:
        """Inbound message → ResponseEngine → reply text."""
        msg = str(message or "").strip()
        if not msg:
            raise ValueError("message must be non-empty")

        with self._lock:
            try:
                result = self.runtime.response.respond(
                    msg,
                    person=person,
                    fmt=fmt,
                    max_tokens=max_tokens,
                )
                self._stats.inbound_count += 1
                self._stats.last_inbound_ts = time.time()
                return str(getattr(result, "text", "")).strip()
            except Exception as exc:
                self._stats.inbound_failures += 1
                logger.warning("ChannelBridge.receive failed: %s", exc)
                raise

    def receive_and_send(
        self,
        message: str,
        *,
        person: str = "josh",
        channel: str = "local",
        fmt: str = "compact",
        max_tokens: int = 400,
    ) -> dict:
        """Convenience: receive() then send() the reply via the same channel."""
        reply = self.receive(message, person=person, channel=channel, fmt=fmt, max_tokens=max_tokens)
        delivered = self.send(reply, channel=channel, person=person, meta={"kind": "reply"})
        return {"text": reply, "delivered": delivered, "channel": channel, "person": person}

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def send(
        self,
        message: str,
        *,
        channel: str = "local",
        person: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> bool:
        """Outbound message delivery via handler."""
        text = str(message or "").strip()
        if not text:
            raise ValueError("message must be non-empty")

        with self._lock:
            handler = self._handlers.get(channel)
            if handler is None:
                self._stats.outbound_failures += 1
                raise ValueError(f"unknown channel '{channel}'")

            ok = False
            try:
                ok = self._call_handler(handler, text, person=person, channel=channel, meta=meta)
            except Exception as exc:
                self._stats.outbound_failures += 1
                logger.warning("ChannelBridge.send failed (channel=%s): %s", channel, exc)
                return False

            self._stats.outbound_count += 1
            self._stats.last_outbound_ts = time.time()

            # Record an episodic trace (non-fatal)
            try:
                if getattr(self.runtime, "episodic", None) is not None:
                    self.runtime.episodic.record(
                        kind="conversation",
                        title=f"Sent message via {channel} → {person or 'unknown'}",
                        content=text,
                        salience=6.0,
                        tags=["bridge", f"channel:{channel}", f"person:{person or 'unknown'}"],
                        source="system",
                    )
            except Exception:
                pass

            return bool(ok)

    # ------------------------------------------------------------------
    # Proactive
    # ------------------------------------------------------------------

    def deliver_proactive(
        self,
        *,
        person: str = "josh",
        channel: Optional[str] = None,
        max_tokens: int = 250,
    ) -> dict:
        """Generate proactive outreach (Dispatcher) then deliver via channel."""
        ch = channel or self.get_preferred_channel(person)

        # Generate text (dispatcher handles cooldown + episodic recording)
        result = self.runtime.dispatcher.dispatch(
            mode="response_only",
            person=person,
            max_tokens=max_tokens,
        )

        if not getattr(result, "dispatched", False):
            return {"ok": False, "dispatched": False, "channel": ch, "person": person, "error": getattr(result, "error", None)}

        ok = self.send(result.text, channel=ch, person=person, meta={"kind": "proactive", "proactive_kind": result.kind})
        return {
            "ok": bool(ok),
            "dispatched": True,
            "delivered": bool(ok),
            "channel": ch,
            "person": person,
            "kind": getattr(result, "kind", None),
            "text": getattr(result, "text", ""),
            "fallback": getattr(result, "fallback", False),
            "episode_id": getattr(result, "episode_id", ""),
            "elapsed_ms": getattr(result, "elapsed_ms", 0),
            "error": getattr(result, "error", None),
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "channels": self.handlers(),
                "stats": self._stats.to_dict(),
                "local_sent": len(self._local.sent),
            }

    @property
    def local(self) -> LocalHandler:
        """Access the LocalHandler for inspection in tests."""
        return self._local
