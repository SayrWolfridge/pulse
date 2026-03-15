"""Tests for pulse.runtime.sensors — SensorManager + individual sensors."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pulse.src.runtime.sensors import (
    BaseSensor,
    CalendarSensor,
    DiscordSensor,
    GitSensor,
    PulseHealthSensor,
    SensorManager,
    TwitterSensor,
    WebSensor,
)
from pulse.src.runtime.state_engine import StateEngine


@pytest.fixture
def tmp_state(tmp_path):
    return StateEngine(tmp_path / "state.json")


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.log_event = MagicMock()
    return ctx


class TestBaseSensor:
    def test_is_due_initially(self):
        sensor = BaseSensor()
        assert sensor.is_due()

    def test_is_due_after_poll(self):
        sensor = BaseSensor()
        sensor.poll_interval_seconds = 9999
        sensor.safe_poll()
        assert not sensor.is_due()

    def test_safe_poll_catches_errors(self):
        class BadSensor(BaseSensor):
            name = "bad"
            def poll(self):
                raise RuntimeError("boom")
        s = BadSensor()
        result = s.safe_poll()
        assert "error" in result


class TestGitSensor:
    def test_poll_returns_dict(self):
        sensor = GitSensor()
        result = sensor.safe_poll()
        assert isinstance(result, dict)
        assert "repos" in result or "error" in result

    def test_poll_has_timestamp(self):
        sensor = GitSensor()
        result = sensor.safe_poll()
        if "error" not in result:
            assert "timestamp" in result


class TestCalendarSensor:
    @patch("pulse.src.runtime.sensors.shutil.which", return_value=None)
    def test_no_icalbuddy(self, mock_which):
        sensor = CalendarSensor()
        result = sensor.poll()
        assert result["available"] is False

    @patch("pulse.src.runtime.sensors.shutil.which", return_value="/usr/local/bin/icalBuddy")
    @patch("pulse.src.runtime.sensors.subprocess.run")
    def test_with_icalbuddy(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Team standup\nLunch meeting\n",
            stderr="",
        )
        sensor = CalendarSensor()
        result = sensor.poll()
        assert result["available"] is True
        assert result["event_count"] == 2


class TestDiscordSensor:
    @patch("pulse.src.runtime.sensors.http.client.HTTPConnection")
    def test_active_sessions(self, mock_conn_cls):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'[{"channel": "discord:general"}]'
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        sensor = DiscordSensor()
        result = sensor.poll()
        assert result["event"] == "DISCORD_ACTIVE"
        assert result["active"] is True

    @patch("pulse.src.runtime.sensors.http.client.HTTPConnection")
    def test_connection_error(self, mock_conn_cls):
        mock_conn_cls.side_effect = ConnectionRefusedError("no gateway")
        sensor = DiscordSensor()
        result = sensor.poll()
        assert result["active"] is False
        assert result["event"] == "DISCORD_QUIET"


class TestTwitterSensor:
    def test_no_queue_files(self, tmp_path):
        sensor = TwitterSensor()
        sensor.QUEUE_PATTERNS = [tmp_path]
        result = sensor.poll()
        assert result["queued"] is False
        assert result["event"] == "X_QUEUE_EMPTY"

    def test_with_queue_file(self, tmp_path):
        queue_file = tmp_path / "x-reply-queue-mar13.md"
        queue_file.write_text("# Queue\nReply to @someone\nReply to @another\n")
        sensor = TwitterSensor()
        sensor.QUEUE_PATTERNS = [tmp_path]
        result = sensor.poll()
        assert result["queued"] is True
        assert result["queued_items"] == 2
        assert result["event"] == "X_REPLIES_QUEUED"


class TestWebSensor:
    @patch("pulse.src.runtime.sensors.http.client.HTTPConnection")
    def test_healthy_endpoint(self, mock_conn_cls):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status":"ok"}'
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        sensor = WebSensor()
        result = sensor._check_endpoint("http://127.0.0.1:9723/runtime/health")
        assert result["healthy"] is True

    def test_unreachable_endpoint(self):
        sensor = WebSensor()
        result = sensor._check_endpoint("http://127.0.0.1:59999/nope")
        assert result["healthy"] is False


class TestPulseHealthSensor:
    def test_poll_returns_dict(self):
        sensor = PulseHealthSensor()
        result = sensor.safe_poll()
        assert isinstance(result, dict)
        assert "event" in result


class TestSensorManager:
    def test_init(self, tmp_state, mock_context):
        mgr = SensorManager(tmp_state, mock_context)
        assert len(mgr._sensors) == 6

    def test_run_all(self, tmp_state, mock_context):
        mgr = SensorManager(tmp_state, mock_context)
        findings = mgr.run_all()
        assert isinstance(findings, dict)
        assert len(findings) == 6
        # Should have logged events
        assert mock_context.log_event.called

    def test_status(self, tmp_state, mock_context):
        mgr = SensorManager(tmp_state, mock_context)
        status = mgr.status()
        assert "git" in status
        assert "calendar" in status
        assert "discord" in status

    def test_tick_respects_intervals(self, tmp_state, mock_context):
        mgr = SensorManager(tmp_state, mock_context)
        # First tick should run all (all are due)
        mgr.tick()
        first_call_count = mock_context.log_event.call_count
        assert first_call_count > 0

        # Second tick should run nothing (too soon)
        mock_context.log_event.reset_mock()
        mgr.tick()
        assert mock_context.log_event.call_count == 0

    def test_findings_stored_in_state(self, tmp_state, mock_context):
        mgr = SensorManager(tmp_state, mock_context)
        mgr.run_all()
        # Check that state was updated
        git_reading = tmp_state.get("sensors.last_readings.git")
        assert git_reading is not None
