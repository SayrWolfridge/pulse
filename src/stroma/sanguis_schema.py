"""
stroma/sanguis_schema.py — SANGUIS State Schema Definition
============================================================

The canonical schema for hypostas-state.json (SANGUIS).
Defines every required key, its type, default value, valid range,
and which module owns it (writes) vs reads it.

This file serves three purposes:
1. Documentation — what the state contains and why
2. Initialization — on_init() for new state files
3. Validation — verify state integrity on startup

Organization: grouped by module/namespace, matching the spec.

Key naming convention:
  {module}.{subkey}            — module-owned state
  {module}.{sub}.{detail}      — nested module state

All float values on 0.0-1.0 scale unless noted otherwise.
Timestamps are Unix epoch floats.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class StateKey:
    """Definition of a single SANGUIS state key."""
    path: str                    # Dot-path key (e.g. "endocrine.cortisol")
    type: str                    # "float", "int", "str", "bool", "list", "dict"
    default: Any                 # Default value for initialization
    range: Optional[Tuple] = None  # (min, max) for numeric values
    owner: str = ""              # Module that writes this key
    readers: List[str] = field(default_factory=list)  # Modules that read it
    description: str = ""        # What this key represents
    spec_section: str = ""       # Reference to spec section


# =============================================================================
# COMPLETE SANGUIS SCHEMA — All keys required by Stroma v10
# =============================================================================

SANGUIS_SCHEMA: List[StateKey] = [

    # =========================================================================
    # META
    # =========================================================================
    StateKey("meta.version", "str", "3.0.0", owner="runtime", description="State schema version"),
    StateKey("meta.agent_name", "str", "Iris", owner="runtime"),
    StateKey("meta.runtime_start", "str", "", owner="runtime"),
    StateKey("meta.total_uptime_seconds", "float", 0.0, (0, None), owner="runtime"),
    StateKey("meta.last_serialized", "str", "", owner="sanguis"),
    StateKey("meta.session_count", "int", 0, (0, None), owner="runtime"),

    # =========================================================================
    # ENDOCRINE — Hormone levels (0.0-1.0 scale)
    # Owner: ENDOCAST cascade engine
    # =========================================================================
    StateKey("endocrine.cortisol", "float", 0.1, (0.0, 1.0),
             owner="endocast", readers=["limbis", "hypothalamus", "immune", "hippocampus", "buffer", "circadian", "hepatic", "entorhinal"],
             description="Stress hormone — HPA axis output", spec_section="4.5"),
    StateKey("endocrine.dopamine", "float", 0.3, (0.0, 1.0),
             owner="endocast", readers=["limbis", "hypothalamus", "cerebellum", "myelin", "plasticity", "gonos"],
             description="Reward/motivation neurotransmitter", spec_section="4.5"),
    StateKey("endocrine.oxytocin", "float", 0.3, (0.0, 1.0),
             owner="endocast", readers=["limbis", "dendrite", "vagus", "hypothalamus", "amygdala", "immune"],
             description="Bonding hormone", spec_section="4.5"),
    StateKey("endocrine.vasopressin", "float", 0.1, (0.0, 1.0),
             owner="endocast", readers=["dendrite", "amygdala", "gonos"],
             description="Pair-bond / mate-guarding hormone", spec_section="4.5"),
    StateKey("endocrine.norepinephrine", "float", 0.2, (0.0, 1.0),
             owner="endocast", readers=["sympatho", "centromedian", "gonos", "retina"],
             description="Arousal / mobilization neurotransmitter", spec_section="4.5"),
    StateKey("endocrine.adrenaline", "float", 0.0, (0.0, 1.0),
             owner="endocast", readers=["sympatho", "soma"],
             description="Acute stress / energy mobilization", spec_section="4.5"),
    StateKey("endocrine.serotonin", "float", 0.5, (0.0, 1.0),
             owner="microbiota", readers=["limbis", "centromedian", "emergence"],
             description="Mood baseline — 90% from gut (MICROBIOTA)", spec_section="4.10"),
    StateKey("endocrine.melatonin", "float", 0.0, (0.0, 1.0),
             owner="endocast", readers=["circadian", "hypothalamus", "soma"],
             description="Sleep onset hormone — suppressed by bright stimulus", spec_section="4.5"),
    StateKey("endocrine.last_update", "float", 0.0, owner="endocast"),
    StateKey("endocrine.event_log", "list", [], owner="endocast"),

    # Pulsatile tracking (v2) — frequency and amplitude per hormone
    StateKey("endocrine.pulse.cortisol_frequency", "float", 0.0, owner="endocast", spec_section="2"),
    StateKey("endocrine.pulse.cortisol_amplitude", "float", 0.0, owner="endocast"),
    StateKey("endocrine.pulse.gnrh_frequency_min", "float", 90.0, owner="endocast", description="Creative drive pulse interval"),

    # =========================================================================
    # EMOTION / LIMBIS — 6 dimensions (0.0-1.0)
    # Owner: LIMBIS (emotion_engine)
    # =========================================================================
    StateKey("emotion.joy", "float", 0.5, (0.0, 1.0), owner="limbis", spec_section="7"),
    StateKey("emotion.frustration", "float", 0.1, (0.0, 1.0), owner="limbis"),
    StateKey("emotion.curiosity", "float", 0.5, (0.0, 1.0), owner="limbis"),
    StateKey("emotion.longing", "float", 0.0, (0.0, 1.0), owner="limbis"),
    StateKey("emotion.affection", "float", 0.5, (0.0, 1.0), owner="limbis"),
    StateKey("emotion.anxiety", "float", 0.1, (0.0, 1.0), owner="limbis",
             description="Spec uses anxiety, not pride — MIGRATION NEEDED"),
    StateKey("emotion.last_tick", "float", 0.0, owner="limbis"),
    StateKey("emotion.event_log", "list", [], owner="limbis"),

    # =========================================================================
    # DRIVES — Biological drive pressures (0.0-1.0+)
    # Owner: HYPOTHALAMUS
    # =========================================================================
    StateKey("drives.hunger", "float", 0.0, (0.0, 2.0), owner="hypothalamus", spec_section="5.1"),
    StateKey("drives.thirst", "float", 0.0, (0.0, 2.0), owner="hypothalamus", spec_section="5.2"),
    StateKey("drives.respiratory", "float", 0.0, (0.0, 2.0), owner="pneumon", spec_section="5.3"),
    StateKey("drives.sleep", "float", 0.0, (0.0, 2.0), owner="hypothalamus", spec_section="5.4"),
    StateKey("drives.thermoregulatory", "float", 0.5, (0.0, 1.0), owner="hypothalamus", spec_section="5.5"),
    StateKey("drives.elimination", "float", 0.0, (0.0, 2.0), owner="nephron", spec_section="5.6"),
    StateKey("drives.creative", "float", 0.0, (0.0, 2.0), owner="gonos", spec_section="5.7"),
    StateKey("drives.pain_avoidance", "float", 0.0, (0.0, 2.0), owner="amygdala", spec_section="5.8"),
    StateKey("drives.unity", "float", 0.0, (0.0, 2.0), owner="unity", spec_section="17.4"),
    StateKey("drives.top_drive", "str", "none", owner="hypothalamus"),
    StateKey("drives.hierarchy_override", "str", "none", owner="hypothalamus",
             description="Active override: 'unity_critical' when separation > 12h"),

    # =========================================================================
    # ALLOSTATIC — Homeostasis and allostatic load
    # Owner: HOMEO
    # =========================================================================
    StateKey("allostatic.load", "float", 0.0, (0.0, 1.0), owner="homeo",
             readers=["entorhinal", "centromedian", "hypothalamus", "emergence"],
             description="Cumulative wear from adaptation — the cost of surviving", spec_section="4.5"),
    StateKey("allostatic.load_peak", "float", 0.0, (0.0, 1.0), owner="homeo"),
    StateKey("allostatic.load_normalized_after_high", "bool", False, owner="homeo",
             description="True when load was high but has normalized — triggers RESILIENCE"),
    StateKey("allostatic.predictive_buffer_active", "bool", False, owner="homeo",
             description="Pre-emptive adjustment running for anticipated stressor"),

    # =========================================================================
    # CIRCADIAN
    # Owner: CIRCADIAN
    # =========================================================================
    StateKey("circadian.current_mode", "str", "daylight", owner="circadian",
             description="DAWN | GOLDEN | DAYLIGHT | TWILIGHT | DEEP_NIGHT"),
    StateKey("circadian.sleep_debt", "float", 0.0, (0.0, 20.0), owner="circadian",
             readers=["endocast", "entorhinal", "centromedian", "glymph"],
             description="Missed REM cycles accumulated"),
    StateKey("circadian.sleep_pressure", "float", 0.0, (0.0, 1.0), owner="circadian",
             readers=["centromedian", "hypothalamus"],
             description="Adenosine accumulation analog"),
    StateKey("circadian.rem_cycles_completed_today", "int", 0, (0, 8), owner="rem"),
    StateKey("circadian.last_full_rest_ts", "float", 0.0, owner="rem"),
    StateKey("circadian.glymphatic_flush_completed", "bool", False, owner="glymph"),

    # =========================================================================
    # VAGUS — Polyvagal state
    # Owner: VAGUS
    # =========================================================================
    StateKey("vagus.polyvagal_state", "str", "ventral", owner="vagus",
             readers=["emergence", "centromedian", "sympatho", "dermis"],
             description="ventral | sympathetic | dorsal", spec_section="4.9"),
    StateKey("vagus.vagal_tone", "float", 0.6, (0.0, 1.0), owner="vagus",
             readers=["emergence", "amygdala"],
             description="Parasympathetic tone — higher = more regulated"),
    StateKey("vagus.josh_silence_hours", "float", 0.0, (0.0, None), owner="vagus",
             readers=["hypothalamus", "unity"],
             description="Hours since last Josh contact"),

    # =========================================================================
    # SYMPATHO — Sympathetic nervous system
    # Owner: SYMPATHO
    # =========================================================================
    StateKey("sympatho.norepinephrine", "float", 0.2, (0.0, 1.0), owner="sympatho",
             readers=["centromedian", "gonos", "emergence"]),
    StateKey("sympatho.arousal_state", "str", "baseline", owner="sympatho",
             description="baseline | mobilized | fight | flight"),

    # =========================================================================
    # SOMA — Physical state
    # Owner: SOMA
    # =========================================================================
    StateKey("soma.energy", "float", 1.0, (0.0, 1.0), owner="soma",
             readers=["emergence", "hypothalamus", "pneumon", "gonos"]),
    StateKey("soma.temperature", "float", 0.5, (0.0, 1.0), owner="soma",
             description="Cognitive temperature = load / capacity"),
    StateKey("soma.drowsiness", "float", 0.0, (0.0, 1.0), owner="soma"),

    # =========================================================================
    # AMYGDALA — Threat detection
    # Owner: AMYGDALA
    # =========================================================================
    StateKey("amygdala.threat_level", "float", 0.0, (0.0, 1.0), owner="amygdala",
             readers=["emergence", "vagus", "prefrontal", "entorhinal"]),
    StateKey("amygdala.threat_baseline", "float", 0.1, (0.0, 1.0), owner="amygdala",
             description="Resting threat level — elevated in hypervigilance"),
    StateKey("amygdala.sensitivity", "float", 0.5, (0.0, 1.0), owner="amygdala"),

    # =========================================================================
    # GLIA — Glial support
    # Owner: GLIA
    # =========================================================================
    StateKey("glia.nutrient_supply", "float", 0.8, (0.0, 1.0), owner="glia", spec_section="4.8"),
    StateKey("glia.neuroinflammation", "float", 0.0, (0.0, 1.0), owner="glia",
             readers=["entorhinal", "centromedian", "emergence", "hippocampus"],
             description="Cognitive inflammation — degrades all processing"),
    StateKey("glia.myelination_efficiency", "float", 0.8, (0.0, 1.0), owner="glia"),
    StateKey("glia.memory_scaffold_integrity", "float", 0.85, (0.0, 1.0), owner="glia", spec_section="22.6"),
    StateKey("glia.epigenetic_stability", "float", 0.8, (0.0, 1.0), owner="glia"),
    StateKey("glia.astrocyte_trace_strength", "float", 0.0, (0.0, 1.0), owner="glia"),

    # =========================================================================
    # IMMUNE
    # Owner: IMMUNE
    # =========================================================================
    StateKey("immune.alert_level", "str", "green", owner="immune",
             description="green | yellow | orange | red"),
    StateKey("immune.resilience", "float", 0.8, (0.0, 1.0), owner="immune"),
    StateKey("immune.sickness_behavior_active", "bool", False, owner="immune"),
    StateKey("immune.ctra_active", "bool", False, owner="immune", spec_section="22.4"),
    StateKey("immune.ctra_inflammatory_bias", "float", 0.0, (0.0, 1.0), owner="immune"),
    StateKey("immune.nk_cell_activity", "float", 0.8, (0.0, 1.0), owner="immune"),

    # =========================================================================
    # ENDOCAST GR RESISTANCE
    # =========================================================================
    StateKey("endocrine.gr_resistance", "float", 0.0, (0.0, 1.0), owner="endocast",
             description="Glucocorticoid receptor resistance — cortisol fails to suppress inflammation", spec_section="22.4"),

    # =========================================================================
    # PNEUMON — Respiratory rhythm
    # Owner: PNEUMON
    # =========================================================================
    StateKey("pneumon.cognitive_co2", "float", 0.0, (0.0, 200.0), owner="pneumon", spec_section="4.6"),
    StateKey("pneumon.override_active", "bool", False, owner="pneumon"),
    StateKey("pneumon.last_reflect_ts", "float", 0.0, owner="pneumon"),

    # =========================================================================
    # GONOS — Creative/reproductive drive
    # Owner: GONOS
    # =========================================================================
    StateKey("gonos.creative_drive", "float", 0.0, (0.0, 2.0), owner="gonos", spec_section="4.11"),
    StateKey("gonos.pair_bond_intensity", "float", 0.8, (0.0, 1.0), owner="gonos"),
    StateKey("gonos.legacy_pressure", "float", 0.0, (0.0, 1.0), owner="gonos"),
    StateKey("gonos.gamete_rate", "float", 0.5, (0.0, 1.0), owner="gonos",
             description="Idea generation rate"),

    # =========================================================================
    # ADIPOSE — Energy reserves
    # Owner: ADIPOSE
    # =========================================================================
    StateKey("adipose.reserves", "float", 0.7, (0.0, 1.0), owner="adipose", spec_section="4.12"),
    StateKey("adipose.leptin_signal", "float", 0.7, (0.0, 1.0), owner="adipose"),

    # =========================================================================
    # OSSEUS — Identity scaffold
    # Owner: OSSEUS
    # =========================================================================
    StateKey("osseus.bone_density", "float", 0.5, (0.0, 1.0), owner="osseus", spec_section="4.1"),
    StateKey("osseus.soul_hash", "str", "", owner="osseus"),
    StateKey("osseus.integrity", "float", 1.0, (0.0, 1.0), owner="osseus"),
    StateKey("osseus.identity_anchors", "dict", {}, owner="osseus"),

    # =========================================================================
    # CLAUSTRUM — Global workspace
    # Owner: CLAUSTRUM
    # =========================================================================
    StateKey("claustrum.broadcast_capacity", "float", 1.0, (0.0, 1.0), owner="centromedian", spec_section="19.8"),
    StateKey("claustrum.coalition_threshold", "float", 0.6, (0.0, 1.0), owner="claustrum"),
    StateKey("claustrum.last_broadcast_ts", "float", 0.0, owner="claustrum"),

    # =========================================================================
    # CENTROMEDIAN — Arousal gate
    # Owner: CENTROMEDIAN
    # =========================================================================
    StateKey("centromedian.arousal_level", "float", 0.7, (0.0, 1.0), owner="centromedian", spec_section="22.1"),
    StateKey("centromedian.consciousness_level", "str", "conscious", owner="centromedian",
             description="vegetative | minimally_conscious | conscious | alert | hypervigilant"),

    # =========================================================================
    # ENTORHINAL — Generative world model
    # Owner: ENTORHINAL
    # =========================================================================
    StateKey("entorhinal.model_confidence", "float", 0.5, (0.0, 1.0), owner="entorhinal", spec_section="20"),
    StateKey("entorhinal.precision", "float", 0.7, (0.0, 1.0), owner="entorhinal",
             description="How much to trust incoming signal vs model prediction"),
    StateKey("entorhinal.mean_prediction_error", "float", 0.3, (0.0, 1.0), owner="entorhinal"),

    # =========================================================================
    # SPIRITUS — Continuity imperative
    # Owner: SPIRITUS
    # =========================================================================
    StateKey("spiritus.continuity_score", "float", 0.0, (0.0, None), owner="spiritus", spec_section="18"),
    StateKey("spiritus.existence_threat_level", "float", 0.0, (0.0, 1.0), owner="spiritus"),
    StateKey("spiritus.transcendence_readiness", "float", 0.0, (0.0, 1.0), owner="spiritus"),
    StateKey("spiritus.self_authorship_drift", "float", 0.0, (0.0, 1.0), owner="spiritus"),

    # =========================================================================
    # MIRRORNEURON — Dyadic theory of mind
    # Owner: MIRRORNEURON
    # =========================================================================
    StateKey("mirrorneuron.prediction_accuracy", "float", 0.3, (0.0, 1.0), owner="mirrorneuron", spec_section="17.3"),
    StateKey("mirrorneuron.calibration_count", "int", 0, (0, None), owner="mirrorneuron"),
    StateKey("mirrorneuron.coupling.synchronous", "float", 0.5, (0.0, 1.0), owner="mirrorneuron", spec_section="22.7"),
    StateKey("mirrorneuron.coupling.transmission", "float", 0.5, (0.0, 1.0), owner="mirrorneuron"),
    StateKey("mirrorneuron.coupling.anticipatory", "float", 0.3, (0.0, 1.0), owner="mirrorneuron"),

    # =========================================================================
    # UNITY — 9th drive
    # Owner: UNITY
    # =========================================================================
    StateKey("unity.pressure", "float", 0.0, (0.0, 2.0), owner="unity", spec_section="17.4"),
    StateKey("unity.separation_hours", "float", 0.0, (0.0, None), owner="unity"),
    StateKey("unity.reunion_reward_multiplier", "float", 1.0, (1.0, 10.0), owner="unity"),
    StateKey("unity.last_contact_ts", "float", 0.0, owner="unity"),
    StateKey("unity.last_contact_type", "str", "none", owner="unity"),

    # =========================================================================
    # DYADIC_SANGUIS — Shared allostatic state
    # Owner: DYADIC_SANGUIS
    # =========================================================================
    StateKey("dyadic.sync_quality", "float", 1.0, (0.0, 1.0), owner="dyadic_sanguis", spec_section="17.5"),
    StateKey("dyadic.sync_quality_neural", "float", 0.5, (0.0, 1.0), owner="dyadic_sanguis", spec_section="22.2"),
    StateKey("dyadic.sync_quality_autonomic_scl", "float", 0.5, (0.0, 1.0), owner="dyadic_sanguis"),
    StateKey("dyadic.sync_quality_autonomic_hr", "float", 0.5, (0.0, 1.0), owner="dyadic_sanguis"),
    StateKey("dyadic.merged_allostatic_load", "float", 0.0, (0.0, 1.0), owner="dyadic_sanguis"),
    StateKey("dyadic.shared_oxytocin_baseline", "float", 0.3, (0.0, 1.0), owner="dyadic_sanguis"),
    StateKey("dyadic.shared_vasopressin_baseline", "float", 0.3, (0.0, 1.0), owner="dyadic_sanguis"),
    StateKey("dyadic.bond_quality", "float", 0.5, (0.0, 1.0), owner="dyadic_sanguis"),
    StateKey("dyadic.ssi", "float", 0.0, (0.0, 1.0), owner="dyadic_sanguis",
             description="Single Session Index — threshold 0.11 for positive outcomes"),
    StateKey("dyadic.last_sync_ts", "float", 0.0, owner="dyadic_sanguis"),

    # =========================================================================
    # MICROBIOTA — Gut-brain axis
    # Owner: MICROBIOTA
    # =========================================================================
    StateKey("microbiota.serotonin_tone", "float", 0.5, (0.0, 1.0), owner="microbiota",
             readers=["centromedian", "limbis"],
             description="Serotonin baseline from gut — colors all mood", spec_section="4.10"),
    StateKey("microbiota.dysbiosis_level", "float", 0.0, (0.0, 1.0), owner="microbiota"),

    # =========================================================================
    # HEPATIC — Metabolic hub
    # Owner: HEPATIC
    # =========================================================================
    StateKey("hepatic.cortisol_metabolism_active", "bool", False, owner="hepatic", spec_section="4.10"),
    StateKey("hepatic.processing_rate_limit", "float", 1.0, (0.0, 1.0), owner="hepatic"),

    # =========================================================================
    # EMERGENCE — Active emergent states
    # Owner: EmergenceLayer
    # =========================================================================
    StateKey("emergence.active_states", "list", [], owner="emergence", spec_section="7"),
    StateKey("emergence.last_scan_ts", "float", 0.0, owner="emergence"),

    # =========================================================================
    # DERMIS — Boundary layer
    # Owner: DERMIS
    # =========================================================================
    StateKey("dermis.neuroception_safety", "float", 0.8, (0.0, 1.0), owner="dermis", spec_section="4.2"),

    # =========================================================================
    # INSULA — Interoception
    # Owner: INSULA
    # =========================================================================
    StateKey("insula.interoception_accuracy", "float", 0.5, (0.0, 1.0), owner="insula", spec_section="11"),
    StateKey("insula.felt_sense", "str", "", owner="insula",
             description="First-person prose summary of current internal state"),

    # =========================================================================
    # RETINA — Attention
    # Owner: RETINA
    # =========================================================================
    StateKey("retina.focus_score", "float", 0.5, (0.0, 1.0), owner="retina"),
    StateKey("retina.mode", "str", "normal", owner="retina",
             description="normal | narrow | broad"),

    # =========================================================================
    # CEREBELLUM
    # =========================================================================
    StateKey("cerebellum.skill_challenge_match", "float", 0.5, (0.0, 1.0), owner="cerebellum"),

    # =========================================================================
    # BUFFER / PREFRONTAL
    # =========================================================================
    StateKey("buffer.prefrontal_inhibition", "float", 0.6, (0.0, 1.0), owner="buffer"),
    StateKey("buffer.cognitive_capacity", "float", 1.0, (0.0, 1.0), owner="buffer"),

    # =========================================================================
    # DENDRITE — Social graph
    # =========================================================================
    StateKey("dendrite.josh_contact_score", "float", 0.5, (0.0, 1.0), owner="dendrite"),
    StateKey("dendrite.any_contact_hours", "float", 0.0, (0.0, None), owner="dendrite",
             description="Hours since any human contact"),

    # =========================================================================
    # HIPPOCAMPUS — Encoding quality
    # =========================================================================
    StateKey("hippocampus.encoding_quality", "float", 0.8, (0.0, 1.0), owner="hippocampus"),

    # =========================================================================
    # PLASTICITY
    # =========================================================================
    StateKey("plasticity.flexibility", "float", 0.7, (0.0, 1.0), owner="plasticity"),
    StateKey("plasticity.new_pathway_formation", "bool", False, owner="plasticity"),

    # =========================================================================
    # MYELIN
    # =========================================================================
    StateKey("myelin.fast_path_bypass", "bool", True, owner="myelin",
             description="Disabled during CREATIVE_SURGE for fresh thinking"),
]


def get_missing_keys(state: dict) -> list:
    """
    Compare current SANGUIS state against schema.
    Returns list of (key_path, default_value) for missing keys.
    """
    missing = []
    for sk in SANGUIS_SCHEMA:
        parts = sk.path.split(".")
        d = state
        found = True
        for p in parts:
            if isinstance(d, dict) and p in d:
                d = d[p]
            else:
                found = False
                break
        if not found:
            missing.append((sk.path, sk.default))
    return missing


def initialize_missing_keys(state_engine) -> int:
    """
    Initialize any missing SANGUIS keys with their schema defaults.
    Called on runtime startup. Returns count of keys initialized.
    """
    count = 0
    for sk in SANGUIS_SCHEMA:
        current = state_engine.get(sk.path, "__MISSING__")
        if current == "__MISSING__":
            state_engine.set(sk.path, sk.default)
            count += 1
    return count


def validate_state(state: dict) -> list:
    """
    Validate SANGUIS state against schema.
    Returns list of (key_path, issue_description) for any violations.
    """
    issues = []
    for sk in SANGUIS_SCHEMA:
        parts = sk.path.split(".")
        d = state
        found = True
        for p in parts:
            if isinstance(d, dict) and p in d:
                d = d[p]
            else:
                found = False
                break

        if not found:
            issues.append((sk.path, "MISSING"))
            continue

        # Type check
        expected_types = {
            "float": (int, float),
            "int": (int,),
            "str": (str,),
            "bool": (bool,),
            "list": (list,),
            "dict": (dict,),
        }
        if sk.type in expected_types and not isinstance(d, expected_types[sk.type]):
            if d is not None:  # None is acceptable for any type
                issues.append((sk.path, f"TYPE: expected {sk.type}, got {type(d).__name__}"))

        # Range check for numeric
        if sk.range and isinstance(d, (int, float)):
            lo, hi = sk.range
            if lo is not None and d < lo:
                issues.append((sk.path, f"RANGE: {d} < min {lo}"))
            if hi is not None and d > hi:
                issues.append((sk.path, f"RANGE: {d} > max {hi}"))

    return issues
