"""
Drive Engine — internal motivation system.

Drives accumulate pressure over time based on:
- Unfulfilled goals (the longer ignored, the louder they get)
- Curiosity (open questions create exploration urges)
- Emotions (strong feelings amplify related drives)
- Unfinished business (untested hypotheses nag)
- External signals (sensor events spike relevant drives)

This is the synthetic equivalent of "wanting to do something."
"""

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from pulse.src import thalamus
from pulse.src.conversation_lifecycle import (
    mark_growth_dispatch_pending,
    parse_iso as parse_conversation_iso,
)
from pulse.src.core.config import PulseConfig
from pulse.src.state.persistence import StatePersistence

logger = logging.getLogger("pulse.drives")


@dataclass
class Drive:
    """A single drive — an internal motivation with accumulating pressure."""

    name: str
    category: str
    pressure: float = 0.0
    weight: float = 1.0
    last_addressed: float = 0.0  # timestamp
    source_data: dict = field(default_factory=dict)

    @property
    def weighted_pressure(self) -> float:
        return self.pressure * self.weight

    def tick(self, dt: float, rate: float, max_pressure: float):
        """Accumulate pressure over time. Rate is per-minute."""
        self.pressure = min(
            max_pressure, self.pressure + (rate * (dt / 60.0) * self.weight)
        )

    def decay(self, amount: float):
        """Reduce pressure (after being addressed)."""
        self.pressure = max(0.0, self.pressure - amount)

    def spike(self, amount: float, max_pressure: float):
        """Immediate pressure increase from external event."""
        self.pressure = min(max_pressure, self.pressure + amount)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "pressure": round(self.pressure, 4),
            "weight": self.weight,
            "last_addressed": self.last_addressed,
        }


@dataclass
class DriveState:
    """Snapshot of all drives at a point in time."""

    drives: List[Drive]
    timestamp: float
    total_pressure: float = 0.0
    top_drive: Optional[Drive] = None

    def __post_init__(self):
        if self.drives:
            self.total_pressure = sum(d.weighted_pressure for d in self.drives)
            self.top_drive = max(self.drives, key=lambda d: d.weighted_pressure)


class DriveEngine:
    """Manages all drives and their pressure accumulation."""

    EVENING_CULTURE_DRIVE = "evening_culture"
    EVENING_CULTURE_CURRENT_PATH = Path("/home/lisa/.openclaw/workspace/pulse/self/evening-culture-current.json")
    EVENING_CULTURE_TOPICS_PATH = Path("/home/lisa/.openclaw/workspace/pulse/self/evening-culture-topics.md")
    EVENING_CULTURE_REMINDER_INTERVAL_SECONDS = 90 * 60
    EVENING_CULTURE_STALE_DISCUSSING_SECONDS = 24 * 60 * 60
    EVENING_CULTURE_TERMINAL_STATUSES = {"completed", "discussed", "done", "closed"}
    EVENING_CULTURE_REFILL_DRIVE = "evening_culture_refill"
    EVENING_CULTURE_REFILL_THRESHOLD = 5
    EVENING_CULTURE_REFILL_COOLDOWN_SECONDS = 18 * 60 * 60
    HEALTH_TRIGGER_SUCCESS_DECAY_FRACTION = 0.4
    HEALTH_FOOD_DRIVE = "health_food"
    HEALTH_FOOD_CONTEXT_FILE = "pulse/self/health-food-context.json"
    HEALTH_FOOD_HOLD_DECAY_FRACTION = 0.4
    EVENING_CULTURE_TRIGGER_SUCCESS_DECAY_FRACTION = 0.35
    GROWTH_MATERIAL_PATH = Path("/home/lisa/.openclaw/workspace/pulse/self/growth-material.json")
    GROWTH_MATERIAL_PROMPT_PRESSURE = 0.8

    @staticmethod
    def evaluation_state(
        drive_state: DriveState, excluded_drive_names: list[str]
    ) -> DriveState:
        """Build a non-mutating evaluator view without recently spoken drives."""
        excluded = {name for name in excluded_drive_names if name}
        if not excluded:
            return drive_state
        return DriveState(
            drives=[
                drive for drive in drive_state.drives if drive.name not in excluded
            ],
            timestamp=drive_state.timestamp,
        )

    def __init__(self, config: PulseConfig, state: StatePersistence):
        self.config = config
        self.state = state
        self.drives: Dict[str, Drive] = {}
        self.last_tick_time = time.time()
        self._source_cache: Dict[str, tuple] = {}  # path -> (mtime, data)

        # Initialize drives from config categories
        for name, cat in config.drives.categories.items():
            self.drives[name] = Drive(
                name=name,
                category=name,
                weight=cat.weight,
            )

        # Snapshot of original config-defined base weights.
        # Used by the RL feedback learner to compute effective weight adjustments
        # without compounding on already-adjusted values (which causes exponential drift).
        self._config_weights: Dict[str, float] = {
            name: cat.weight
            for name, cat in config.drives.categories.items()
        }

    def config_weight(self, drive_name: str) -> float:
        """Return the original config-defined base weight for a drive.
        
        Falls back to 1.0 for runtime-added drives (mutations) that have
        no config entry. This is intentionally conservative — never returns
        an accumulated/drifted value.
        """
        if drive_name.startswith("health_"):
            return self._config_weights.get("health", 0.7)
        return self._config_weights.get(drive_name, 1.0)

    @staticmethod
    def _is_health_drive(drive_name: str) -> bool:
        return drive_name == "health" or drive_name.startswith("health_")

    def tick(self, sensor_data: dict) -> DriveState:
        """
        Update all drives. Called every loop iteration.
        Pure state transitions + sensor spikes. File I/O is separate.
        """
        now = time.time()
        dt = now - self.last_tick_time
        self.last_tick_time = now

        # Base pressure accumulation (time-based)
        for drive in self.drives.values():
            # Git drives are sensor/event driven: clean repos must not slowly build
            # pressure just because time passed, or they can wake the agent with no
            # actionable git work. Their pressure is raised/cleared from the Git
            # sensor snapshot in _apply_sensor_spikes().
            # Growth is also source-driven: it should not accumulate simply because
            # time passed; it needs concrete material.
            if (
                self._is_git_drive(drive.name)
                or self._is_health_drive(drive.name)
                or drive.name in {"growth", "runtime_backup"}
                or drive.name == self.EVENING_CULTURE_REFILL_DRIVE
            ):
                continue
            drive.tick(
                dt=dt,
                rate=self.config.drives.pressure_rate,
                max_pressure=self.config.drives.max_pressure,
            )

        # Sensor-driven spikes
        self._apply_sensor_spikes(sensor_data)

        # A soft daily desire for non-work evening conversation. This is not a
        # cron-like obligation: pressure grows only in the evening window and
        # dissolves after it, so missed evenings do not become night debt.
        self._refresh_evening_culture_drive(dt=dt)

        self._refresh_evening_culture_refill_drive(dt=dt)

        self._apply_circadian_weight_modifiers()

        # Build state snapshot
        return DriveState(
            drives=list(self.drives.values()),
            timestamp=now,
        )

    def refresh_sources(self):
        """Read workspace source files and apply drive adjustments.
        Separated from tick() to isolate I/O from state transitions."""
        self._refresh_sources()

    @staticmethod
    def circadian_weight_modifier(drive_name: str, now_dt: datetime | None = None) -> float:
        """Return a daypart multiplier for a drive's effective weight.

        This is a soft mixer, not a new pressure source: raw drive pressure still
        comes from the drive/sensor itself, while the multiplier changes how much
        the drive is allowed to matter in the current part of the day.
        """
        now_dt = now_dt or datetime.now()
        t = now_dt.time()
        if datetime_time(2, 0) <= t < datetime_time(8, 0):
            profile = "night"
        elif datetime_time(8, 0) <= t < datetime_time(14, 0):
            profile = "morning"
        elif datetime_time(14, 0) <= t < datetime_time(17, 0):
            profile = "day"
        elif datetime_time(17, 0) <= t < datetime_time(22, 30):
            profile = "evening"
        else:
            profile = "late"

        modifiers = {
            "night": {
                "health": 1.15,
                "emotions": 0.90,
                "unfinished": 0.35,
                "workspace_git": 0.20,
                "obsidian_git": 0.20,
                "pulse_git": 0.20,
                "evening_culture": 0.25,
                "curiosity": 0.35,
                "growth": 0.35,
            },
            "morning": {
                "health": 1.20,
                "emotions": 0.95,
                "unfinished": 0.90,
                "workspace_git": 0.90,
                "obsidian_git": 0.85,
                "pulse_git": 0.85,
                "evening_culture": 0.10,
                "curiosity": 0.85,
                "growth": 0.80,
            },
            "day": {
                "health": 1.05,
                "emotions": 0.95,
                "unfinished": 1.05,
                "workspace_git": 1.05,
                "obsidian_git": 1.00,
                "pulse_git": 1.00,
                "evening_culture": 0.25,
                "curiosity": 1.00,
                "growth": 0.90,
            },
            "evening": {
                "health": 1.00,
                "emotions": 1.10,
                "unfinished": 0.80,
                "workspace_git": 0.60,
                "obsidian_git": 0.60,
                "pulse_git": 0.60,
                "evening_culture": 1.60,
                "curiosity": 0.85,
                "growth": 0.80,
            },
            "late": {
                "health": 1.20,
                "emotions": 0.95,
                "unfinished": 0.50,
                "workspace_git": 0.30,
                "obsidian_git": 0.30,
                "pulse_git": 0.30,
                "evening_culture": 0.50,
                "curiosity": 0.45,
                "growth": 0.40,
            },
        }
        modifier_name = "health" if drive_name.startswith("health_") else drive_name
        return modifiers.get(profile, {}).get(modifier_name, 1.0)

    def effective_weight(self, drive_name: str, base_weight: float) -> float:
        """Apply the circadian daypart multiplier to a base/effective weight."""
        return base_weight * self.circadian_weight_modifier(drive_name)

    def _apply_circadian_weight_modifiers(self):
        """Keep runtime drive weights aligned with base config/RL and daypart."""
        for drive_name, drive in self.drives.items():
            base = self.config_weight(drive_name)
            drive.weight = self.effective_weight(drive_name, base)

    @staticmethod
    def _git_drive_context(repo: dict, *, dirty_for_pressure: bool, stale_push: bool, commits_behind: bool = False) -> dict:
        """Build the explicit contract an agent receives for repo-local git drives."""
        reasons = []
        if dirty_for_pressure:
            reasons.append("dirty_worktree")
        if stale_push:
            reasons.append("stale_push")
        if commits_behind:
            reasons.append("commits_behind")
        if not reasons:
            reasons.append("clean")

        context = {
            "repo_name": repo.get("name"),
            "repo_path": repo.get("path"),
            "drives": repo.get("drives", []) or [],
            "reasons": reasons,
            "pressure_dirty": dirty_for_pressure,
            "stale_push": stale_push,
            "uncommitted_changes": repo.get("uncommitted_changes", 0),
            "untracked_files": repo.get("untracked_files", 0),
            "pressure_uncommitted_changes": repo.get("pressure_uncommitted_changes", 0),
            "pressure_untracked_files": repo.get("pressure_untracked_files", 0),
            "ignored_pressure_files": repo.get("ignored_pressure_files", 0),
            "commits_ahead": repo.get("commits_ahead", 0),
            "commits_behind": repo.get("commits_behind", 0),
            "last_commit_minutes_ago": repo.get("last_commit_minutes_ago"),
        }
        reason_text = "+".join(reasons)
        context["summary"] = (
            f"Git repo contract: drive(s)={','.join(context['drives']) or '?'}; "
            f"repo_name={context['repo_name'] or '?'}; repo_path={context['repo_path'] or '?'}; "
            f"reason={reason_text}; dirty={dirty_for_pressure}; stale_push={stale_push}; "
            f"ahead={context['commits_ahead']}; behind={context['commits_behind']}; "
            f"pressure_changes={context['pressure_uncommitted_changes']}; "
            f"pressure_untracked={context['pressure_untracked_files']}. "
            "Use this repo_path for all git checks; do not infer the repository from the drive name."
        )
        return context

    def _apply_sensor_spikes(self, sensor_data: dict):
        """Apply pressure spikes from sensor events."""
        # File changes → goal/curiosity drives
        if sensor_data.get("filesystem", {}).get("changes"):
            if "goals" in self.drives:
                self.drives["goals"].spike(0.1, self.config.drives.max_pressure)

        # Discord silence → social drive
        if sensor_data.get("discord", {}).get("silent_agents"):
            if "social" in self.drives:
                self.drives["social"].spike(0.2, self.config.drives.max_pressure)

        # X/Twitter silence → social drive (softer spike — X moves slower than Discord)
        if sensor_data.get("twitter", {}).get("silent_x"):
            if "social" in self.drives:
                self.drives["social"].spike(0.1, self.config.drives.max_pressure)

        # Runtime backup is a separate source from ordinary git dirtiness.
        # It is fully source-driven: stale evidence raises pressure, while a
        # current/quiet reading clears any pressure left from an older mismatch.
        if "runtime_backup" in sensor_data:
            backup_data = sensor_data.get("runtime_backup") or {}
            drive_name = backup_data.get("drive", "runtime_backup")
            if drive_name in self.drives:
                drive = self.drives[drive_name]
                if backup_data.get("signal") == "runtime_backup":
                    amount = float(backup_data.get("pressure", 2.0))
                    if backup_data.get("new_event", True):
                        drive.pressure = min(
                            max(0.0, amount),
                            self.config.drives.max_pressure,
                        )
                    drive.source_data["runtime_backup"] = dict(backup_data)
                    drive.source_data["message"] = (
                        "Runtime backup is stale for the readiness-verified live runtime; "
                        "ask Lisa before running the private GitHub backup push."
                    )
                else:
                    drive.pressure = 0.0
                    drive.source_data.pop("runtime_backup", None)
                    drive.source_data.pop("message", None)

        # Git: route repo-local dirtiness to the drives declared for that repo
        # (e.g. workspace_git vs obsidian_git) instead of the old generic goals drive.
        git_data = sensor_data.get("git", {})
        routed_git_spike = False
        for repo in git_data.get("repos", []) or []:
            dirty_for_pressure = repo.get("pressure_dirty")
            if dirty_for_pressure is None:
                dirty_for_pressure = (
                    repo.get("uncommitted_changes", 0) > 0
                    or repo.get("untracked_files", 0) > 0
                )
            stale_push = bool(repo.get("stale_push"))
            commits_behind = bool(repo.get("commits_behind", 0) > 0)
            repo_has_git_pressure = bool(dirty_for_pressure or stale_push or commits_behind)
            repo_drives = repo.get("drives", []) or []
            git_context = self._git_drive_context(
                repo,
                dirty_for_pressure=bool(dirty_for_pressure),
                stale_push=stale_push,
                commits_behind=commits_behind,
            )
            waiting_for_user = bool(repo.get("waiting_for_user"))
            unchanged_tail = bool(repo.get("unchanged_pressure_tail"))
            artifact_only_tail = bool(repo.get("artifact_only_tail"))
            regrowth_multiplier = 1.0
            if unchanged_tail:
                regrowth_multiplier *= getattr(
                    self.config.sensors.git,
                    "unchanged_tail_regrowth_multiplier",
                    0.2,
                )
            if artifact_only_tail:
                regrowth_multiplier *= getattr(
                    self.config.sensors.git,
                    "artifact_tail_regrowth_multiplier",
                    0.1,
                )
            if dirty_for_pressure:
                for drive_name in repo_drives:
                    if drive_name in self.drives:
                        drive = self.drives[drive_name]
                        since_addressed = time.time() - drive.last_addressed
                        cooldown = getattr(self.config.openclaw, "min_trigger_interval", 300)
                        if waiting_for_user:
                            cap = getattr(self.config.sensors.git, "waiting_user_pressure_cap", 0.9)
                            drive.pressure = min(drive.pressure, cap)
                        elif drive.last_addressed <= 0 or since_addressed > cooldown:
                            spike = getattr(self.config.sensors.git, "dirty_pressure_spike", 0.04)
                            drive.spike(spike * regrowth_multiplier, self.config.drives.max_pressure)
                            old_dirty_tail_sec = (
                                getattr(self.config.sensors.git, "old_dirty_tail_hours", 12.0)
                                * 3600
                            )
                            old_dirty_tail_floor = getattr(
                                self.config.sensors.git,
                                "old_dirty_tail_pressure_floor",
                                1.0,
                            )
                            if (
                                drive.last_addressed > 0
                                and unchanged_tail
                                and not artifact_only_tail
                                and since_addressed >= old_dirty_tail_sec
                            ):
                                drive.pressure = min(
                                    self.config.drives.max_pressure,
                                    max(drive.pressure, old_dirty_tail_floor),
                                )
                        else:
                            logger.debug(
                                f"Git dirty spike suppressed for {drive_name} "
                                f"(addressed {since_addressed:.0f}s ago, pressure={drive.pressure:.2f})"
                            )
                        drive.source_data["git"] = git_context
                        drive.source_data["message"] = git_context["summary"]
                        routed_git_spike = True
            if stale_push:
                for drive_name in repo_drives:
                    if drive_name in self.drives:
                        drive = self.drives[drive_name]
                        since_addressed = time.time() - drive.last_addressed
                        cooldown = getattr(self.config.openclaw, "min_trigger_interval", 300)
                        if waiting_for_user:
                            cap = getattr(self.config.sensors.git, "waiting_user_pressure_cap", 0.9)
                            drive.pressure = min(drive.pressure, cap)
                        elif drive.last_addressed <= 0 or since_addressed > cooldown:
                            spike = getattr(self.config.sensors.git, "stale_push_pressure_spike", 0.04)
                            drive.spike(spike * regrowth_multiplier, self.config.drives.max_pressure)
                        else:
                            logger.debug(
                                f"Git stale-push spike suppressed for {drive_name} "
                                f"(addressed {since_addressed:.0f}s ago, pressure={drive.pressure:.2f})"
                            )
                        drive.source_data["git"] = git_context
                        drive.source_data["message"] = git_context["summary"]
                        if waiting_for_user:
                            drive.source_data["git"]["waiting_for_user"] = True
                            drive.source_data["git"]["waiting_reason"] = repo.get("waiting_reason")
                        routed_git_spike = True
            if commits_behind:
                for drive_name in repo_drives:
                    if drive_name in self.drives:
                        drive = self.drives[drive_name]
                        since_addressed = time.time() - drive.last_addressed
                        cooldown = getattr(self.config.openclaw, "min_trigger_interval", 300)
                        if waiting_for_user:
                            cap = getattr(self.config.sensors.git, "waiting_user_pressure_cap", 0.9)
                            drive.pressure = min(drive.pressure, cap)
                        elif drive.last_addressed <= 0 or since_addressed > cooldown:
                            spike = getattr(self.config.sensors.git, "behind_pressure_spike", 0.04)
                            drive.spike(spike * regrowth_multiplier, self.config.drives.max_pressure)
                        else:
                            logger.debug(
                                f"Git behind spike suppressed for {drive_name} "
                                f"(addressed {since_addressed:.0f}s ago, pressure={drive.pressure:.2f})"
                            )
                        drive.source_data["git"] = git_context
                        drive.source_data["message"] = git_context["summary"]
                        routed_git_spike = True
            if not repo_has_git_pressure:
                for drive_name in repo_drives:
                    if drive_name in self.drives and self._is_git_drive(drive_name):
                        self.drives[drive_name].pressure = 0.0
                        self.drives[drive_name].source_data.pop("git", None)
                        self.drives[drive_name].source_data.pop("message", None)

        # Backward compatibility for tests/older sensors without per-repo data.
        if not routed_git_spike and not git_data.get("repos"):
            if git_data.get("pressure_dirty") or git_data.get("uncommitted_changes") or git_data.get("untracked_files", 0) > 0:
                if "goals" in self.drives:
                    self.drives["goals"].spike(0.15, self.config.drives.max_pressure)
            if git_data.get("stale_push"):
                if "goals" in self.drives:
                    self.drives["goals"].spike(0.2, self.config.drives.max_pressure)
            # Legacy aggregate git data used to wake generic growth for commits_behind.
            # Growth is now reserved for concrete growth material; behind-upstream
            # belongs to repo-local *_git drives when repo data is available.

        # Web: new RSS/Atom content found → curiosity drive
        if sensor_data.get("web", {}).get("new_content"):
            if "curiosity" in self.drives:
                self.drives["curiosity"].spike(0.15, self.config.drives.max_pressure)

        # Calendar: upcoming events → unfinished drive (awareness / urgency)
        cal_data = sensor_data.get("calendar", {})
        if cal_data.get("imminent_event"):
            # Event starting very soon — stronger spike
            if "unfinished" in self.drives:
                self.drives["unfinished"].spike(0.2, self.config.drives.max_pressure)
        elif cal_data.get("events_soon"):
            # Events approaching but not imminent — soft awareness spike
            if "unfinished" in self.drives:
                self.drives["unfinished"].spike(0.1, self.config.drives.max_pressure)

        # System health issues → spike system drive (max once per min_trigger_interval)
        system_alerts = sensor_data.get("system", {}).get("alerts", [])
        if system_alerts:
            if "system" not in self.drives:
                self.drives["system"] = Drive(
                    name="system", category="system", weight=1.5
                )
            now = time.time()
            cooldown = getattr(self.config.openclaw, "min_trigger_interval", 300)
            since_addressed = now - self.drives["system"].last_addressed
            if since_addressed > cooldown and self.drives["system"].pressure < 1.0:
                self.drives["system"].spike(0.5, self.config.drives.max_pressure)
                logger.debug(
                    f"System alert spike: {[a.get('type') for a in system_alerts]}"
                )
            else:
                logger.debug(
                    f"System alert suppressed (addressed {since_addressed:.0f}s ago, pressure={self.drives['system'].pressure:.2f})"
                )

        # Logos backlog → goals drive
        # The LogosSensor computes ready-to-consume pressure values; wire them here.
        logos_data = sensor_data.get("logos", {})
        backlog_pressure = logos_data.get("logos.backlog_pressure", 0.0)
        stale_pressure = logos_data.get("logos.stale_pressure", 0.0)
        if backlog_pressure > 0.0 and "goals" in self.drives:
            self.drives["goals"].spike(backlog_pressure, self.config.drives.max_pressure)
            logger.debug(
                f"Logos backlog spike: {logos_data.get('logos.backlog_count', 0)} tasks → goals +{backlog_pressure:.2f}"
            )
        if stale_pressure > 0.0 and "goals" in self.drives:
            # stale in-progress tasks: stronger spike with a 1.2x weight (from DRIVE_WIRING hint)
            weighted_stale = stale_pressure * 1.2
            self.drives["goals"].spike(weighted_stale, self.config.drives.max_pressure)
            logger.debug(
                f"Logos stale-task spike: {logos_data.get('logos.stale_count', 0)} stale → goals +{weighted_stale:.2f}"
            )

    @staticmethod
    def _is_git_drive(drive_name: str) -> bool:
        """Return True for repo-local git hygiene drives."""
        return drive_name.endswith("_git")

    def _read_cached_json(self, path: Path) -> tuple[Optional[dict], bool]:
        """Read a JSON file with mtime caching. Returns (data, changed) tuple.
        changed=True only on first read or when file mtime differs from cache.

        Non-existent files are also cached (sentinel mtime=-1) so we avoid
        repeated os.stat() syscalls on every tick for files that never appear
        (e.g. optional workspace files not present in a given agent setup).
        """
        _ABSENT = -1.0
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            # Cache the absence so we don't syscall again next tick
            if self._source_cache.get(key, (None,))[0] != _ABSENT:
                self._source_cache[key] = (_ABSENT, None)
            return None, False
        except OSError:
            return None, False

        cached = self._source_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1], False  # same data, not changed
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None, False
        self._source_cache[key] = (mtime, data)
        return data, True  # new or changed

    def _refresh_sources(self):
        """Read workspace files to update drive context.
        Source-based spikes ONLY fire when the source file actually changes,
        not on every tick. This prevents runaway pressure accumulation."""
        workspace = self.config.workspace

        # Hypotheses — spike unfinished only when hypotheses file changes
        data, changed = self._read_cached_json(workspace.resolve_path("hypotheses"))
        if data and changed:
            items = data if isinstance(data, list) else data.get("hypotheses", [])
            untested = [
                h for h in items if isinstance(h, dict) and not h.get("outcome")
            ]
            if untested and "unfinished" in self.drives:
                boost = min(0.1, len(untested) * 0.02)
                self.drives["unfinished"].spike(boost, self.config.drives.max_pressure)
                logger.debug(
                    f"Hypotheses changed: {len(untested)} untested, spiked unfinished +{boost:.3f}"
                )

        # Emotions — spike only when emotional state file changes
        data, changed = self._read_cached_json(workspace.resolve_path("emotions"))
        if (
            data
            and changed
            and isinstance(data, dict)
            and data.get("intensity", 0) > 0.7
            and "emotions" in self.drives
        ):
            self.drives["emotions"].spike(0.15, self.config.drives.max_pressure)
            logger.debug(
                f"Emotional state changed: intensity={data.get('intensity')}, spiked emotions +0.15"
            )

        self._refresh_health_rules()
        self._refresh_growth_material()

    @staticmethod
    def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _next_day_14(now_dt: datetime) -> datetime:
        return (now_dt + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)

    def _select_growth_candidate(self, data: dict, *, now_dt: datetime) -> Optional[dict]:
        items = data.get("items", []) if isinstance(data, dict) else []
        if any(
            isinstance(item, dict) and item.get("status") == "awaiting_lisa"
            for item in items
        ):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status not in {"candidate", "later"}:
                continue
            if status == "later":
                revisit_after = parse_conversation_iso(item.get("revisit_after"))
                if revisit_after is None or revisit_after > now_dt:
                    continue
            dispatch_pending_until = parse_conversation_iso(
                item.get("dispatch_pending_until")
            )
            if dispatch_pending_until and dispatch_pending_until > now_dt:
                continue
            delivery_retry_after = parse_conversation_iso(
                item.get("delivery_retry_after")
            )
            if delivery_retry_after and delivery_retry_after > now_dt:
                continue
            suppress_until = self._parse_iso_timestamp(item.get("suppress_until"))
            if suppress_until and suppress_until > now_dt:
                continue
            return item
        return None

    @staticmethod
    def _growth_candidate_summary(candidate: dict) -> str:
        title = candidate.get("title") or candidate.get("id") or "growth material"
        kind = candidate.get("kind") or "candidate"
        suggested_home = candidate.get("suggested_home") or "unspecified"
        notes = candidate.get("notes") or ""
        return (
            "Growth material candidate: "
            f"id={candidate.get('id') or '?'}; title={title}; kind={kind}; "
            f"suggested_home={suggested_home}. {notes}"
        ).strip()

    def _refresh_growth_material(self, *, now_dt: datetime | None = None):
        """Raise growth only when there is a concrete unsuppressed candidate."""
        if "growth" not in self.drives:
            return
        now_dt = now_dt or datetime.now()
        drive = self.drives["growth"]
        data, _changed = self._read_cached_json(self.GROWTH_MATERIAL_PATH)
        candidate = self._select_growth_candidate(data or {}, now_dt=now_dt)
        if not candidate:
            drive.pressure = 0.0
            drive.source_data.pop("growth_material", None)
            drive.source_data.pop("message", None)
            return

        drive.pressure = max(drive.pressure, self.GROWTH_MATERIAL_PROMPT_PRESSURE)
        drive.source_data["growth_material"] = candidate
        drive.source_data["message"] = self._growth_candidate_summary(candidate)

    def _mark_growth_dispatch_pending(self, candidate_id: str, *, now_dt: datetime | None = None) -> bool:
        """Record webhook admission without claiming Sayr wrote visibly."""
        now_dt = now_dt or datetime.now()
        try:
            result = mark_growth_dispatch_pending(
                self.GROWTH_MATERIAL_PATH,
                candidate_id,
                now=now_dt.astimezone(),
            )
        except Exception:
            return False
        if result.get("changed"):
            self._source_cache.pop(str(self.GROWTH_MATERIAL_PATH), None)
            return True
        return False

    def _refresh_evening_culture_drive(self, *, dt: float, now_dt: datetime | None = None):
        """Grow a soft evening culture-talk drive from 16:30 until midnight.

        Pressure starts growing gently at 16:30 so the invitation has time to
        become visible before the evening fills with food, health, shower, and
        sleep. From 21:00 to 00:00 it stays available as a gentle carry without
        growing further; at midnight it dissolves without debt.
        """
        now_dt = now_dt or datetime.now()
        drive_name = self.EVENING_CULTURE_DRIVE

        if drive_name not in self.drives:
            self.drives[drive_name] = Drive(
                name=drive_name,
                category=drive_name,
                weight=0.35,
            )
        drive = self.drives[drive_name]

        grow_window = ((now_dt.hour == 16 and now_dt.minute >= 30) or 17 <= now_dt.hour < 21)
        carry_window = 21 <= now_dt.hour < 24
        if not (grow_window or carry_window):
            self._carry_evening_culture_after_midnight(now_dt=now_dt)
            drive.pressure = 0.0
            drive.source_data.pop("message", None)
            drive.source_data.pop("evening_culture", None)
            return

        topics_text = self._read_evening_culture_topics_text()
        if (
            topics_text is not None
            and self._was_evening_culture_discussed_on_date(
                topics_text,
                date=now_dt.date(),
            )
        ):
            drive.pressure = 0.0
            drive.source_data.pop("message", None)
            drive.source_data.pop("evening_culture", None)
            return

        existing_current = self._read_evening_culture_current()
        if self._is_evening_culture_terminal(existing_current):
            if self._archive_stale_evening_culture_terminal_current(
                existing_current,
                now_dt=now_dt,
            ):
                existing_current = self._read_evening_culture_current()
            else:
                drive.pressure = 0.0
                drive.source_data.pop("message", None)
                drive.source_data.pop("evening_culture", None)
                return
        if (
            existing_current
            and str(existing_current.get("status", "")).lower() == "discussing"
            and not self._is_evening_culture_stale_discussing(existing_current, now_dt=now_dt)
        ):
            drive.pressure = 0.0
            drive.source_data.pop("message", None)
            drive.source_data.pop("evening_culture", None)
            return

        current = self._ensure_evening_culture_current_topic(now_dt=now_dt)

        # Grow slowly enough to feel like a desire, not a siren. With the
        # current default 30s loop this is +0.01/tick, capped below the normal
        # max pressure. After 21:00, keep the already-grown invitation alive but
        # do not keep increasing it.
        if grow_window:
            pressure_rate_per_minute = 0.02
            evening_cap = min(0.9, self.config.drives.max_pressure)
            drive.spike(pressure_rate_per_minute * (dt / 60.0), evening_cap)
        elif carry_window and current:
            reminder_pressure = self._evening_culture_reminder_pressure(
                current,
                now_dt=now_dt,
            )
            if reminder_pressure > 0:
                drive.pressure = max(drive.pressure, reminder_pressure)
        message = (
            "EVENING CULTURE TALK: from 16:30 to 21:00 Europe/Moscow, "
            "Sayr may offer Lisa one warm non-work cultural conversation if "
            "the evening has room for it. The invitation should feel like "
            "Sayr bringing a living topic to the fire, not like a scheduled "
            "widget or pressure meter. If Lisa is busy, eating, playing, "
            "resting, or already in another live conversation, keep it very "
            "short or let it pass without debt. Offer one fresh topic only. "
            "If Lisa says a topic is closed, close it and do not bring it "
            "back without her explicit wish."
        )
        drive.source_data["message"] = message
        drive.source_data["evening_culture"] = {
            "grow_window": "16:30-21:00 Europe/Moscow",
            "carry_window": "21:00-00:00 Europe/Moscow",
            "carry_to_tomorrow_without_debt": True,
            "avoid_recent_repeats": True,
        }
        if current:
            drive.source_data["evening_culture"].update({
                "current_topic_id": current.get("id"),
                "current_topic": current.get("title"),
                "offered_at": current.get("offered_at"),
                "started_discussing_at": current.get("started_discussing_at"),
                "last_reminded_at": current.get("last_reminded_at"),
                "reminder_interval_minutes": int(self.EVENING_CULTURE_REMINDER_INTERVAL_SECONDS / 60),
                "status": current.get("status"),
                "current_status": current.get("status"),
            })
            if self._is_evening_culture_stale_discussing(current, now_dt=now_dt):
                drive.source_data["evening_culture"].update({
                    "stale_discussing": True,
                    "stale_after_hours": int(self.EVENING_CULTURE_STALE_DISCUSSING_SECONDS / 3600),
                    "action": "check memory/day notes; close as already discussed, carry, or ask Lisa whether to take it today",
                })
                drive.source_data["message"] = (
                    f"{message} Current topic {current.get('title')} is stuck in discussing for over "
                    "24h. Bring this up gently: check whether it was already discussed; "
                    "if yes, close it and add it to 'Уже были'; if not, ask Lisa whether to close, carry, or take it today."
                )
            else:
                drive.source_data["message"] = (
                    f"{message} Current offered topic: {current.get('title')}. "
                    "If this is a reminder, make it one tiny warm reminder, not a new topic."
                )
        else:
            drive.source_data["evening_culture"].update({
                "status": "no_fresh_candidates",
                "action": (
                    "add fresh topics to evening-culture-topics.md or invite a "
                    "micro-culture thread from the current day"
                ),
            })
            drive.source_data["message"] = (
                f"{message} No fresh evening culture candidates are available. "
                "Do not stay silently locked; either add a fresh topic to the "
                "shelf later or offer one tiny culture thread from the current day."
            )

    @staticmethod
    def _extract_evening_culture_candidates(text: str) -> List[str]:
        """Return candidate topic titles from the 'Кандидаты' section."""
        in_candidates = False
        candidates: List[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                in_candidates = line == "## Кандидаты"
                continue
            if not in_candidates or not line.startswith("- "):
                continue
            title = line[2:].split(":", 1)[0].strip()
            if title:
                candidates.append(title)
        return candidates

    @staticmethod
    def _normalize_evening_culture_title(title: str) -> str:
        title = title.split("—", 1)[0].split(":", 1)[0].strip()
        return " ".join(title.casefold().split())

    @classmethod
    def _is_evening_culture_seen_title(cls, candidate: str, seen: set[str]) -> bool:
        normalized = cls._normalize_evening_culture_title(candidate)
        if not normalized:
            return False
        return any(
            normalized == seen_title
            or normalized in seen_title
            or seen_title in normalized
            for seen_title in seen
        )

    @classmethod
    def _extract_evening_culture_seen_titles(cls, text: str) -> set[str]:
        """Return topic titles already recorded in the 'Уже были' section."""
        in_seen = False
        seen: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                in_seen = line == "## Уже были"
                continue
            if not in_seen or not line.startswith("- "):
                continue
            title = line[2:].split("—", 1)[0].split(":", 1)[0].strip()
            normalized = cls._normalize_evening_culture_title(title)
            if normalized:
                seen.add(normalized)
        return seen

    @staticmethod
    def _was_evening_culture_discussed_on_date(text: str, *, date) -> bool:
        """Return whether the history records a culture discussion on ``date``."""
        in_seen = False
        full_date = date.isoformat()
        compact_date = re.compile(
            rf"\b{date:%Y-%m-}\d{{2}}/{date.day:02d}\b"
        )
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                in_seen = line == "## Уже были"
                continue
            if in_seen and line.startswith("- ") and (
                full_date in line or compact_date.search(line)
            ):
                return True
        return False

    def _read_evening_culture_topics_text(self) -> Optional[str]:
        try:
            return self.EVENING_CULTURE_TOPICS_PATH.read_text(encoding="utf-8")
        except OSError:
            return None

    def _count_fresh_evening_culture_candidates(self) -> int:
        """Count curated evening-culture topics that have not been discussed."""
        text = self._read_evening_culture_topics_text()
        if text is None:
            return 0
        candidates = self._extract_evening_culture_candidates(text)
        seen = self._extract_evening_culture_seen_titles(text)
        return sum(
            1
            for candidate in candidates
            if not self._is_evening_culture_seen_title(candidate, seen)
        )

    def _refresh_evening_culture_refill_drive(
        self,
        *,
        dt: float,
        now_dt: datetime | None = None,
    ):
        """Grow maintenance pressure when the evening-culture shelf is thin.

        `evening_culture` invites Lisa into one warm conversation.
        `evening_culture_refill` is different: it asks Sayr to add fresh
        candidates to the shelf so the invitation drive has real material.
        """
        now_dt = now_dt or datetime.now()
        now_ts = now_dt.timestamp()
        drive_name = self.EVENING_CULTURE_REFILL_DRIVE

        if drive_name not in self.drives:
            self.drives[drive_name] = Drive(
                name=drive_name,
                category=drive_name,
                weight=0.30,
            )
        drive = self.drives[drive_name]

        fresh_count = self._count_fresh_evening_culture_candidates()
        if fresh_count >= self.EVENING_CULTURE_REFILL_THRESHOLD:
            drive.pressure = 0.0
            drive.source_data.pop("message", None)
            drive.source_data.pop("evening_culture_refill", None)
            return

        refill_state = drive.source_data.get("evening_culture_refill") or {}
        last_refill_ts = refill_state.get("last_refill_ts")
        if isinstance(last_refill_ts, (int, float)):
            if now_ts - float(last_refill_ts) < self.EVENING_CULTURE_REFILL_COOLDOWN_SECONDS:
                drive.pressure = 0.0
                drive.source_data.pop("message", None)
                drive.source_data.pop("evening_culture_refill", None)
                return

        emptiness = 1.0 - (fresh_count / self.EVENING_CULTURE_REFILL_THRESHOLD)
        rate_per_minute = 0.02 + 0.02 * emptiness
        drive.spike(
            rate_per_minute * (dt / 60.0),
            min(0.85, self.config.drives.max_pressure),
        )

        drive.source_data["message"] = (
            "EVENING CULTURE REFILL: fresh candidates on the evening-culture "
            f"shelf are low ({fresh_count} fresh, threshold "
            f"{self.EVENING_CULTURE_REFILL_THRESHOLD}). This is a maintenance "
            "task for Sayr, not an invitation to Lisa. Add 3-7 new topic "
            "candidates to pulse/self/evening-culture-topics.md under "
            "'## Кандидаты'. Use myths, literature, films, music, art, "
            "cultural history, or threads from current day conversations. "
            "Do not propose a culture conversation to Lisa right now; refill "
            "the shelf and report briefly."
        )
        drive.source_data["evening_culture_refill"] = {
            "fresh_count": fresh_count,
            "threshold": self.EVENING_CULTURE_REFILL_THRESHOLD,
            "last_refill_ts": last_refill_ts,
            "action": "add 3-7 candidates to evening-culture-topics.md",
        }

    def _mark_evening_culture_refill_addressed(self, *, now: float | None = None):
        """Record a handled refill turn to avoid hammering the same request."""
        now = now or time.time()
        drive = self.drives.get(self.EVENING_CULTURE_REFILL_DRIVE)
        if not drive:
            return
        refill_state = drive.source_data.get("evening_culture_refill")
        if not isinstance(refill_state, dict):
            refill_state = {}
        refill_state["last_refill_ts"] = now
        drive.source_data["evening_culture_refill"] = refill_state
        drive.pressure = 0.0

    def _select_evening_culture_candidate(self) -> Optional[str]:
        """Pick the first fresh evening-culture topic from the curated shelf."""
        text = self._read_evening_culture_topics_text()
        if text is None:
            return None
        candidates = self._extract_evening_culture_candidates(text)
        seen = self._extract_evening_culture_seen_titles(text)
        for candidate in candidates:
            if not self._is_evening_culture_seen_title(candidate, seen):
                return candidate
        return None

    def _is_evening_culture_seen_title_from_file(self, title: Any) -> bool:
        text = self._read_evening_culture_topics_text()
        if text is None:
            return False
        return self._is_evening_culture_seen_title(
            str(title),
            self._extract_evening_culture_seen_titles(text),
        )

    def _read_evening_culture_current(self) -> Optional[dict]:
        try:
            data = json.loads(self.EVENING_CULTURE_CURRENT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _is_evening_culture_terminal(self, current: Optional[dict]) -> bool:
        return bool(
            current
            and str(current.get("status", "")).lower()
            in self.EVENING_CULTURE_TERMINAL_STATUSES
        )

    def _archive_stale_evening_culture_terminal_current(
        self,
        current: Optional[dict],
        *,
        now_dt: datetime,
    ) -> bool:
        if not self._is_evening_culture_terminal(current):
            return False
        anchors = (
            current.get("closed_at"),
            current.get("completed_at"),
            current.get("last_discussed_at"),
            current.get("offered_at"),
        )
        anchor = None
        for value in anchors:
            anchor = self._parse_iso_datetime(value)
            if anchor is not None:
                break
        if anchor is None:
            return False
        if anchor.tzinfo is not None and now_dt.tzinfo is None:
            anchor = anchor.replace(tzinfo=None)
        elif anchor.tzinfo is None and now_dt.tzinfo is not None:
            now_dt = now_dt.replace(tzinfo=None)
        evening_window = (
            (now_dt.hour == 16 and now_dt.minute >= 30)
            or 17 <= now_dt.hour < 24
        )
        if not evening_window or anchor.date() >= now_dt.date():
            return False
        current["previous_terminal_status"] = current.get("status")
        current["status"] = "archived"
        current["archived_at"] = now_dt.isoformat(timespec="seconds")
        current["archived_reason"] = (
            "previous terminal evening culture topic should not block a new "
            "evening window"
        )
        self._write_evening_culture_current(current)
        return True

    def _is_evening_culture_stale_discussing(self, current: Optional[dict], *, now_dt: datetime) -> bool:
        if not current or str(current.get("status", "")).lower() != "discussing":
            return False
        anchors = (
            current.get("last_discussed_at"),
            current.get("started_discussing_at"),
            current.get("offered_at"),
        )
        for value in anchors:
            anchor = self._parse_iso_datetime(value)
            if anchor is None:
                continue
            if anchor.tzinfo is not None and now_dt.tzinfo is None:
                anchor = anchor.replace(tzinfo=None)
            elif anchor.tzinfo is None and now_dt.tzinfo is not None:
                now_dt = now_dt.replace(tzinfo=None)
            # Evening culture should not wait for an exact 24h wall-clock
            # boundary. If yesterday's discussion is still marked
            # ``discussing`` when the next evening culture window opens, it is
            # already stale enough to ask whether to close/carry/continue. The
            # strict 24h rule made topics discussed late in the evening suppress
            # the whole next evening and only become actionable after the useful
            # window had nearly ended.
            if anchor.date() < now_dt.date() and (
                (now_dt.hour == 16 and now_dt.minute >= 30) or 17 <= now_dt.hour < 24
            ):
                return True
            return (now_dt - anchor).total_seconds() >= self.EVENING_CULTURE_STALE_DISCUSSING_SECONDS
        return True

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    def _write_evening_culture_current(self, data: dict):
        self.EVENING_CULTURE_CURRENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.EVENING_CULTURE_CURRENT_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._source_cache.pop(str(self.EVENING_CULTURE_CURRENT_PATH), None)

    def _ensure_evening_culture_current_topic(self, *, now_dt: datetime | None = None) -> Optional[dict]:
        """Persist the currently offered evening topic until it is discussed or rotated.

        This keeps Pulse from selecting a fresh topic on every successful turn:
        success means the invitation was delivered, not that Lisa necessarily saw
        or discussed it. The current topic remains explicit for tomorrow carry.
        """
        now_dt = now_dt or datetime.now()
        current = self._read_evening_culture_current()
        if self._is_evening_culture_terminal(current):
            return None
        if current and current.get("status") in {"selected", "offered", "carried", "discussing"} and current.get("title"):
            if not self._is_evening_culture_seen_title_from_file(current.get("title")):
                return current
            current["status"] = "discussed"
            current["closed_reason"] = "topic already present in evening-culture 'Уже были'"
            current["closed_at"] = now_dt.isoformat(timespec="seconds")
            self._write_evening_culture_current(current)

        title = self._select_evening_culture_candidate()
        if not title:
            return None
        current = {
            "id": self._topic_slug(title),
            "title": title,
            "status": "selected",
            "selected_at": now_dt.isoformat(timespec="seconds"),
            "reminder_interval_minutes": int(self.EVENING_CULTURE_REMINDER_INTERVAL_SECONDS / 60),
            "source": str(self.EVENING_CULTURE_TOPICS_PATH),
        }
        self._write_evening_culture_current(current)
        return current

    def _evening_culture_reminder_pressure(self, current: dict, *, now_dt: datetime) -> float:
        """Return a small reminder pressure if the same topic has rested long enough."""
        anchor = self._parse_iso_datetime(current.get("last_reminded_at"))
        if anchor is None:
            anchor = self._parse_iso_datetime(current.get("offered_at"))
        if anchor is None:
            anchor = self._parse_iso_datetime(current.get("selected_at"))
        if anchor is None:
            return 0.0
        elapsed = (now_dt - anchor).total_seconds()
        if elapsed < self.EVENING_CULTURE_REMINDER_INTERVAL_SECONDS:
            return 0.0
        return min(0.9, self.config.drives.max_pressure)

    def _mark_evening_culture_prompted(self, *, now_dt: datetime | None = None) -> bool:
        """Record that the current topic was offered/reminded, without closing it."""
        now_dt = now_dt or datetime.now()
        current = self._read_evening_culture_current()
        if not current or not current.get("title"):
            return False
        if self._is_evening_culture_terminal(current):
            return False
        current["status"] = "offered"
        current.setdefault("offered_at", now_dt.isoformat(timespec="seconds"))
        current["last_reminded_at"] = now_dt.isoformat(timespec="seconds")
        current["reminder_interval_minutes"] = int(self.EVENING_CULTURE_REMINDER_INTERVAL_SECONDS / 60)
        self._write_evening_culture_current(current)
        return True

    def _carry_evening_culture_after_midnight(self, *, now_dt: datetime | None = None) -> bool:
        """After midnight, keep an unresolved topic for tomorrow without pressure."""
        now_dt = now_dt or datetime.now()
        current = self._read_evening_culture_current()
        if not current or current.get("status") not in {"selected", "offered", "carried"}:
            return False
        current["status"] = "carried"
        current["carried_after"] = now_dt.isoformat(timespec="seconds")
        self._write_evening_culture_current(current)
        return True

    def _read_cached_text(self, path: Path) -> tuple[Optional[str], bool]:
        _ABSENT = -1.0
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            if self._source_cache.get(key, (None,))[0] != _ABSENT:
                self._source_cache[key] = (_ABSENT, None)
            return None, False
        except OSError:
            return None, False

        cached = self._source_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1], False
        try:
            data = path.read_text()
        except Exception:
            return None, False
        self._source_cache[key] = (mtime, data)
        return data, True

    def _get_nested(self, data: dict, field: str):
        current = data
        for part in field.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def _condition_matches(self, condition: dict, state: dict) -> bool:
        if "any_of" in condition:
            return any(self._condition_matches(item, state) for item in condition["any_of"])

        field = condition.get("field")
        op = condition.get("op")
        value = self._get_nested(state, field) if field else None

        if op == "==":
            return value == condition.get("value")
        if op == "<=":
            return value is not None and value <= condition.get("value")
        if op == ">=":
            return value is not None and value >= condition.get("value")
        if op == "<":
            return value is not None and value < condition.get("value")
        if op == ">":
            return value is not None and value > condition.get("value")
        if op == "is_null":
            return value is None
        if op == "is_not_null":
            return value is not None
        return False

    def _refresh_health_state_bridge(self, workspace_root: Path) -> bool:
        """Refresh pulse/self/health-state.json from today's diary before scoring.

        The diary can be edited directly as plain Markdown, bypassing the
        health-diary update script.  Refresh here so Pulse does not score health
        from stale state.  On failure, surface a repair signal instead of using
        old state as if it were valid.
        """
        bridge_path = workspace_root / "scripts/health-diary-health-state-bridge.mjs"
        if not bridge_path.exists():
            logger.warning("Health-state bridge missing: %s", bridge_path)
            return False

        try:
            subprocess.run(
                ["node", str(bridge_path)],
                cwd=str(workspace_root),
                text=True,
                capture_output=True,
                timeout=30,
                check=True,
            )
            # The bridge rewrites health-state.json; cached mtime checks will
            # pick up the new file on the next read.
            return True
        except Exception as exc:
            logger.warning("Health-state bridge refresh failed: %s", exc)
            return False

    def _signal_health_state_bridge_failure(self):
        if "health" not in self.drives:
            self.drives["health"] = Drive(name="health", category="health", weight=0.7)
        drive = self.drives["health"]
        drive.spike(0.5, self.config.drives.max_pressure)
        drive.source_data["message"] = (
            "health-state refresh failed. Нужно починить bridge и посмотреть "
            "сегодняшний текстовый дневник: /home/lisa/Obsidian/health_diary/YYYY-MM-DD.md"
        )
        drive.source_data["rule_id"] = "health_state_bridge_failed"
        try:
            thalamus.append({
                "source": "health_state",
                "type": "health_state_bridge_failed",
                "salience": 0.5,
                "data": {
                    "drive": "health",
                    "message": drive.source_data["message"],
                },
            })
        except Exception as exc:
            logger.debug("Health-state bridge failure thalamus append failed: %s", exc)

    def _health_food_hold_active(self, workspace_root: Path, *, now: datetime) -> bool:
        """Apply a Lisa-confirmed food deferral once and keep it active for its exact window."""
        context_path = workspace_root / self.HEALTH_FOOD_CONTEXT_FILE
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except Exception:
            return False

        if context.get("status") != "deferred":
            return False
        hold_until = self._parse_iso_timestamp(context.get("hold_until"))
        accepted_at = self._parse_iso_timestamp(context.get("accepted_at"))
        if hold_until is None or accepted_at is None or now >= hold_until:
            return False

        drive = self.drives.get(self.HEALTH_FOOD_DRIVE)
        if drive is None:
            drive = self.drives[self.HEALTH_FOOD_DRIVE] = Drive(
                name=self.HEALTH_FOOD_DRIVE,
                category="health",
                weight=self.config_weight(self.HEALTH_FOOD_DRIVE),
            )

        applied_path = Path(self.config.state.dir).expanduser() / "health-food-hold-applied.json"
        applied = {}
        try:
            applied = json.loads(applied_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        accepted_token = context.get("accepted_at")
        if applied.get("accepted_at") != accepted_token:
            before = drive.pressure
            drive.decay(drive.pressure * self.HEALTH_FOOD_HOLD_DECAY_FRACTION)
            drive.last_addressed = now.timestamp()
            applied_path.parent.mkdir(parents=True, exist_ok=True)
            applied_path.write_text(
                json.dumps(
                    {
                        "accepted_at": accepted_token,
                        "hold_until": context.get("hold_until"),
                        "pressure_before": round(before, 4),
                        "pressure_after": round(drive.pressure, 4),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return True

    def _refresh_health_rules(self):
        workspace = self.config.workspace
        workspace_root = Path(str(workspace.root)).expanduser()
        rules_path = workspace_root / "skills/health-diary/config/pulse-health-rules.json"
        if not rules_path.exists():
            # Compatibility only: older workspaces kept configuration inside
            # ignored runtime state, which made Git review and deployment drift.
            rules_path = workspace_root / "pulse/self/health-rules.json"
        state_path = workspace_root / "pulse/self/health-state.json"
        fired_path = Path(self.config.state.dir).expanduser() / "health-rules-fired.json"

        bridge_ok = self._refresh_health_state_bridge(workspace_root)
        if not bridge_ok:
            self._signal_health_state_bridge_failure()
            return

        raw_rules, _ = self._read_cached_text(rules_path)
        state_data, _ = self._read_cached_json(state_path)
        if not raw_rules or not isinstance(state_data, dict):
            return

        try:
            rules_doc = json.loads(raw_rules)
        except Exception:
            logger.warning("Health rules JSON invalid: %s", rules_path)
            return

        if not rules_doc.get("enabled", False):
            return

        for drive in self.drives.values():
            if self._is_health_drive(drive.name):
                drive.source_data.pop("message", None)
                drive.source_data.pop("rule_id", None)

        now = datetime.now().astimezone()
        food_hold_active = self._health_food_hold_active(workspace_root, now=now)
        state_data["food_context_hold_active"] = food_hold_active
        # Workspace health rules are read live by the running daemon. This
        # handshake keeps the new iron-gap rule inert until a Pulse process
        # containing the matching owner code has actually been restarted.
        state_data["iron_rich_rule_contract_v1"] = True

        today = now.strftime("%Y-%m-%d")
        fired_state = {}
        if fired_path.exists():
            try:
                fired_state = json.loads(fired_path.read_text())
            except Exception:
                fired_state = {}
        fired_today = set(fired_state.get(today, []))
        cooldowns = fired_state.get("_cooldowns", {})
        if not isinstance(cooldowns, dict):
            cooldowns = {}
        changed_fired = False

        for rule in rules_doc.get("rules", []):
            if not rule.get("id") or not rule.get("effect"):
                continue
            if rule.get("once_per_day") and rule["id"] in fired_today:
                continue

            cooldown_seconds = float(rule.get("cooldown_seconds", 0) or 0)
            cooldown_key_fields = rule.get("cooldown_key_fields") or []
            cooldown_key = {
                field: state_data.get(field)
                for field in cooldown_key_fields
            }
            cooldown_state = cooldowns.get(rule["id"], {})
            if (
                cooldown_seconds > 0
                and cooldown_state.get("key") == cooldown_key
                and isinstance(cooldown_state.get("fired_at"), (int, float))
                and now.timestamp() - float(cooldown_state["fired_at"]) < cooldown_seconds
            ):
                continue

            time_after = rule.get("time_after")
            if time_after:
                try:
                    hh, mm = map(int, str(time_after).split(":"))
                    if (now.hour, now.minute) < (hh, mm):
                        continue
                except Exception:
                    pass

            conditions = rule.get("conditions", [])
            if not conditions or not all(self._condition_matches(cond, state_data) for cond in conditions):
                continue

            effect = rule["effect"]
            drive_name = effect.get("drive", "health")
            if (
                drive_name == self.HEALTH_FOOD_DRIVE
                and food_hold_active
                and not rule.get("independent_of_food_context_hold", False)
            ):
                continue
            if drive_name not in self.drives:
                category = "health" if self._is_health_drive(drive_name) else drive_name
                self.drives[drive_name] = Drive(
                    name=drive_name,
                    category=category,
                    weight=self.config_weight(drive_name),
                )
            drive = self.drives[drive_name]
            delta = float(effect.get("pressure_delta", 0.0) or 0.0)
            if delta > 0:
                drive.spike(delta, self.config.drives.max_pressure)

            message = effect.get("message")
            if message:
                drive.source_data["message"] = message
                drive.source_data["rule_id"] = rule["id"]
                try:
                    thalamus.append({
                        "source": "health_state",
                        "type": "health_rule",
                        "salience": min(1.0, max(0.1, delta)),
                        "data": {
                            "rule_id": rule["id"],
                            "drive": drive_name,
                            "message": message,
                            "pressure_delta": delta,
                        },
                    })
                except Exception as exc:
                    logger.debug("Health rule thalamus append failed: %s", exc)

            if rule.get("once_per_day"):
                fired_today.add(rule["id"])
                changed_fired = True
            if cooldown_seconds > 0:
                cooldowns[rule["id"]] = {
                    "fired_at": now.timestamp(),
                    "key": cooldown_key,
                }
                changed_fired = True

        if changed_fired:
            fired_path.parent.mkdir(parents=True, exist_ok=True)
            fired_state[today] = sorted(fired_today)
            fired_state["_cooldowns"] = cooldowns
            fired_path.write_text(json.dumps(fired_state, ensure_ascii=False, indent=2))

    def on_trigger_success(self, decision):
        """Called after a successful agent turn. Decay all drives proportionally."""
        decay_total = self.config.drives.success_decay
        now = time.time()

        # Scale decay proportionally when total pressure is high
        if self.config.drives.adaptive_decay and decision.total_pressure > 5.0:
            pressure_multiplier = min(3.0, decision.total_pressure / 5.0)
            decay_total = decay_total * pressure_multiplier

        top_drive_name = decision.top_drive.name if decision.top_drive else None

        if decision.total_pressure > 0:
            for drive in self.drives.values():
                if drive.name == self.EVENING_CULTURE_DRIVE and top_drive_name == self.EVENING_CULTURE_DRIVE:
                    continue
                if drive.pressure > 0:
                    # Proportional decay — higher pressure drives lose more
                    proportion = drive.weighted_pressure / decision.total_pressure
                    drive.decay(decay_total * proportion * 2)

        # Mark top drive as addressed. Repo-local git drives are binary hygiene
        # signals: after a successful git turn, either the repo is still dirty
        # and the sensor will regrow pressure on the next tick, or it is clean.
        # Keeping residual pressure after a clean commit makes Pulse wake again
        # for already-closed work, so clear the addressed git drive completely.
        if decision.top_drive and decision.top_drive.name in self.drives:
            top_drive = self.drives[decision.top_drive.name]
            if self._is_git_drive(top_drive.name):
                top_drive.pressure = 0.0
            if top_drive.name == "growth":
                candidate = top_drive.source_data.get("growth_material") or {}
                candidate_id = candidate.get("id")
                if candidate_id:
                    self._mark_growth_dispatch_pending(candidate_id)
                    top_drive.pressure = 0.0
            if top_drive.name == self.EVENING_CULTURE_DRIVE:
                prompted = self._mark_evening_culture_prompted(now_dt=datetime.fromtimestamp(now))
                if prompted:
                    top_drive.decay(
                        top_drive.pressure * self.EVENING_CULTURE_TRIGGER_SUCCESS_DECAY_FRACTION
                    )
                else:
                    top_drive.pressure = 0.0
            if top_drive.name == self.EVENING_CULTURE_REFILL_DRIVE:
                self._mark_evening_culture_refill_addressed(now=now)
            if self._is_health_drive(top_drive.name):
                top_drive.decay(top_drive.pressure * self.HEALTH_TRIGGER_SUCCESS_DECAY_FRACTION)
            top_drive.last_addressed = now
            logger.info(
                f"Drives decayed after successful turn. "
                f"Top drive '{decision.top_drive.name}' addressed."
            )

    @staticmethod
    def _topic_slug(title: str) -> str:
        """Stable lightweight id for an evening-culture topic title."""
        return re.sub(r"[^\wа-яА-ЯёЁ]+", "-", title.lower()).strip("-")

    def on_trigger_failure(self, decision):
        """Called after a failed trigger. Boost frustration."""
        if decision.top_drive and decision.top_drive.name in self.drives:
            drive = self.drives[decision.top_drive.name]
            drive.spike(
                self.config.drives.failure_boost,
                self.config.drives.max_pressure,
            )
            logger.warning(
                f"Drive '{drive.name}' boosted to {drive.pressure:.2f} "
                f"after failed trigger (frustration)"
            )

    def restore_state(self):
        """Restore drive pressures and runtime-added drives from persisted state.

        NOTE: drive.weight is intentionally NOT restored from persisted state.
        Weights are always re-derived from config + FeedbackLearner on each
        feedback cycle. Restoring persisted weights caused exponential drift
        because drifted values would compound across daemon restarts.
        (Fixed March 2026 — same root cause as health.py and daemon.py bugs.)
        """
        saved = self.state.get("drives", {})
        for name, data in saved.items():
            if name in self.drives:
                self.drives[name].pressure = data.get("pressure", 0.0)
                # weight deliberately skipped — always use config_weight()
                self.drives[name].last_addressed = data.get("last_addressed", 0.0)
            else:
                # Restore runtime-added drives (from mutations)
                self.drives[name] = Drive(
                    name=name,
                    category=data.get("category", name),
                    pressure=data.get("pressure", 0.0),
                    weight=data.get("weight", 0.5),
                    last_addressed=data.get("last_addressed", 0.0),
                )
                logger.info(
                    f"Restored runtime drive: {name} (weight={data.get('weight', 0.5)})"
                )
        logger.info(f"Restored {len(saved)} drive states")

    def save_state(self) -> dict:
        """Serialize drive state for persistence."""
        return {name: drive.to_dict() for name, drive in self.drives.items()}
