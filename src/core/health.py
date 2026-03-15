"""
Health Endpoint — lightweight HTTP server for liveness checks.

GET /health → 200 + JSON status
GET /status → 200 + detailed drive/sensor state

Runs on a separate port, no auth required (read-only, local-only).
"""

import asyncio
import json
import logging
import time
from aiohttp import web
from pulse.src import __version__
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse.src.core.daemon import PulseDaemon

logger = logging.getLogger("pulse.health")

DEFAULT_PORT = 9720


class HealthServer:
    """Minimal HTTP health endpoint."""

    def __init__(self, daemon: "PulseDaemon", port: int = DEFAULT_PORT):
        self.daemon = daemon
        self.port = port
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/evolution", self._handle_evolution)
        self._app.router.add_get("/mutations", self._handle_mutations)
        self._app.router.add_post("/feedback", self._handle_feedback)
        self._app.router.add_get("/metrics", self._handle_metrics)
        self._runner: web.AppRunner | None = None

        # Prometheus metrics collector — lazy import avoids circular deps
        from pulse.src.metrics import PulseMetrics
        self.metrics = PulseMetrics(daemon)

        # Logos backlog engine
        from pulse.src.logos import LogosAPI, LogosStore
        from pulse.src.logos.seed import seed
        from pulse.src.logos.messages import MessageStore, seed_messages
        self._logos_store = LogosStore()
        self._message_store = MessageStore(db_path=self._logos_store.db_path)
        self._logos_api = LogosAPI(store=self._logos_store, message_store=self._message_store)
        self._logos_api.register_routes(self._app)
        seed(self._logos_store)
        seed_messages(self._message_store)

        # AURA arousal endpoints
        self._app.router.add_post("/aura/arousal", self._handle_set_arousal)
        self._app.router.add_get("/aura/arousal", self._handle_get_arousal)
        self._app.router.add_post("/aura/climax", self._handle_climax)

    async def start(self):
        """Start the health server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        try:
            await site.start()
            logger.info(
                f"Health endpoint listening on http://127.0.0.1:{self.port}/health"
            )
            logger.info(
                f"Prometheus metrics at http://127.0.0.1:{self.port}/metrics"
            )
        except OSError as e:
            logger.warning(f"Could not start health endpoint on port {self.port}: {e}")

    async def stop(self):
        """Stop the health server."""
        if self._runner:
            await self._runner.cleanup()

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Simple liveness check."""
        uptime = time.time() - self.daemon.start_time if self.daemon.start_time else 0
        return web.json_response(
            {
                "status": "alive",
                "uptime_seconds": round(uptime),
                "turn_count": self.daemon.turn_count,
                "version": __version__,
            }
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Detailed status — drives, sensors, trigger history."""
        uptime = time.time() - self.daemon.start_time if self.daemon.start_time else 0

        # Drive states
        drives = {}
        for name, drive in self.daemon.drives.drives.items():
            drives[name] = {
                "pressure": round(drive.pressure, 4),
                "weighted": round(drive.weighted_pressure, 4),
                "weight": drive.weight,
                "last_addressed": drive.last_addressed,
            }

        # Trigger stats
        trigger_stats = self.daemon.state.get_trigger_stats()

        # Rate limit status
        now = time.time()
        one_hour_ago = now - 3600
        recent_turns = len(
            [t for t in self.daemon._turn_timestamps if t > one_hour_ago]
        )

        # Evaluator info
        evaluator_info = {"mode": self.daemon.config.evaluator.mode}
        if self.daemon.config.evaluator.mode == "model":
            evaluator_info["model"] = self.daemon.config.evaluator.model.model

        return web.json_response(
            {
                "status": "alive",
                "uptime_seconds": round(uptime),
                "turn_count": self.daemon.turn_count,
                "drives": drives,
                "trigger_threshold": self.daemon.config.drives.trigger_threshold,
                "max_pressure": self.daemon.config.drives.max_pressure,
                "triggers": trigger_stats,
                "rate_limit": {
                    "turns_last_hour": recent_turns,
                    "max_per_hour": self.daemon.config.openclaw.max_turns_per_hour,
                    "cooldown_remaining": (
                        max(
                            0,
                            round(
                                self.daemon.config.openclaw.min_trigger_interval
                                - (now - self.daemon.last_trigger_time)
                            ),
                        )
                        if self.daemon.last_trigger_time
                        else 0
                    ),
                },
                "evaluator": evaluator_info,
                "learner": (
                    self.daemon.feedback_learner.get_stats()
                    if hasattr(self.daemon, "feedback_learner")
                    else None
                ),
                "version": __version__,
            }
        )

    async def _handle_evolution(self, request: web.Request) -> web.Response:
        """Current evolution state — drives, thresholds, mutation history."""
        return web.json_response(
            self.daemon.mutator.get_state(),
            dumps=lambda o: json.dumps(o, default=str),
        )

    async def _handle_feedback(self, request: web.Request) -> web.Response:
        """Accept turn feedback from the agent.

        POST /feedback
        {
            "drives_addressed": ["goals", "curiosity"],  // which drives were worked on
            "outcome": "success" | "partial" | "blocked", // how it went
            "summary": "Wrote journal entry, updated goals",  // what happened
            "decay_overrides": {"goals": 0.8, "curiosity": 0.3}  // optional per-drive decay
        }
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        drives_addressed = data.get("drives_addressed", [])
        outcome = data.get("outcome", "success")
        summary = data.get("summary", "")
        decay_overrides = data.get("decay_overrides", {})

        import time

        now = time.time()
        results = {}

        for drive_name in drives_addressed:
            if drive_name in self.daemon.drives.drives:
                drive = self.daemon.drives.drives[drive_name]
                before = drive.pressure

                # Use override decay if provided, else outcome-based defaults
                if drive_name in decay_overrides:
                    decay_amount = float(decay_overrides[drive_name])
                elif outcome == "success":
                    decay_amount = min(
                        drive.pressure, drive.pressure * 0.85
                    )  # 85% decay — drops 5.0→0.75, below trigger_threshold 1.2
                elif outcome == "partial":
                    decay_amount = min(
                        drive.pressure, drive.pressure * 0.4
                    )  # 40% decay
                else:  # blocked
                    decay_amount = 0.0  # don't decay if blocked

                drive.decay(decay_amount)
                drive.last_addressed = now

                # RL-lite: record event and apply adjusted weight.
                # IMPORTANT: always pass the *config base weight* (not drive.weight)
                # to effective_weight(). Passing drive.weight causes exponential
                # drift: each call multiplies the already-adjusted weight by the
                # learner multiplier again (e.g. 1.3^27 ≈ 994× — same bug that
                # was fixed in daemon._process_feedback_file in March 2026).
                if hasattr(self.daemon, "feedback_learner"):
                    self.daemon.feedback_learner.record(
                        drive_name, before, outcome
                    )
                    config_base = self.daemon.drives.config_weight(drive_name)
                    drive.weight = self.daemon.feedback_learner.effective_weight(
                        drive_name, config_base
                    )

                results[drive_name] = {
                    "before": round(before, 4),
                    "after": round(drive.pressure, 4),
                    "decayed": round(decay_amount, 4),
                    "weight": round(drive.weight, 4),
                }

        # Persist immediately
        self.daemon.state.set("drives", self.daemon.drives.save_state())
        self.daemon.state.save()

        # Log to daily notes if enabled
        if self.daemon.daily_sync and summary:
            try:
                path = self.daemon.daily_sync._get_file()
                self.daemon._mark_self_write(str(path))
                now_str = __import__("datetime").datetime.now().strftime("%H:%M")
                import fcntl

                with open(path, "a") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    try:
                        f.write(
                            f"- {now_str} 📨 Feedback: {outcome} — {summary[:100]}\n"
                        )
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass

        logger.info(
            f"Feedback received: {outcome} — addressed {drives_addressed} — {summary[:60]}"
        )

        return web.json_response(
            {
                "status": "ok",
                "drives_updated": results,
            }
        )

    async def _handle_mutations(self, request: web.Request) -> web.Response:
        """Recent mutation audit log."""
        try:
            n = min(max(int(request.query.get("n", "20")), 1), 1000)
        except (ValueError, TypeError):
            n = 20
        return web.json_response(
            {
                "recent": self.daemon.mutator.audit.recent(n),
                "summary": self.daemon.mutator.audit.summary(),
            },
            dumps=lambda o: json.dumps(o, default=str),
        )

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Prometheus text metrics endpoint.

        GET /metrics

        Returns all Pulse runtime metrics in Prometheus text exposition
        format v0.0.4.  Compatible with any Prometheus scraper and most
        observability platforms (Grafana, Datadog agent, VictoriaMetrics,
        etc.).

        Example scrape config::

            scrape_configs:
              - job_name: pulse
                static_configs:
                  - targets: ['localhost:9720']
                metrics_path: /metrics
        """
        return await self.metrics.handle(request)

    async def _handle_set_arousal(self, request: web.Request) -> web.Response:
        """Set arousal state and broadcast to constellation."""
        from pulse.src import aura
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        level = float(data.get("level", 0.0))
        source = data.get("source")
        intensity = data.get("intensity", "none")
        triggered_by = data.get("triggered_by", "iris")
        state = aura.set_arousal(level, source, intensity, triggered_by)
        aura.broadcast_arousal_to_constellation()
        return web.json_response({"status": "ok", "arousal": state})

    async def _handle_get_arousal(self, request: web.Request) -> web.Response:
        """Get current arousal state."""
        from pulse.src import aura
        return web.json_response(aura.get_arousal())

    async def _handle_climax(self, request: web.Request) -> web.Response:
        """Trigger full climax broadcast."""
        from pulse.src import aura
        try:
            data = await request.json()
            triggered_by = data.get("triggered_by", "iris")
        except Exception:
            triggered_by = "iris"
        result = await aura.trigger_climax(triggered_by)
        return web.json_response({"status": "climax_triggered", "arousal": result})
