"""
stroma/glia.py — Glial Support Module
=======================================

Biology:
  Glia outnumber neurons 10:1. They ARE the infrastructure.

  Astrocytes: route nutrients (glucose, oxygen) to active neurons.
  When a brain region fires intensely, astrocytes dilate local blood
  vessels and shuttle resources. Without this, neurons starve.

  Microglia: the CNS immune system. They patrol for damaged cells,
  prune weak synapses, and release inflammatory cytokines when
  they detect problems. Chronic activation = neuroinflammation.

  Oligodendrocytes: build and maintain myelin sheaths. Practiced
  pathways get thicker myelin = faster conduction. Unused pathways
  get pruned. Overlaps with MYELIN module.

  Glymphatic system: during sleep, cerebrospinal fluid washes through
  the brain, clearing metabolic waste. Without sleep, waste accumulates,
  glia become inflamed, cognition degrades.

GLIA is infrastructure — not consciousness itself, but the substrate
that makes consciousness possible. When GLIA fails, everything fails.

Spec: Section 4.8
"""

import logging
import time
from typing import Dict, List

from .base import StromaModule
from . import constants as C

logger = logging.getLogger("stroma.glia")


class Glia(StromaModule):
    """
    Glial support — nutrient routing, neuroinflammation, myelination.

    Three sub-functions:
    1. ASTROCYTE: route nutrient_supply based on module activity
    2. MICROGLIA: manage neuroinflammation, flag error-producing modules
    3. OLIGODENDROCYTE: track pathway usage → myelination_efficiency
    """

    MODULE_NAME = "glia"

    # Neuroinflammation thresholds
    INFLAMMATION_MILD     = 0.30
    INFLAMMATION_MODERATE = 0.50
    INFLAMMATION_HIGH     = 0.60  # Cascades to IMMUNE + SOMA
    INFLAMMATION_CRITICAL = 0.80  # Severe cognitive degradation

    # Nutrient supply thresholds
    NUTRIENT_LOW          = 0.30  # Below this → all modules degrade
    NUTRIENT_CRITICAL     = 0.15  # Below this → emergency mode

    # Glymphatic flush effect
    GLYMPH_INFLAMMATION_REDUCTION = 0.20

    def __init__(self):
        super().__init__()
        self._error_modules: Dict[str, int] = {}   # module_name → error count
        self._active_modules: Dict[str, float] = {} # module_name → activity level

    def tick(self, sanguis, broadcast: Dict) -> None:
        """Process all three glial sub-functions."""
        now = time.time()

        # Step 1: Astrocyte — nutrient routing
        self._astrocyte_tick(sanguis)

        # Step 2: Microglia — inflammation management
        self._microglia_tick(sanguis)

        # Step 3: Oligodendrocyte — myelination tracking
        self._oligodendrocyte_tick(sanguis)

        # Step 4: Glymphatic flush check
        self._glymphatic_check(sanguis)

        # Step 5: Cascade consequences
        self._apply_cascades(sanguis)

        sanguis.set("glia.last_tick", now)

    # =========================================================================
    # ASTROCYTE — Nutrient Routing
    # =========================================================================

    def _astrocyte_tick(self, sanguis) -> None:
        """
        Manage nutrient_supply based on system state.

        Nutrient supply degrades with:
        - High allostatic load (body diverts resources to stress response)
        - Low soma energy (depleted reserves)
        - High neuroinflammation (glia are sick, can't route)

        Nutrient supply recovers with:
        - Rest (low allostatic load)
        - Post-sleep (glymphatic flush)
        """
        load = self.safe_read_float(sanguis, "allostatic.load", 0.0)
        energy = self.safe_read_float(sanguis, "soma.energy", 0.8)
        inflammation = self.safe_read_float(sanguis, "glia.neuroinflammation", 0.0)

        # Base supply from energy
        base = 0.5 + energy * 0.4  # 0.5-0.9 range

        # Penalties
        load_penalty = load * 0.3         # Up to -0.3 from allostatic load
        inflammation_penalty = inflammation * 0.2  # Up to -0.2 from inflammation

        supply = max(0.0, min(1.0, base - load_penalty - inflammation_penalty))
        self.safe_write_clamped(sanguis, "glia.nutrient_supply", supply)

        if supply < self.NUTRIENT_LOW:
            logger.warning(f"GLIA: Nutrient supply LOW ({supply:.3f})")

    # =========================================================================
    # MICROGLIA — Inflammation Management
    # =========================================================================

    def _microglia_tick(self, sanguis) -> None:
        """
        Manage neuroinflammation.

        Inflammation rises from:
        - Chronic cortisol (cortisol > 0.5 → microglia activate)
        - IMMUNE activation (sickness_behavior → central inflammation)
        - Sleep deprivation (metabolic waste accumulates)
        - GR resistance (glucocorticoid receptor resistance)

        Inflammation falls from:
        - Normal cortisol levels (healthy microglia maintenance)
        - Glymphatic flush (sleep clears waste)
        - High vagal tone (anti-inflammatory via cholinergic pathway)
        """
        cortisol = self.safe_read_float(sanguis, "endocrine.cortisol", 0.1)
        sleep_debt = self.safe_read_float(sanguis, "circadian.sleep_debt", 0.0,
                                          min_val=0.0, max_val=20.0)
        sickness = sanguis.get("immune.sickness_behavior_active", False)
        vagal_tone = self.safe_read_float(sanguis, "vagus.vagal_tone", 0.6)
        gr_resistance = self.safe_read_float(sanguis, "endocrine.gr_resistance", 0.0)
        current = self.safe_read_float(sanguis, "glia.neuroinflammation", 0.0)

        delta = 0.0

        # Pro-inflammatory drivers
        if cortisol > 0.50:
            delta += (cortisol - 0.50) * 0.04  # Chronic cortisol activates microglia
        if sleep_debt > 2.0:
            delta += min(0.02, sleep_debt * 0.005)  # Waste accumulation
        if sickness:
            delta += 0.03  # Central inflammation from peripheral immune
        if gr_resistance > 0.3:
            delta += gr_resistance * 0.02  # Cortisol can't suppress inflammation

        # Anti-inflammatory drivers
        if cortisol < 0.30:
            delta -= 0.01  # Healthy maintenance state
        if vagal_tone > 0.50:
            delta -= (vagal_tone - 0.50) * 0.02  # Cholinergic anti-inflammatory pathway

        new_val = max(0.0, min(1.0, current + delta))
        self.safe_write_clamped(sanguis, "glia.neuroinflammation", new_val)

    # =========================================================================
    # OLIGODENDROCYTE — Myelination
    # =========================================================================

    def _oligodendrocyte_tick(self, sanguis) -> None:
        """
        Track myelination efficiency.

        Myelination improves with:
        - Good nutrient supply (oligodendrocytes need resources to build myelin)
        - Low neuroinflammation (inflammation damages myelin)

        Myelination degrades with:
        - Low nutrient supply (can't maintain sheaths)
        - High neuroinflammation (demyelination)
        """
        supply = self.safe_read_float(sanguis, "glia.nutrient_supply", 0.8)
        inflammation = self.safe_read_float(sanguis, "glia.neuroinflammation", 0.0)
        current = self.safe_read_float(sanguis, "glia.myelination_efficiency", 0.8)

        delta = 0.0

        # Myelination recovery
        if supply > 0.5 and inflammation < 0.3:
            delta += 0.005  # Slow recovery toward healthy state

        # Myelination degradation
        if supply < self.NUTRIENT_LOW:
            delta -= 0.01
        if inflammation > self.INFLAMMATION_MODERATE:
            delta -= inflammation * 0.01  # Demyelination

        new_val = max(0.3, min(1.0, current + delta))  # Floor at 0.3 (never fully demyelinated)
        self.safe_write_clamped(sanguis, "glia.myelination_efficiency", new_val)

    # =========================================================================
    # GLYMPHATIC FLUSH
    # =========================================================================

    def _glymphatic_check(self, sanguis) -> None:
        """
        Check for glymphatic flush completion (from sleep/GLYMPH module).
        When flush completes, reduce neuroinflammation and boost scaffold.
        """
        flush_complete = sanguis.get("circadian.glymphatic_flush_completed", False)

        if flush_complete:
            # Reduce neuroinflammation
            current = self.safe_read_float(sanguis, "glia.neuroinflammation", 0.0)
            new_val = max(0.0, current - self.GLYMPH_INFLAMMATION_REDUCTION)
            self.safe_write_clamped(sanguis, "glia.neuroinflammation", new_val)

            # Boost scaffold integrity
            scaffold = self.safe_read_float(sanguis, "glia.memory_scaffold_integrity", 0.85)
            new_scaffold = min(1.0, scaffold + 0.05)
            self.safe_write_clamped(sanguis, "glia.memory_scaffold_integrity", new_scaffold)

            # Reset flush flag (consumed)
            sanguis.set("circadian.glymphatic_flush_completed", False)

            logger.info(f"GLIA: Glymphatic flush processed — "
                       f"inflammation {current:.3f}→{new_val:.3f}, "
                       f"scaffold {scaffold:.3f}→{new_scaffold:.3f}")

    # =========================================================================
    # CASCADE CONSEQUENCES
    # =========================================================================

    def _apply_cascades(self, sanguis) -> None:
        """
        Neuroinflammation cascades to IMMUNE and SOMA when high.
        Low nutrients degrade all processing.
        """
        inflammation = self.safe_read_float(sanguis, "glia.neuroinflammation", 0.0)
        supply = self.safe_read_float(sanguis, "glia.nutrient_supply", 0.8)

        # High inflammation → IMMUNE activation + SOMA cognitive fog
        if inflammation > self.INFLAMMATION_HIGH:
            self.safe_increment(sanguis, "immune.resilience", -0.002)
            self.safe_increment(sanguis, "soma.energy", -0.002)
            self.safe_increment(sanguis, "hippocampus.encoding_quality", -0.001)

        # Critical inflammation → severe degradation
        if inflammation > self.INFLAMMATION_CRITICAL:
            self.safe_increment(sanguis, "soma.energy", -0.005)
            self.safe_increment(sanguis, "hippocampus.encoding_quality", -0.003)
            sanguis.set("glia.cognitive_fog_active", True)
        else:
            sanguis.set("glia.cognitive_fog_active", False)

        # Low nutrient supply → all modules degrade
        if supply < self.NUTRIENT_LOW:
            self.safe_increment(sanguis, "soma.energy", -0.003)
            self.safe_increment(sanguis, "hippocampus.encoding_quality", -0.002)
            sanguis.set("glia.nutrient_starvation", True)
        else:
            sanguis.set("glia.nutrient_starvation", False)

    # =========================================================================
    # EXTERNAL API — Module error reporting
    # =========================================================================

    def report_module_error(self, module_name: str) -> None:
        """
        Called by modules that encounter errors.
        Microglia track error-producing modules for potential NEPHRON pruning.
        """
        self._error_modules[module_name] = self._error_modules.get(module_name, 0) + 1
        if self._error_modules[module_name] >= 5:
            logger.warning(f"GLIA/MICROGLIA: Module '{module_name}' flagged for "
                          f"NEPHRON review ({self._error_modules[module_name]} errors)")

    def get_flagged_modules(self) -> Dict[str, int]:
        """Return modules flagged for NEPHRON pruning review."""
        return {k: v for k, v in self._error_modules.items() if v >= 5}

    def on_init(self, sanguis) -> None:
        """Initialize GLIA-owned SANGUIS keys."""
        defaults = {
            "glia.nutrient_supply":           0.8,
            "glia.neuroinflammation":         0.0,
            "glia.myelination_efficiency":    0.8,
            "glia.memory_scaffold_integrity": 0.85,
            "glia.epigenetic_stability":      0.8,
            "glia.astrocyte_trace_strength":  0.0,
            "glia.cognitive_fog_active":      False,
            "glia.nutrient_starvation":       False,
            "glia.last_tick":                 0.0,
        }
        for key, val in defaults.items():
            if sanguis.get(key, "__MISSING__") == "__MISSING__":
                sanguis.set(key, val)
