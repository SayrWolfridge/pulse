"""Tests for Logos sensor → DriveEngine wiring.

Verifies that logos.backlog_pressure and logos.stale_pressure from the
LogosSensor are properly wired into the goals drive via
_apply_sensor_spikes().

Added: March 15, 2026 — closing the loop on Pulse Trigger #318 (system drive,
logos backlog → drive engine gap).
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.drives.engine import Drive, DriveEngine


def _make_engine():
    """Build a minimal DriveEngine with goals/curiosity drives."""
    cat_goals = MagicMock()
    cat_goals.weight = 1.0
    cat_curiosity = MagicMock()
    cat_curiosity.weight = 0.6

    config = MagicMock()
    config.drives.categories = {
        "goals": cat_goals,
        "curiosity": cat_curiosity,
    }
    config.drives.pressure_rate = 0.1
    config.drives.max_pressure = 5.0
    config.drives.success_decay = 0.5
    config.drives.adaptive_decay = False
    config.drives.failure_boost = 0.2

    state = MagicMock()
    state.get.return_value = {}

    return DriveEngine(config=config, state=state)


class TestLogosDriverWiring:
    """Logos sensor output feeds into goals drive via _apply_sensor_spikes."""

    def test_backlog_pressure_spikes_goals(self):
        """logos.backlog_pressure in sensor_data should raise goals drive."""
        engine = _make_engine()
        initial = engine.drives["goals"].pressure

        sensor_data = {
            "logos": {
                "logos.backlog_count": 3,
                "logos.backlog_pressure": 0.75,  # 3 tasks × 0.25
                "logos.stale_count": 0,
                "logos.stale_pressure": 0.0,
                "logos.in_progress_count": 0,
                "logos.requires_human_count": 1,
            }
        }
        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["goals"].pressure == pytest.approx(initial + 0.75, abs=1e-6)

    def test_stale_pressure_spikes_goals_with_weight(self):
        """logos.stale_pressure should be multiplied by 1.2 before spiking goals."""
        engine = _make_engine()
        initial = engine.drives["goals"].pressure

        sensor_data = {
            "logos": {
                "logos.backlog_count": 0,
                "logos.backlog_pressure": 0.0,
                "logos.stale_count": 1,
                "logos.stale_pressure": 1.5,
                "logos.in_progress_count": 1,
                "logos.requires_human_count": 0,
            }
        }
        engine._apply_sensor_spikes(sensor_data)

        # stale_pressure × 1.2 weight = 1.8
        assert engine.drives["goals"].pressure == pytest.approx(initial + 1.8, abs=1e-6)

    def test_backlog_and_stale_both_spike(self):
        """Both backlog and stale pressure can spike in the same cycle."""
        engine = _make_engine()

        sensor_data = {
            "logos": {
                "logos.backlog_count": 4,
                "logos.backlog_pressure": 1.0,
                "logos.stale_count": 1,
                "logos.stale_pressure": 1.5,
                "logos.in_progress_count": 1,
                "logos.requires_human_count": 2,
            }
        }
        engine._apply_sensor_spikes(sensor_data)

        # 1.0 (backlog) + 1.5 × 1.2 (stale) = 1.0 + 1.8 = 2.8
        assert engine.drives["goals"].pressure == pytest.approx(2.8, abs=1e-6)

    def test_no_logos_data_does_not_change_goals(self):
        """Missing logos key in sensor_data → no spike."""
        engine = _make_engine()
        initial = engine.drives["goals"].pressure

        engine._apply_sensor_spikes({})  # no logos key

        assert engine.drives["goals"].pressure == initial

    def test_zero_logos_pressure_no_spike(self):
        """Empty backlog → no spike even when logos key is present."""
        engine = _make_engine()
        initial = engine.drives["goals"].pressure

        sensor_data = {
            "logos": {
                "logos.backlog_count": 0,
                "logos.backlog_pressure": 0.0,
                "logos.stale_count": 0,
                "logos.stale_pressure": 0.0,
                "logos.in_progress_count": 0,
                "logos.requires_human_count": 0,
            }
        }
        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["goals"].pressure == initial

    def test_backlog_pressure_capped_at_max(self):
        """Spike cannot exceed max_pressure even with a massive backlog."""
        engine = _make_engine()
        engine.drives["goals"].pressure = 4.9  # near ceiling

        sensor_data = {
            "logos": {
                "logos.backlog_count": 50,
                "logos.backlog_pressure": 3.0,  # max_backlog_pressure cap
                "logos.stale_count": 0,
                "logos.stale_pressure": 0.0,
                "logos.in_progress_count": 0,
                "logos.requires_human_count": 0,
            }
        }
        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["goals"].pressure == pytest.approx(5.0, abs=1e-6)

    def test_logos_does_not_affect_other_drives(self):
        """Logos pressure only touches goals, not curiosity or other drives."""
        engine = _make_engine()
        curiosity_before = engine.drives["curiosity"].pressure

        sensor_data = {
            "logos": {
                "logos.backlog_count": 5,
                "logos.backlog_pressure": 1.25,
                "logos.stale_count": 0,
                "logos.stale_pressure": 0.0,
                "logos.in_progress_count": 0,
                "logos.requires_human_count": 0,
            }
        }
        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["curiosity"].pressure == curiosity_before
