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

from .state_engine import StateEngine
from .context_engine import ContextEngine
from .thought_loop import ThoughtLoop
from .bridge import RuntimeBridge
from .self_model import SelfModel

logger = logging.getLogger("pulse.runtime")

__all__ = ["HypostasRuntime", "StateEngine", "ContextEngine", "ThoughtLoop", "RuntimeBridge", "SelfModel"]


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
        self.thought_loop: ThoughtLoop = ThoughtLoop(self.state, self.context, self.self_model)
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
                else:
                    self._respond(404, b"Not found")

            def _respond(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
