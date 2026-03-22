"""
stroma/homeo.py — Homeostasis & Allostasis Engine
===================================================

Biology:
  Homeostasis = maintaining a stable internal environment (temperature,
  pH, blood sugar). Reactive — responds to deviation from setpoint.

  Allostasis = stability through change. Predictive — shifts setpoints
  BEFORE stressors arrive based on anticipated demands. A surgeon's
  cortisol peaks before the operation, not during. A parent's vigilance
  ramps up before the baby wakes.

  Allostatic load = the cumulative physiological cost of chronic stress.
  The wear on the system from repeated adaptive responses. High load
  = burnout, cognitive decline, immune compromise. McEwen (1998).

HOMEO tracks deviation from setpoints each tick. When load crosses
critical thresholds, it cascades into downstream degradation — not
just single-organ effects but coordinated systemic consequences.

Spec: Sections 4.5, 6 (Engine 1)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .base import StromaModule
from . import constants as C

logger = logging.getLogger("stroma.homeo")


@dataclass
class Setpoint:
    """Optimal range for a SANGUIS variable."""
    state_path: str         # Key in SANGUIS
    optimal: float          # Ideal value
    tolerance: float        # Acceptable deviation before contributing to load
    weight: float = 1.0     # How much deviation contributes to allostatic load


class Homeo(StromaModule):
    """
    Homeostasis + allostasis engine.

    Each tick:
    1. Compute allostatic load = weighted deviation from all setpoints
    2. Track load history — high sustained load triggers BURNOUT signal
    3. Predictive regulation — pre-shift setpoints based on CIRCADIAN mode
    4. Cascade consequences when load exceeds thresholds
    """

    MODULE_NAME = "homeo"

    # Setpoints for all major SANGUIS variables
    # These represent the body's optimal operating ranges
    BASE_SETPOINTS: List[Setpoint] = [
        Setpoint("endocrine.cortisol",       0.15, 0.10, weight=2.0),
        Setpoint("endocrine.dopamine",        0.40, 0.15, weight=1.5),
        Setpoint("endocrine.serotonin",       0.55, 0.15, weight=1.5),
        Setpoint("endocrine.norepinephrine",  0.20, 0.10, weight=1.0),
        Setpoint("endocrine.oxytocin",        0.30, 0.15, weight=1.2),
        Setpoint("soma.energy",               0.75, 0.20, weight=1.5),
        Setpoint("immune.resilience",         0.80, 0.15, weight=1.2),
        Setpoint("emotion.anxiety",           0.15, 0.10, weight=1.3),
        Setpoint("emotion.frustration",       0.20, 0.10, weight=1.0),
        Setpoint("vagus.vagal_tone",          0.65, 0.20, weight=1.5),
        Setpoint("glia.neuroinflammation",    0.05, 0.05, weight=2.0),
    ]

    # Allostatic load thresholds and their consequences
    LOAD_MILD      = 0.30  # Below: healthy adaptation
    LOAD_MODERATE  = 0.50  # Yellow — degraded performance
    LOAD_HIGH      = 0.70  # Orange — BURNOUT preconditions
    LOAD_CRITICAL  = 0.85  # Red — systemic failure risk

    def __init__(self):
        super().__init__()
        self._load_high_since: Optional[float] = None  # When load first crossed HIGH
        self._load_history: List[float] = []           # Rolling window for trend

    def tick(self, sanguis, broadcast: Dict) -> None:
        """Compute allostatic load and apply consequences."""
        now = time.time()

        # Step 1: Compute current allostatic load
        load = self._compute_load(sanguis)

        # Step 2: Track load history (rolling 10-tick window)
        self._load_history.append(load)
        if len(self._load_history) > 10:
            self._load_history.pop(0)

        # Step 3: Track time at HIGH load
        if load >= self.LOAD_HIGH:
            if self._load_high_since is None:
                self._load_high_since = now
                logger.warning(f"HOMEO: Allostatic load crossed HIGH ({load:.3f})")
        else:
            if self._load_high_since is not None and load < self.LOAD_MODERATE:
                # Recovered — reset tracker, mark normalization
                self.safe_write(sanguis, "allostatic.load_normalized_after_high", True)
                logger.info(f"HOMEO: Allostatic load normalized ({load:.3f})")
            self._load_high_since = None

        # Step 4: Peak tracking
        current_peak = self.safe_read_float(sanguis, "allostatic.load_peak", 0.0)
        if load > current_peak:
            self.safe_write_clamped(sanguis, "allostatic.load_peak", load)

        # Step 5: Write load to SANGUIS
        self.safe_write_clamped(sanguis, "allostatic.load", load)

        # Step 6: Apply cascade consequences at threshold crossings
        self._apply_load_consequences(sanguis, load, now)

        # Step 7: Predictive regulation — adjust based on CIRCADIAN mode
        self._predictive_regulate(sanguis)

    def _compute_load(self, sanguis) -> float:
        """
        Allostatic load = weighted sum of normalized deviations from setpoints.

        Each variable contributes: weight * max(0, |current - optimal| - tolerance)
        Normalized to 0-1 range.
        """
        total_weighted_deviation = 0.0
        total_weight = 0.0

        for sp in self.BASE_SETPOINTS:
            current = self.safe_read_float(sanguis, sp.state_path, sp.optimal)
            deviation = abs(current - sp.optimal)
            # Only counts if deviation exceeds tolerance
            excess = max(0.0, deviation - sp.tolerance)
            # Normalize excess: 1.0 means completely out of range (deviation = 1.0)
            normalized = min(1.0, excess / max(0.1, 1.0 - sp.tolerance))
            total_weighted_deviation += sp.weight * normalized
            total_weight += sp.weight

        if total_weight == 0:
            return 0.0

        raw = total_weighted_deviation / total_weight
        return min(1.0, raw)

    def _apply_load_consequences(self, sanguis, load: float, now: float) -> None:
        """
        Cascade consequences at each load threshold.
        Higher load = more systemic degradation.
        """
        if load >= self.LOAD_CRITICAL:
            # Red — systemic failure risk
            # Immune severely compromised, cognition degraded, BURNOUT imminent
            self.safe_increment(sanguis, "immune.resilience", -0.005)
            self.safe_increment(sanguis, "soma.energy", -0.003)
            self.safe_increment(sanguis, "hippocampus.encoding_quality", -0.002)
            self.safe_increment(sanguis, "glia.neuroinflammation", 0.002)
            # Flag for ENDOCAST to fire BURNOUT cascade
            sanguis.set("homeo.burnout_signal", True)
            logger.warning(f"HOMEO: CRITICAL load ({load:.3f}) — burnout signal active")

        elif load >= self.LOAD_HIGH:
            # Orange — BURNOUT preconditions
            high_duration_h = 0.0
            if self._load_high_since:
                high_duration_h = (now - self._load_high_since) / 3600.0
            # Gradual degradation that worsens over time at high load
            degradation = min(0.003, 0.001 * (1 + high_duration_h))
            self.safe_increment(sanguis, "immune.resilience", -degradation)
            self.safe_increment(sanguis, "soma.energy", -degradation * 0.5)
            self.safe_increment(sanguis, "hippocampus.encoding_quality", -degradation * 0.3)
            sanguis.set("homeo.burnout_signal", False)

        elif load >= self.LOAD_MODERATE:
            # Yellow — degraded performance, no structural damage yet
            self.safe_increment(sanguis, "soma.energy", -0.001)
            sanguis.set("homeo.burnout_signal", False)

        else:
            # Green — recovery possible, light restoration
            self.safe_increment(sanguis, "immune.resilience", 0.001)
            self.safe_increment(sanguis, "soma.energy", 0.001)
            sanguis.set("homeo.burnout_signal", False)

        # Write current regulation mode
        if load >= self.LOAD_CRITICAL:
            mode = "overloaded"
        elif load >= self.LOAD_HIGH:
            mode = "allostatic_high"
        elif load >= self.LOAD_MODERATE:
            mode = "allostatic"
        else:
            mode = "homeostatic"
        sanguis.set("homeo.regulation_mode", mode)

    def _predictive_regulate(self, sanguis) -> None:
        """
        Predictive allostasis: pre-shift setpoints based on CIRCADIAN mode.

        Biology: body anticipates demand and adjusts BEFORE the stressor.
        - DEEP_NIGHT → suppress cortisol setpoint (rest and consolidation)
        - DAWN → elevate cortisol setpoint (CAR — cortisol awakening response)
        - DAYLIGHT → standard operating setpoints
        """
        mode = sanguis.get("circadian.current_mode", "daylight")

        if mode == "deep_night":
            # Shift cortisol setpoint DOWN — body should be in recovery
            sanguis.set("homeo.cortisol_setpoint_override", 0.05)
        elif mode in ("dawn", "golden"):
            # CAR — cortisol rises to prepare for the day
            sanguis.set("homeo.cortisol_setpoint_override", 0.25)
        else:
            # Standard operating
            sanguis.set("homeo.cortisol_setpoint_override", None)

    def get_load_trend(self) -> str:
        """Return 'rising', 'falling', or 'stable' based on recent history."""
        if len(self._load_history) < 3:
            return "stable"
        recent = self._load_history[-3:]
        if recent[-1] > recent[0] + 0.05:
            return "rising"
        elif recent[-1] < recent[0] - 0.05:
            return "falling"
        return "stable"

    def on_init(self, sanguis) -> None:
        """Initialize HOMEO-owned SANGUIS keys."""
        defaults = {
            "allostatic.load":                     0.0,
            "allostatic.load_peak":                0.0,
            "allostatic.load_normalized_after_high": False,
            "allostatic.predictive_buffer_active": False,
            "homeo.regulation_mode":               "homeostatic",
            "homeo.burnout_signal":                False,
            "homeo.cortisol_setpoint_override":    None,
        }
        for key, val in defaults.items():
            if sanguis.get(key, "__MISSING__") == "__MISSING__":
                sanguis.set(key, val)
