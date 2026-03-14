"""
Tests for HypostasRuntime — Day 6 of Pulse v2 Phase 1 sprint.

Covers: init, start/stop lifecycle, status shape, health endpoint,
SYSTEM_EVENT logging, idempotent start, uptime tracking, daemon attach.
"""

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pulse.src.runtime import HypostasRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Get an available port from the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rt(tmp_path: Path):
    """HypostasRuntime with isolated tmp state dir. Auto-stopped after test."""
    r = HypostasRuntime(state_dir=tmp_path, port=_free_port())
    yield r
    if r._running:
        r.stop()


@pytest.fixture
def started_rt(tmp_path: Path):
    """Already-started runtime on a free port. Stopped after test."""
    port = _free_port()
    r = HypostasRuntime(state_dir=tmp_path, port=port)
    r.start()
    time.sleep(0.1)  # let health server bind
    yield r
    if r._running:
        r.stop()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestInit:
    def test_components_created(self, rt):
        assert rt.state is not None
        assert rt.context is not None
        assert rt.thought_loop is not None
        assert rt.bridge is not None

    def test_not_running_before_start(self, rt):
        assert rt._running is False

    def test_start_time_none_before_start(self, rt):
        assert rt._start_time is None

    def test_state_dir_created(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        r = HypostasRuntime(state_dir=nested, port=_free_port())
        assert nested.exists()
        if r._running:
            r.stop()

    def test_custom_port(self, tmp_path):
        r = HypostasRuntime(state_dir=tmp_path, port=19999)
        assert r._port == 19999

    def test_default_port(self, tmp_path):
        r = HypostasRuntime(state_dir=tmp_path)
        assert r._port == HypostasRuntime.PORT


# ---------------------------------------------------------------------------
# Start / Stop lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_marks_running(self, started_rt):
        assert started_rt._running is True

    def test_stop_clears_running(self, started_rt):
        started_rt.stop()
        assert started_rt._running is False

    def test_start_sets_start_time(self, started_rt):
        assert started_rt._start_time is not None

    def test_double_start_idempotent(self, started_rt):
        """Calling start() twice should not raise or reset start_time."""
        first_start = started_rt._start_time
        started_rt.start()
        assert started_rt._start_time == first_start
        assert started_rt._running is True

    def test_double_stop_idempotent(self, started_rt):
        started_rt.stop()
        started_rt.stop()  # Should not raise
        assert started_rt._running is False

    def test_stop_without_start_is_safe(self, rt):
        rt.stop()  # Should not raise
        assert rt._running is False


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_shape_before_start(self, rt):
        s = rt.status()
        assert "running" in s
        assert "uptime_seconds" in s
        assert "port" in s
        assert "thought_loop" in s
        assert "bridge" in s

    def test_status_running_false_before_start(self, rt):
        assert rt.status()["running"] is False

    def test_status_running_true_after_start(self, started_rt):
        assert started_rt.status()["running"] is True

    def test_status_port_matches(self, started_rt):
        assert started_rt.status()["port"] == started_rt._port

    def test_uptime_increases(self, started_rt):
        time.sleep(0.15)
        s = started_rt.status()
        assert s["uptime_seconds"] > 0.0

    def test_uptime_zero_before_start(self, rt):
        assert rt.status()["uptime_seconds"] == 0.0


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def _get(self, path: str, port: int) -> dict:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=2
        ) as resp:
            return json.loads(resp.read())

    def test_health_returns_ok(self, started_rt):
        body = self._get("/runtime/health", started_rt._port)
        assert body["status"] == "ok"
        assert body["running"] is True

    def test_status_endpoint_returns_dict(self, started_rt):
        body = self._get("/runtime/status", started_rt._port)
        assert "running" in body
        assert "thought_loop" in body

    def test_unknown_path_returns_404(self, started_rt):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._get("/runtime/undefined", started_rt._port)
        assert exc_info.value.code == 404

    def test_health_server_thread_is_daemon(self, started_rt):
        """Health thread must be daemon so process exits cleanly."""
        assert started_rt._health_thread is not None
        assert started_rt._health_thread.daemon is True

    def test_health_server_survives_port_bind_failure(self, tmp_path):
        """If port is already in use, runtime should still start without crashing."""
        port = _free_port()
        r1 = HypostasRuntime(state_dir=tmp_path / "r1", port=port)
        r2 = HypostasRuntime(state_dir=tmp_path / "r2", port=port)  # same port!
        r1.start()
        time.sleep(0.05)
        r2.start()  # port clash — should log warning and continue
        assert r2._running is True  # still running even if health server failed
        assert r2._health_httpd is None  # could not bind
        r2.stop()
        r1.stop()


# ---------------------------------------------------------------------------
# SYSTEM_EVENT logging
# ---------------------------------------------------------------------------

class TestEventLogging:
    def test_runtime_started_event_logged(self, started_rt):
        events = started_rt.context.get_events_by_type("SYSTEM_EVENT", hours=1)
        assert any(e.get("event") == "runtime_started" for e in events)

    def test_runtime_stopped_event_logged(self, started_rt):
        started_rt.stop()
        events = started_rt.context.get_events_by_type("SYSTEM_EVENT", hours=1)
        assert any(e.get("event") == "runtime_stopped" for e in events)

    def test_runtime_started_event_has_port(self, started_rt):
        events = started_rt.context.get_events_by_type("SYSTEM_EVENT", hours=1)
        started = [e for e in events if e.get("event") == "runtime_started"]
        assert len(started) >= 1
        assert started[0].get("port") == started_rt._port

    def test_stopped_event_has_uptime(self, started_rt):
        time.sleep(0.1)
        started_rt.stop()
        events = started_rt.context.get_events_by_type("SYSTEM_EVENT", hours=1)
        stopped = [e for e in events if e.get("event") == "runtime_stopped"]
        assert len(stopped) >= 1
        assert stopped[0].get("uptime_seconds") > 0


# ---------------------------------------------------------------------------
# Daemon integration (optional)
# ---------------------------------------------------------------------------

class TestDaemonIntegration:
    def test_daemon_none_does_not_attach_bridge(self, tmp_path):
        r = HypostasRuntime(state_dir=tmp_path, daemon=None, port=_free_port())
        r.start()
        assert r.bridge._attached is False
        r.stop()

    def test_daemon_provided_attaches_bridge(self, tmp_path):
        mock_daemon = MagicMock()
        mock_daemon.runtime_bridge = None
        mock_bus = MagicMock()
        mock_daemon.bus = mock_bus

        r = HypostasRuntime(state_dir=tmp_path, daemon=mock_daemon, port=_free_port())
        r.start()
        assert r.bridge._attached is True
        r.stop()


# ---------------------------------------------------------------------------
# Uptime accumulation
# ---------------------------------------------------------------------------

class TestUptimeAccumulation:
    def test_total_uptime_accumulated_on_stop(self, tmp_path):
        r = HypostasRuntime(state_dir=tmp_path, port=_free_port())
        r.start()
        time.sleep(0.2)
        r.stop()
        saved = r.state.get("meta.total_uptime_seconds")
        assert saved is not None
        assert saved > 0.0

    def test_state_file_exists_after_stop(self, tmp_path):
        r = HypostasRuntime(state_dir=tmp_path, port=_free_port())
        r.start()
        r.stop()
        assert (tmp_path / "hypostas-state.json").exists()
