"""
SENSORS — Unified Sensor Manager for HypostasRuntime
=======================================================
Ported from v1 pulse.src.sensors into v2 HypostasRuntime.

Wraps all sensor types into a single SensorManager that polls on
configurable intervals. Findings go into:
  1. Hot tier via ContextEngine.log_event()
  2. StateEngine under sensors.last_readings.<sensor_name>

All sensors fail gracefully — if a dependency is missing (no icalBuddy,
no network, no git), the sensor logs a warning and returns empty data.

All state persisted via StateEngine under ``sensors.*`` dot-paths.
"""

from __future__ import annotations

import glob
import http.client
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .context_engine import ContextEngine

logger = logging.getLogger("pulse.runtime.sensors")


# ---------------------------------------------------------------------------
# Base Sensor
# ---------------------------------------------------------------------------

class BaseSensor:
    """Base class for v2 runtime sensors."""

    name: str = "base"
    poll_interval_seconds: int = 300  # 5 minutes default

    def __init__(self) -> None:
        self._last_run: float = 0.0
        self._last_finding: Optional[dict] = None

    def is_due(self) -> bool:
        """Check if enough time has elapsed since last poll."""
        return (time.time() - self._last_run) >= self.poll_interval_seconds

    def poll(self) -> dict:
        """Override in subclasses. Return findings dict."""
        return {}

    def safe_poll(self) -> dict:
        """Poll with error handling. Never raises."""
        try:
            finding = self.poll()
            self._last_run = time.time()
            self._last_finding = finding
            return finding
        except Exception as exc:
            logger.warning("Sensor '%s' error: %s", self.name, exc)
            return {"error": str(exc), "sensor": self.name}


# ---------------------------------------------------------------------------
# GitSensor
# ---------------------------------------------------------------------------

class GitSensor(BaseSensor):
    """Monitor git repos for new commits, changed files, branch activity."""

    name = "git"
    poll_interval_seconds = 600  # 10 minutes

    REPOS = [
        Path("~/.openclaw/workspace").expanduser(),
        Path("~/.openclaw/workspace/pulse").expanduser(),
    ]

    def poll(self) -> dict:
        repos: List[dict] = []
        for repo_path in self.REPOS:
            if not repo_path.is_dir():
                continue
            info = self._check_repo(repo_path)
            if info:
                repos.append(info)

        events = []
        for r in repos:
            if r.get("uncommitted_changes", 0) > 0 or r.get("untracked_files", 0) > 0:
                events.append({"event": "GIT_DIRTY", "path": r["path"]})
            if r.get("commits_ahead", 0) > 0:
                events.append({"event": "GIT_UNPUSHED", "path": r["path"], "ahead": r["commits_ahead"]})

        return {
            "repos": repos,
            "events": events,
            "timestamp": time.time(),
        }

    def _check_repo(self, path: Path) -> Optional[dict]:
        """Check a single git repo."""
        try:
            # Verify it's a git repo
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(path), capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None

            info: dict = {"path": str(path)}

            # Status
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(path), capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                lines = [l for l in result.stdout.strip().splitlines() if l]
                info["uncommitted_changes"] = sum(1 for l in lines if not l.startswith("??"))
                info["untracked_files"] = sum(1 for l in lines if l.startswith("??"))

            # Branch
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=str(path), capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                info["branch"] = result.stdout.strip()

            # Commits ahead/behind
            result = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
                cwd=str(path), capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) == 2:
                    info["commits_ahead"] = int(parts[0])
                    info["commits_behind"] = int(parts[1])

            # Last commit age
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=str(path), capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                epoch = float(result.stdout.strip())
                info["last_commit_minutes_ago"] = round((time.time() - epoch) / 60, 1)

            return info
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.debug("GitSensor: error checking %s: %s", path, exc)
            return None


# ---------------------------------------------------------------------------
# CalendarSensor
# ---------------------------------------------------------------------------

class CalendarSensor(BaseSensor):
    """Read upcoming calendar events (next 24h) via icalBuddy CLI."""

    name = "calendar"
    poll_interval_seconds = 300  # 5 minutes

    def poll(self) -> dict:
        # Check if icalBuddy is available
        ical_path = shutil.which("icalBuddy")
        if not ical_path:
            return {"available": False, "events": [], "note": "icalBuddy not installed"}

        try:
            result = subprocess.run(
                ["icalBuddy", "-n", "-ea", "-nc", "-li", "10",
                 "eventsFrom:today", "to:today+1"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"available": True, "events": [], "error": result.stderr.strip()}

            events = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and not line.startswith("•"):
                    # Event title lines
                    events.append({"summary": line[:200]})

            event_types = []
            if events:
                event_types.append({
                    "event": "CALENDAR_EVENT_UPCOMING",
                    "count": len(events),
                })

            return {
                "available": True,
                "events": events[:10],
                "event_count": len(events),
                "upcoming_events": event_types,
                "timestamp": time.time(),
            }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.debug("CalendarSensor error: %s", exc)
            return {"available": False, "events": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# DiscordSensor
# ---------------------------------------------------------------------------

class DiscordSensor(BaseSensor):
    """Check if Discord bots are active.

    NOTE:
    - The original implementation polled `http://127.0.0.1:9720/api/sessions`.
      Port 9720 is Pulse itself, so this produced noisy 404s in Pulse logs and
      artificially inflated the `system` drive.

    Preferred source of truth is OpenClaw's local sessions registry file.
    """

    name = "discord"
    poll_interval_seconds = 300  # 5 minutes

    # OpenClaw session registry (file-based, no network).
    SESSIONS_FILE = Path("~/.openclaw/agents/main/sessions/sessions.json").expanduser()

    # Treat a Discord session as "active" if updated recently.
    ACTIVE_WINDOW_SECONDS = 15 * 60

    def poll(self) -> dict:
        # 1) Preferred: file-based sessions registry.
        try:
            if self.SESSIONS_FILE.exists():
                raw = self.SESSIONS_FILE.read_text(errors="ignore")
                data = json.loads(raw) if raw.strip() else {}
                if isinstance(data, dict):
                    now_ms = time.time() * 1000
                    active_keys = []
                    all_discord_keys = []
                    for k, v in data.items():
                        if ":discord:" not in str(k):
                            continue
                        all_discord_keys.append(k)
                        updated_at = 0
                        if isinstance(v, dict):
                            updated_at = int(v.get("updatedAt") or 0)
                        if updated_at and (now_ms - updated_at) <= (self.ACTIVE_WINDOW_SECONDS * 1000):
                            active_keys.append(k)

                    active = len(active_keys) > 0
                    return {
                        "active": active,
                        "event": "DISCORD_ACTIVE" if active else "DISCORD_QUIET",
                        "source": "sessions_file",
                        "session_count": len(data),
                        "discord_session_count": len(all_discord_keys),
                        "active_discord_session_count": len(active_keys),
                        "timestamp": time.time(),
                    }

                # If file exists but isn't a dict, fall through to legacy.
        except Exception as exc:
            logger.debug("DiscordSensor: sessions file read failed (%s)", exc)

        # 2) Legacy fallback: best-effort HTTP probe (kept for portability).
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 9720, timeout=5)
            conn.request("GET", "/api/sessions")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()

            if resp.status == 200:
                data = json.loads(body)
                sessions = data if isinstance(data, list) else data.get("sessions", [])
                discord_sessions = [
                    s
                    for s in sessions
                    if isinstance(s, dict) and "discord" in str(s.get("channel", "")).lower()
                ]
                active = len(discord_sessions) > 0
                return {
                    "active": active,
                    "event": "DISCORD_ACTIVE" if active else "DISCORD_QUIET",
                    "source": "http_legacy",
                    "session_count": len(sessions),
                    "discord_session_count": len(discord_sessions),
                    "timestamp": time.time(),
                }

            return {
                "active": False,
                "event": "DISCORD_QUIET",
                "source": "http_legacy",
                "error": f"HTTP {resp.status}",
            }
        except Exception as exc:
            logger.debug("DiscordSensor error: %s", exc)
            return {
                "active": False,
                "event": "DISCORD_QUIET",
                "source": "http_legacy",
                "error": str(exc),
            }


# ---------------------------------------------------------------------------
# TwitterSensor
# ---------------------------------------------------------------------------

class TwitterSensor(BaseSensor):
    """Check for queued X/Twitter replies in workspace memory files."""

    name = "twitter"
    poll_interval_seconds = 600  # 10 minutes

    QUEUE_PATTERNS = [
        Path("~/.openclaw/workspace/memory").expanduser(),
    ]

    def poll(self) -> dict:
        queued_files: List[str] = []
        total_items = 0

        for search_dir in self.QUEUE_PATTERNS:
            if not search_dir.is_dir():
                continue
            for f in search_dir.iterdir():
                if f.is_file() and "x-reply-queue" in f.name.lower() and f.suffix == ".md":
                    try:
                        content = f.read_text(errors="ignore")
                        lines = [l for l in content.strip().splitlines() if l.strip() and not l.startswith("#")]
                        if lines:
                            queued_files.append(str(f))
                            total_items += len(lines)
                    except OSError:
                        pass

        has_queue = total_items > 0
        return {
            "queued": has_queue,
            "event": "X_REPLIES_QUEUED" if has_queue else "X_QUEUE_EMPTY",
            "queued_items": total_items,
            "queue_files": queued_files,
            "timestamp": time.time(),
        }


# ---------------------------------------------------------------------------
# WebSensor
# ---------------------------------------------------------------------------

class WebSensor(BaseSensor):
    """Check if key services are reachable."""

    name = "web"
    poll_interval_seconds = 120  # 2 minutes

    ENDPOINTS = [
        ("runtime", "http://127.0.0.1:9723/runtime/health"),
        ("biosensor_bridge", "http://127.0.0.1:9721/health"),
    ]

    def poll(self) -> dict:
        results: Dict[str, dict] = {}
        events: List[dict] = []

        for name, url in self.ENDPOINTS:
            status = self._check_endpoint(url)
            results[name] = status
            event_type = "SERVICE_HEALTHY" if status["healthy"] else "SERVICE_DOWN"
            events.append({"event": event_type, "service": name, "url": url})

        all_healthy = all(r["healthy"] for r in results.values())
        return {
            "services": results,
            "events": events,
            "all_healthy": all_healthy,
            "timestamp": time.time(),
        }

    def _check_endpoint(self, url: str) -> dict:
        """Check a single HTTP endpoint."""
        try:
            # Parse URL manually to use http.client
            if url.startswith("http://"):
                host_port = url[7:].split("/", 1)
                host_part = host_port[0]
                path = "/" + host_port[1] if len(host_port) > 1 else "/"
                if ":" in host_part:
                    host, port_str = host_part.split(":", 1)
                    port = int(port_str)
                else:
                    host = host_part
                    port = 80
            else:
                return {"healthy": False, "error": "unsupported scheme"}

            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return {"healthy": resp.status == 200, "status_code": resp.status}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# PulseHealthSensor
# ---------------------------------------------------------------------------

class PulseHealthSensor(BaseSensor):
    """Check LaunchAgent plist status, runtime uptime, test suite last run."""

    name = "pulse_health"
    poll_interval_seconds = 600  # 10 minutes

    PLIST_NAME = "com.pulse.daemon"

    def poll(self) -> dict:
        result: dict = {"timestamp": time.time()}

        # Check LaunchAgent
        try:
            proc = subprocess.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                plist_running = self.PLIST_NAME in proc.stdout
                result["launchagent_active"] = plist_running
            else:
                result["launchagent_active"] = None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            result["launchagent_active"] = None

        # Check runtime health endpoint
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 9723, timeout=5)
            conn.request("GET", "/runtime/health")
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            result["runtime_healthy"] = resp.status == 200
            result["runtime_uptime"] = body.get("uptime_seconds", 0)
        except Exception:
            result["runtime_healthy"] = False
            result["runtime_uptime"] = 0

        # Check last test run
        test_dir = Path("~/.openclaw/workspace/pulse").expanduser()
        pytest_cache = test_dir / ".pytest_cache"
        if pytest_cache.exists():
            try:
                age_hours = (time.time() - pytest_cache.stat().st_mtime) / 3600
                result["tests_last_run_hours"] = round(age_hours, 1)
            except OSError:
                result["tests_last_run_hours"] = None
        else:
            result["tests_last_run_hours"] = None

        # Determine overall health
        healthy = result.get("runtime_healthy", False)
        result["event"] = "PULSE_HEALTHY" if healthy else "PULSE_DEGRADED"
        return result


# ---------------------------------------------------------------------------
# SensorManager
# ---------------------------------------------------------------------------

class SensorManager:
    """Unified sensor manager for HypostasRuntime.

    Wraps all sensor types. Each sensor polls its source and writes
    findings to the hot tier via ContextEngine and StateEngine.
    """

    def __init__(self, state: "StateEngine", context: "ContextEngine") -> None:
        self._state = state
        self._context = context
        self._sensors: List[BaseSensor] = [
            GitSensor(),
            CalendarSensor(),
            DiscordSensor(),
            TwitterSensor(),
            WebSensor(),
            PulseHealthSensor(),
        ]

    def run_all(self) -> dict:
        """Run all sensors regardless of interval, return findings dict."""
        findings: Dict[str, dict] = {}
        for sensor in self._sensors:
            finding = sensor.safe_poll()
            findings[sensor.name] = finding
            self._record_finding(sensor.name, finding)
        return findings

    def status(self) -> dict:
        """Last findings from each sensor."""
        result: Dict[str, Any] = {}
        for sensor in self._sensors:
            result[sensor.name] = {
                "last_run": sensor._last_run,
                "last_finding": sensor._last_finding,
                "poll_interval": sensor.poll_interval_seconds,
                "is_due": sensor.is_due(),
            }
        return result

    def tick(self) -> None:
        """Called by ThoughtLoop — run sensors that are due."""
        for sensor in self._sensors:
            if sensor.is_due():
                finding = sensor.safe_poll()
                self._record_finding(sensor.name, finding)

    def _record_finding(self, sensor_name: str, finding: dict) -> None:
        """Write finding to hot tier and StateEngine."""
        try:
            self._context.log_event({
                "type": "SENSOR_READING",
                "content": finding,
                "source": sensor_name,
            })
        except Exception as exc:
            logger.debug("Sensor %s: failed to log event: %s", sensor_name, exc)

        try:
            self._state.set(f"sensors.last_readings.{sensor_name}", finding)
        except Exception as exc:
            logger.debug("Sensor %s: failed to update state: %s", sensor_name, exc)
