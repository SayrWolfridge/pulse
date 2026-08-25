"""Tests for state persistence — load, save, atomic writes."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import PulseConfig
from src.state.persistence import StatePersistence


class TestStatePersistence:
    """Test state save/load cycle."""

    def _make_persistence(self, tmpdir: str) -> StatePersistence:
        config = PulseConfig()
        config.state.dir = tmpdir
        return StatePersistence(config)

    def test_fresh_state_loads_empty(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            sp.load()
            assert sp._data == {}

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            sp.load()
            sp.set("test_key", {"value": 42})
            sp.save()

            sp2 = self._make_persistence(d)
            sp2.load()
            assert sp2.get("test_key") == {"value": 42}

    def test_save_creates_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            sp.load()
            sp.save()
            assert sp.state_file.exists()

    def test_state_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            sp.load()
            sp.set("hello", "world")
            sp.save()
            data = json.loads(sp.state_file.read_text())
            assert data["hello"] == "world"
            assert "_saved_at" in data

    def test_corrupt_state_file_starts_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            # Write corrupt JSON
            sp.state_file.write_text("{invalid json!!")
            sp.load()
            assert sp._data == {}

    def test_drive_diversity_bootstraps_from_successful_history(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            sp.history_file.write_text(
                "\n".join(
                    [
                        json.dumps({"top_drive": "health", "success": True}),
                        json.dumps({"top_drive": "goals", "success": False}),
                        json.dumps({"top_drive": "emotions", "success": True}),
                    ]
                )
                + "\n"
            )
            sp.load()

            assert sp.recent_successful_top_drives() == ["health", "emotions"]

    def test_failed_trigger_does_not_advance_drive_diversity_window(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            sp.load()
            sp.set("recent_successful_top_drives", ["health", "emotions"])
            failed = SimpleNamespace(
                reason="test",
                total_pressure=1.0,
                top_drive=SimpleNamespace(name="goals"),
            )

            sp.log_trigger(failed, success=False)

            assert sp.recent_successful_top_drives() == ["health", "emotions"]

    def test_successful_trigger_advances_and_persists_drive_diversity_window(self):
        with tempfile.TemporaryDirectory() as d:
            sp = self._make_persistence(d)
            sp.load()
            sp.set("recent_successful_top_drives", ["health", "emotions"])
            success = SimpleNamespace(
                reason="test",
                total_pressure=1.0,
                top_drive=SimpleNamespace(name="goals"),
            )

            sp.log_trigger(success, success=True)
            sp.save()
            restored = self._make_persistence(d)
            restored.load()

            assert restored.recent_successful_top_drives() == ["emotions", "goals"]
