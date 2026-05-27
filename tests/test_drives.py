"""Tests for Drive Engine — pressure accumulation, decay, and state snapshots."""

import time
from datetime import datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.drives.engine import Drive, DriveEngine, DriveState


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


class TestEveningCultureDrive:
    """Tests for the soft evening culture-talk drive."""

    def _make_engine(self):
        from unittest.mock import MagicMock

        config = MagicMock()
        config.drives.categories = {}
        config.drives.max_pressure = 5.0
        state = MagicMock()
        state.get.return_value = {}
        return DriveEngine(config=config, state=state)

    def test_grows_from_16_30_until_21(self):
        engine = self._make_engine()
        engine._refresh_evening_culture_drive(
            dt=60.0,
            now_dt=datetime(2026, 5, 26, 16, 29),
        )
        drive = engine.drives[DriveEngine.EVENING_CULTURE_DRIVE]
        assert drive.pressure == 0.0

        engine._refresh_evening_culture_drive(
            dt=60.0,
            now_dt=datetime(2026, 5, 26, 16, 30),
        )

        assert drive.pressure > 0.0
        assert drive.source_data["evening_culture"]["grow_window"] == "16:30-21:00 Europe/Moscow"

    def test_carries_after_21_without_growing(self):
        engine = self._make_engine()
        engine._refresh_evening_culture_drive(
            dt=60.0,
            now_dt=datetime(2026, 5, 26, 20, 30),
        )
        drive = engine.drives[DriveEngine.EVENING_CULTURE_DRIVE]
        grown_pressure = drive.pressure

        engine._refresh_evening_culture_drive(
            dt=600.0,
            now_dt=datetime(2026, 5, 26, 21, 30),
        )

        assert drive.pressure == grown_pressure
        assert drive.source_data["evening_culture"]["carry_window"] == "21:00-00:00 Europe/Moscow"

    def test_resets_after_midnight(self):
        engine = self._make_engine()
        engine._refresh_evening_culture_drive(
            dt=60.0,
            now_dt=datetime(2026, 5, 26, 20, 30),
        )
        drive = engine.drives[DriveEngine.EVENING_CULTURE_DRIVE]
        assert drive.pressure > 0.0

        engine._refresh_evening_culture_drive(
            dt=60.0,
            now_dt=datetime(2026, 5, 27, 0, 1),
        )

        assert drive.pressure == 0.0
        assert "evening_culture" not in drive.source_data

    def test_addressed_today_suppresses_until_tomorrow(self):
        engine = self._make_engine()
        drive_name = DriveEngine.EVENING_CULTURE_DRIVE
        addressed_at = datetime(2026, 5, 26, 19, 0).timestamp()
        engine.drives[drive_name] = Drive(
            name=drive_name,
            category=drive_name,
            pressure=0.5,
            last_addressed=addressed_at,
        )

        engine._refresh_evening_culture_drive(
            dt=60.0,
            now_dt=datetime(2026, 5, 26, 20, 0),
        )

        assert engine.drives[drive_name].pressure == 0.0
        assert "evening_culture" not in engine.drives[drive_name].source_data


class TestGrowthDrive:
    """Growth should be source-driven, not passive time pressure."""

    def _make_engine(self):
        from unittest.mock import MagicMock

        cat_growth = MagicMock()
        cat_growth.weight = 0.5
        cat_goals = MagicMock()
        cat_goals.weight = 1.0

        config = MagicMock()
        config.drives.categories = {"growth": cat_growth, "goals": cat_goals}
        config.drives.pressure_rate = 0.1
        config.drives.max_pressure = 5.0
        config.drives.success_decay = 0.5
        config.drives.adaptive_decay = False
        state = MagicMock()
        state.get.return_value = {}
        return DriveEngine(config=config, state=state)

    def test_growth_does_not_accumulate_from_time(self):
        engine = self._make_engine()
        growth_before = engine.drives["growth"].pressure
        engine.last_tick_time -= 60.0

        engine.tick(sensor_data={})

        assert engine.drives["growth"].pressure == growth_before

    def test_goals_still_accumulate_from_time(self):
        engine = self._make_engine()
        goals_before = engine.drives["goals"].pressure
        engine.last_tick_time -= 60.0

        engine.tick(sensor_data={})

        assert engine.drives["goals"].pressure > goals_before

    def test_growth_material_candidate_sets_source_data(self, tmp_path):
        engine = self._make_engine()
        material_path = tmp_path / "growth-material.json"
        material_path.write_text(
            '{"items":[{"id":"g1","status":"candidate","kind":"stable_formula",'
            '"title":"Ясность — честность",'
            '"suggested_home":"IDENTITY.md","notes":"говорить правду как опору"}]}',
            encoding="utf-8",
        )
        engine.GROWTH_MATERIAL_PATH = material_path

        engine._refresh_growth_material(now_dt=datetime(2026, 5, 27, 15, 0))

        growth = engine.drives["growth"]
        assert growth.pressure == engine.GROWTH_MATERIAL_PROMPT_PRESSURE
        assert growth.source_data["growth_material"]["id"] == "g1"
        assert "Ясность" in growth.source_data["message"]

    def test_empty_growth_material_clears_growth(self, tmp_path):
        engine = self._make_engine()
        material_path = tmp_path / "growth-material.json"
        material_path.write_text('{"items":[]}', encoding="utf-8")
        engine.GROWTH_MATERIAL_PATH = material_path
        engine.drives["growth"].pressure = 1.0
        engine.drives["growth"].source_data["growth_material"] = {"id": "old"}

        engine._refresh_growth_material(now_dt=datetime(2026, 5, 27, 15, 0))

        growth = engine.drives["growth"]
        assert growth.pressure == 0.0
        assert "growth_material" not in growth.source_data

    def test_suppressed_growth_candidate_does_not_raise_growth(self, tmp_path):
        engine = self._make_engine()
        material_path = tmp_path / "growth-material.json"
        material_path.write_text(
            '{"items":[{"id":"g1","status":"candidate",'
            '"suppress_until":"2026-05-28T14:00:00"}]}',
            encoding="utf-8",
        )
        engine.GROWTH_MATERIAL_PATH = material_path

        engine._refresh_growth_material(now_dt=datetime(2026, 5, 27, 15, 0))

        assert engine.drives["growth"].pressure == 0.0

    def test_non_candidate_growth_material_does_not_raise_growth(self, tmp_path):
        engine = self._make_engine()
        material_path = tmp_path / "growth-material.json"
        material_path.write_text(
            '{"items":[{"id":"g1","status":"accepted","title":"already placed"}]}',
            encoding="utf-8",
        )
        engine.GROWTH_MATERIAL_PATH = material_path

        engine._refresh_growth_material(now_dt=datetime(2026, 5, 27, 15, 0))

        assert engine.drives["growth"].pressure == 0.0

    def test_prompted_growth_material_is_suppressed_until_tomorrow_14(self, tmp_path):
        engine = self._make_engine()
        material_path = tmp_path / "growth-material.json"
        material_path.write_text(
            '{"items":[{"id":"g1","status":"candidate","title":"bridge"}]}',
            encoding="utf-8",
        )
        engine.GROWTH_MATERIAL_PATH = material_path

        changed = engine._suppress_prompted_growth_material(
            "g1",
            now_dt=datetime(2026, 5, 27, 15, 48, 30),
        )

        data = __import__("json").loads(material_path.read_text(encoding="utf-8"))
        item = data["items"][0]
        assert changed is True
        assert item["last_prompted_at"] == "2026-05-27T15:48:30"
        assert item["suppress_until"] == "2026-05-28T14:00:00"

    def test_growth_success_suppresses_candidate(self, tmp_path):
        from unittest.mock import MagicMock

        engine = self._make_engine()
        material_path = tmp_path / "growth-material.json"
        material_path.write_text(
            '{"items":[{"id":"g1","status":"candidate","title":"bridge"}]}',
            encoding="utf-8",
        )
        engine.GROWTH_MATERIAL_PATH = material_path
        growth = engine.drives["growth"]
        growth.pressure = 0.8
        growth.source_data["growth_material"] = {"id": "g1"}

        decision = MagicMock()
        decision.total_pressure = 0.8
        decision.top_drive = growth
        engine.on_trigger_success(decision)

        data = __import__("json").loads(material_path.read_text(encoding="utf-8"))
        item = data["items"][0]
        assert item["suppress_until"].endswith("T14:00:00")
        assert engine.drives["growth"].pressure == 0.0


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
