"""
Tests for Pulse Prometheus metrics (src/metrics.py).

Covers:
- Text format helpers (_labels, _metric_line, _format_value, _metric_block)
- PulseMetrics.collect() — full output shape and content
- Counter mutations (record_feedback, record_trigger, record_instinct)
- Edge cases: no drives, special float values (inf, nan, 0)
- aiohttp handler integration (async)
- Content-Type header
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from pulse.src.metrics import (
    PulseMetrics,
    _labels,
    _metric_line,
    _format_value,
    _metric_block,
)


# ─── Helpers ─────────────────────────────────────────────────


def make_drive(name: str, pressure: float = 1.0, weight: float = 1.0):
    drive = MagicMock()
    drive.name = name
    drive.pressure = pressure
    drive.weight = weight
    return drive


def make_daemon(drives=None):
    daemon = MagicMock()
    daemon.start_time = time.time() - 60  # 60 s uptime
    daemon.turn_count = 5

    drives_obj = MagicMock()
    drives_obj.drives = drives or {
        "goals": make_drive("goals", pressure=2.29, weight=1.0),
        "curiosity": make_drive("curiosity", pressure=0.5, weight=1.2),
    }
    daemon.drives = drives_obj
    return daemon


# ─── Text Format Tests ────────────────────────────────────────


class TestLabels:
    def test_empty_dict(self):
        assert _labels({}) == ""

    def test_single_label(self):
        result = _labels({"drive": "goals"})
        assert result == '{drive="goals"}'

    def test_multiple_labels_sorted(self):
        result = _labels({"outcome": "success", "drive": "goals"})
        # Should be sorted alphabetically
        assert result == '{drive="goals",outcome="success"}'

    def test_label_with_spaces_passthrough(self):
        # Labels are passed as-is (caller's responsibility to sanitise)
        result = _labels({"name": "my drive"})
        assert 'name="my drive"' in result


class TestFormatValue:
    def test_integer_like(self):
        assert _format_value(42.0) == "42"

    def test_float(self):
        assert _format_value(2.29) == "2.29"

    def test_zero(self):
        assert _format_value(0.0) == "0"

    def test_positive_inf(self):
        assert _format_value(float("inf")) == "+Inf"

    def test_negative_inf(self):
        assert _format_value(float("-inf")) == "-Inf"

    def test_nan(self):
        assert _format_value(float("nan")) == "NaN"

    def test_small_float(self):
        result = _format_value(0.000123)
        assert "0.000123" in result or "1.23e-04" in result


class TestMetricLine:
    def test_no_labels_no_timestamp(self):
        line = _metric_line("pulse_uptime_seconds", 3600.0)
        assert line == "pulse_uptime_seconds 3600"

    def test_with_labels(self):
        line = _metric_line("pulse_drives_pressure", 2.29, {"drive": "goals"})
        assert line == 'pulse_drives_pressure{drive="goals"} 2.29'

    def test_with_timestamp(self):
        line = _metric_line("my_metric", 1.0, {}, timestamp_ms=1234567890000)
        assert line == "my_metric 1 1234567890000"


class TestMetricBlock:
    def test_basic_block(self):
        lines = _metric_block(
            "pulse_uptime_seconds",
            "gauge",
            "Uptime in seconds.",
            [(None, 42.0)],
        )
        assert lines[0] == "# HELP pulse_uptime_seconds Uptime in seconds."
        assert lines[1] == "# TYPE pulse_uptime_seconds gauge"
        assert lines[2] == "pulse_uptime_seconds 42"

    def test_multiple_samples(self):
        lines = _metric_block(
            "pulse_drives_pressure",
            "gauge",
            "Drive pressure.",
            [
                ({"drive": "goals"}, 2.29),
                ({"drive": "curiosity"}, 0.5),
            ],
        )
        assert len(lines) == 4  # HELP + TYPE + 2 samples
        assert any("goals" in l for l in lines)
        assert any("curiosity" in l for l in lines)


# ─── PulseMetrics Unit Tests ──────────────────────────────────


class TestPulseMetricsInit:
    def test_init_creates_metrics(self):
        daemon = make_daemon()
        m = PulseMetrics(daemon)
        assert m._daemon is daemon
        assert m._feedback_counts == {"success": 0, "partial": 0, "blocked": 0}
        assert m._trigger_counts == {}
        assert m._trigger_failure_counts == {}
        assert m._instinct_counts == {}


class TestRecordFeedback:
    def test_record_success(self):
        m = PulseMetrics(make_daemon())
        m.record_feedback("success")
        assert m._feedback_counts["success"] == 1

    def test_record_partial(self):
        m = PulseMetrics(make_daemon())
        m.record_feedback("partial")
        assert m._feedback_counts["partial"] == 1

    def test_record_blocked(self):
        m = PulseMetrics(make_daemon())
        m.record_feedback("blocked")
        assert m._feedback_counts["blocked"] == 1

    def test_record_unknown_goes_to_blocked(self):
        m = PulseMetrics(make_daemon())
        m.record_feedback("some_weird_outcome")
        assert m._feedback_counts["blocked"] == 1

    def test_record_multiple(self):
        m = PulseMetrics(make_daemon())
        m.record_feedback("success")
        m.record_feedback("success")
        m.record_feedback("partial")
        assert m._feedback_counts["success"] == 2
        assert m._feedback_counts["partial"] == 1


class TestRecordTrigger:
    def test_record_success_trigger(self):
        m = PulseMetrics(make_daemon())
        m.record_trigger("single_drive_threshold", success=True)
        assert m._trigger_counts["single_drive_threshold"] == 1
        assert m._trigger_failure_counts == {}

    def test_record_failed_trigger(self):
        m = PulseMetrics(make_daemon())
        m.record_trigger("single_drive_threshold", success=False)
        assert m._trigger_failure_counts["single_drive_threshold"] == 1
        assert m._trigger_counts == {}

    def test_record_multiple_reasons(self):
        m = PulseMetrics(make_daemon())
        m.record_trigger("single_drive_threshold", success=True)
        m.record_trigger("combined_pressure", success=True)
        m.record_trigger("single_drive_threshold", success=True)
        assert m._trigger_counts["single_drive_threshold"] == 2
        assert m._trigger_counts["combined_pressure"] == 1


class TestRecordInstinct:
    def test_record_instinct(self):
        m = PulseMetrics(make_daemon())
        m.record_instinct("weather-market-scan")
        assert m._instinct_counts["weather-market-scan"] == 1

    def test_record_instinct_multiple(self):
        m = PulseMetrics(make_daemon())
        m.record_instinct("memory-maintenance")
        m.record_instinct("memory-maintenance")
        m.record_instinct("x-engagement")
        assert m._instinct_counts["memory-maintenance"] == 2
        assert m._instinct_counts["x-engagement"] == 1


# ─── PulseMetrics.collect() Tests ────────────────────────────


class TestCollect:
    def test_output_is_string(self):
        m = PulseMetrics(make_daemon())
        output = m.collect()
        assert isinstance(output, str)

    def test_ends_with_newline(self):
        m = PulseMetrics(make_daemon())
        assert m.collect().endswith("\n")

    def test_contains_help_comments(self):
        output = PulseMetrics(make_daemon()).collect()
        assert "# HELP pulse_uptime_seconds" in output
        assert "# HELP pulse_drives_pressure" in output
        assert "# HELP pulse_triggers_total" in output
        assert "# HELP pulse_feedback_total" in output

    def test_contains_type_comments(self):
        output = PulseMetrics(make_daemon()).collect()
        assert "# TYPE pulse_uptime_seconds gauge" in output
        assert "# TYPE pulse_triggers_total counter" in output

    def test_uptime_positive(self):
        daemon = make_daemon()
        daemon.start_time = time.time() - 120
        output = PulseMetrics(daemon).collect()
        # Should contain a value > 0 for uptime
        for line in output.splitlines():
            if line.startswith("pulse_uptime_seconds ") and not line.startswith("#"):
                val = float(line.split()[-1])
                assert val > 0
                break
        else:
            pytest.fail("pulse_uptime_seconds sample not found")

    def test_drive_pressures_present(self):
        output = PulseMetrics(make_daemon()).collect()
        assert 'drive="goals"' in output
        assert 'drive="curiosity"' in output

    def test_turn_count_reflected(self):
        daemon = make_daemon()
        daemon.turn_count = 7
        output = PulseMetrics(daemon).collect()
        assert "pulse_turn_count_total 7" in output

    def test_no_drives(self):
        """Should degrade gracefully when drives dict is empty."""
        daemon = make_daemon(drives={})
        output = PulseMetrics(daemon).collect()
        assert "pulse_drives_pressure" in output  # block still emitted
        assert "_none" in output  # sentinel label

    def test_feedback_counters_in_output(self):
        m = PulseMetrics(make_daemon())
        m.record_feedback("success")
        m.record_feedback("success")
        m.record_feedback("partial")
        output = m.collect()
        assert 'outcome="success"' in output
        assert 'outcome="partial"' in output

    def test_trigger_counters_in_output(self):
        m = PulseMetrics(make_daemon())
        m.record_trigger("single_drive_threshold", success=True)
        m.record_trigger("combined_pressure", success=False)
        output = m.collect()
        assert 'reason="single_drive_threshold"' in output
        assert 'reason="combined_pressure"' in output

    def test_instinct_counters_in_output(self):
        m = PulseMetrics(make_daemon())
        m.record_instinct("weather-market-scan")
        output = m.collect()
        assert 'instinct="weather-market-scan"' in output

    def test_info_metric_present(self):
        output = PulseMetrics(make_daemon()).collect()
        assert "pulse_info" in output
        assert "python_version" in output

    def test_prometheus_parseable(self):
        """Verify every non-comment line has the right number of fields."""
        output = PulseMetrics(make_daemon()).collect()
        for line in output.strip().splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            assert len(parts) in (2, 3), f"Unexpected line format: {line!r}"
            # Value field must be a valid float or special string
            val_str = parts[1]
            if val_str not in ("+Inf", "-Inf", "NaN"):
                float(val_str)  # should not raise

    def test_no_start_time(self):
        """Daemon with no start_time should still work (uptime=0)."""
        daemon = make_daemon()
        daemon.start_time = None
        output = PulseMetrics(daemon).collect()
        assert "pulse_uptime_seconds 0" in output


# ─── aiohttp handler Tests ────────────────────────────────────


class TestHandleAsync:
    @pytest.mark.asyncio
    async def test_handle_returns_response(self):
        from aiohttp import web

        m = PulseMetrics(make_daemon())
        request = MagicMock()
        response = await m.handle(request)
        assert isinstance(response, web.Response)

    @pytest.mark.asyncio
    async def test_handle_status_200(self):
        m = PulseMetrics(make_daemon())
        request = MagicMock()
        response = await m.handle(request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_handle_content_type(self):
        m = PulseMetrics(make_daemon())
        request = MagicMock()
        response = await m.handle(request)
        ct = response.content_type
        assert "text/plain" in ct

    @pytest.mark.asyncio
    async def test_handle_body_contains_metrics(self):
        m = PulseMetrics(make_daemon())
        request = MagicMock()
        response = await m.handle(request)
        body = response.text
        assert "pulse_uptime_seconds" in body
        assert "pulse_drives_pressure" in body

    @pytest.mark.asyncio
    async def test_handle_error_returns_500(self):
        """If collect() raises, handler should return 500."""
        m = PulseMetrics(make_daemon())

        def boom():
            raise RuntimeError("collection failure")

        m.collect = boom
        request = MagicMock()
        response = await m.handle(request)
        assert response.status == 500


# ─── HealthServer Integration ─────────────────────────────────


class TestHealthServerMetrics:
    def test_health_server_has_metrics_attr(self):
        """HealthServer should attach a PulseMetrics instance on init."""
        from pulse.src.core.health import HealthServer

        daemon = make_daemon()
        # HealthServer accesses daemon.config.state.dir in _handle_feedback
        # and daemon.mutator.audit in _handle_mutations — mock those
        daemon.config = MagicMock()
        daemon.mutator = MagicMock()

        hs = HealthServer(daemon, port=9799)
        assert hasattr(hs, "metrics")
        from pulse.src.metrics import PulseMetrics
        assert isinstance(hs.metrics, PulseMetrics)

    def test_health_server_metrics_route_registered(self):
        """The /metrics route should be registered on the aiohttp app."""
        from pulse.src.core.health import HealthServer

        daemon = make_daemon()
        daemon.config = MagicMock()
        daemon.mutator = MagicMock()

        hs = HealthServer(daemon, port=9800)
        routes = [str(r.resource) for r in hs._app.router.routes()]
        assert any("/metrics" in r for r in routes)
