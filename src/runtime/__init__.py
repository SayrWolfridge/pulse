"""
Pulse v2 Runtime — HypostasRuntime
====================================
The always-running cognition layer for Iris.

Orchestrates StateEngine + ContextEngine + ThoughtLoop + RuntimeBridge
into one persistent process that keeps Iris alive between messages.

Entry point: python -m pulse.runtime
Health:      GET http://127.0.0.1:9723/runtime/health
Status:      GET http://127.0.0.1:9723/runtime/status
"""

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .state_engine import StateEngine
from .context_engine import ContextEngine
from .thought_loop import ThoughtLoop
from .bridge import RuntimeBridge
from .self_model import SelfModel
from .goal_engine import GoalEngine
from .episodic_buffer import EpisodicBuffer
from .narrative_engine import NarrativeEngine
from .emotion_engine import EmotionEngine
from .relationship_graph import RelationshipGraph
from .context_assembler import ContextAssembler
from .response_engine import ResponseEngine
from .proactive_engine import ProactiveEngine
from .proactive_dispatcher import ProactiveDispatcher

logger = logging.getLogger("pulse.runtime")

__all__ = ["HypostasRuntime", "StateEngine", "ContextEngine", "ThoughtLoop", "RuntimeBridge", "SelfModel", "GoalEngine", "EpisodicBuffer", "NarrativeEngine", "EmotionEngine", "RelationshipGraph", "ContextAssembler", "ResponseEngine", "ProactiveEngine", "ProactiveDispatcher"]


class HypostasRuntime:
    """
    The always-running cognition layer.

    Starts two background threads (StateEngine autosave + ThoughtLoop) and an
    HTTP health endpoint on port 9723.  Optionally attaches a RuntimeBridge to
    an existing PulseDaemon so every triggered session gets live cognitive
    context injected into its prompt.

    Lifecycle::

        runtime = HypostasRuntime()
        runtime.start()
        # ... runs until stopped ...
        runtime.stop()

    Or as a process::

        python -m pulse.runtime
    """

    PORT: int = 9723
    DEFAULT_STATE_DIR: Path = Path("~/.pulse/state").expanduser()

    def __init__(
        self,
        state_dir: Optional[Path] = None,
        daemon=None,
        port: Optional[int] = None,
    ) -> None:
        self._state_dir: Path = state_dir or self.DEFAULT_STATE_DIR
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._port: int = port if port is not None else self.PORT

        # Core components
        self.state: StateEngine = StateEngine(self._state_dir / "hypostas-state.json")
        self.context: ContextEngine = ContextEngine(self._state_dir)
        self.self_model: SelfModel = SelfModel(self.state)
        self.goal_engine: GoalEngine = GoalEngine(self.state)
        self.episodic: EpisodicBuffer = EpisodicBuffer(self.state, path=self._state_dir / "episodes.jsonl")
        self.emotion: EmotionEngine = EmotionEngine(self.state, episodic=self.episodic)
        self.relationships: RelationshipGraph = RelationshipGraph(
            context=self.context,
            state=self.state,
            emotion=self.emotion,
        )
        self.narrative: NarrativeEngine = NarrativeEngine(
            state=self.state,
            self_model=self.self_model,
            episodic=self.episodic,
            goal_engine=self.goal_engine,
            context=self.context,
            emotion=self.emotion,
        )
        self.assembler: ContextAssembler = ContextAssembler(
            state=self.state,
            self_model=self.self_model,
            goal_engine=self.goal_engine,
            episodic=self.episodic,
            narrative=self.narrative,
            emotion=self.emotion,
            relationships=self.relationships,
        )
        self.response: ResponseEngine = ResponseEngine(
            assembler=self.assembler,
            narrative=self.narrative,
            emotion=self.emotion,
            episodic=self.episodic,
            self_model=self.self_model,
            state=self.state,
        )
        self.proactive: ProactiveEngine = ProactiveEngine(
            state=self.state,
            emotion=self.emotion,
            goal_engine=self.goal_engine,
            relationships=self.relationships,
            episodic=self.episodic,
        )
        self.dispatcher: ProactiveDispatcher = ProactiveDispatcher(
            proactive=self.proactive,
            response=self.response,
            episodic=self.episodic,
            state=self.state,
        )
        self.thought_loop: ThoughtLoop = ThoughtLoop(
            self.state,
            self.context,
            self_model=self.self_model,
            goal_engine=self.goal_engine,
            episodic=self.episodic,
            narrative=self.narrative,
        )
        self.bridge: RuntimeBridge = RuntimeBridge(self)  # passes self so bridge can access .state/.context/.thought_loop

        # Optional: existing PulseDaemon (wires EventBus hooks)
        self._daemon = daemon

        # Health server state
        self._health_httpd: Optional[HTTPServer] = None
        self._health_thread: Optional[threading.Thread] = None

        # Runtime state
        self._running: bool = False
        self._start_time: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Boot sequence:
          1. Load state from disk (crash recovery)
          2. Start StateEngine autosave thread
          3. Start ThoughtLoop background thread
          4. Attach bridge to Pulse daemon (if provided)
          5. Start health endpoint
          6. Log SYSTEM_EVENT: runtime_started
        """
        if self._running:
            return

        # 1. Load state from disk (StateEngine loads on __init__ via _load_at_startup;
        #    calling it again here ensures any on-disk changes since init are picked up)
        self.state._load_at_startup()

        # 1b. Load goals (seed or disk)
        try:
            self.goal_engine.load()
        except Exception as e:
            logger.warning("GoalEngine load failed (non-fatal): %s", e)

        # 1c. Load episodic buffer
        try:
            self.episodic.load()
        except Exception as e:
            logger.warning("EpisodicBuffer load failed (non-fatal): %s", e)

        # 2. Start StateEngine autosave thread
        self.state.start_autosave()

        # 3. Start ThoughtLoop background thread
        self.thought_loop.start()

        # 4. Attach RuntimeBridge to daemon (if provided)
        if self._daemon is not None:
            self.bridge.attach(self._daemon)

        # 5. Start health endpoint
        self._start_health_server()

        # 6. Mark running + record start time
        self._running = True
        self._start_time = datetime.now(timezone.utc)

        # Log startup event to hot tier
        try:
            self.context.log_event(
                {
                    "type": "SYSTEM_EVENT",
                    "event": "runtime_started",
                    "ts": self._start_time.isoformat(),
                    "port": self._port,
                    "state_dir": str(self._state_dir),
                }
            )
        except Exception as e:
            logger.warning("Could not log runtime_started event: %s", e)

        logger.info(
            "HypostasRuntime started | health: http://127.0.0.1:%d/runtime/health",
            self._port,
        )

    def stop(self) -> None:
        """
        Graceful shutdown:
          1. Stop ThoughtLoop (finish current cycle)
          2. Log SYSTEM_EVENT: runtime_stopped
          3. Final StateEngine serialize
          4. Stop health server
        """
        if not self._running:
            return

        # 1. Stop ThoughtLoop gracefully
        try:
            self.thought_loop.stop()
        except Exception as e:
            logger.warning("ThoughtLoop stop error: %s", e)

        # 2. Log runtime_stopped before final save so the event is persisted
        try:
            self.context.log_event(
                {
                    "type": "SYSTEM_EVENT",
                    "event": "runtime_stopped",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "uptime_seconds": self._uptime_seconds(),
                }
            )
        except Exception as e:
            logger.warning("Could not log runtime_stopped event: %s", e)

        # 3. Final state serialize (accumulate uptime before saving)
        try:
            current = self.state.get("meta.total_uptime_seconds") or 0
            self.state.set(
                "meta.total_uptime_seconds",
                current + self._uptime_seconds(),
            )
            self.state.save()
        except Exception as e:
            logger.warning("Final state save error: %s", e)

        # 4. Stop health server
        if self._health_httpd is not None:
            try:
                self._health_httpd.shutdown()
            except Exception as e:
                logger.warning("Health server shutdown error: %s", e)

        self._running = False
        logger.info("HypostasRuntime stopped")

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a plain-dict status snapshot (JSON-serialisable)."""
        return {
            "running": self._running,
            "uptime_seconds": self._uptime_seconds(),
            "port": self._port,
            "state_dir": str(self._state_dir),
            "thought_loop": self.thought_loop.status(),
            "bridge": self.bridge.status(),
            "self_model": self.self_model.status(),
            "goals": self.goal_engine.status(),
            "episodic": self.episodic.status(),
            "narrative": self.narrative.snapshot(),
            "emotion": self.emotion.status(),
            "relationships": self.relationships.status() if hasattr(self.relationships, "status") else {"count": len(self.context.get_all_relationships() or {})},
            "assembler": self.assembler.snapshot(),
            "response": self.response.status(),
            "proactive": self.proactive.snapshot(),
            "dispatcher": self.dispatcher.status(),
        }

    # ------------------------------------------------------------------
    # Health server (port 9723)
    # ------------------------------------------------------------------

    def _start_health_server(self) -> None:
        """Start a simple HTTP health endpoint on port 9723."""
        runtime = self  # capture for handler closure

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/runtime/health":
                    body = json.dumps(
                        {
                            "status": "ok",
                            "running": runtime._running,
                            "uptime_seconds": runtime._uptime_seconds(),
                        }
                    ).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/status":
                    body = json.dumps(runtime.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/goals":
                    body = json.dumps(runtime.goal_engine.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/episodes":
                    body = json.dumps(runtime.episodic.snapshot(top=20)).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/episodes/context":
                    body = json.dumps({"narrative": runtime.episodic.context_narrative()}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/narrative":
                    body = json.dumps(runtime.narrative.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/emotion":
                    body = json.dumps(runtime.emotion.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/emotion/events":
                    body = json.dumps({"events": runtime.emotion.known_events()}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/relationships":
                    body = json.dumps(runtime.relationships.snapshot(top=20)).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/relationships/reconnect":
                    body = json.dumps({"candidates": runtime.relationships.reconnect_candidates()}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/proactive":
                    body = json.dumps(runtime.proactive.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/proactive/top":
                    top = runtime.proactive.top_candidate()
                    body = json.dumps({"top": top.to_dict() if top else None}).encode()
                    self._respond(200, body)
                elif self.path.startswith("/runtime/context"):
                    try:
                        parsed = urlparse(self.path)
                        qs = parse_qs(parsed.query)
                        fmt = qs.get("format", ["standard"])[0]
                        person = qs.get("person", [None])[0]
                        text = runtime.assembler.assemble(fmt=fmt, person=person)
                        body = json.dumps({
                            "context": text,
                            "format": fmt,
                            "person": person,
                            "chars": len(text),
                            "snapshot": runtime.assembler.snapshot(),
                        }).encode()
                        self._respond(200, body)
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                else:
                    self._respond(404, b"Not found")

            def _respond(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/runtime/episodes":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        ep = runtime.episodic.record(
                            kind=payload.get("kind", "other"),
                            title=payload.get("title", ""),
                            content=payload.get("content", ""),
                            salience=payload.get("salience"),
                            tags=payload.get("tags"),
                            source=payload.get("source", "manual"),
                            linked_goal=payload.get("linked_goal"),
                        )
                        self._respond(201, json.dumps(ep).encode())
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/narrative/refresh":
                    try:
                        runtime.narrative.invalidate()
                        runtime.narrative._last_source_hash = ""  # force actual rebuild
                        text = runtime.narrative.build()
                        body = json.dumps({"text": text, "chars": len(text)}).encode()
                        self._respond(200, body)
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/goals/update":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        goal_id = payload.get("id", "")
                        field = payload.get("field", "")
                        value = payload.get("value")

                        if field == "status" and value == "completed":
                            ok = runtime.goal_engine.complete_goal(goal_id)
                        elif field == "progress":
                            ok = runtime.goal_engine.update_progress(goal_id, float(value))
                        elif field == "add_blocker":
                            ok = runtime.goal_engine.add_blocker(goal_id, str(value))
                        elif field == "remove_blocker":
                            ok = runtime.goal_engine.remove_blocker(goal_id, str(value))
                        else:
                            self._respond(400, json.dumps({"error": f"unknown field: {field}"}).encode())
                            return

                        result = {"ok": ok, "goals": runtime.goal_engine.status()}
                        self._respond(200, json.dumps(result).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/emotion/event":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        event_name = payload.get("event", "")
                        note = payload.get("note", "")
                        applied = runtime.emotion.apply_event(event_name, note=note)
                        body = json.dumps({
                            "ok": True,
                            "event": event_name,
                            "applied": applied,
                            "emotion": runtime.emotion.status(),
                        }).encode()
                        self._respond(200, body)
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/emotion/update":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        emotion_name = payload.get("emotion", "")
                        delta = float(payload.get("delta", 0))
                        reason = payload.get("reason", "")
                        new_val = runtime.emotion.update(emotion_name, delta, reason=reason)
                        body = json.dumps({
                            "ok": True,
                            "emotion": emotion_name,
                            "new_value": new_val,
                            "state": runtime.emotion.status(),
                        }).encode()
                        self._respond(200, body)
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/context/prime":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        fmt = payload.get("format", "standard")
                        person = payload.get("person")
                        # Invalidate cache for fresh assembly
                        runtime.assembler.invalidate(fmt=fmt, person=person)
                        text = runtime.assembler.assemble(fmt=fmt, person=person)
                        body = json.dumps({
                            "context": text,
                            "format": fmt,
                            "person": person,
                            "chars": len(text),
                        }).encode()
                        self._respond(200, body)
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/respond":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        message = str(payload.get("message", "")).strip()
                        if not message:
                            self._respond(400, json.dumps({"error": "missing message"}).encode())
                            return
                        person = payload.get("person")
                        fmt = payload.get("format", "compact")
                        max_tokens = int(payload.get("max_tokens", 400))
                        result = runtime.response.respond(
                            message,
                            person=person,
                            fmt=fmt,
                            max_tokens=max_tokens,
                        )
                        self._respond(200, json.dumps(result.to_dict()).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/proactive/sent":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        kind = str(payload.get("kind", "")).strip()
                        if not kind:
                            self._respond(400, json.dumps({"error": "missing kind"}).encode())
                            return
                        runtime.proactive.mark_sent(kind)
                        # Optional: mark milestone goal ids as announced
                        if kind == "milestone":
                            goal_ids = payload.get("goal_ids") or []
                            if isinstance(goal_ids, list) and goal_ids:
                                existing = set(runtime.state.get("proactive.announced_goal_ids") or [])
                                for gid in goal_ids:
                                    if gid:
                                        existing.add(str(gid))
                                runtime.state.set("proactive.announced_goal_ids", sorted(existing))
                        self._respond(200, json.dumps({"ok": True, "kind": kind}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/proactive/deliver":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        mode = str(payload.get("mode", "response_only")).strip()
                        person = str(payload.get("person", "josh")).strip()
                        max_tokens = int(payload.get("max_tokens", 250))
                        result = runtime.dispatcher.dispatch(
                            mode=mode,
                            person=person,
                            max_tokens=max_tokens,
                        )
                        status_code = 200 if result.dispatched else 204
                        self._respond(status_code, json.dumps(result.to_dict()).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/relationships/event":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        rec = runtime.relationships.record_event(
                            person=str(payload.get("person", "")),
                            kind=str(payload.get("kind", "message")),
                            note=str(payload.get("note", "")),
                            themes=payload.get("themes"),
                            delta_bond=payload.get("delta_bond"),
                            tier=payload.get("tier"),
                        )
                        self._respond(200, json.dumps({"ok": True, "relationship": rec}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                else:
                    self._respond(404, b"Not found")

            def log_message(self, fmt: str, *args) -> None:  # noqa: ANN001
                pass  # Suppress access logs — keep logs clean

        try:
            self._health_httpd = HTTPServer(("127.0.0.1", self._port), _Handler)
        except OSError as e:
            logger.warning(
                "Could not bind health server on port %d: %s (continuing without health endpoint)",
                self._port,
                e,
            )
            return

        self._health_thread = threading.Thread(
            target=self._health_httpd.serve_forever,
            daemon=True,
            name="hypostas-health",
        )
        self._health_thread.start()
        logger.info("Health endpoint: http://127.0.0.1:%d/runtime/health", self._port)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return (datetime.now(timezone.utc) - self._start_time).total_seconds()
