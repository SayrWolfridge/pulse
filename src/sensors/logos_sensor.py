"""
Logos Sensor — Backlog pressure integration for DriveEngine

Reads the Logos task backlog and translates accumulation + stagnation
signals into drive pressure:

  - goals  : rises when unblocked tasks for this agent pile up
  - growth : rises when tasks have been in-progress too long (stale)
  - rest   : tiny decay bonus when backlog is empty (nothing to do)

Config (pulse.yaml):
  sensors:
    logos:
      enabled: true
      agent: iris               # which agent's backlog to watch
      db_path: ~/.pulse/logos.db  # optional override
      backlog_pressure_per_task: 0.25   # goals pressure per unblocked backlog task
      max_backlog_pressure: 3.0         # cap on backlog-driven goals spike
      stale_in_progress_minutes: 120    # minutes before in-progress task is "stale"
      stale_pressure: 1.5               # goals spike for stale in-progress tasks
"""

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import BaseSensor

logger = logging.getLogger("pulse.sensors.logos")

DEFAULT_DB_PATH = os.path.expanduser("~/.pulse/logos.db")


@dataclass
class LogosSensorState:
    """Tracks last-seen task counts for delta detection."""
    last_backlog_count: int = 0
    last_in_progress_count: int = 0
    last_check_ts: float = field(default_factory=time.time)


class LogosSensor(BaseSensor):
    """Monitor the Logos backlog and inject drive pressure.

    Feeds:
      - ``logos.backlog_count``      → total unblocked tasks for this agent
      - ``logos.in_progress_count``  → tasks currently being worked
      - ``logos.stale_count``        → in-progress tasks updated > N minutes ago
      - ``logos.backlog_pressure``   → computed goals pressure (0.0 – max)
      - ``logos.stale_pressure``     → computed goals pressure from stale tasks
    """

    name = "logos"

    def __init__(self, config: PulseConfig):
        # BaseSensor has no __init__; keep this lightweight.
        self.config = config
        cfg = config.sensors.logos

        self.agent: str = cfg.agent
        # Prefer configured path, but fall back to the canonical LogosStore path.
        configured = os.path.expanduser(cfg.db_path)
        if Path(configured).exists():
            self.db_path = configured
        elif Path(DEFAULT_DB_PATH).exists():
            self.db_path = DEFAULT_DB_PATH
        else:
            # Older installs used ~/.pulse/logos.db
            self.db_path = DEFAULT_DB_PATH
        self.pressure_per_task: float = cfg.backlog_pressure_per_task
        self.max_backlog_pressure: float = cfg.max_backlog_pressure
        self.stale_minutes: int = cfg.stale_in_progress_minutes
        self.stale_pressure: float = cfg.stale_pressure

        self._state = LogosSensorState()

    # ------------------------------------------------------------------
    # BaseSensor interface
    # ------------------------------------------------------------------

    async def read(self) -> dict:
        """Query the Logos SQLite store and return sensor readings."""
        if not Path(self.db_path).exists():
            logger.debug("logos db not found at %s — sensor returning zeros", self.db_path)
            return self._zero_reading()

        try:
            return self._query_store()
        except Exception as exc:
            logger.warning("logos sensor read error: %s", exc, exc_info=True)
            return self._zero_reading()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _zero_reading(self) -> dict:
        return {
            "logos.backlog_count": 0,
            "logos.in_progress_count": 0,
            "logos.stale_count": 0,
            "logos.backlog_pressure": 0.0,
            "logos.stale_pressure": 0.0,
            "logos.requires_human_count": 0,
        }

    def _query_store(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return self._compute_readings(conn)
        finally:
            conn.close()

    def _compute_readings(self, conn: sqlite3.Connection) -> dict:
        stale_cutoff_ts = time.time() - (self.stale_minutes * 60)

        # Backlog: tasks assigned to this agent, status=backlog, not requires_human
        backlog_rows = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM tasks
            WHERE agent = ? AND status = 'backlog' AND requires_human = 0
            """,
            (self.agent,),
        ).fetchone()
        backlog_count = backlog_rows["cnt"] if backlog_rows else 0

        # Requires-human tasks (still useful to surface, but don't create pressure)
        human_rows = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM tasks
            WHERE agent = ? AND status = 'backlog' AND requires_human = 1
            """,
            (self.agent,),
        ).fetchone()
        human_count = human_rows["cnt"] if human_rows else 0

        # In-progress count
        in_progress_rows = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM tasks
            WHERE agent = ? AND status = 'in_progress'
            """,
            (self.agent,),
        ).fetchone()
        in_progress_count = in_progress_rows["cnt"] if in_progress_rows else 0

        # Stale in-progress: updated_at older than stale_cutoff (stored as ISO string)
        # Fall back to zero if updated_at column absent
        stale_count = 0
        try:
            stale_rows = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM tasks
                WHERE agent = ? AND status = 'in_progress'
                  AND (updated_at IS NULL OR
                       CAST(strftime('%s', updated_at) AS INTEGER) < ?)
                """,
                (self.agent, int(stale_cutoff_ts)),
            ).fetchone()
            stale_count = stale_rows["cnt"] if stale_rows else 0
        except sqlite3.OperationalError:
            pass

        # Compute pressure values
        backlog_pressure = min(
            backlog_count * self.pressure_per_task,
            self.max_backlog_pressure,
        )
        computed_stale_pressure = self.stale_pressure if stale_count > 0 else 0.0

        self._state.last_backlog_count = backlog_count
        self._state.last_in_progress_count = in_progress_count
        self._state.last_check_ts = time.time()

        logger.debug(
            "logos sensor: backlog=%d in_progress=%d stale=%d "
            "backlog_pressure=%.2f stale_pressure=%.2f human_gated=%d",
            backlog_count, in_progress_count, stale_count,
            backlog_pressure, computed_stale_pressure, human_count,
        )

        return {
            "logos.backlog_count": backlog_count,
            "logos.in_progress_count": in_progress_count,
            "logos.stale_count": stale_count,
            "logos.backlog_pressure": backlog_pressure,
            "logos.stale_pressure": computed_stale_pressure,
            "logos.requires_human_count": human_count,
        }


# ------------------------------------------------------------------
# Drive wiring hints (used by DriveEngine to interpret sensor output)
# ------------------------------------------------------------------

DRIVE_WIRING = {
    # logos.backlog_pressure → goals drive (want to clear the backlog)
    "logos.backlog_pressure": {
        "drive": "goals",
        "weight": 1.0,
        "description": "Unblocked tasks accumulating in Logos backlog",
    },
    # logos.stale_pressure → goals drive (in-progress task is stuck)
    "logos.stale_pressure": {
        "drive": "goals",
        "weight": 1.2,
        "description": "In-progress task stale — needs attention or re-queue",
    },
}
