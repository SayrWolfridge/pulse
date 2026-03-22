"""
stroma/endocast.py — Endocrine Cascade Engine
===============================================

The hormonal amplification system. The HEART of Stroma.

Biology: The endocrine system — event-triggered cascade chains with
amplification and negative feedback. A stressor fires → CRH → ACTH →
cortisol → systemic effects → cortisol detected by hypothalamus →
CRH suppressed → cascade terminates. Each step amplifies. Each step
has feedback. This is fundamentally different from steady-state
circulation (SANGUIS) or discrete neural spikes (COR).

ENDOCAST is what connects 20+ disconnected bio modules into a
functioning nervous system. Without it, modules write state that
nothing reads. With it, cortisol rises → immune suppresses →
hippocampus degrades → frustration elevates → creativity drops.
The cascade IS the connection.

Implementation:
- Modules register EndocrineDeclarations (what hormones they produce,
  what triggers them, what they cascade to, what feedback terminates them)
- Each COR cycle: check triggers → fire cascades → apply targets →
  process feedback → decay toward setpoints → check for runaway
- Pulsatile signaling: hormones pulse at characteristic frequencies,
  not continuous levels. Disrupted pulses → receptor desensitization.

Spec: Sections 2, 4.5, 6 (Engine 2)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import StromaModule
from . import constants as C

logger = logging.getLogger("stroma.endocast")


# =============================================================================
# CASCADE DATA STRUCTURES
# =============================================================================

@dataclass
class CascadeTarget:
    """A single downstream target of a cascade."""
    target_state_path: str          # SANGUIS key to modify (e.g. "immune.resilience")
    signal_delta: float             # How much to change (+/-)
    description: str = ""           # Human-readable description


@dataclass
class TriggerCondition:
    """When a cascade should fire."""
    state_path: str                 # SANGUIS key to monitor (e.g. "amygdala.threat_level")
    threshold: float                # Value that triggers
    direction: str = "above"        # "above" or "below"


@dataclass
class NegativeFeedback:
    """Self-terminating feedback loop."""
    monitor_state_path: str         # What to watch (e.g. "endocrine.cortisol")
    threshold: float                # When to activate suppression
    suppresses_state_path: str      # What to suppress (e.g. "endocrine.cortisol")
    suppression_rate: float         # How fast per tick (e.g. 0.02)
    delay_min: float = 0.0         # Minutes at threshold before feedback activates


@dataclass
class PulsePattern:
    """Pulsatile hormone release pattern."""
    base_frequency_min: float       # Natural pulse interval (minutes)
    base_amplitude: float           # Normal pulse height
    current_frequency: float = 0.0  # Actual (may be disrupted)
    current_amplitude: float = 0.0  # Actual
    last_pulse_ts: float = 0.0     # Timestamp of last pulse
    desensitization_count: int = 0 # Continuous pulses without rest


@dataclass
class ActiveCascade:
    """A currently running cascade instance."""
    cascade_id: str                 # Unique ID for this cascade run
    hormone: str                    # Which hormone (e.g. "cortisol")
    started_ts: float               # When it fired
    duration_min: float             # How long the cascade lasts
    intensity: float                # 0.0-1.0, scales all targets
    targets: List[CascadeTarget]    # What it affects
    feedback: List[NegativeFeedback]  # What terminates it
    feedback_active: bool = False   # Whether feedback has kicked in
    feedback_activated_ts: float = 0.0
    terminated: bool = False        # Marked for removal


@dataclass
class CascadeDefinition:
    """
    Complete definition of a cascade chain.
    Registered once. Can fire multiple times.
    """
    name: str                       # e.g. "CORTISOL", "DOPAMINE"
    hormone: str                    # Primary hormone (SANGUIS key suffix)
    trigger: TriggerCondition       # When to fire
    spike_amplitude: float          # How much hormone spikes on trigger
    duration_min: float             # How long the cascade runs
    half_life_min: float            # Hormone decay half-life
    targets: List[CascadeTarget]    # Downstream effects
    feedback: List[NegativeFeedback]  # Termination conditions
    pulse_pattern: Optional[PulsePattern] = None
    cooldown_min: float = 5.0       # Minimum time between firings
    max_duration_min: float = 240.0  # Safety cap (4 hours)
    last_fired_ts: float = 0.0


# =============================================================================
# ENDOCAST MODULE
# =============================================================================

class Endocast(StromaModule):
    """
    The Endocrine Cascade Engine.

    Manages all hormonal cascade chains in the nervous system.
    Each COR cycle:
    1. Check all registered cascade triggers
    2. Fire new cascades when conditions are met
    3. Apply cascade targets to SANGUIS (the downstream effects)
    4. Process negative feedback loops
    5. Decay all hormones toward setpoints
    6. Process pulsatile patterns
    7. Check for runaway cascades (safety)
    """

    MODULE_NAME = "endocast"

    # Hormone setpoints — baseline levels the system decays toward
    HORMONE_SETPOINTS = {
        "endocrine.cortisol": 0.1,
        "endocrine.dopamine": 0.3,
        "endocrine.oxytocin": 0.2,
        "endocrine.vasopressin": 0.1,
        "endocrine.norepinephrine": 0.2,
        "endocrine.adrenaline": 0.0,
        "endocrine.serotonin": 0.5,     # Set by MICROBIOTA, not decay
        "endocrine.melatonin": 0.0,
    }

    # Hormone half-lives (minutes) — how fast each decays without re-stimulation
    HORMONE_HALF_LIVES = {
        "endocrine.cortisol": C.CORTISOL_HALF_LIFE_MIN,
        "endocrine.dopamine": 60.0,
        "endocrine.oxytocin": C.OXYTOCIN_HALF_LIFE_MIN,
        "endocrine.vasopressin": 90.0,
        "endocrine.norepinephrine": 30.0,
        "endocrine.adrenaline": 15.0,
        "endocrine.melatonin": 60.0,
    }

    def __init__(self):
        super().__init__()
        self.cascade_definitions: Dict[str, CascadeDefinition] = {}
        self.active_cascades: List[ActiveCascade] = []
        self._cascade_counter = 0
        self._last_tick_ts = 0.0

    # =========================================================================
    # REGISTRATION — Modules register their cascade definitions
    # =========================================================================

    def register_cascade(self, definition: CascadeDefinition) -> None:
        """Register a cascade definition. Called once at startup per cascade."""
        self.cascade_definitions[definition.name] = definition
        self._log.info(f"Registered cascade: {definition.name} "
                       f"(trigger: {definition.trigger.state_path} "
                       f"{definition.trigger.direction} {definition.trigger.threshold})")

    # =========================================================================
    # TICK — The main cascade processing loop
    # =========================================================================

    def tick(self, sanguis, broadcast: Dict[str, Any]) -> None:
        """
        Process all cascades each COR cycle.
        This is the heartbeat of hormonal signaling.
        """
        now = time.time()
        dt_min = (now - self._last_tick_ts) / 60.0 if self._last_tick_ts > 0 else 1.0
        self._last_tick_ts = now

        # Step 1: Check triggers and fire new cascades
        self._check_triggers(sanguis, now)

        # Step 2: Apply active cascade targets to SANGUIS
        self._apply_cascade_targets(sanguis, now)

        # Step 3: Process negative feedback loops
        self._process_feedback(sanguis, now)

        # Step 4: Decay hormones toward setpoints
        self._decay_hormones(sanguis, dt_min)

        # Step 5: Check for runaway cascades (safety)
        self._safety_check(sanguis, now)

        # Step 6: Clean up terminated cascades
        self.active_cascades = [c for c in self.active_cascades if not c.terminated]

        # Write cascade state to SANGUIS for other modules
        self.safe_write(sanguis, "endocast.active_cascade_count",
                        len(self.active_cascades), min_val=0, max_val=100)
        self.safe_write(sanguis, "endocrine.last_update", now)

    # =========================================================================
    # STEP 1: Trigger Evaluation
    # =========================================================================

    def _check_triggers(self, sanguis, now: float) -> None:
        """Check all registered cascade triggers. Fire if conditions met."""
        for name, defn in self.cascade_definitions.items():
            # Cooldown check
            if (now - defn.last_fired_ts) < (defn.cooldown_min * 60):
                continue

            # Check if already running (don't stack same cascade)
            if any(c.hormone == defn.hormone and not c.terminated
                   for c in self.active_cascades):
                continue

            # Evaluate trigger condition
            current = self.safe_read_float(sanguis, defn.trigger.state_path, 0.5)

            fired = False
            if defn.trigger.direction == "above" and current > defn.trigger.threshold:
                fired = True
            elif defn.trigger.direction == "below" and current < defn.trigger.threshold:
                fired = True

            if fired:
                self._fire_cascade(sanguis, defn, now)

    def _fire_cascade(self, sanguis, defn: CascadeDefinition, now: float,
                      intensity: float = 1.0) -> None:
        """Fire a cascade — spike the hormone and create active cascade."""
        self._cascade_counter += 1
        cascade_id = f"{defn.name}_{self._cascade_counter}"

        # Spike the primary hormone
        self.safe_increment(sanguis, f"endocrine.{defn.hormone}",
                           defn.spike_amplitude * intensity)

        # Create active cascade
        active = ActiveCascade(
            cascade_id=cascade_id,
            hormone=defn.hormone,
            started_ts=now,
            duration_min=defn.duration_min,
            intensity=intensity,
            targets=defn.targets,
            feedback=defn.feedback,
        )
        self.active_cascades.append(active)
        defn.last_fired_ts = now

        self._log.info(
            f"CASCADE FIRED: {defn.name} (intensity={intensity:.2f}, "
            f"spike={defn.spike_amplitude * intensity:.3f}, "
            f"duration={defn.duration_min}min)"
        )

    # =========================================================================
    # STEP 2: Apply Cascade Targets
    # =========================================================================

    def _apply_cascade_targets(self, sanguis, now: float) -> None:
        """Apply downstream effects of all active cascades."""
        for cascade in self.active_cascades:
            if cascade.terminated:
                continue

            # Check if cascade has expired
            elapsed_min = (now - cascade.started_ts) / 60.0
            if elapsed_min > cascade.duration_min:
                cascade.terminated = True
                self._log.info(f"CASCADE EXPIRED: {cascade.cascade_id} "
                              f"(ran {elapsed_min:.1f}min)")
                continue

            # Apply targets (scaled by intensity, applied as increment per tick)
            # Scale factor: convert per-cascade delta to per-tick delta
            # Assume ~12 ticks per hour (5 min interval)
            ticks_per_cascade = max(1, (cascade.duration_min / 5.0))
            per_tick_scale = 1.0 / ticks_per_cascade

            for target in cascade.targets:
                delta = target.signal_delta * cascade.intensity * per_tick_scale
                self.safe_increment(sanguis, target.target_state_path, delta)

    # =========================================================================
    # STEP 3: Negative Feedback
    # =========================================================================

    def _process_feedback(self, sanguis, now: float) -> None:
        """Process negative feedback loops — self-terminating cascades."""
        for cascade in self.active_cascades:
            if cascade.terminated:
                continue

            for fb in cascade.feedback:
                current = self.safe_read_float(sanguis, fb.monitor_state_path, 0.0)

                if current > fb.threshold:
                    # Check delay
                    if not cascade.feedback_active:
                        if fb.delay_min > 0:
                            if cascade.feedback_activated_ts == 0:
                                cascade.feedback_activated_ts = now
                            elapsed = (now - cascade.feedback_activated_ts) / 60.0
                            if elapsed < fb.delay_min:
                                continue
                        cascade.feedback_active = True
                        self._log.info(
                            f"FEEDBACK ACTIVATED: {cascade.cascade_id} "
                            f"({fb.monitor_state_path}={current:.3f} > "
                            f"{fb.threshold})")

                    # Apply suppression
                    self.safe_increment(sanguis, fb.suppresses_state_path,
                                       -fb.suppression_rate)

    # =========================================================================
    # STEP 4: Hormone Decay
    # =========================================================================

    def _decay_hormones(self, sanguis, dt_min: float) -> None:
        """Decay all hormones toward their setpoints using half-life model."""
        for hormone_key, setpoint in self.HORMONE_SETPOINTS.items():
            # Serotonin is managed by MICROBIOTA, skip decay
            if hormone_key == "endocrine.serotonin":
                continue

            current = self.safe_read_float(sanguis, hormone_key, setpoint)
            half_life = self.HORMONE_HALF_LIVES.get(hormone_key, 60.0)

            if abs(current - setpoint) < 0.001:
                continue  # Close enough, skip

            # Exponential decay toward setpoint
            # decay_factor = 0.5 ^ (dt / half_life)
            if half_life > 0 and dt_min > 0:
                decay_factor = 0.5 ** (dt_min / half_life)
                new_val = setpoint + (current - setpoint) * decay_factor
                self.safe_write_clamped(sanguis, hormone_key, new_val)

    # =========================================================================
    # STEP 5: Safety Check
    # =========================================================================

    def _safety_check(self, sanguis, now: float) -> None:
        """
        Terminate any cascade running beyond max_duration.
        This prevents runaway cascades from permanently distorting state.
        """
        for cascade in self.active_cascades:
            if cascade.terminated:
                continue

            elapsed_min = (now - cascade.started_ts) / 60.0

            # Find the definition to get max_duration
            defn = self.cascade_definitions.get(
                cascade.cascade_id.rsplit("_", 1)[0], None)
            max_dur = defn.max_duration_min if defn else 240.0

            if elapsed_min > max_dur:
                cascade.terminated = True
                self._log.warning(
                    f"SAFETY: Cascade {cascade.cascade_id} terminated at "
                    f"{elapsed_min:.1f}min (max={max_dur}min)")

    # =========================================================================
    # EXTERNAL API — For modules to trigger cascades directly
    # =========================================================================

    def trigger_cascade(self, sanguis, cascade_name: str,
                        intensity: float = 1.0) -> bool:
        """
        Externally trigger a named cascade.

        Called by modules that detect cascade-worthy events:
        - AMYGDALA detects threat → trigger_cascade("CORTISOL")
        - UNITY reunion → trigger_cascade("REUNION_FLOOD", intensity=2.5)
        - MIRRORNEURON prediction match → trigger_cascade("BOND_REWARD", intensity=0.8)

        Args:
            sanguis: StateEngine
            cascade_name: registered cascade name
            intensity: 0.0-1.0+ scaling factor for all effects

        Returns:
            True if cascade was fired, False if not found or on cooldown
        """
        defn = self.cascade_definitions.get(cascade_name)
        if not defn:
            self._log.warning(f"trigger_cascade: unknown cascade '{cascade_name}'")
            return False

        now = time.time()

        # Cooldown check
        if (now - defn.last_fired_ts) < (defn.cooldown_min * 60):
            self._log.debug(f"trigger_cascade: {cascade_name} on cooldown")
            return False

        self._fire_cascade(sanguis, defn, now, intensity=intensity)
        return True

    # =========================================================================
    # INTROSPECTION
    # =========================================================================

    def get_active_cascades(self) -> List[Dict[str, Any]]:
        """Return list of currently active cascades for INSULA / debugging."""
        return [
            {
                "id": c.cascade_id,
                "hormone": c.hormone,
                "elapsed_min": (time.time() - c.started_ts) / 60.0,
                "intensity": c.intensity,
                "feedback_active": c.feedback_active,
            }
            for c in self.active_cascades
            if not c.terminated
        ]

    def on_init(self, sanguis) -> None:
        """Initialize ENDOCAST-owned SANGUIS keys."""
        for hormone_key, setpoint in self.HORMONE_SETPOINTS.items():
            current = sanguis.get(hormone_key, "__MISSING__")
            if current == "__MISSING__":
                sanguis.set(hormone_key, setpoint)

        # Initialize pulsatile tracking
        sanguis.set("endocrine.pulse.cortisol_frequency", 0.0)
        sanguis.set("endocrine.pulse.cortisol_amplitude", 0.0)
        sanguis.set("endocrine.pulse.gnrh_frequency_min",
                     C.GNRH_PULSE_INTERVAL_MIN)
        sanguis.set("endocrine.gr_resistance", 0.0)
        sanguis.set("endocast.active_cascade_count", 0)
