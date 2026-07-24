"""
Runtime Backup Sensor — passive staleness check for the private
OpenClaw runtime GitHub backup.

Compares two *local* JSON files:
  - provenance registry (written by readiness checks)
  - last successful backup state (written by backup-current-runtime-to-private-github.py)

If the provenance registry has a passing readiness entry for the
*current* runtime repo (resolved via realpath) whose HEAD+tree do not
match the last backup record, emits a ``runtime_backup`` signal.

Never contacts the network, never pushes, never runs readiness itself.
Fails closed (quiet, no pressure) on any ambiguity.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import BaseSensor

logger = logging.getLogger("pulse.sensors.runtime_backup")


class RuntimeBackupSensor(BaseSensor):
    name = "runtime_backup"

    def __init__(
        self,
        config: PulseConfig,
        runtime: str = "/home/lisa/src/openclaw-current",
        provenance: str = "/home/lisa/.openclaw/workspace/automation/ops/openclaw-runtime-provenance.json",
        backup: str = "/home/lisa/.openclaw/workspace/state/repo-audit/runtime-private-backup-latest.json",
        pressure_spike: float = 0.15,
    ) -> None:
        self._runtime_link = Path(runtime)
        self._provenance_path = Path(provenance)
        self._backup_path = Path(backup)
        self._pressure_spike = pressure_spike

    async def initialize(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def read(self) -> Dict[str, Any]:
        return self._read_sync()

    # ── logic ──────────────────────────────────────────────────────────

    def _read_sync(self) -> Dict[str, Any]:
        try:
            live_repo = self._resolve_runtime()
        except Exception as exc:
            logger.debug("runtime_backup: cannot resolve runtime: %s", exc)
            return self._quiet("runtime_unresolved")

        try:
            prov = self._load_json(self._provenance_path)
        except Exception as exc:
            logger.debug("runtime_backup: provenance load failed: %s", exc)
            return self._quiet("provenance_unreadable")

        entry = self._find_passing_entry(prov, live_repo)
        if entry is None:
            # No passing readiness for current runtime — not our job to signal
            return self._quiet("no_passing_readiness")

        live_head: str = entry["head"]
        live_tree: str = entry["tree"]

        try:
            backup = self._load_json(self._backup_path)
        except Exception as exc:
            logger.debug("runtime_backup: backup state load failed: %s", exc)
            # Readiness passed but we have no backup record at all → stale
            return self._signal(live_head, live_tree, None, "no_backup_record")

        backup_head: Optional[str] = backup.get("head")
        backup_tree: Optional[str] = backup.get("tree")
        restore_ok = backup.get("restoreCheck") == "pass"

        if backup_head == live_head and backup_tree == live_tree and restore_ok:
            # Everything matches — no pressure
            return self._quiet("backup_current")

        return self._signal(live_head, live_tree, backup_head, "stale_backup")

    # ── helpers ─────────────────────────────────────────────────────────

    def _resolve_runtime(self) -> str:
        if not self._runtime_link.exists():
            raise FileNotFoundError(f"runtime link missing: {self._runtime_link}")
        resolved = self._runtime_link.resolve(strict=True)
        if not (resolved / ".git").exists():
            raise NotADirectoryError(f"not a git checkout: {resolved}")
        return str(resolved)

    @staticmethod
    def _load_json(path: Path) -> dict:
        if not path.exists():
            raise FileNotFoundError(str(path))
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _find_passing_entry(provenance: dict, repo_path: str) -> Optional[dict]:
        """Return the last entry matching *repo_path* with passing readiness."""
        entries = provenance.get("entries", [])
        matches = [e for e in entries if e.get("path") == repo_path]
        if not matches:
            return None
        entry = matches[-1]
        evidence = entry.get("readinessEvidence") or {}
        if evidence.get("result") != "pass":
            return None
        if not evidence.get("head") or not evidence.get("tree"):
            return None
        return entry

    def _quiet(self, reason: str) -> Dict[str, Any]:
        return {"signal": None, "reason": reason}

    def _signal(
        self,
        live_head: str,
        live_tree: str,
        backup_head: Optional[str],
        reason: str,
    ) -> Dict[str, Any]:
        return {
            "signal": "runtime_backup",
            "reason": reason,
            "live_head": live_head,
            "live_tree": live_tree,
            "backup_head": backup_head,
            "drive": "runtime_backup",
            "pressure": self._pressure_spike,
        }
