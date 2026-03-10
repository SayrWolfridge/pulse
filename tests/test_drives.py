"""Tests for Drive Engine — pressure accumulation, decay, and state snapshots."""

import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.drives.engine import Drive, DriveState


class TestDrive:
    """Test individual drive mechanics."""

    def test_initial_pressure_is_zero(self):
        d = Drive(name="goals", category="goals")
        assert d.pressure == 0.0

    def test_tick_increases_pressure(self):
        d = Drive(name="goals", category="goals", weight=1.0)
        d.tick(dt=60.0, rate=0.5, max_pressure=5.0)
        assert d.pressure > 0.0

    def test_tick_respects_max_pressure(self):
        d = Drive(name="goals", category="goals", weight=1.0, pressure=4.9)
        d.tick(dt=600.0, rate=1.0, max_pressure=5.0)
        assert d.pressure == 5.0

    def test_decay_reduces_pressure(self):
        d = Drive(name="goals", category="goals", pressure=3.0)
        d.decay(1.5)
        assert d.pressure == 1.5

    def test_decay_cannot_go_negative(self):
        d = Drive(name="goals", category="goals", pressure=1.0)
        d.decay(5.0)
        assert d.pressure == 0.0

    def test_spike_increases_pressure(self):
        d = Drive(name="goals", category="goals", pressure=1.0)
        d.spike(2.0, max_pressure=5.0)
        assert d.pressure == 3.0

    def test_spike_capped_at_max(self):
        d = Drive(name="goals", category="goals", pressure=4.0)
        d.spike(3.0, max_pressure=5.0)
        assert d.pressure == 5.0

    def test_weighted_pressure(self):
        d = Drive(name="goals", category="goals", pressure=2.0, weight=1.5)
        assert d.weighted_pressure == 3.0

    def test_to_dict_roundtrip(self):
        d = Drive(name="goals", category="goals", pressure=1.234, weight=0.8)
        data = d.to_dict()
        assert data["name"] == "goals"
        assert data["pressure"] == 1.234
        assert data["weight"] == 0.8


class TestDriveState:
    """Test drive state snapshots."""

    def test_total_pressure_sum(self):
        drives = [
            Drive(name="goals", category="goals", pressure=2.0, weight=1.0),
            Drive(name="curiosity", category="curiosity", pressure=1.0, weight=1.0),
        ]
        state = DriveState(drives=drives, timestamp=time.time())
        assert state.total_pressure == 3.0

    def test_top_drive_selection(self):
        drives = [
            Drive(name="goals", category="goals", pressure=1.0, weight=1.0),
            Drive(name="curiosity", category="curiosity", pressure=3.0, weight=1.0),
        ]
        state = DriveState(drives=drives, timestamp=time.time())
        assert state.top_drive.name == "curiosity"

    def test_top_drive_considers_weight(self):
        drives = [
            Drive(
                name="goals", category="goals", pressure=2.0, weight=2.0
            ),  # weighted=4
            Drive(
                name="curiosity", category="curiosity", pressure=3.0, weight=1.0
            ),  # weighted=3
        ]
        state = DriveState(drives=drives, timestamp=time.time())
        assert state.top_drive.name == "goals"

    def test_empty_drives(self):
        state = DriveState(drives=[], timestamp=time.time())
        assert state.total_pressure == 0.0
        assert state.top_drive is None

    def test_pressure_accumulation_over_time(self):
        """Simulate multiple ticks and verify monotonic increase."""
        d = Drive(name="goals", category="goals", weight=1.0)
        pressures = []
        for _ in range(10):
            d.tick(dt=30.0, rate=0.5, max_pressure=5.0)
            pressures.append(d.pressure)
        # Should be monotonically increasing
        assert all(pressures[i] <= pressures[i + 1] for i in range(len(pressures) - 1))


class TestDriveEngineWeightDrift:
    """Regression tests for exponential weight drift bug (fixed March 2026).

    Root cause: health.py passed ``drive.weight`` (already adjusted) to
    ``effective_weight(drive_name, base_weight)`` instead of the config base
    weight.  After N feedback cycles the weight became base * multiplier^N
    (e.g. 1.3^27 ≈ 994×).  Parallel bug: restore_state() reloaded persisted
    (drifted) weight instead of always using config_weight().
    """

    def _make_engine(self, tmp_path):
        """Build a minimal DriveEngine with a 'goals' drive (weight=1.0)."""
        from unittest.mock import MagicMock
        from src.drives.engine import DriveEngine

        # Use plain MagicMock (no spec) so attribute access never raises
        cat_goals = MagicMock()
        cat_goals.weight = 1.0
        cat_curiosity = MagicMock()
        cat_curiosity.weight = 1.0

        config = MagicMock()
        config.drives.categories = {"goals": cat_goals, "curiosity": cat_curiosity}
        config.drives.pressure_rate = 0.1
        config.drives.max_pressure = 100.0
        config.drives.success_decay = 2.0
        config.drives.adaptive_decay = False
        config.drives.failure_boost = 0.5

        state = MagicMock()
        state.get.return_value = {}

        engine = DriveEngine(config=config, state=state)
        return engine

    def test_config_weight_returns_original(self, tmp_path):
        engine = self._make_engine(tmp_path)
        # Manually drift the weight to simulate the old bug
        engine.drives["goals"].weight = 500.0
        # config_weight should always return the original 1.0
        assert engine.config_weight("goals") == 1.0

    def test_restore_state_does_not_load_drifted_weight(self, tmp_path):
        """restore_state must ignore persisted weights to prevent drift."""
        engine = self._make_engine(tmp_path)

        # Simulate persisted state with a severely drifted weight
        drifted_state = {
            "goals": {"pressure": 2.5, "weight": 1117.0, "last_addressed": 0.0},
        }
        engine.state.get.return_value = drifted_state
        engine.restore_state()

        # Weight must remain config value (1.0), not the drifted 1117.0
        assert engine.drives["goals"].weight == 1.0
        # Pressure should be restored correctly
        assert engine.drives["goals"].pressure == 2.5

    def test_weight_stays_bounded_after_many_success_feedbacks(self, tmp_path):
        """Simulates the pattern that caused 1117× drift:
        effective_weight(drive_name, drive.weight) called 30 times."""
        from pulse.src.feedback_learner import FeedbackLearner

        engine = self._make_engine(tmp_path)
        learner = FeedbackLearner(tmp_path)

        # Simulate 30 success feedback cycles using config_weight (correct fix)
        for _ in range(30):
            config_base = engine.config_weight("goals")
            engine.drives["goals"].weight = learner.effective_weight("goals", config_base)
            learner.record("goals", 2.0, "success")

        # Weight must stay bounded: max is config_base * 1.3 = 1.3
        assert engine.drives["goals"].weight <= 1.3 + 1e-9, (
            f"Weight drifted to {engine.drives['goals'].weight:.2f} "
            f"(expected ≤ 1.3, old bug would produce ~994×)"
        )

    def test_weight_drift_without_fix_would_explode(self, tmp_path):
        """Documents what the old bug produced — weight compounding per cycle."""
        from pulse.src.feedback_learner import FeedbackLearner

        learner = FeedbackLearner(tmp_path)
        # Simulate the OLD broken pattern: pass drive.weight as base each time
        weight = 1.0  # starting weight
        for _ in range(27):
            learner.record("goals", 2.0, "success")
            multiplier = learner.get_weight_adjustment("goals")
            weight = weight * multiplier  # BUG: compounds each call

        # Old pattern would produce weight ≈ 1.3^27 ≈ 994
        assert weight > 100, f"Expected old bug to drift weight > 100×, got {weight:.2f}"
