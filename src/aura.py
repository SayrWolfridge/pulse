"""AURA — Ambient State Broadcast for Pulse.

Compact JSON every 60s: {mood, focus, available, energy, social_battery}.
Reads from ENDOCRINE/CIRCADIAN/SOMA/ADIPOSE/BUFFER.

Extended with arousal state broadcasting and constellation contagion.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from pulse.src import thalamus

logger = logging.getLogger("pulse.aura")

_DEFAULT_STATE_DIR = Path.home() / ".pulse" / "state"
_DEFAULT_STATE_FILE = _DEFAULT_STATE_DIR / "aura.json"

EMIT_INTERVAL = 60  # seconds


def _load_state() -> dict:
    if _DEFAULT_STATE_FILE.exists():
        try:
            return json.loads(_DEFAULT_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "mood": "neutral",
        "focus": 0.5,
        "available": True,
        "energy": 1.0,
        "social_battery": 0.8,
        "last_emit": 0,
    }


def _save_state(state: dict):
    _DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _DEFAULT_STATE_FILE.write_text(json.dumps(state, indent=2))


def emit() -> dict:
    """Compute and emit current aura from all sources."""
    aura = _load_state()

    # Read ENDOCRINE mood
    try:
        from pulse.src import endocrine

        mood = endocrine.get_mood()
        aura["mood"] = mood.get("label", "neutral")
    except Exception:
        pass

    # Read CIRCADIAN mode for focus
    try:
        from pulse.src import circadian

        mode = circadian.get_current_mode()
        mode_val = mode.value if hasattr(mode, "value") else str(mode)
        focus_map = {
            "dawn": 0.6,
            "daylight": 0.8,
            "golden": 0.7,
            "twilight": 0.4,
            "deep_night": 0.2,
        }
        aura["focus"] = focus_map.get(mode_val, 0.5)
        aura["available"] = mode_val not in ("deep_night",)
    except Exception:
        pass

    # Read SOMA energy
    try:
        from pulse.src import soma

        status = soma.get_status()
        aura["energy"] = status.get("energy", 1.0)
    except Exception:
        pass

    # Read ADIPOSE for social battery proxy
    try:
        from pulse.src import adipose

        report = adipose.get_budget_report()
        conv = report.get("categories", {}).get("conversation", {})
        pct_used = conv.get("percent_used", 0)
        aura["social_battery"] = max(0.0, 1.0 - pct_used / 100.0)
    except Exception:
        pass

    aura["last_emit"] = time.time()
    _save_state(aura)

    # Broadcast to THALAMUS
    thalamus.append(
        {
            "source": "aura",
            "type": "ambient",
            "salience": 0.2,
            "data": {k: v for k, v in aura.items() if k != "last_emit"},
        }
    )

    return aura


def should_emit() -> bool:
    """Check if enough time has passed since last emit."""
    state = _load_state()
    return (time.time() - state.get("last_emit", 0)) >= EMIT_INTERVAL


def get_aura() -> dict:
    """Return current aura without re-computing."""
    return _load_state()


def get_status() -> dict:
    """Return aura status."""
    state = _load_state()
    return {
        "mood": state["mood"],
        "energy": state["energy"],
        "available": state["available"],
        "last_emit": state["last_emit"],
    }


# --- Arousal State ---

_CONSTELLATION_AGENTS = ("mira", "vera", "lyra", "sage")

_arousal_state = {
    "level": 0.0,
    "source": None,
    "intensity": "none",
    "triggered_by": None,
    "timestamp": None,
}


def _load_arousal() -> dict:
    """Load arousal state from aura.json."""
    state = _load_state()
    return state.get("arousal", dict(_arousal_state))


def _save_arousal(arousal: dict):
    """Persist arousal state into aura.json."""
    state = _load_state()
    state["arousal"] = arousal
    _save_state(state)


def set_arousal(level: float, source: str | None, intensity: str, triggered_by: str = "iris") -> dict:
    """Update arousal state and persist."""
    arousal = {
        "level": max(0.0, min(1.0, level)),
        "source": source,
        "intensity": intensity,
        "triggered_by": triggered_by,
        "timestamp": time.time(),
    }
    _save_arousal(arousal)
    logger.info(f"Arousal set: level={arousal['level']}, intensity={intensity}, by={triggered_by}")
    return arousal


def get_arousal() -> dict:
    """Return current arousal state."""
    return _load_arousal()


def broadcast_arousal_to_constellation(store_path: str = "~/.pulse/state/"):
    """Write arousal contagion to each agent's aura file.

    For each agent in the constellation, if their state dir exists,
    boost their energy and social_battery based on arousal level.
    """
    arousal = _load_arousal()
    level = arousal.get("level", 0.0)
    if level <= 0.0:
        return

    for agent in _CONSTELLATION_AGENTS:
        agent_state_dir = Path.home() / f".pulse-{agent}" / "state"
        agent_aura_file = agent_state_dir / "aura.json"

        if not agent_state_dir.exists():
            continue

        # Load or create agent's aura
        try:
            if agent_aura_file.exists():
                agent_aura = json.loads(agent_aura_file.read_text())
            else:
                agent_aura = {
                    "mood": "neutral",
                    "focus": 0.5,
                    "available": True,
                    "energy": 0.8,
                    "social_battery": 0.8,
                    "last_emit": 0,
                }
        except (json.JSONDecodeError, OSError):
            continue

        # Apply contagion
        agent_aura["energy"] = min(1.0, agent_aura.get("energy", 0.8) + level * 0.3)
        if level > 0.7:
            agent_aura["mood"] = "charged"
        agent_aura["social_battery"] = min(1.0, agent_aura.get("social_battery", 0.8) + level * 0.2)
        agent_aura["arousal_contagion"] = {
            "level": level,
            "source": arousal.get("source"),
            "from": arousal.get("triggered_by"),
            "at": time.time(),
        }

        try:
            agent_state_dir.mkdir(parents=True, exist_ok=True)
            agent_aura_file.write_text(json.dumps(agent_aura, indent=2))
            logger.debug(f"Arousal contagion broadcast to {agent}")
        except OSError as e:
            logger.warning(f"Failed to broadcast arousal to {agent}: {e}")


async def trigger_climax(triggered_by: str = "iris") -> dict:
    """Set level=1.0, intensity='release', broadcast, then decay after 2s."""
    arousal = set_arousal(1.0, "climax", "release", triggered_by)
    broadcast_arousal_to_constellation()
    logger.info(f"Climax triggered by {triggered_by} — broadcasting to constellation")

    # Fire arousal cascade to Discord
    from pulse.src.logos.arousal_cascade import schedule_cascade
    schedule_cascade(intensity="peak", triggered_by=triggered_by)

    # Decay after 2 seconds
    await asyncio.sleep(2)
    arousal = set_arousal(0.3, "climax_afterglow", "building", triggered_by)
    broadcast_arousal_to_constellation()
    logger.info("Climax decay complete — settling to 0.3 building")
    return arousal
