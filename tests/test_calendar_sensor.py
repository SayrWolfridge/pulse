"""Tests for the Calendar sensor (Phase 3 integration)."""

import json
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pulse.src.core.config import PulseConfig, CalendarSensorConfig
from pulse.src.drives.engine import DriveEngine, Drive
from pulse.src.state.persistence import StatePersistence
from pulse.src.sensors.calendar_sensor import (
    CalendarSensor,
    _parse_ics_datetime,
    _parse_ics_events,
    _parse_osascript_output,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cal_config(tmp_path):
    config = PulseConfig()
    config.sensors.calendar = CalendarSensorConfig(
        enabled=True,
        backend="auto",
        ics_paths=[],
        lookahead_minutes=120,
        imminent_threshold_minutes=30,
        check_interval_minutes=5,
        request_timeout=5,
    )
    config.state.dir = str(tmp_path / "state")
    return config


@pytest.fixture
def sensor(cal_config):
    return CalendarSensor(cal_config)


# ---------------------------------------------------------------------------
# ICS datetime parsing
# ---------------------------------------------------------------------------


class TestParseIcsDatetime:
    def test_utc_z_suffix(self):
        dt = _parse_ics_datetime("20260308T090000Z")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 9
        assert dt.minute == 0

    def test_floating_treated_as_utc(self):
        dt = _parse_ics_datetime("20260308T150000")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 15

    def test_tzid_prefix_stripped(self):
        dt = _parse_ics_datetime("TZID=America/New_York:20260308T100000")
        assert dt is not None
        assert dt.hour == 10

    def test_date_only(self):
        dt = _parse_ics_datetime("20260308")
        assert dt is not None
        assert dt.hour == 0
        assert dt.month == 3
        assert dt.day == 8

    def test_invalid_returns_none(self):
        assert _parse_ics_datetime("not-a-date") is None
        assert _parse_ics_datetime("") is None


# ---------------------------------------------------------------------------
# ICS event parsing
# ---------------------------------------------------------------------------

ICS_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-001@test
SUMMARY:Team Standup
DTSTART:20260308T140000Z
DTEND:20260308T143000Z
END:VEVENT
BEGIN:VEVENT
UID:event-002@test
SUMMARY:Sprint Review
DTSTART:20260308T160000Z
DTEND:20260308T170000Z
END:VEVENT
BEGIN:VEVENT
UID:event-past@test
SUMMARY:Yesterday's Meeting
DTSTART:20260307T100000Z
DTEND:20260307T110000Z
END:VEVENT
BEGIN:VEVENT
UID:event-far@test
SUMMARY:Far Future Event
DTSTART:20260308T230000Z
DTEND:20260308T233000Z
END:VEVENT
END:VCALENDAR"""


class TestParseIcsEvents:
    def _now(self) -> datetime:
        # "now" = 2026-03-08 13:00 UTC — Standup is in 60 min, Sprint Review in 3h
        return datetime(2026, 3, 8, 13, 0, 0, tzinfo=timezone.utc)

    def test_returns_events_in_window(self):
        now = self._now()
        events = _parse_ics_events(ICS_SAMPLE, now, timedelta(minutes=120))
        uids = [e["uid"] for e in events]
        assert "event-001@test" in uids
        # Sprint Review is 180 min away — outside 120-min window
        assert "event-002@test" not in uids

    def test_excludes_past_events(self):
        now = self._now()
        events = _parse_ics_events(ICS_SAMPLE, now, timedelta(minutes=120))
        uids = [e["uid"] for e in events]
        assert "event-past@test" not in uids

    def test_excludes_events_beyond_lookahead(self):
        now = self._now()
        events = _parse_ics_events(ICS_SAMPLE, now, timedelta(minutes=60))
        # Only Standup (60 min) is within window; Sprint Review (3h) is not
        assert all(e["minutes_away"] <= 60 for e in events)

    def test_sorted_by_start_time(self):
        now = self._now()
        events = _parse_ics_events(ICS_SAMPLE, now, timedelta(hours=24))
        mins = [e["minutes_away"] for e in events]
        assert mins == sorted(mins)

    def test_minutes_away_correct(self):
        now = self._now()
        events = _parse_ics_events(ICS_SAMPLE, now, timedelta(minutes=120))
        standup = next(e for e in events if e["uid"] == "event-001@test")
        assert standup["minutes_away"] == 60

    def test_empty_ics_returns_empty_list(self):
        now = self._now()
        assert _parse_ics_events("", now, timedelta(hours=2)) == []

    def test_no_events_in_window_returns_empty(self):
        now = datetime(2026, 3, 8, 20, 0, 0, tzinfo=timezone.utc)
        events = _parse_ics_events(ICS_SAMPLE, now, timedelta(minutes=30))
        assert events == []


# ---------------------------------------------------------------------------
# osascript output parsing
# ---------------------------------------------------------------------------


class TestParseOsascriptOutput:
    def _now(self) -> datetime:
        return datetime(2026, 3, 8, 13, 0, 0, tzinfo=timezone.utc)

    def test_parses_single_event(self):
        raw = "abc-uid|Team Standup|45\n"
        events = _parse_osascript_output(raw, self._now())
        assert len(events) == 1
        assert events[0]["uid"] == "abc-uid"
        assert events[0]["summary"] == "Team Standup"
        assert events[0]["minutes_away"] == 45

    def test_parses_multiple_events_sorted(self):
        raw = "uid-b|Sprint Review|90\nuid-a|Standup|15\n"
        events = _parse_osascript_output(raw, self._now())
        assert events[0]["minutes_away"] == 15
        assert events[1]["minutes_away"] == 90

    def test_skips_malformed_lines(self):
        raw = "malformed\n|no-uid|missing-mins\nuid-ok|Good Event|20\n"
        events = _parse_osascript_output(raw, self._now())
        assert len(events) == 1
        assert events[0]["uid"] == "uid-ok"

    def test_skips_negative_minutes(self):
        raw = "uid-past|Past Event|-5\nuid-ok|Good Event|20\n"
        events = _parse_osascript_output(raw, self._now())
        assert len(events) == 1
        assert events[0]["uid"] == "uid-ok"

    def test_empty_output_returns_empty(self):
        events = _parse_osascript_output("", self._now())
        assert events == []


# ---------------------------------------------------------------------------
# CalendarSensor lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCalendarSensorLifecycle:
    async def test_initialize_creates_state_dir(self, sensor, tmp_path):
        await sensor.initialize()
        state_dir = Path(sensor.config.state.dir)
        assert state_dir.exists()

    async def test_stop_saves_state(self, sensor, tmp_path):
        await sensor.initialize()
        sensor._state.last_checked = 12345.0
        await sensor.stop()
        assert sensor._state_file is not None
        assert sensor._state_file.exists()
        data = json.loads(sensor._state_file.read_text())
        assert data["last_checked"] == 12345.0

    async def test_state_roundtrip(self, sensor, tmp_path):
        await sensor.initialize()
        sensor._state.last_checked = 99999.0
        sensor._save_state()
        sensor._state.last_checked = 0.0
        sensor._load_state()
        assert sensor._state.last_checked == 99999.0


# ---------------------------------------------------------------------------
# CalendarSensor.read() — interval throttling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCalendarSensorRead:
    async def test_skips_read_within_interval(self, sensor, tmp_path):
        await sensor.initialize()
        # Set last_checked to just now — next read should be skipped
        sensor._state.last_checked = time.time()
        result = await sensor.read()
        assert result["events_soon"] is False
        assert result["backend_used"] == "none"

    async def test_reads_when_interval_expired(self, sensor, tmp_path):
        await sensor.initialize()
        # Set last_checked way in the past
        sensor._state.last_checked = time.time() - 99999

        with patch(
            "pulse.src.sensors.calendar_sensor._run_osascript",
            new_callable=AsyncMock,
        ) as mock_osa:
            # osascript returns empty string → no events
            mock_osa.return_value = ""
            result = await sensor.read()

        assert result["timestamp"] > 0

    async def test_events_soon_from_macos_backend(self, sensor, tmp_path):
        await sensor.initialize()
        sensor._state.last_checked = 0  # force scan

        fake_osascript_output = "event-123|Team Standup|45\nevent-456|1:1|90\n"

        with patch(
            "pulse.src.sensors.calendar_sensor._run_osascript",
            new_callable=AsyncMock,
            return_value=fake_osascript_output,
        ):
            result = await sensor.read()

        assert result["events_soon"] is True
        assert result["event_count"] == 2
        assert result["next_event_minutes"] == 45
        assert result["imminent_event"] is False  # 45 min > 30 min threshold
        assert result["backend_used"] == "macos"

    async def test_imminent_event_flagged(self, sensor, tmp_path):
        await sensor.initialize()
        sensor._state.last_checked = 0

        # Event in 15 minutes → imminent
        with patch(
            "pulse.src.sensors.calendar_sensor._run_osascript",
            new_callable=AsyncMock,
            return_value="event-urgent|Critical Review|15\n",
        ):
            result = await sensor.read()

        assert result["imminent_event"] is True
        assert result["next_event_minutes"] == 15

    async def test_no_events_result(self, sensor, tmp_path):
        await sensor.initialize()
        sensor._state.last_checked = 0

        with patch(
            "pulse.src.sensors.calendar_sensor._run_osascript",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await sensor.read()

        assert result["events_soon"] is False
        assert result["event_count"] == 0
        assert result["next_event_minutes"] == -1
        assert result["imminent_event"] is False

    async def test_ics_fallback_when_osascript_fails(self, tmp_path):
        """If osascript returns None (not macOS), ICS backend is used."""
        config = PulseConfig()
        ics_file = tmp_path / "test.ics"

        # Write an ICS with an event 60 minutes from now (UTC)
        now_utc = datetime.now(timezone.utc)
        event_start = now_utc + timedelta(minutes=60)
        dtstart = event_start.strftime("%Y%m%dT%H%M%SZ")
        ics_content = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:fallback-event@test
SUMMARY:ICS Fallback Event
DTSTART:{dtstart}
END:VEVENT
END:VCALENDAR"""
        ics_file.write_text(ics_content)

        config.sensors.calendar = CalendarSensorConfig(
            enabled=True,
            backend="auto",
            ics_paths=[str(ics_file)],
            lookahead_minutes=120,
            imminent_threshold_minutes=30,
            check_interval_minutes=5,
            request_timeout=5,
        )
        config.state.dir = str(tmp_path / "state")
        s = CalendarSensor(config)
        await s.initialize()
        s._state.last_checked = 0

        with patch(
            "pulse.src.sensors.calendar_sensor._run_osascript",
            new_callable=AsyncMock,
            return_value=None,  # osascript failed
        ):
            result = await s.read()

        assert result["events_soon"] is True
        assert result["backend_used"] == "ics"


# ---------------------------------------------------------------------------
# Drive engine integration
# ---------------------------------------------------------------------------


class TestCalendarDriveIntegration:
    def _make_engine(self):
        config = PulseConfig()
        config.state.dir = tempfile.mkdtemp()
        sp = StatePersistence(config)
        engine = DriveEngine(config, sp)
        # Pre-seed the unfinished drive so spike tests can observe changes
        engine.drives["unfinished"] = Drive(
            name="unfinished", category="output", weight=1.0
        )
        return engine

    def test_events_soon_spikes_unfinished(self):
        engine = self._make_engine()
        before = engine.drives["unfinished"].pressure

        sensor_data = {
            "calendar": {
                "events_soon": True,
                "imminent_event": False,
                "event_count": 1,
                "next_event_minutes": 60,
            }
        }
        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["unfinished"].pressure > before

    def test_imminent_event_spikes_stronger(self):
        engine_soft = self._make_engine()
        engine_strong = self._make_engine()

        # Soft spike (events_soon only, no imminent)
        engine_soft._apply_sensor_spikes({
            "calendar": {"events_soon": True, "imminent_event": False}
        })

        # Stronger spike (imminent)
        engine_strong._apply_sensor_spikes({
            "calendar": {"events_soon": True, "imminent_event": True}
        })

        soft_pressure = engine_soft.drives["unfinished"].pressure
        strong_pressure = engine_strong.drives["unfinished"].pressure
        assert strong_pressure > soft_pressure

    def test_no_calendar_data_no_spike(self):
        engine = self._make_engine()
        before = engine.drives["unfinished"].pressure
        engine._apply_sensor_spikes({})
        # unfinished drive should not spike from empty sensor data
        assert engine.drives["unfinished"].pressure == before


# ---------------------------------------------------------------------------
# SensorManager registration
# ---------------------------------------------------------------------------


class TestCalendarSensorManagerRegistration:
    def test_registers_when_enabled_auto_backend(self, tmp_path):
        from pulse.src.sensors.manager import SensorManager

        config = PulseConfig()
        config.sensors.calendar = CalendarSensorConfig(
            enabled=True, backend="auto"
        )
        config.state.dir = str(tmp_path / "state")
        manager = SensorManager(config)
        names = [s.name for s in manager.sensors]
        assert "calendar" in names

    def test_registers_when_enabled_macos_backend(self, tmp_path):
        from pulse.src.sensors.manager import SensorManager

        config = PulseConfig()
        config.sensors.calendar = CalendarSensorConfig(
            enabled=True, backend="macos"
        )
        config.state.dir = str(tmp_path / "state")
        manager = SensorManager(config)
        names = [s.name for s in manager.sensors]
        assert "calendar" in names

    def test_not_registered_when_disabled(self, tmp_path):
        from pulse.src.sensors.manager import SensorManager

        config = PulseConfig()
        config.sensors.calendar = CalendarSensorConfig(enabled=False)
        config.state.dir = str(tmp_path / "state")
        manager = SensorManager(config)
        names = [s.name for s in manager.sensors]
        assert "calendar" not in names

    def test_not_registered_ics_backend_without_paths(self, tmp_path, caplog):
        from pulse.src.sensors.manager import SensorManager
        import logging

        config = PulseConfig()
        config.sensors.calendar = CalendarSensorConfig(
            enabled=True, backend="ics", ics_paths=[]
        )
        config.state.dir = str(tmp_path / "state")
        with caplog.at_level(logging.WARNING, logger="pulse.sensors"):
            manager = SensorManager(config)
        names = [s.name for s in manager.sensors]
        assert "calendar" not in names

    def test_config_yaml_round_trip(self, tmp_path):
        """CalendarSensorConfig survives a YAML config load round-trip."""
        import yaml

        yaml_str = """
sensors:
  calendar:
    enabled: true
    backend: ics
    ics_paths:
      - ~/calendar/work.ics
    lookahead_minutes: 90
    imminent_threshold_minutes: 20
    check_interval_minutes: 10
    request_timeout: 8
"""
        config = PulseConfig._from_dict(yaml.safe_load(yaml_str))
        cal = config.sensors.calendar
        assert cal.enabled is True
        assert cal.backend == "ics"
        assert cal.ics_paths == ["~/calendar/work.ics"]
        assert cal.lookahead_minutes == 90
        assert cal.imminent_threshold_minutes == 20
        assert cal.check_interval_minutes == 10
        assert cal.request_timeout == 8
