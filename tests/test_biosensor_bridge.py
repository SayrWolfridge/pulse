"""Tests for the biosensor bridge — Phase E1."""

import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.biosensor_bridge import (
    _hr_zone,
    _hrv_stress,
    _load_biosensor_state,
    _save_biosensor_state,
    _qty,
    _parse_hae_workout,
)


class TestHRZone:
    def test_resting(self):
        assert _hr_zone(55) == "resting"

    def test_relaxed(self):
        assert _hr_zone(70) == "relaxed"

    def test_moderate(self):
        assert _hr_zone(95) == "moderate"

    def test_elevated(self):
        assert _hr_zone(115) == "elevated"

    def test_high(self):
        assert _hr_zone(145) == "high"

    def test_max(self):
        assert _hr_zone(175) == "max"

    def test_boundary_resting_relaxed(self):
        assert _hr_zone(60) == "relaxed"

    def test_boundary_high(self):
        assert _hr_zone(130) == "high"


class TestHRVStress:
    def test_low_stress_high_hrv(self):
        assert _hrv_stress(70) == "low"

    def test_moderate_stress(self):
        assert _hrv_stress(50) == "moderate"

    def test_elevated_stress(self):
        assert _hrv_stress(35) == "elevated"

    def test_high_stress_low_hrv(self):
        assert _hrv_stress(20) == "high"

    def test_boundary_low_moderate(self):
        # 60 is not > 60, so falls to moderate
        assert _hrv_stress(60) == "moderate"
        assert _hrv_stress(61) == "low"

    def test_boundary_elevated_high(self):
        # 25 is not > 25, so falls to high
        assert _hrv_stress(25) == "high"
        assert _hrv_stress(26) == "elevated"


class TestBiosensorState:
    def test_default_state_structure(self):
        with patch("src.biosensor_bridge._DEFAULT_BIOSENSOR_FILE") as mock_path:
            mock_path.exists.return_value = False
            state = _load_biosensor_state()
        assert "heart_rate" in state
        assert "hrv" in state
        assert "activity" in state
        assert "sleep" in state
        assert "workout" in state

    def test_heart_rate_defaults(self):
        with patch("src.biosensor_bridge._DEFAULT_BIOSENSOR_FILE") as mock_path:
            mock_path.exists.return_value = False
            state = _load_biosensor_state()
        hr = state["heart_rate"]
        assert hr["value"] is None
        assert hr["zone"] is None

    def test_workout_defaults(self):
        with patch("src.biosensor_bridge._DEFAULT_BIOSENSOR_FILE") as mock_path:
            mock_path.exists.return_value = False
            state = _load_biosensor_state()
        assert state["workout"]["active"] is False

    def test_activity_defaults(self):
        with patch("src.biosensor_bridge._DEFAULT_BIOSENSOR_FILE") as mock_path:
            mock_path.exists.return_value = False
            state = _load_biosensor_state()
        assert state["activity"]["goal_move"] == 600


class TestEndpointMapping:
    """Verify that biometric values map to correct Pulse signals."""

    def test_high_hr_triggers_adrenaline_keywords(self):
        """Zone 'high' should result in adrenaline + cortisol increase."""
        assert _hr_zone(145) == "high"
        # If zone is "high", endocrine update logic applies adrenaline +0.3

    def test_low_hrv_means_high_stress(self):
        """Low HRV (< 25ms) = high stress = cortisol increase."""
        assert _hrv_stress(20) == "high"

    def test_high_hrv_means_low_stress(self):
        """High HRV (> 60ms) = low stress = serotonin + cortisol decay."""
        assert _hrv_stress(65) == "low"

    def test_resting_hr_means_low_adrenaline(self):
        """Resting HR < 60 bpm = adrenaline decay."""
        assert _hr_zone(55) == "resting"


class TestQtyHelper:
    """Tests for the _qty() value-extraction helper."""

    def test_plain_number(self):
        assert _qty(350.5) == 350.5

    def test_dict_with_qty(self):
        assert _qty({"qty": 420.0, "units": "kcal"}) == 420.0

    def test_none(self):
        assert _qty(None) is None

    def test_dict_missing_qty_key(self):
        assert _qty({"units": "kcal"}) is None

    def test_zero_is_preserved(self):
        assert _qty(0) == 0
        assert _qty({"qty": 0, "units": "kcal"}) == 0


class TestParseHAEWorkout:
    """Tests for _parse_hae_workout — Health Auto Export batch workout parser."""

    def _base_workout(self, **kwargs):
        defaults = {
            "name": "Running",
            "durationMin": 30.0,
            "activeEnergyBurned": 310.5,
            "averageHeartRate": 148.0,
            "date": "2026-03-16T06:00:00",
        }
        defaults.update(kwargs)
        return defaults

    def test_basic_fields(self):
        w = _parse_hae_workout(self._base_workout())
        assert w["activity"] == "Running"
        assert w["duration_min"] == 30.0
        assert w["calories"] == 310.5
        assert w["avg_hr"] == 148.0
        assert w["started"] == "2026-03-16T06:00:00"
        assert w["active"] is False

    def test_activity_fallback_chain(self):
        """Try each field alias in order."""
        assert _parse_hae_workout({"name": "Cycling"})["activity"] == "Cycling"
        assert _parse_hae_workout({"workoutActivityType": "Swimming"})["activity"] == "Swimming"
        assert _parse_hae_workout({"activityType": "Yoga"})["activity"] == "Yoga"
        assert _parse_hae_workout({"type": "Strength"})["activity"] == "Strength"
        assert _parse_hae_workout({})["activity"] == "unknown"

    def test_calories_dict_format(self):
        """Calories wrapped in {"qty": X, "units": "kcal"} format."""
        w = _parse_hae_workout(self._base_workout(activeEnergyBurned={"qty": 285.3, "units": "kcal"}))
        assert w["calories"] == 285.3

    def test_calories_fallback_to_totalEnergyBurned(self):
        d = {"name": "Run", "totalEnergyBurned": 200.0}
        assert _parse_hae_workout(d)["calories"] == 200.0

    def test_calories_fallback_to_calories_key(self):
        d = {"name": "Run", "calories": 150.0}
        assert _parse_hae_workout(d)["calories"] == 150.0

    def test_no_calories(self):
        d = {"name": "Run"}
        assert _parse_hae_workout(d)["calories"] is None

    def test_duration_in_seconds_converted(self):
        """duration > 1440 is treated as seconds and converted to minutes."""
        d = {"name": "Run", "duration": 3600}  # 3600 seconds = 60 minutes
        assert _parse_hae_workout(d)["duration_min"] == 60.0

    def test_duration_min_preferred_over_duration(self):
        d = {"name": "Run", "durationMin": 45.0, "duration": 9000}
        assert _parse_hae_workout(d)["duration_min"] == 45.0

    def test_avg_hr_dict_format(self):
        d = {"name": "Run", "averageHeartRate": {"qty": 155.0, "units": "bpm"}}
        assert _parse_hae_workout(d)["avg_hr"] == 155.0

    def test_avg_hr_fallback_to_avgHeartRate(self):
        d = {"name": "Run", "avgHeartRate": 162.0}
        assert _parse_hae_workout(d)["avg_hr"] == 162.0

    def test_started_fallback_to_startDate(self):
        d = {"name": "Run", "startDate": "2026-03-16T07:00:00"}
        assert _parse_hae_workout(d)["started"] == "2026-03-16T07:00:00"

    def test_distance_field(self):
        d = {"name": "Run", "totalDistance": 5.2}
        assert _parse_hae_workout(d)["distance"] == 5.2

    def test_empty_dict_returns_defaults(self):
        w = _parse_hae_workout({})
        assert w["active"] is False
        assert w["activity"] == "unknown"
        assert w["duration_min"] is None
        assert w["calories"] is None
        assert w["avg_hr"] is None
