"""
Calendar Sensor — Phase 3 Integration

Monitors upcoming calendar events and feeds the agent's ``unfinished``
drive — so it becomes more alert and focused as scheduled work approaches.

Two backends, tried in order:
  1. **macOS / Apple Calendar** — via ``osascript``; zero extra dependencies.
     Works wherever macOS Calendar.app is configured (personal, Exchange,
     Google, iCloud, CalDAV — Calendar.app handles them all).
  2. **ICS file** — parses one or more local ``.ics`` files using stdlib only.
     Cross-platform fallback; useful for exported calendars, CI environments,
     or Linux deployments.

Architecture:
  - Checks for events within a configurable look-ahead window
    (``lookahead_minutes``, default 120 min / 2 hours).
  - An "imminent" threshold (``imminent_threshold_minutes``, default 30 min)
    triggers a stronger drive spike — something is *starting very soon*.
  - Respects ``check_interval_minutes`` so fast Pulse cycles don't hammer
    osascript or re-parse large ICS files unnecessarily.
  - State (last-check timestamp + seen event IDs) is persisted to disk via
    a JSON state file so restart-idempotency is preserved.
  - Graceful degradation: if both backends fail, sensor returns empty data
    and logs a warning — Pulse keeps running.

Config (pulse.yaml):
  sensors:
    calendar:
      enabled: true
      backend: auto           # "auto" | "macos" | "ics"
      ics_paths: []           # paths to .ics files (used when backend="ics" or "auto" fallback)
      lookahead_minutes: 120  # how far ahead to scan for events
      imminent_threshold_minutes: 30   # events within this window = imminent
      check_interval_minutes: 5        # minimum gap between full scans
      request_timeout: 5               # osascript timeout (seconds)

Reported keys:
  events_soon             bool   — True if ≥1 event in lookahead window
  event_count             int    — number of upcoming events found
  next_event_minutes      int    — minutes until nearest event (-1 = none)
  imminent_event          bool   — True if next event ≤ imminent_threshold_minutes
  backend_used            str    — "macos" | "ics" | "none"
  timestamp               float  — epoch of this read
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import BaseSensor

logger = logging.getLogger("pulse.sensors.calendar")


# ---------------------------------------------------------------------------
# ICS parsing helpers (stdlib-only)
# ---------------------------------------------------------------------------

def _parse_ics_datetime(value: str) -> Optional[datetime]:
    """Parse an iCalendar DTSTART/DTEND value to an aware datetime.

    Handles:
      - ``TZID=America/New_York:20260308T090000``  (tz prefix, local)
      - ``20260308T090000Z``                         (UTC Z-suffix)
      - ``20260308T090000``                          (floating, treated as UTC)
      - ``20260308``                                  (date-only, treated as UTC midnight)
    """
    # Strip any TZID= parameter prefix (we treat all as UTC for simplicity)
    if ":" in value:
        value = value.split(":", 1)[1]
    value = value.strip()

    try:
        if value.endswith("Z"):
            return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc
            )
        if "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            return dt.replace(tzinfo=timezone.utc)
        # Date-only
        dt = datetime.strptime(value, "%Y%m%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_ics_events(ics_text: str, now: datetime, lookahead: timedelta) -> List[Dict]:
    """Extract VEVENT blocks from ICS text; return those within the lookahead window.

    Each returned dict has: ``uid``, ``summary``, ``start_dt``, ``minutes_away``.
    """
    events: List[Dict] = []
    window_end = now + lookahead

    # Split into VEVENT blocks
    vevent_pattern = re.compile(
        r"BEGIN:VEVENT(.*?)END:VEVENT", re.DOTALL
    )

    for match in vevent_pattern.finditer(ics_text):
        block = match.group(1)

        uid_m = re.search(r"^UID[^:]*:(.+)$", block, re.MULTILINE)
        summary_m = re.search(r"^SUMMARY[^:]*:(.+)$", block, re.MULTILINE)
        dtstart_m = re.search(r"^DTSTART[^:]*:(.+)$", block, re.MULTILINE)

        if not dtstart_m:
            continue

        uid = uid_m.group(1).strip() if uid_m else ""
        summary = summary_m.group(1).strip() if summary_m else "(no title)"
        start_dt = _parse_ics_datetime(dtstart_m.group(1).strip())

        if start_dt is None:
            continue

        # Only include events starting within (now, window_end]
        if now < start_dt <= window_end:
            minutes_away = int((start_dt - now).total_seconds() / 60)
            events.append(
                {
                    "uid": uid,
                    "summary": summary,
                    "start_dt": start_dt,
                    "minutes_away": minutes_away,
                }
            )

    events.sort(key=lambda e: e["start_dt"])
    return events


# ---------------------------------------------------------------------------
# macOS Apple Calendar backend via osascript
# ---------------------------------------------------------------------------

_OSASCRIPT_TEMPLATE = r"""
tell application "Calendar"
    set _now to current date
    set _end to _now + ({lookahead} * minutes)
    set _results to {}
    repeat with c in (every calendar)
        set _evts to (every event of c whose start date > _now and start date ≤ _end)
        repeat with e in _evts
            set _uid to uid of e
            set _title to summary of e
            set _start to start date of e
            set _mins to ((_start - _now) as integer) div 60
            set end of _results to (_uid & "|" & _title & "|" & (_mins as string))
        end repeat
    end repeat
    set _out to ""
    repeat with r in _results
        set _out to _out & r & "\n"
    end repeat
    _out
end tell
"""


async def _run_osascript(lookahead_minutes: int, timeout: int) -> Optional[str]:
    """Run the osascript and return raw output, or None on failure."""
    script = _OSASCRIPT_TEMPLATE.replace("{lookahead}", str(lookahead_minutes))
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("Calendar sensor: osascript timed out after %ds", timeout)
            return None

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.warning("Calendar sensor: osascript error: %s", err)
            return None

        return stdout.decode(errors="replace").strip()
    except FileNotFoundError:
        logger.debug("Calendar sensor: osascript not available (not macOS?)")
        return None
    except Exception as exc:
        logger.warning("Calendar sensor: osascript exception: %s", exc)
        return None


def _parse_osascript_output(raw: str, now: datetime) -> List[Dict]:
    """Parse the `uid|title|mins` lines from osascript output."""
    events: List[Dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        uid, summary, mins_str = parts
        try:
            minutes_away = int(mins_str.strip())
        except ValueError:
            continue
        if minutes_away < 0:
            continue
        start_dt = now + timedelta(minutes=minutes_away)
        events.append(
            {
                "uid": uid.strip(),
                "summary": summary.strip(),
                "start_dt": start_dt,
                "minutes_away": minutes_away,
            }
        )
    events.sort(key=lambda e: e["minutes_away"])
    return events


# ---------------------------------------------------------------------------
# Sensor class
# ---------------------------------------------------------------------------

@dataclass
class _SensorState:
    last_checked: float = 0.0
    last_seen_uids: List[str] = field(default_factory=list)


class CalendarSensor(BaseSensor):
    """Monitor Apple Calendar (macOS) or ICS files for upcoming events.

    Feeds:
      - ``calendar.events_soon``    → ``unfinished`` drive spike (0.1) — awareness
      - ``calendar.imminent_event`` → ``unfinished`` drive spike (0.2) — urgency
    """

    name = "calendar"

    def __init__(self, config: PulseConfig):
        self.config = config
        self.cal_cfg = config.sensors.calendar  # type: ignore[attr-defined]
        self._state: _SensorState = _SensorState()
        self._state_file: Optional[Path] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        state_dir = Path(self.config.state.dir).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = state_dir / "calendar_sensor_state.json"
        self._load_state()
        logger.info(
            "Calendar sensor initialised — backend=%s lookahead=%dmin imminent=%dmin",
            self.cal_cfg.backend,
            self.cal_cfg.lookahead_minutes,
            self.cal_cfg.imminent_threshold_minutes,
        )

    async def stop(self) -> None:
        self._save_state()

    # ------------------------------------------------------------------
    # Main read
    # ------------------------------------------------------------------

    async def read(self) -> dict:
        now_epoch = time.time()
        interval_sec = self.cal_cfg.check_interval_minutes * 60

        if (now_epoch - self._state.last_checked) < interval_sec:
            logger.debug(
                "Calendar sensor: inside check-interval (%ds remaining), skipping",
                int(interval_sec - (now_epoch - self._state.last_checked)),
            )
            return self._empty_result()

        self._state.last_checked = now_epoch
        now_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)

        events, backend_used = await self._fetch_events(now_dt)
        self._save_state()

        if not events:
            return {
                "events_soon": False,
                "event_count": 0,
                "next_event_minutes": -1,
                "imminent_event": False,
                "backend_used": backend_used,
                "timestamp": now_epoch,
            }

        next_mins = events[0]["minutes_away"]
        imminent = next_mins <= self.cal_cfg.imminent_threshold_minutes

        logger.info(
            "Calendar sensor: %d event(s) in next %dmin — next in %dmin%s [%s]",
            len(events),
            self.cal_cfg.lookahead_minutes,
            next_mins,
            " ⚡ IMMINENT" if imminent else "",
            backend_used,
        )

        return {
            "events_soon": True,
            "event_count": len(events),
            "next_event_minutes": next_mins,
            "imminent_event": imminent,
            "backend_used": backend_used,
            "timestamp": now_epoch,
        }

    # ------------------------------------------------------------------
    # Backend dispatch
    # ------------------------------------------------------------------

    async def _fetch_events(
        self, now_dt: datetime
    ) -> Tuple[List[Dict], str]:
        """Try backends in configured order; return (events, backend_name)."""
        backend = self.cal_cfg.backend
        lookahead = timedelta(minutes=self.cal_cfg.lookahead_minutes)

        if backend in ("auto", "macos"):
            raw = await _run_osascript(
                self.cal_cfg.lookahead_minutes, self.cal_cfg.request_timeout
            )
            if raw is not None:
                return _parse_osascript_output(raw, now_dt), "macos"
            if backend == "macos":
                # Explicit macos requested but unavailable — return empty
                return [], "none"
            # auto: fall through to ICS

        if backend in ("auto", "ics"):
            events = self._read_ics_files(now_dt, lookahead)
            if events is not None:
                return events, "ics"

        return [], "none"

    def _read_ics_files(
        self, now_dt: datetime, lookahead: timedelta
    ) -> Optional[List[Dict]]:
        """Parse all configured ICS files; return combined event list or None."""
        paths = getattr(self.cal_cfg, "ics_paths", [])
        if not paths:
            return None

        all_events: List[Dict] = []
        found_any = False

        for raw_path in paths:
            p = Path(raw_path).expanduser().resolve()
            if not p.is_file():
                logger.warning("Calendar sensor: ICS path not found: %s", p)
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                events = _parse_ics_events(text, now_dt, lookahead)
                all_events.extend(events)
                found_any = True
            except Exception as exc:
                logger.warning("Calendar sensor: failed to parse %s: %s", p, exc)

        if not found_any:
            return None

        all_events.sort(key=lambda e: e["start_dt"])
        return all_events

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if self._state_file and self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                self._state.last_checked = float(data.get("last_checked", 0.0))
                self._state.last_seen_uids = data.get("last_seen_uids", [])
            except Exception as exc:
                logger.warning("Calendar sensor: failed to load state: %s", exc)

    def _save_state(self) -> None:
        if self._state_file:
            try:
                payload = {
                    "last_checked": self._state.last_checked,
                    "last_seen_uids": self._state.last_seen_uids,
                }
                self._state_file.write_text(json.dumps(payload, indent=2))
            except Exception as exc:
                logger.warning("Calendar sensor: failed to save state: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_result() -> dict:
        return {
            "events_soon": False,
            "event_count": 0,
            "next_event_minutes": -1,
            "imminent_event": False,
            "backend_used": "none",
            "timestamp": time.time(),
        }
