"""GENOME — Exportable DNA Config for Pulse.

All module settings/thresholds/weights in one exportable config.
Mutatable by PLASTICITY. Import/export for cloning.

Schema versions
---------------
  v1 (legacy): modules + version only.
  v2 (current): full identity bundle — modules, identity/phenotype, drives
                snapshot, learned_weights, sensor_config.
                Discriminated by ``schema_version: "2.0"``.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pulse.src import thalamus

_DEFAULT_STATE_DIR = Path.home() / ".pulse" / "state"
_DEFAULT_STATE_FILE = _DEFAULT_STATE_DIR / "genome.json"

SCHEMA_VERSION_V2 = "2.0"
PULSE_VERSION = "0.5.5"

# EMA bounds for import sanitisation
_EMA_MIN, _EMA_MAX = -1.0, 1.0
_MULTIPLIER_MIN, _MULTIPLIER_MAX = 0.7, 1.3

# Default genome
_DEFAULT_GENOME = {
    "version": "3.0",
    "created_at": 0,
    "modules": {
        "endocrine": {
            "decay_rates": {
                "cortisol": -0.05,
                "dopamine": -0.08,
                "serotonin": -0.02,
                "oxytocin": -0.04,
                "adrenaline": -0.28,
                "melatonin": -0.01,
            },
            "high_threshold": 0.5,
            "low_threshold": 0.3,
        },
        "limbic": {
            "half_life_ms": 14400000,
            "decay_threshold": 0.5,
            "contagion_multiplier": 0.5,
        },
        "retina": {
            "default_threshold": 0.3,
            "focus_threshold": 0.8,
        },
        "circadian": {
            "dawn_hours": [6, 9],
            "daylight_hours": [9, 17],
            "golden_hours": [17, 22],
        },
        "amygdala": {
            "fast_path_threshold": 0.7,
        },
        "phenotype": {
            "default_humor": 0.3,
            "default_intensity": 0.5,
        },
        "telomere": {
            "drift_threshold": 0.3,
        },
        "hypothalamus": {
            "signal_threshold": 3,
            "retirement_days": 30,
            "weight_floor": 0.1,
        },
        "soma": {
            "energy_cost_per_token": 0.001,
            "rem_replenish": 0.5,
        },
        "dendrite": {
            "trust_increment": 0.01,
            "trust_decrement": 0.05,
        },
        "vestibular": {
            "building_shipping_range": [0.3, 0.7],
            "working_reflecting_range": [0.4, 0.8],
        },
    },
}


# ── internal helpers ───────────────────────────────────────────────────────────


def _load_state() -> dict:
    if _DEFAULT_STATE_FILE.exists():
        try:
            return json.loads(_DEFAULT_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    g = dict(_DEFAULT_GENOME)
    g["created_at"] = time.time()
    return g


def _save_state(state: dict):
    _DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _DEFAULT_STATE_FILE.write_text(json.dumps(state, indent=2))


def _read_json_state(filename: str, state_dir: Optional[Path] = None) -> dict:
    """Read a state JSON file from the state dir. Returns {} on any failure."""
    sd = state_dir if state_dir is not None else _DEFAULT_STATE_DIR
    path = sd / filename
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json_state(filename: str, data: dict, state_dir: Optional[Path] = None):
    """Atomically write a state JSON file."""
    sd = state_dir if state_dir is not None else _DEFAULT_STATE_DIR
    sd.mkdir(parents=True, exist_ok=True)
    path = sd / filename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ── v1 API (retained for backward compat) ─────────────────────────────────────


def export_genome() -> dict:
    """Export full genome config (v1 format)."""
    g = _load_state()
    g["exported_at"] = time.time()
    return g


def import_genome(g: dict) -> dict:
    """Import a genome config (v1 format). Returns the imported genome."""
    g["imported_at"] = time.time()
    _save_state(g)

    thalamus.append(
        {
            "source": "genome",
            "type": "import",
            "salience": 0.6,
            "data": {"version": g.get("version", "unknown")},
        }
    )
    return g


def get_module_config(module_name: str) -> Optional[dict]:
    """Get config for a specific module."""
    g = _load_state()
    return g.get("modules", {}).get(module_name)


def mutate(module_name: str, key: str, value) -> dict:
    """Mutate a specific setting. Used by PLASTICITY."""
    g = _load_state()
    if module_name not in g.get("modules", {}):
        g.setdefault("modules", {})[module_name] = {}
    g["modules"][module_name][key] = value
    g["last_mutation"] = {"module": module_name, "key": key, "ts": time.time()}
    _save_state(g)

    thalamus.append(
        {
            "source": "genome",
            "type": "mutation",
            "salience": 0.4,
            "data": {"module": module_name, "key": key},
        }
    )
    return g["modules"][module_name]


def get_status() -> dict:
    """Return genome status."""
    g = _load_state()
    return {
        "version": g.get("version", "unknown"),
        "modules": len(g.get("modules", {})),
        "last_mutation": g.get("last_mutation"),
    }


# ── v2 API ─────────────────────────────────────────────────────────────────────


def export_genome_v2(state_dir: Optional[Path] = None) -> dict:
    """Export a full v2 identity bundle.

    Captures:
      - ``modules``         — all module thresholds/weights (same as v1)
      - ``identity``        — current phenotype snapshot
      - ``drives``          — live drive pressures and weights
      - ``learned_weights`` — RL-lite EMA multipliers from FeedbackLearner
      - ``sensor_config``   — which sensors are currently enabled

    The bundle is self-contained: importing it on a fresh instance reproduces
    the agent's personality calibration, including learned reinforcement.
    """
    # ── base v1 modules ──────────────────────────────────────────────────────
    base = _load_state()
    modules = base.get("modules", dict(_DEFAULT_GENOME["modules"]))

    # ── identity / phenotype ─────────────────────────────────────────────────
    phenotype_state = _read_json_state("phenotype-state.json", state_dir)
    phenotype = phenotype_state.get("current", {})

    identity: Dict = {}
    if phenotype:
        identity["phenotype"] = {
            k: v for k, v in phenotype.items()
            if k in ("tone", "intensity", "humor", "vulnerability", "emoji_density", "sentence_length")
        }

    # ── drives snapshot ──────────────────────────────────────────────────────
    pulse_state = _read_json_state("pulse-state.json", state_dir)
    raw_drives = pulse_state.get("drives", {})
    drives: Dict = {}
    for name, info in raw_drives.items():
        if isinstance(info, dict):
            drives[name] = {
                "pressure": round(float(info.get("pressure", 0.0)), 4),
                "weight": round(float(info.get("weight", 1.0)), 4),
                "last_addressed": info.get("last_addressed"),
            }

    # ── learned weights (RL-lite EMAs) ───────────────────────────────────────
    learner_state = _read_json_state("feedback_learner.json", state_dir)
    raw_ema = learner_state.get("ema", {})
    raw_history = learner_state.get("history", {})
    learned_weights: Dict = {}
    for drive_name, ema_val in raw_ema.items():
        ema = _clamp(float(ema_val), _EMA_MIN, _EMA_MAX)
        multiplier = _clamp(1.0 + ema * 0.30, _MULTIPLIER_MIN, _MULTIPLIER_MAX)
        history = raw_history.get(drive_name, [])
        successes = sum(1 for e in history if e.get("outcome") == "success")
        success_rate = round(successes / len(history), 4) if history else 0.0
        learned_weights[drive_name] = {
            "ema": round(ema, 4),
            "multiplier": round(multiplier, 4),
            "success_rate": success_rate,
            "event_count": len(history),
        }

    # ── sensor config ────────────────────────────────────────────────────────
    # Read from PARIETAL or fall back to minimal structure
    parietal_state = _read_json_state("parietal-state.json", state_dir)
    sensor_config: Dict = {}
    sensor_health = parietal_state.get("sensors", {})
    for sensor_name, health in sensor_health.items():
        if isinstance(health, dict):
            sensor_config[sensor_name] = {
                "enabled": health.get("healthy", True),
                "last_check": health.get("last_check"),
            }

    bundle: Dict = {
        "schema_version": SCHEMA_VERSION_V2,
        "pulse_version": PULSE_VERSION,
        "exported_at": time.time(),
        "identity": identity,
        "drives": drives,
        "learned_weights": learned_weights,
        "sensor_config": sensor_config,
        # v1 compat fields preserved
        "version": base.get("version", "3.0"),
        "modules": modules,
    }
    if base.get("created_at"):
        bundle["created_at"] = base["created_at"]

    return bundle


def validate_genome_v2(g: dict) -> Tuple[bool, List[str]]:
    """Validate a v2 genome bundle.

    Returns ``(is_valid, errors)`` where ``errors`` is a list of human-readable
    problem strings (empty on success).

    Rules
    -----
    - ``schema_version`` must equal ``"2.0"``
    - ``modules`` key must be present and be a dict
    - ``identity`` if present, must be a dict
    - ``learned_weights`` if present: each entry must have ``ema`` in [-1, 1]
      and ``multiplier`` in [0.7, 1.3]
    - ``drives`` if present: each entry must have numeric ``weight``
    """
    errors: List[str] = []

    if g.get("schema_version") != SCHEMA_VERSION_V2:
        errors.append(
            f"schema_version must be '{SCHEMA_VERSION_V2}', "
            f"got: {g.get('schema_version')!r}"
        )

    if "modules" not in g:
        errors.append("missing required key: 'modules'")
    elif not isinstance(g["modules"], dict):
        errors.append("'modules' must be a dict")

    if "identity" in g and not isinstance(g["identity"], dict):
        errors.append("'identity' must be a dict")

    lw = g.get("learned_weights", {})
    if not isinstance(lw, dict):
        errors.append("'learned_weights' must be a dict")
    else:
        for drive_name, entry in lw.items():
            if not isinstance(entry, dict):
                errors.append(f"learned_weights.{drive_name} must be a dict")
                continue
            if "ema" in entry:
                ema = entry["ema"]
                if not isinstance(ema, (int, float)):
                    errors.append(f"learned_weights.{drive_name}.ema must be numeric")
                elif not (_EMA_MIN <= float(ema) <= _EMA_MAX):
                    errors.append(
                        f"learned_weights.{drive_name}.ema out of range "
                        f"[{_EMA_MIN}, {_EMA_MAX}]: {ema}"
                    )
            if "multiplier" in entry:
                mult = entry["multiplier"]
                if not isinstance(mult, (int, float)):
                    errors.append(
                        f"learned_weights.{drive_name}.multiplier must be numeric"
                    )
                elif not (_MULTIPLIER_MIN <= float(mult) <= _MULTIPLIER_MAX):
                    errors.append(
                        f"learned_weights.{drive_name}.multiplier out of range "
                        f"[{_MULTIPLIER_MIN}, {_MULTIPLIER_MAX}]: {mult}"
                    )

    drives = g.get("drives", {})
    if not isinstance(drives, dict):
        errors.append("'drives' must be a dict")
    else:
        for drive_name, entry in drives.items():
            if not isinstance(entry, dict):
                errors.append(f"drives.{drive_name} must be a dict")
                continue
            if "weight" in entry and not isinstance(entry["weight"], (int, float)):
                errors.append(f"drives.{drive_name}.weight must be numeric")

    return len(errors) == 0, errors


def import_genome_v2(
    g: dict,
    state_dir: Optional[Path] = None,
    merge_policy: str = "overwrite",
) -> Tuple[dict, List[str]]:
    """Import a v2 genome bundle.

    Parameters
    ----------
    g : dict
        The genome bundle to import.
    state_dir : Path, optional
        Override state directory (used in tests).
    merge_policy : str
        ``"overwrite"`` — replace all learned EMAs with imported values (default).
        ``"blend"``     — average imported and current EMAs element-wise.

    Returns
    -------
    (imported_genome, warnings) : tuple
        ``warnings`` is a list of non-fatal notices (e.g. skipped fields).

    Raises
    ------
    ValueError
        If ``validate_genome_v2`` returns errors.
    """
    is_valid, errors = validate_genome_v2(g)
    if not is_valid:
        raise ValueError(f"Invalid v2 genome: {'; '.join(errors)}")

    warnings: List[str] = []

    # 1. Apply modules (same as v1)
    base = _load_state()
    base["modules"] = g.get("modules", base.get("modules", {}))
    base["version"] = g.get("version", base.get("version", "3.0"))
    base["imported_at"] = time.time()
    base["imported_schema"] = SCHEMA_VERSION_V2
    _save_state(base)

    # 2. Restore learned weights → feedback_learner.json
    learned_weights = g.get("learned_weights", {})
    if learned_weights:
        current_learner = _read_json_state("feedback_learner.json", state_dir)
        current_ema: Dict = current_learner.get("ema", {})

        new_ema: Dict = {}
        for drive_name, entry in learned_weights.items():
            imported_ema = _clamp(float(entry.get("ema", 0.0)), _EMA_MIN, _EMA_MAX)
            if merge_policy == "blend" and drive_name in current_ema:
                blended = (imported_ema + float(current_ema[drive_name])) / 2.0
                new_ema[drive_name] = round(blended, 4)
            else:
                new_ema[drive_name] = round(imported_ema, 4)

        # Preserve existing history; only update EMA values
        current_learner["ema"] = new_ema
        current_learner["imported_at"] = time.time()
        current_learner["import_policy"] = merge_policy
        _write_json_state("feedback_learner.json", current_learner, state_dir)
    else:
        warnings.append("learned_weights absent — feedback learner state unchanged")

    # 3. Note identity/phenotype (informational — we don't overwrite live phenotype)
    identity = g.get("identity", {})
    if identity.get("phenotype"):
        warnings.append(
            "identity.phenotype noted but not applied to live state "
            "(phenotype is driven by live sensors; restart daemon to rebase)"
        )

    # 4. Note drives snapshot (informational — drives are live state)
    if g.get("drives"):
        warnings.append(
            "drives snapshot noted but not applied to live state "
            "(drive pressures are always live; weights require daemon restart)"
        )

    # 5. THALAMUS signal
    thalamus.append(
        {
            "source": "genome",
            "type": "import_v2",
            "salience": 0.6,
            "data": {
                "schema_version": SCHEMA_VERSION_V2,
                "pulse_version": g.get("pulse_version", "unknown"),
                "learned_drives": list(learned_weights.keys()),
                "merge_policy": merge_policy,
            },
        }
    )

    imported = dict(g)
    imported["imported_at"] = base["imported_at"]
    return imported, warnings
