"""Focused tests for RuntimeBackupSensor."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pulse.src.sensors.runtime_backup_sensor import RuntimeBackupSensor


# ── fixtures ────────────────────────────────────────────────────────────

REPO_PATH = "/home/lisa/src/openclaw-fake-runtime"
LIVE_HEAD = "aaa111aaa111aaa111aaa111aaa111aaa111aaa1"
LIVE_TREE = "bbb222bbb222bbb222bbb222bbb222bbb222bbb2"
BACKUP_HEAD = "ccc333ccc333ccc333ccc333ccc333ccc333ccc3"
BACKUP_TREE = "ddd444ddd444ddd444ddd444ddd444ddd444ddd4"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _provenance(repo_path: str, head: str, tree: str, result: str = "pass") -> dict:
    return {
        "entries": [
            {
                "path": repo_path,
                "head": head,
                "tree": tree,
                "readinessEvidence": {
                    "result": result,
                    "path": repo_path,
                    "head": head,
                    "tree": tree,
                },
            }
        ]
    }


def _backup_state(head: str, tree: str, restore: str = "pass") -> dict:
    return {"head": head, "tree": tree, "restoreCheck": restore}


def _make_sensor(tmp: Path, provenance_data: dict | None, backup_data: dict | None):
    prov_path = tmp / "provenance.json"
    backup_path = tmp / "backup.json"
    if provenance_data is not None:
        _write_json(prov_path, provenance_data)
    if backup_data is not None:
        _write_json(backup_path, backup_data)

    sensor = RuntimeBackupSensor(
        config=None,
        runtime="/home/lisa/src/openclaw-current",
        provenance=str(prov_path),
        backup=str(backup_path),
        pressure_spike=0.2,
    )
    return sensor


# ── tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backup_current_no_pressure(tmp_path):
    """Matching HEAD+tree+restore → no signal."""
    sensor = _make_sensor(
        tmp_path,
        _provenance(REPO_PATH, LIVE_HEAD, LIVE_TREE),
        _backup_state(LIVE_HEAD, LIVE_TREE),
    )
    with patch.object(Path, "resolve", return_value=Path(REPO_PATH)), \
         patch.object(Path, "exists", return_value=True):
        result = await sensor.read()
    assert result["signal"] is None
    assert result["reason"] == "backup_current"


@pytest.mark.asyncio
async def test_stale_backup_signals(tmp_path):
    """Readiness HEAD differs from backup HEAD → signal."""
    sensor = _make_sensor(
        tmp_path,
        _provenance(REPO_PATH, LIVE_HEAD, LIVE_TREE),
        _backup_state(BACKUP_HEAD, BACKUP_TREE),
    )
    with patch.object(Path, "resolve", return_value=Path(REPO_PATH)), \
         patch.object(Path, "exists", return_value=True):
        result = await sensor.read()
    assert result["signal"] == "runtime_backup"
    assert result["reason"] == "stale_backup"
    assert result["live_head"] == LIVE_HEAD
    assert result["backup_head"] == BACKUP_HEAD
    assert result["pressure"] == 0.2


@pytest.mark.asyncio
async def test_no_backup_record_signals(tmp_path):
    """Readiness passes but backup file missing → signal."""
    sensor = _make_sensor(
        tmp_path,
        _provenance(REPO_PATH, LIVE_HEAD, LIVE_TREE),
        None,  # no backup file
    )
    with patch.object(Path, "resolve", return_value=Path(REPO_PATH)), \
         patch.object(Path, "exists", return_value=True):
        result = await sensor.read()
    assert result["signal"] == "runtime_backup"
    assert result["reason"] == "no_backup_record"


@pytest.mark.asyncio
async def test_no_passing_readiness_quiet(tmp_path):
    """Readiness result != pass → quiet."""
    sensor = _make_sensor(
        tmp_path,
        _provenance(REPO_PATH, LIVE_HEAD, LIVE_TREE, result="fail"),
        _backup_state(LIVE_HEAD, LIVE_TREE),
    )
    with patch.object(Path, "resolve", return_value=Path(REPO_PATH)), \
         patch.object(Path, "exists", return_value=True):
        result = await sensor.read()
    assert result["signal"] is None
    assert result["reason"] == "no_passing_readiness"


@pytest.mark.asyncio
async def test_malformed_provenance_quiet(tmp_path):
    """Provenance file unreadable → quiet."""
    sensor = _make_sensor(tmp_path, None, _backup_state(LIVE_HEAD, LIVE_TREE))
    with patch.object(Path, "resolve", return_value=Path(REPO_PATH)), \
         patch.object(Path, "exists", return_value=True):
        result = await sensor.read()
    assert result["signal"] is None
    assert result["reason"] == "provenance_unreadable"


@pytest.mark.asyncio
async def test_restore_check_not_pass_signals(tmp_path):
    """Backup HEAD matches but restoreCheck != pass → still stale."""
    sensor = _make_sensor(
        tmp_path,
        _provenance(REPO_PATH, LIVE_HEAD, LIVE_TREE),
        _backup_state(LIVE_HEAD, LIVE_TREE, restore="failed"),
    )
    with patch.object(Path, "resolve", return_value=Path(REPO_PATH)), \
         patch.object(Path, "exists", return_value=True):
        result = await sensor.read()
    assert result["signal"] == "runtime_backup"
    assert result["reason"] == "stale_backup"


@pytest.mark.asyncio
async def test_runtime_unresolvable_quiet(tmp_path):
    """Runtime symlink missing → quiet."""
    sensor = _make_sensor(
        tmp_path,
        _provenance(REPO_PATH, LIVE_HEAD, LIVE_TREE),
        _backup_state(LIVE_HEAD, LIVE_TREE),
    )
    # Path.exists() → False to simulate missing symlink
    with patch.object(Path, "exists", return_value=False):
        result = await sensor.read()
    assert result["signal"] is None
    assert result["reason"] == "runtime_unresolved"
