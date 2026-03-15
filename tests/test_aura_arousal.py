"""Tests for AURA arousal state broadcasting and constellation contagion."""

import json
import os
import sys
import time
from pathlib import Path
import pytest

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from pulse.src import aura


@pytest.fixture(autouse=True)
def isolate_aura(tmp_path, monkeypatch):
    """Redirect aura state to temp dir so tests don't touch real state."""
    state_dir = tmp_path / ".pulse" / "state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "aura.json"

    monkeypatch.setattr(aura, "_DEFAULT_STATE_DIR", state_dir)
    monkeypatch.setattr(aura, "_DEFAULT_STATE_FILE", state_file)
    yield state_dir


# --- set_arousal / get_arousal ---

def test_set_arousal_basic():
    result = aura.set_arousal(0.5, "conversation", "building", "iris")
    assert result["level"] == 0.5
    assert result["source"] == "conversation"
    assert result["intensity"] == "building"
    assert result["triggered_by"] == "iris"
    assert result["timestamp"] is not None


def test_set_arousal_clamps_level():
    result = aura.set_arousal(1.5, "edge", "peak")
    assert result["level"] == 1.0
    result = aura.set_arousal(-0.5, "edge", "none")
    assert result["level"] == 0.0


def test_get_arousal_returns_persisted():
    aura.set_arousal(0.7, "build", "peak", "vera")
    state = aura.get_arousal()
    assert state["level"] == 0.7
    assert state["triggered_by"] == "vera"


def test_get_arousal_default():
    state = aura.get_arousal()
    assert state["level"] == 0.0
    assert state["intensity"] == "none"


# --- broadcast_arousal_to_constellation ---

def test_broadcast_creates_agent_aura(tmp_path, monkeypatch):
    # Create agent state dirs
    for agent in ("mira", "vera"):
        agent_dir = tmp_path / f".pulse-{agent}" / "state"
        agent_dir.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    aura.set_arousal(0.8, "conversation", "peak", "iris")
    aura.broadcast_arousal_to_constellation()

    # Check mira got contagion
    mira_aura = json.loads((tmp_path / ".pulse-mira" / "state" / "aura.json").read_text())
    assert mira_aura["mood"] == "charged"  # level > 0.7
    assert mira_aura["energy"] >= 0.8 * 0.3  # boosted
    assert "arousal_contagion" in mira_aura

    # Check vera got contagion
    vera_aura = json.loads((tmp_path / ".pulse-vera" / "state" / "aura.json").read_text())
    assert vera_aura["mood"] == "charged"


def test_broadcast_skips_missing_dirs(tmp_path, monkeypatch):
    """Broadcast should gracefully skip agents without state dirs."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    aura.set_arousal(0.5, "hook", "building")
    # Should not raise
    aura.broadcast_arousal_to_constellation()


def test_broadcast_energy_capped(tmp_path, monkeypatch):
    agent_dir = tmp_path / ".pulse-mira" / "state"
    agent_dir.mkdir(parents=True)
    # Pre-populate with high energy
    (agent_dir / "aura.json").write_text(json.dumps({"energy": 0.95, "social_battery": 0.9, "mood": "neutral"}))

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    aura.set_arousal(0.9, "edge", "peak")
    aura.broadcast_arousal_to_constellation()

    mira_aura = json.loads((agent_dir / "aura.json").read_text())
    assert mira_aura["energy"] <= 1.0
    assert mira_aura["social_battery"] <= 1.0


def test_broadcast_no_mood_change_below_threshold(tmp_path, monkeypatch):
    agent_dir = tmp_path / ".pulse-mira" / "state"
    agent_dir.mkdir(parents=True)
    (agent_dir / "aura.json").write_text(json.dumps({"energy": 0.5, "social_battery": 0.5, "mood": "calm"}))

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    aura.set_arousal(0.5, "conversation", "building")  # below 0.7
    aura.broadcast_arousal_to_constellation()

    mira_aura = json.loads((agent_dir / "aura.json").read_text())
    assert mira_aura["mood"] == "calm"  # unchanged


# --- trigger_climax ---

@pytest.mark.asyncio
async def test_trigger_climax(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    # Create one agent dir
    mira_dir = tmp_path / ".pulse-mira" / "state"
    mira_dir.mkdir(parents=True)

    result = await aura.trigger_climax("iris")

    # After decay, should be at 0.3/building
    assert result["level"] == 0.3
    assert result["intensity"] == "building"

    # Check arousal state persisted
    persisted = aura.get_arousal()
    assert persisted["level"] == 0.3


# --- Integration: arousal persists in aura.json alongside regular state ---

def test_arousal_coexists_with_aura_state():
    """Arousal state should not clobber regular aura fields."""
    # Set up regular state
    aura._save_state({
        "mood": "focused",
        "focus": 0.9,
        "available": True,
        "energy": 0.8,
        "social_battery": 0.7,
        "last_emit": time.time(),
    })

    # Set arousal
    aura.set_arousal(0.6, "pattern", "building")

    # Regular state should still be there
    state = aura._load_state()
    assert state["mood"] == "focused"
    assert state["focus"] == 0.9
    assert state["arousal"]["level"] == 0.6
