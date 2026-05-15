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
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pulse.src import thalamus
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
        return self._config_weights.get(drive_name, 1.0)

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
            drive.tick(
                dt=dt,
                rate=self.config.drives.pressure_rate,
                max_pressure=self.config.drives.max_pressure,
            )

        # Sensor-driven spikes
        self._apply_sensor_spikes(sensor_data)

        # Build state snapshot
        return DriveState(
            drives=list(self.drives.values()),
            timestamp=now,
        )

    def refresh_sources(self):
        """Read workspace source files and apply drive adjustments.
        Separated from tick() to isolate I/O from state transitions."""
        self._refresh_sources()

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

        # Git: uncommitted changes or stale push → goals drive
        git_data = sensor_data.get("git", {})
        if git_data.get("uncommitted_changes") or git_data.get("untracked_files", 0) > 0:
            if "goals" in self.drives:
                self.drives["goals"].spike(0.15, self.config.drives.max_pressure)
        if git_data.get("stale_push"):
            if "goals" in self.drives:
                self.drives["goals"].spike(0.2, self.config.drives.max_pressure)
        # Git: remote has updates we haven't pulled → growth drive
        if git_data.get("commits_behind", 0) > 0:
            if "growth" in self.drives:
                self.drives["growth"].spike(0.1, self.config.drives.max_pressure)

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

    def _refresh_health_rules(self):
        workspace = self.config.workspace
        workspace_root = Path(str(workspace.root)).expanduser()
        rules_path = workspace_root / "pulse/self/health-rules.json"
        state_path = workspace_root / "pulse/self/health-state.json"
        fired_path = Path(self.config.state.dir).expanduser() / "health-rules-fired.json"

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

        if "health" not in self.drives:
            self.drives["health"] = Drive(name="health", category="health", weight=0.7)

        health_drive = self.drives["health"]
        health_drive.source_data.pop("message", None)
        health_drive.source_data.pop("rule_id", None)

        today = datetime.now().strftime("%Y-%m-%d")
        fired_state = {}
        if fired_path.exists():
            try:
                fired_state = json.loads(fired_path.read_text())
            except Exception:
                fired_state = {}
        fired_today = set(fired_state.get(today, []))
        changed_fired = False

        for rule in rules_doc.get("rules", []):
            if not rule.get("id") or not rule.get("effect"):
                continue
            if rule.get("once_per_day") and rule["id"] in fired_today:
                continue

            time_after = rule.get("time_after")
            if time_after:
                try:
                    hh, mm = map(int, str(time_after).split(":"))
                    now = datetime.now()
                    if (now.hour, now.minute) < (hh, mm):
                        continue
                except Exception:
                    pass

            conditions = rule.get("conditions", [])
            if not conditions or not all(self._condition_matches(cond, state_data) for cond in conditions):
                continue

            effect = rule["effect"]
            drive_name = effect.get("drive", "health")
            if drive_name not in self.drives:
                self.drives[drive_name] = Drive(name=drive_name, category=drive_name, weight=0.7)
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

        if changed_fired:
            fired_path.parent.mkdir(parents=True, exist_ok=True)
            fired_state = {today: sorted(fired_today)}
            fired_path.write_text(json.dumps(fired_state, ensure_ascii=False, indent=2))

    def on_trigger_success(self, decision):
        """Called after a successful agent turn. Decay all drives proportionally."""
        decay_total = self.config.drives.success_decay
        now = time.time()

        # Scale decay proportionally when total pressure is high
        if self.config.drives.adaptive_decay and decision.total_pressure > 5.0:
            pressure_multiplier = min(3.0, decision.total_pressure / 5.0)
            decay_total = decay_total * pressure_multiplier

        if decision.total_pressure > 0:
            for drive in self.drives.values():
                if drive.pressure > 0:
                    # Proportional decay — higher pressure drives lose more
                    proportion = drive.weighted_pressure / decision.total_pressure
                    drive.decay(decay_total * proportion * 2)

        # Mark top drive as addressed
        if decision.top_drive and decision.top_drive.name in self.drives:
            self.drives[decision.top_drive.name].last_addressed = now
            logger.info(
                f"Drives decayed after successful turn. "
                f"Top drive '{decision.top_drive.name}' addressed."
            )

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
