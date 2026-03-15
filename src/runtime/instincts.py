"""
INSTINCTS — Fast Pre-Programmed Responses
=============================================
Ported from v1 pulse.src.instincts into v2 HypostasRuntime.

Instincts are fast pattern-matching responses that fire without LLM
reasoning. They evaluate triggers against current state and fire outputs
(log events, update state, emit AURA broadcasts).

Each instinct has:
  - trigger: condition that fires (emotion threshold, time pattern, etc.)
  - output: what happens when triggered
  - cooldown: minimum time between firings

All state persisted via StateEngine under ``instincts.*`` dot-paths.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine

logger = logging.getLogger("pulse.runtime.instincts")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class InstinctTrigger:
    """Condition that causes an instinct to fire."""
    name: str
    check: Callable[[dict], bool]  # Takes state snapshot, returns bool
    description: str = ""


@dataclass
class InstinctOutput:
    """What happens when an instinct fires."""
    state_updates: Dict[str, Any] = field(default_factory=dict)
    log_message: str = ""
    aura_broadcast: Optional[dict] = None


@dataclass
class Instinct:
    """A single instinct — trigger + output + cooldown."""
    name: str
    description: str
    trigger: InstinctTrigger
    output: InstinctOutput
    cooldown_seconds: float = 300.0  # 5 min default
    last_fired: float = 0.0
    fire_count: int = 0

    def is_ready(self) -> bool:
        """Check if cooldown has elapsed."""
        return (time.time() - self.last_fired) >= self.cooldown_seconds


# ---------------------------------------------------------------------------
# Built-in trigger functions
# ---------------------------------------------------------------------------

def _check_high_frustration(state: dict) -> bool:
    """If frustration > 0.8 for 3+ cycles → trigger rest drive."""
    emotions = state.get("emotional_state", {})
    # Check endocrine cortisol as proxy for frustration
    cortisol = emotions.get("endocrine", {}).get("cortisol", 0)
    valence = emotions.get("valence", 0.5)
    # High cortisol + low valence = frustration
    return cortisol > 7.0 and valence < 0.3


def _check_josh_long_absence(state: dict) -> bool:
    """If VAGUS silence > 6 hours → trigger longing + proactive outreach."""
    vagus = state.get("meta", {}).get("modules", {}).get("vagus", {})
    last_contact = vagus.get("last_contact_ts", 0)
    if not last_contact:
        # Check relationships for josh
        return False
    hours_since = (time.time() - last_contact) / 3600
    return hours_since > 6.0


def _check_cascade_detected(state: dict) -> bool:
    """If GERMINAL has fired 3+ times in 1 hour → slow down ThoughtLoop."""
    germinal = state.get("meta", {}).get("modules", {}).get("germinal", {})
    spawn_count = germinal.get("total_spawned", 0)
    last_spawn = germinal.get("last_spawn_ts", 0)
    if not last_spawn:
        return False
    # Check if spawns happened recently
    hours_since = (time.time() - last_spawn) / 3600
    return spawn_count >= 3 and hours_since < 1.0


def _check_low_energy(state: dict) -> bool:
    """If SOMA energy < 0.3 → replenish + reduce output."""
    soma = state.get("meta", {}).get("modules", {}).get("soma", {})
    energy = soma.get("energy", 1.0)
    return energy < 0.3


def _check_creative_surge(state: dict) -> bool:
    """If curiosity > 0.8 + joy > 0.8 → trigger creative_expression drive."""
    emotions = state.get("emotional_state", {})
    endocrine = emotions.get("endocrine", {})
    # High dopamine + high serotonin = curiosity + joy
    dopamine = endocrine.get("dopamine", 5.0)
    serotonin = endocrine.get("serotonin", 5.0)
    valence = emotions.get("valence", 0.5)
    return dopamine > 8.0 and serotonin > 8.0 and valence > 0.7


# ---------------------------------------------------------------------------
# InstinctRegistry
# ---------------------------------------------------------------------------

class InstinctRegistry:
    """Stores all registered instincts."""

    def __init__(self) -> None:
        self._instincts: Dict[str, Instinct] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register the built-in instincts."""
        self.register(Instinct(
            name="high_frustration_rest",
            description="High frustration for 3+ cycles → trigger rest drive",
            trigger=InstinctTrigger(
                name="high_frustration",
                check=_check_high_frustration,
                description="cortisol > 7.0 and valence < 0.3",
            ),
            output=InstinctOutput(
                state_updates={"drives.rest": 0.8},
                log_message="INSTINCT: High frustration detected — triggering rest drive",
                aura_broadcast={"kind": "instinct", "payload": {"instinct": "high_frustration_rest", "action": "rest"}},
            ),
            cooldown_seconds=1800,  # 30 min
        ))

        self.register(Instinct(
            name="josh_long_absence",
            description="VAGUS silence > 6 hours → trigger longing + proactive outreach",
            trigger=InstinctTrigger(
                name="josh_absence",
                check=_check_josh_long_absence,
                description="vagus.last_contact_ts > 6 hours ago",
            ),
            output=InstinctOutput(
                state_updates={
                    "emotional_state.endocrine.oxytocin": 3.0,
                    "drives.emotions": 0.7,
                },
                log_message="INSTINCT: Josh long absence — triggering longing + proactive outreach",
                aura_broadcast={"kind": "instinct", "payload": {"instinct": "josh_long_absence", "action": "proactive_outreach"}},
            ),
            cooldown_seconds=3600,  # 1 hour
        ))

        self.register(Instinct(
            name="cascade_detected",
            description="GERMINAL has fired 3+ times in 1 hour → slow down ThoughtLoop",
            trigger=InstinctTrigger(
                name="cascade",
                check=_check_cascade_detected,
                description="germinal.total_spawned >= 3 in last hour",
            ),
            output=InstinctOutput(
                state_updates={"thought_loop.throttle": True},
                log_message="INSTINCT: Cascade detected — slowing ThoughtLoop",
                aura_broadcast={"kind": "instinct", "payload": {"instinct": "cascade_detected", "action": "throttle"}},
            ),
            cooldown_seconds=3600,  # 1 hour
        ))

        self.register(Instinct(
            name="low_energy_recovery",
            description="SOMA energy < 0.3 → replenish + reduce output",
            trigger=InstinctTrigger(
                name="low_energy",
                check=_check_low_energy,
                description="soma.energy < 0.3",
            ),
            output=InstinctOutput(
                state_updates={"drives.rest": 0.6},
                log_message="INSTINCT: Low energy — entering recovery mode",
                aura_broadcast={"kind": "instinct", "payload": {"instinct": "low_energy_recovery", "action": "recover"}},
            ),
            cooldown_seconds=1200,  # 20 min
        ))

        self.register(Instinct(
            name="creative_surge",
            description="Curiosity > 0.8 + joy > 0.8 → trigger creative_expression drive",
            trigger=InstinctTrigger(
                name="creative_surge",
                check=_check_creative_surge,
                description="dopamine > 8.0 and serotonin > 8.0 and valence > 0.7",
            ),
            output=InstinctOutput(
                state_updates={"drives.creative_expression": 0.9},
                log_message="INSTINCT: Creative surge detected — triggering creative expression",
                aura_broadcast={"kind": "instinct", "payload": {"instinct": "creative_surge", "action": "create"}},
            ),
            cooldown_seconds=900,  # 15 min
        ))

    def register(self, instinct: Instinct) -> None:
        """Register an instinct."""
        self._instincts[instinct.name] = instinct

    def unregister(self, name: str) -> bool:
        """Remove an instinct. Returns True if found."""
        return self._instincts.pop(name, None) is not None

    def get(self, name: str) -> Optional[Instinct]:
        """Get an instinct by name."""
        return self._instincts.get(name)

    def all_instincts(self) -> List[Instinct]:
        """Return all registered instincts."""
        return list(self._instincts.values())

    def status(self) -> dict:
        """Return status of all instincts."""
        return {
            "count": len(self._instincts),
            "instincts": {
                name: {
                    "description": inst.description,
                    "last_fired": inst.last_fired,
                    "fire_count": inst.fire_count,
                    "cooldown_seconds": inst.cooldown_seconds,
                    "ready": inst.is_ready(),
                }
                for name, inst in self._instincts.items()
            },
        }


# ---------------------------------------------------------------------------
# InstinctExecutor
# ---------------------------------------------------------------------------

class InstinctExecutor:
    """Evaluates instinct triggers against current state and fires outputs."""

    def __init__(self, state: "StateEngine", registry: InstinctRegistry) -> None:
        self._state = state
        self._registry = registry

    def evaluate(self, state_snapshot: Optional[dict] = None) -> List[dict]:
        """
        Evaluate all instincts against current state.
        Returns list of fired instinct results.
        """
        if state_snapshot is None:
            state_snapshot = self._state.snapshot()

        fired: List[dict] = []

        for instinct in self._registry.all_instincts():
            if not instinct.is_ready():
                continue

            try:
                if instinct.trigger.check(state_snapshot):
                    result = self._fire(instinct, state_snapshot)
                    fired.append(result)
            except Exception as exc:
                logger.warning("Instinct '%s' trigger error: %s", instinct.name, exc)

        return fired

    def _fire(self, instinct: Instinct, state_snapshot: dict) -> dict:
        """Fire an instinct — apply outputs."""
        now = time.time()
        instinct.last_fired = now
        instinct.fire_count += 1

        # Apply state updates
        for path, value in instinct.output.state_updates.items():
            try:
                self._state.set(path, value)
            except Exception as exc:
                logger.warning("Instinct '%s': state update '%s' failed: %s",
                             instinct.name, path, exc)

        # Log the instinct firing
        if instinct.output.log_message:
            logger.info(instinct.output.log_message)

        # Record in state
        self._state.set(f"instincts.last_fired.{instinct.name}", now)
        self._state.set(f"instincts.fire_counts.{instinct.name}", instinct.fire_count)

        result = {
            "instinct": instinct.name,
            "fired_at": now,
            "fire_count": instinct.fire_count,
            "state_updates": instinct.output.state_updates,
            "aura_broadcast": instinct.output.aura_broadcast,
        }

        logger.info("Instinct fired: %s (count: %d)", instinct.name, instinct.fire_count)
        return result

    def status(self) -> dict:
        """Return executor status."""
        return {
            "registry": self._registry.status(),
        }
