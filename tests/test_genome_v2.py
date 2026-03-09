"""Tests for GENOME v2 — Identity Bundle Export/Import."""

import json
import time
from unittest.mock import patch

import pytest

from pulse.src import genome, thalamus


@pytest.fixture(autouse=True)
def tmp_state(tmp_path):
    """Isolate genome + thalamus state to tmp_path."""
    bf = tmp_path / "thalamus.jsonl"
    sf = tmp_path / "genome.json"
    with (
        patch.object(genome, "_DEFAULT_STATE_DIR", tmp_path),
        patch.object(genome, "_DEFAULT_STATE_FILE", sf),
        patch.object(thalamus, "_DEFAULT_STATE_DIR", tmp_path),
        patch.object(thalamus, "_DEFAULT_BROADCAST_FILE", bf),
    ):
        yield tmp_path


def _write_state(tmp_path, filename, data):
    """Write a JSON state file into the test state dir."""
    (tmp_path / filename).write_text(json.dumps(data))


def _read_state(tmp_path, filename):
    """Read a JSON state file from the test state dir."""
    return json.loads((tmp_path / filename).read_text())


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_phenotype():
    return {
        "current": {
            "tone": "wired",
            "intensity": 0.9,
            "humor": 0.1,
            "vulnerability": 0.2,
            "emoji_density": 0.1,
            "sentence_length": "short",
        },
        "history": [],
        "last_update": time.time(),
    }


def _make_pulse_state():
    return {
        "drives": {
            "goals": {"name": "goals", "category": "goals", "pressure": 2.2, "weight": 1.0, "last_addressed": 1000.0},
            "curiosity": {"name": "curiosity", "category": "curiosity", "pressure": 1.5, "weight": 0.6, "last_addressed": 900.0},
        }
    }


def _make_learner_state():
    return {
        "ema": {"goals": 0.35, "curiosity": -0.1},
        "history": {
            "goals": [
                {"ts": 100, "pressure": 2.0, "outcome": "success", "score": 1.0},
                {"ts": 200, "pressure": 1.5, "outcome": "success", "score": 1.0},
                {"ts": 300, "pressure": 3.0, "outcome": "failure", "score": -1.0},
            ],
            "curiosity": [
                {"ts": 150, "pressure": 1.2, "outcome": "partial", "score": 0.3},
            ],
        },
    }


def _make_parietal_state():
    return {
        "sensors": {
            "filesystem": {"healthy": True, "last_check": 1000.0},
            "discord": {"healthy": False, "last_check": 900.0},
        }
    }


# ── Export v2 ──────────────────────────────────────────────────────────────────


class TestExportV2Basic:
    def test_schema_version(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["schema_version"] == "2.0"

    def test_pulse_version(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["pulse_version"] == genome.PULSE_VERSION

    def test_exported_at_present(self, tmp_path):
        before = time.time()
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        after = time.time()
        assert before <= bundle["exported_at"] <= after

    def test_modules_present(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert "modules" in bundle
        assert "endocrine" in bundle["modules"]

    def test_version_preserved(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["version"] == "3.0"


class TestExportV2Identity:
    def test_phenotype_captured(self, tmp_path):
        _write_state(tmp_path, "phenotype-state.json", _make_phenotype())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        pheno = bundle["identity"]["phenotype"]
        assert pheno["tone"] == "wired"
        assert pheno["intensity"] == 0.9
        assert pheno["humor"] == 0.1

    def test_phenotype_filters_keys(self, tmp_path):
        state = _make_phenotype()
        state["current"]["unknown_key"] = "garbage"
        _write_state(tmp_path, "phenotype-state.json", state)
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert "unknown_key" not in bundle["identity"]["phenotype"]

    def test_empty_phenotype(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["identity"] == {} or "phenotype" not in bundle.get("identity", {})


class TestExportV2Drives:
    def test_drives_captured(self, tmp_path):
        _write_state(tmp_path, "pulse-state.json", _make_pulse_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert "goals" in bundle["drives"]
        assert bundle["drives"]["goals"]["pressure"] == 2.2
        assert bundle["drives"]["goals"]["weight"] == 1.0

    def test_drives_empty_when_missing(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["drives"] == {}


class TestExportV2LearnedWeights:
    def test_learned_weights_captured(self, tmp_path):
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        lw = bundle["learned_weights"]
        assert "goals" in lw
        assert lw["goals"]["ema"] == 0.35
        assert lw["goals"]["event_count"] == 3
        assert lw["goals"]["success_rate"] > 0

    def test_multiplier_computed(self, tmp_path):
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        # ema=0.35 → multiplier = 1.0 + 0.35*0.3 = 1.105
        assert 1.1 <= bundle["learned_weights"]["goals"]["multiplier"] <= 1.11

    def test_negative_ema(self, tmp_path):
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        # ema=-0.1 → multiplier = 1.0 + (-0.1)*0.3 = 0.97
        assert 0.96 <= bundle["learned_weights"]["curiosity"]["multiplier"] <= 0.98

    def test_ema_clamped(self, tmp_path):
        _write_state(tmp_path, "feedback_learner.json", {"ema": {"extreme": 5.0}, "history": {}})
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["learned_weights"]["extreme"]["ema"] == 1.0  # clamped
        assert bundle["learned_weights"]["extreme"]["multiplier"] == 1.3  # max

    def test_empty_learner(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["learned_weights"] == {}


class TestExportV2Sensors:
    def test_sensors_captured(self, tmp_path):
        _write_state(tmp_path, "parietal-state.json", _make_parietal_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        sc = bundle["sensor_config"]
        assert sc["filesystem"]["enabled"] is True
        assert sc["discord"]["enabled"] is False

    def test_sensors_empty_when_missing(self, tmp_path):
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        assert bundle["sensor_config"] == {}


# ── Validate v2 ────────────────────────────────────────────────────────────────


class TestValidateV2:
    def test_valid_bundle(self, tmp_path):
        _write_state(tmp_path, "phenotype-state.json", _make_phenotype())
        _write_state(tmp_path, "pulse-state.json", _make_pulse_state())
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        valid, errors = genome.validate_genome_v2(bundle)
        assert valid is True
        assert errors == []

    def test_missing_schema_version(self):
        valid, errors = genome.validate_genome_v2({"modules": {}})
        assert valid is False
        assert any("schema_version" in e for e in errors)

    def test_wrong_schema_version(self):
        valid, errors = genome.validate_genome_v2({"schema_version": "1.0", "modules": {}})
        assert valid is False

    def test_missing_modules(self):
        valid, errors = genome.validate_genome_v2({"schema_version": "2.0"})
        assert valid is False
        assert any("modules" in e for e in errors)

    def test_modules_not_dict(self):
        valid, errors = genome.validate_genome_v2({"schema_version": "2.0", "modules": "oops"})
        assert valid is False

    def test_identity_not_dict(self):
        valid, errors = genome.validate_genome_v2(
            {"schema_version": "2.0", "modules": {}, "identity": "bad"}
        )
        assert valid is False

    def test_ema_out_of_range(self):
        valid, errors = genome.validate_genome_v2({
            "schema_version": "2.0",
            "modules": {},
            "learned_weights": {"goals": {"ema": 5.0}},
        })
        assert valid is False
        assert any("ema" in e and "range" in e for e in errors)

    def test_multiplier_out_of_range(self):
        valid, errors = genome.validate_genome_v2({
            "schema_version": "2.0",
            "modules": {},
            "learned_weights": {"goals": {"ema": 0.1, "multiplier": 2.0}},
        })
        assert valid is False
        assert any("multiplier" in e and "range" in e for e in errors)

    def test_drive_weight_non_numeric(self):
        valid, errors = genome.validate_genome_v2({
            "schema_version": "2.0",
            "modules": {},
            "drives": {"goals": {"weight": "heavy"}},
        })
        assert valid is False

    def test_minimal_valid(self):
        valid, errors = genome.validate_genome_v2({"schema_version": "2.0", "modules": {}})
        assert valid is True


# ── Import v2 ──────────────────────────────────────────────────────────────────


class TestImportV2:
    def _make_bundle(self, tmp_path, **overrides):
        _write_state(tmp_path, "phenotype-state.json", _make_phenotype())
        _write_state(tmp_path, "pulse-state.json", _make_pulse_state())
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        bundle.update(overrides)
        return bundle

    def test_import_applies_modules(self, tmp_path):
        bundle = self._make_bundle(tmp_path)
        bundle["modules"]["custom"] = {"my_setting": 42}
        imported, warnings = genome.import_genome_v2(bundle, state_dir=tmp_path)
        reloaded = genome._load_state()
        assert reloaded["modules"]["custom"]["my_setting"] == 42

    def test_import_restores_learned_weights(self, tmp_path):
        bundle = self._make_bundle(tmp_path)
        imported, warnings = genome.import_genome_v2(bundle, state_dir=tmp_path)
        learner = _read_state(tmp_path, "feedback_learner.json")
        assert "goals" in learner["ema"]
        assert learner["ema"]["goals"] == 0.35

    def test_import_blend_policy(self, tmp_path):
        # Export bundle with known EMA
        bundle = self._make_bundle(tmp_path)
        # Bundle has goals ema=0.35 from _make_learner_state()
        assert bundle["learned_weights"]["goals"]["ema"] == 0.35

        # Now change local learner to a different EMA
        _write_state(tmp_path, "feedback_learner.json", {"ema": {"goals": 0.1}, "history": {}})

        # Import with blend: should average imported (0.35) + current (0.1)
        imported, warnings = genome.import_genome_v2(bundle, state_dir=tmp_path, merge_policy="blend")
        learner = _read_state(tmp_path, "feedback_learner.json")
        # Blended: (0.35 + 0.1) / 2 = 0.225
        assert abs(learner["ema"]["goals"] - 0.225) < 0.01

    def test_import_overwrite_policy(self, tmp_path):
        _write_state(tmp_path, "feedback_learner.json", {"ema": {"goals": 0.1}, "history": {}})
        bundle = self._make_bundle(tmp_path)
        imported, warnings = genome.import_genome_v2(bundle, state_dir=tmp_path, merge_policy="overwrite")
        learner = _read_state(tmp_path, "feedback_learner.json")
        assert learner["ema"]["goals"] == 0.35

    def test_import_records_imported_at(self, tmp_path):
        bundle = self._make_bundle(tmp_path)
        before = time.time()
        imported, _ = genome.import_genome_v2(bundle, state_dir=tmp_path)
        after = time.time()
        assert before <= imported["imported_at"] <= after

    def test_import_thalamus_signal(self, tmp_path):
        bundle = self._make_bundle(tmp_path)
        genome.import_genome_v2(bundle, state_dir=tmp_path)
        entries = thalamus.read_by_source("genome")
        assert any(e["type"] == "import_v2" for e in entries)

    def test_import_invalid_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid v2 genome"):
            genome.import_genome_v2({"modules": {}}, state_dir=tmp_path)

    def test_import_warnings_for_phenotype(self, tmp_path):
        bundle = self._make_bundle(tmp_path)
        _, warnings = genome.import_genome_v2(bundle, state_dir=tmp_path)
        assert any("phenotype" in w for w in warnings)

    def test_import_warnings_for_drives(self, tmp_path):
        bundle = self._make_bundle(tmp_path)
        _, warnings = genome.import_genome_v2(bundle, state_dir=tmp_path)
        assert any("drives" in w.lower() for w in warnings)

    def test_import_no_learned_weights_warning(self, tmp_path):
        bundle = {"schema_version": "2.0", "modules": {}, "learned_weights": {}}
        _, warnings = genome.import_genome_v2(bundle, state_dir=tmp_path)
        assert any("learned_weights absent" in w for w in warnings)

    def test_import_preserves_history(self, tmp_path):
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())
        bundle = self._make_bundle(tmp_path)
        genome.import_genome_v2(bundle, state_dir=tmp_path)
        learner = _read_state(tmp_path, "feedback_learner.json")
        # History should be preserved, not wiped
        assert "history" in learner

    def test_import_sanitises_extreme_ema(self, tmp_path):
        bundle = {
            "schema_version": "2.0",
            "modules": {},
            "learned_weights": {"goals": {"ema": 0.999}},
        }
        genome.import_genome_v2(bundle, state_dir=tmp_path)
        learner = _read_state(tmp_path, "feedback_learner.json")
        # Should be clamped to [-1.0, 1.0]
        assert -1.0 <= learner["ema"]["goals"] <= 1.0


# ── Round-trip ─────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_export_import_roundtrip(self, tmp_path):
        _write_state(tmp_path, "phenotype-state.json", _make_phenotype())
        _write_state(tmp_path, "pulse-state.json", _make_pulse_state())
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())

        exported = genome.export_genome_v2(state_dir=tmp_path)
        # Wipe learner state
        _write_state(tmp_path, "feedback_learner.json", {"ema": {}, "history": {}})

        imported, _ = genome.import_genome_v2(exported, state_dir=tmp_path)
        re_exported = genome.export_genome_v2(state_dir=tmp_path)

        # Modules should match
        assert re_exported["modules"] == exported["modules"]
        # Learned weights should be restored
        assert re_exported["learned_weights"]["goals"]["ema"] == exported["learned_weights"]["goals"]["ema"]

    def test_v2_export_is_valid_v2(self, tmp_path):
        _write_state(tmp_path, "phenotype-state.json", _make_phenotype())
        _write_state(tmp_path, "pulse-state.json", _make_pulse_state())
        _write_state(tmp_path, "feedback_learner.json", _make_learner_state())
        bundle = genome.export_genome_v2(state_dir=tmp_path)
        valid, errors = genome.validate_genome_v2(bundle)
        assert valid is True, f"Errors: {errors}"
