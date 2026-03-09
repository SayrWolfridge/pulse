"""
Prometheus Metrics — Phase 4.

Exposes Pulse runtime state as Prometheus-compatible text metrics.

Zero external dependencies — implements the Prometheus text exposition
format (https://prometheus.io/docs/instrumenting/exposition_formats/)
directly. The format is trivial: HELP + TYPE comments, then
``name{labels} value [timestamp_ms]`` lines.

Endpoint: GET /metrics  (shares the health port, default 9720)

Metrics exposed
---------------
Gauges (current value):
    pulse_uptime_seconds                      — daemon uptime
    pulse_drives_pressure{drive}              — current pressure per drive
    pulse_drives_weight{drive}                — configured weight per drive

Counters (monotonically increasing):
    pulse_triggers_total{reason}              — triggers fired by reason
    pulse_trigger_failures_total{reason}      — trigger attempts that failed
    pulse_feedback_total{outcome}             — feedback calls by outcome
    pulse_turn_count_total                    — total agent turns
    pulse_instincts_fired_total{instinct}     — instinct executions

Info / build:
    pulse_info{version, python_version}       — always 1.0

Scrape example (Prometheus config)::

    scrape_configs:
      - job_name: pulse
        static_configs:
          - targets: ['localhost:9720']
        metrics_path: /metrics
"""

import sys
import time
import logging
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from pulse.src.core.daemon import PulseDaemon

logger = logging.getLogger("pulse.metrics")


# ─── Text Format Helpers ─────────────────────────────────────


def _labels(pairs: Dict[str, str]) -> str:
    """Render a label dict to Prometheus label syntax.

    >>> _labels({"drive": "goals"})
    '{drive="goals"}'
    >>> _labels({})
    ''
    """
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(pairs.items()))
    return "{" + inner + "}"


def _metric_line(
    name: str,
    value: float,
    labels: Optional[Dict[str, str]] = None,
    timestamp_ms: Optional[int] = None,
) -> str:
    """Render a single metric sample line."""
    label_str = _labels(labels or {})
    parts = [f"{name}{label_str}", _format_value(value)]
    if timestamp_ms is not None:
        parts.append(str(timestamp_ms))
    return " ".join(parts)


def _format_value(v: float) -> str:
    """Format a float for Prometheus (handle inf/nan gracefully)."""
    if v != v:  # NaN
        return "NaN"
    if v == float("inf"):
        return "+Inf"
    if v == float("-inf"):
        return "-Inf"
    # Use 6 significant digits; strip trailing zeros for readability
    formatted = f"{v:.6g}"
    return formatted


def _metric_block(
    name: str,
    type_: str,
    help_: str,
    samples: List[Tuple[Optional[Dict[str, str]], float]],
) -> List[str]:
    """Render a complete metric family (HELP + TYPE + samples)."""
    lines = [
        f"# HELP {name} {help_}",
        f"# TYPE {name} {type_}",
    ]
    for labels, value in samples:
        lines.append(_metric_line(name, value, labels))
    return lines


# ─── Metrics Collector ───────────────────────────────────────


class PulseMetrics:
    """Collects and exposes Pulse runtime state as Prometheus text.

    Designed to be lightweight — no background threads, no state of
    its own. Every ``collect()`` call reads live from the daemon.
    The daemon reference is held weakly via attribute access (no
    circular import risk since TYPE_CHECKING guards the import).

    Usage::

        metrics = PulseMetrics(daemon)
        text = metrics.collect()           # plain string
        # or via aiohttp:
        response = await metrics.handle(request)
    """

    CONTENT_TYPE = (
        "text/plain; version=0.0.4; charset=utf-8"
    )

    def __init__(self, daemon: "PulseDaemon") -> None:
        self._daemon = daemon
        # Internal counters for feedback outcomes (incremented by daemon callbacks)
        self._feedback_counts: Dict[str, int] = {
            "success": 0,
            "partial": 0,
            "blocked": 0,
        }
        # Trigger reason counters {reason: {"fired": int, "failed": int}}
        self._trigger_counts: Dict[str, int] = {}
        self._trigger_failure_counts: Dict[str, int] = {}
        # Instinct fire counters
        self._instinct_counts: Dict[str, int] = {}

    # ── Public mutators (called by daemon hooks) ──────────────

    def record_feedback(self, outcome: str) -> None:
        """Increment feedback counter. Call from daemon after processing feedback."""
        key = outcome if outcome in self._feedback_counts else "blocked"
        self._feedback_counts[key] = self._feedback_counts.get(key, 0) + 1

    def record_trigger(self, reason: str, success: bool) -> None:
        """Increment trigger counter. Call from daemon after each trigger attempt."""
        if success:
            self._trigger_counts[reason] = self._trigger_counts.get(reason, 0) + 1
        else:
            self._trigger_failure_counts[reason] = (
                self._trigger_failure_counts.get(reason, 0) + 1
            )

    def record_instinct(self, instinct_name: str) -> None:
        """Increment instinct fire counter."""
        self._instinct_counts[instinct_name] = (
            self._instinct_counts.get(instinct_name, 0) + 1
        )

    # ── Collection ────────────────────────────────────────────

    def collect(self) -> str:
        """Snapshot all metrics and return Prometheus text format."""
        lines: List[str] = []
        daemon = self._daemon

        # ── pulse_info ──────────────────────────────────────
        try:
            from pulse import __version__ as pulse_version
        except Exception:
            pulse_version = "unknown"

        lines += _metric_block(
            "pulse_info",
            "gauge",
            "Pulse daemon build metadata. Value is always 1.",
            [
                (
                    {
                        "version": pulse_version,
                        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    },
                    1.0,
                )
            ],
        )

        # ── pulse_uptime_seconds ─────────────────────────────
        uptime = (
            time.time() - daemon.start_time
            if getattr(daemon, "start_time", None)
            else 0.0
        )
        lines += _metric_block(
            "pulse_uptime_seconds",
            "gauge",
            "Seconds since the Pulse daemon started.",
            [(None, uptime)],
        )

        # ── pulse_turn_count_total ───────────────────────────
        turn_count = getattr(daemon, "turn_count", 0)
        lines += _metric_block(
            "pulse_turn_count_total",
            "counter",
            "Total number of agent turns fired since daemon start.",
            [(None, float(turn_count))],
        )

        # ── pulse_drives_pressure / pulse_drives_weight ──────
        drive_pressure_samples = []
        drive_weight_samples = []

        drives_obj = getattr(daemon, "drives", None)
        if drives_obj is not None:
            drives_dict = getattr(drives_obj, "drives", {})
            for drive_name, drive in drives_dict.items():
                lbl = {"drive": drive_name}
                pressure = getattr(drive, "pressure", 0.0)
                weight = getattr(drive, "weight", 1.0)
                drive_pressure_samples.append((lbl, float(pressure)))
                drive_weight_samples.append((lbl, float(weight)))

        lines += _metric_block(
            "pulse_drives_pressure",
            "gauge",
            "Current pressure level for each drive (0.0–5.0+).",
            drive_pressure_samples or [({"drive": "_none"}, 0.0)],
        )
        lines += _metric_block(
            "pulse_drives_weight",
            "gauge",
            "Configured weight multiplier for each drive.",
            drive_weight_samples or [({"drive": "_none"}, 0.0)],
        )

        # ── pulse_triggers_total ─────────────────────────────
        trigger_samples = [
            ({"reason": reason}, float(count))
            for reason, count in self._trigger_counts.items()
        ] or [({"reason": "_none"}, 0.0)]

        lines += _metric_block(
            "pulse_triggers_total",
            "counter",
            "Total successful trigger fires, labelled by trigger reason.",
            trigger_samples,
        )

        # ── pulse_trigger_failures_total ─────────────────────
        failure_samples = [
            ({"reason": reason}, float(count))
            for reason, count in self._trigger_failure_counts.items()
        ] or [({"reason": "_none"}, 0.0)]

        lines += _metric_block(
            "pulse_trigger_failures_total",
            "counter",
            "Total trigger attempts that did not fire, labelled by reason.",
            failure_samples,
        )

        # ── pulse_feedback_total ─────────────────────────────
        feedback_samples = [
            ({"outcome": outcome}, float(count))
            for outcome, count in self._feedback_counts.items()
        ]
        lines += _metric_block(
            "pulse_feedback_total",
            "counter",
            "Total feedback calls received from the agent, labelled by outcome.",
            feedback_samples,
        )

        # ── pulse_instincts_fired_total ──────────────────────
        instinct_samples = [
            ({"instinct": name}, float(count))
            for name, count in self._instinct_counts.items()
        ] or [({"instinct": "_none"}, 0.0)]

        lines += _metric_block(
            "pulse_instincts_fired_total",
            "counter",
            "Total instinct executions, labelled by instinct name.",
            instinct_samples,
        )

        # ── pulse_learner_* ──────────────────────────────────
        learner = getattr(daemon, "feedback_learner", None)
        if learner is not None:
            learner_stats = learner.get_stats().get("drives", {})
            learner_ema_samples = [
                ({"drive": name}, float(s["ema"]))
                for name, s in learner_stats.items()
            ] or [({"drive": "_none"}, 0.0)]
            learner_mult_samples = [
                ({"drive": name}, float(s["multiplier"]))
                for name, s in learner_stats.items()
            ] or [({"drive": "_none"}, 1.0)]
            learner_ev_samples = [
                ({"drive": name}, float(s["events"]))
                for name, s in learner_stats.items()
            ] or [({"drive": "_none"}, 0.0)]
            learner_sr_samples = [
                ({"drive": name}, float(s["success_rate"]))
                for name, s in learner_stats.items()
            ] or [({"drive": "_none"}, 0.0)]

            lines += _metric_block(
                "pulse_learner_ema",
                "gauge",
                "RL-lite EMA outcome score per drive. Range [-1, 1]; positive = reward.",
                learner_ema_samples,
            )
            lines += _metric_block(
                "pulse_learner_multiplier",
                "gauge",
                "RL-lite weight multiplier applied to drive weight. Range [0.7, 1.3].",
                learner_mult_samples,
            )
            lines += _metric_block(
                "pulse_learner_events",
                "gauge",
                "Number of feedback events in the rolling window per drive.",
                learner_ev_samples,
            )
            lines += _metric_block(
                "pulse_learner_success_rate",
                "gauge",
                "Fraction of feedback events with success or partial outcome per drive.",
                learner_sr_samples,
            )

        # Prometheus requires a trailing newline
        return "\n".join(lines) + "\n"

    # ── aiohttp handler ───────────────────────────────────────

    async def handle(self, request: Any) -> Any:  # aiohttp.web.Response
        """aiohttp request handler — return metrics as Prometheus text."""
        try:
            from aiohttp import web  # already required by health.py

            body = self.collect()
            return web.Response(
                text=body,
                headers={"Content-Type": self.CONTENT_TYPE},
            )
        except Exception as exc:
            logger.error(f"Metrics collection failed: {exc}")
            try:
                from aiohttp import web

                return web.Response(
                    text=f"# ERROR: {exc}\n",
                    status=500,
                    content_type="text/plain",
                )
            except Exception:
                raise
