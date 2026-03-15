"""
GENOME — Exportable DNA Config / Shareable Identity Spec
==========================================================
Ported from v1 pulse.src.genome into v2 HypostasRuntime.

All module settings/thresholds/weights in one exportable config.
Creates a shareable identity spec — a JSON document capturing core
identity, preferences, values, and configuration.

All state persisted via StateEngine under ``genome.*`` dot-paths.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .self_model import SelfModel

SCHEMA_VERSION = "2.0"
PULSE_VERSION = "0.5.5"

_DEFAULT_MODULES = {
    "endocrine": {
        "decay_rates": {
            "cortisol": -0.05, "dopamine": -0.08, "serotonin": -0.02,
            "oxytocin": -0.04, "adrenaline": -0.28, "melatonin": -0.01,
        },
        "high_threshold": 0.5,
        "low_threshold": 0.3,
    },
    "limbic": {"half_life_ms": 14400000, "decay_threshold": 0.5, "contagion_multiplier": 0.5},
    "retina": {"default_threshold": 0.3, "focus_threshold": 0.8},
    "circadian": {"dawn_hours": [6, 9], "daylight_hours": [9, 17], "golden_hours": [17, 22]},
    "amygdala": {"fast_path_threshold": 0.7},
    "phenotype": {"default_humor": 0.3, "default_intensity": 0.5},
    "telomere": {"drift_threshold": 0.3},
    "hypothalamus": {"signal_threshold": 3, "retirement_days": 30, "weight_floor": 0.1},
    "soma": {"energy_cost_per_token": 0.001, "rem_replenish": 0.5},
    "dendrite": {"trust_increment": 0.01, "trust_decrement": 0.05},
    "vestibular": {"building_shipping_range": [0.3, 0.7], "working_reflecting_range": [0.4, 0.8]},
}


class Genome:
    """Exportable DNA config — shareable identity spec."""

    _KEY = "genome"

    def __init__(self, state: "StateEngine", self_model: "SelfModel") -> None:
        self._state = state
        self._self_model = self_model
        if self._state.get(f"{self._KEY}.version") is None:
            self._state.set(f"{self._KEY}.version", "3.0")
            self._state.set(f"{self._KEY}.modules", _DEFAULT_MODULES)
            self._state.set(f"{self._KEY}.created_at", time.time())
            self._state.set(f"{self._KEY}.last_mutation", None)

    def export_genome(self) -> dict:
        """Export full genome config with identity data from SelfModel."""
        modules = self._state.get(f"{self._KEY}.modules") or _DEFAULT_MODULES

        # Pull identity from SelfModel
        identity = {}
        try:
            sm_status = self._self_model.status()
            identity = {
                "description": sm_status.get("description", ""),
                "traits": sm_status.get("traits", []),
                "insights_count": sm_status.get("insights_count", 0),
            }
        except Exception:
            pass

        # Pull drives snapshot
        drives = self._state.get("drives") or {}

        return {
            "schema_version": SCHEMA_VERSION,
            "pulse_version": PULSE_VERSION,
            "exported_at": time.time(),
            "version": self._state.get(f"{self._KEY}.version") or "3.0",
            "modules": modules,
            "identity": identity,
            "drives": drives,
            "created_at": self._state.get(f"{self._KEY}.created_at"),
        }

    def import_genome(self, g: dict) -> Tuple[dict, List[str]]:
        """Import a genome config. Returns (imported, warnings)."""
        warnings = []

        if "modules" in g:
            self._state.set(f"{self._KEY}.modules", g["modules"])
        else:
            warnings.append("no modules key — modules unchanged")

        if "version" in g:
            self._state.set(f"{self._KEY}.version", g["version"])

        self._state.set(f"{self._KEY}.imported_at", time.time())

        if g.get("identity"):
            warnings.append("identity noted but not applied to live state")

        if g.get("drives"):
            warnings.append("drives snapshot noted but not applied to live state")

        return g, warnings

    def get_module_config(self, module_name: str) -> Optional[dict]:
        """Get config for a specific module."""
        modules = self._state.get(f"{self._KEY}.modules") or {}
        return modules.get(module_name)

    def mutate(self, module_name: str, key: str, value: Any) -> dict:
        """Mutate a specific setting. Used by PLASTICITY."""
        modules = dict(self._state.get(f"{self._KEY}.modules") or {})
        if module_name not in modules:
            modules[module_name] = {}
        modules[module_name][key] = value
        self._state.set(f"{self._KEY}.modules", modules)
        self._state.set(f"{self._KEY}.last_mutation", {
            "module": module_name,
            "key": key,
            "ts": time.time(),
        })
        return modules[module_name]

    def tick(self) -> None:
        """No-op — genome is event-driven."""
        pass

    def status(self) -> dict:
        modules = self._state.get(f"{self._KEY}.modules") or {}
        return {
            "version": self._state.get(f"{self._KEY}.version") or "unknown",
            "module_count": len(modules),
            "last_mutation": self._state.get(f"{self._KEY}.last_mutation"),
            "created_at": self._state.get(f"{self._KEY}.created_at"),
        }
