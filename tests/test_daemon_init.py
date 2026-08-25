"""Regression tests for PulseDaemon initialization behavior."""

import time
from unittest.mock import MagicMock, patch

import pytest


class TestLastTriggerTimeInit:
    """Verify last_trigger_time initializes to now, not epoch zero.

    Regression for the bug where last_trigger_time = 0.0 caused
    idle counters to report ~56 years (1.77B seconds) in trigger
    reason strings on the very first high_pressure_override check.
    """

    def test_last_trigger_time_not_epoch(self):
        """last_trigger_time must not be 0.0 (epoch) after __init__."""
        from pulse.src.core.daemon import PulseDaemon

        before = time.time()
        with patch.object(PulseDaemon, "__init__", autospec=True) as mock_init:
            # Bypass full init — just check the actual raw value
            mock_init.side_effect = lambda self, *a, **kw: None
            daemon = PulseDaemon.__new__(PulseDaemon)
            # Call actual __init__ on a minimal config
            pass

        # Simpler: inspect the source default directly
        import inspect

        source = inspect.getsource(PulseDaemon.__init__)
        # Must NOT contain bare 0.0 assignment for last_trigger_time
        assert "self.last_trigger_time = 0.0" not in source, (
            "last_trigger_time must not be initialized to epoch 0.0. "
            "Use time.time() so idle counter starts at 0 on first check."
        )

    def test_last_trigger_time_is_recent(self):
        """last_trigger_time must be within a second of daemon construction."""
        from pulse.src.core.daemon import PulseDaemon

        config = MagicMock()
        config.daemon.health_port = 0
        config.daemon.integration = "none"
        config.workspace.root = "/tmp"
        config.workspace.state_file = "/tmp/pulse-state-test.json"
        config.workspace.daily_notes = "memory/"

        # Patch out heavy components so we can instantiate cheaply
        with (
            patch("pulse.src.core.daemon.StatePersistence"),
            patch("pulse.src.core.daemon.DriveEngine"),
            patch("pulse.src.core.daemon.SensorManager"),
            patch("pulse.src.core.daemon.OpenClawWebhook"),
            patch("pulse.src.core.daemon.HealthServer"),
            patch("pulse.src.core.daemon.Mutator"),
            patch("pulse.src.core.daemon._load_integration"),
        ):
            before = time.time()
            daemon = PulseDaemon(config=config)
            after = time.time()

        assert daemon.last_trigger_time >= before, (
            f"last_trigger_time ({daemon.last_trigger_time}) is before construction time ({before}). "
            "Should be initialized to time.time()."
        )
        assert (
            daemon.last_trigger_time <= after + 0.1
        ), f"last_trigger_time ({daemon.last_trigger_time}) is suspiciously in the future."

    def test_idle_string_not_astronomical(self):
        """The idle value in trigger reason must be <3600s on a fresh daemon.

        Regression: before fix, idle was reported as 1773007112s (56 years)
        because last_trigger_time was initialized to 0.0 (epoch).
        """
        from pulse.src.core.daemon import PulseDaemon

        config = MagicMock()
        config.daemon.health_port = 0
        config.daemon.integration = "none"
        config.workspace.root = "/tmp"
        config.workspace.state_file = "/tmp/pulse-state-test2.json"
        config.workspace.daily_notes = "memory/"

        with (
            patch("pulse.src.core.daemon.StatePersistence"),
            patch("pulse.src.core.daemon.DriveEngine"),
            patch("pulse.src.core.daemon.SensorManager"),
            patch("pulse.src.core.daemon.OpenClawWebhook"),
            patch("pulse.src.core.daemon.HealthServer"),
            patch("pulse.src.core.daemon.Mutator"),
            patch("pulse.src.core.daemon._load_integration"),
        ):
            daemon = PulseDaemon(config=config)

        idle = time.time() - daemon.last_trigger_time
        assert idle < 3600, (
            f"Fresh daemon reports idle={idle:.0f}s — "
            "this suggests last_trigger_time was initialized to epoch (0.0) instead of time.time(). "
            "A 56-year idle is a misleading artifact, not a real measurement."
        )


class TestCooldownContinuity:
    def test_restore_uses_last_real_trigger(self):
        from pulse.src.core.daemon import PulseDaemon

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.state = MagicMock()
        daemon.state.get.return_value = {"timestamp": 4_500.0}
        daemon.config = MagicMock()
        daemon.config.openclaw.min_trigger_interval = 1_200
        daemon._turn_timestamps = []

        with patch("pulse.src.core.daemon.time.time", return_value=5_000.0):
            daemon._restore_last_trigger_time()

        assert daemon.last_trigger_time == 4_500.0
        assert daemon._turn_timestamps == [4_500.0]

    def test_missing_trigger_does_not_add_restart_cooldown(self):
        from pulse.src.core.daemon import PulseDaemon

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.state = MagicMock()
        daemon.state.get.return_value = None
        daemon.config = MagicMock()
        daemon.config.openclaw.min_trigger_interval = 1_200
        daemon._turn_timestamps = []

        with patch("pulse.src.core.daemon.time.time", return_value=5_000.0):
            daemon._restore_last_trigger_time()

        assert daemon.last_trigger_time == 3_800.0
        assert daemon._turn_timestamps == []

    def test_unrelated_mutation_does_not_snapshot_cooldown(self):
        from pulse.src.core.daemon import PulseDaemon

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.state = MagicMock()
        daemon.state.get.return_value = {"trigger_threshold": 0.7}
        daemon.config = MagicMock()
        daemon.config.drives.trigger_threshold = 0.7
        daemon.config.drives.pressure_rate = 0.01
        daemon.config.openclaw.min_trigger_interval = 1_200
        daemon.config.openclaw.max_turns_per_hour = 10

        daemon._persist_applied_mutation_overrides(
            [{"status": "applied", "type": "adjust_weight"}]
        )

        daemon.state.set.assert_not_called()

    def test_explicit_cooldown_mutation_still_persists(self):
        from pulse.src.core.daemon import PulseDaemon

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.state = MagicMock()
        daemon.state.get.return_value = {"trigger_threshold": 0.7}
        daemon.config = MagicMock()
        daemon.config.drives.trigger_threshold = 0.7
        daemon.config.drives.pressure_rate = 0.01
        daemon.config.openclaw.min_trigger_interval = 900
        daemon.config.openclaw.max_turns_per_hour = 10

        daemon._persist_applied_mutation_overrides(
            [{"status": "applied", "type": "adjust_cooldown"}]
        )

        daemon.state.set.assert_called_once_with(
            "config_overrides",
            {"trigger_threshold": 0.7, "min_trigger_interval": 900},
        )

    def test_explicit_turn_limit_mutation_still_persists(self):
        from pulse.src.core.daemon import PulseDaemon

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.state = MagicMock()
        daemon.state.get.return_value = {}
        daemon.config = MagicMock()
        daemon.config.drives.trigger_threshold = 0.7
        daemon.config.drives.pressure_rate = 0.01
        daemon.config.openclaw.min_trigger_interval = 1_200
        daemon.config.openclaw.max_turns_per_hour = 12

        daemon._persist_applied_mutation_overrides(
            [{"status": "applied", "type": "adjust_turns_per_hour"}]
        )

        daemon.state.set.assert_called_once_with(
            "config_overrides", {"max_turns_per_hour": 12}
        )


class TestDriveDiversitySelection:
    def test_normal_selection_excludes_last_two_successful_drives(self):
        from pulse.src.core.daemon import PulseDaemon
        from pulse.src.drives.engine import Drive, DriveState

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.state = MagicMock()
        daemon.state.recent_successful_top_drives.return_value = [
            "health",
            "emotions",
        ]
        health = Drive(name="health", category="health", pressure=4.0)
        emotions = Drive(name="emotions", category="emotions", pressure=3.0)
        goals = Drive(name="goals", category="goals", pressure=2.0)
        state = DriveState(drives=[health, emotions, goals], timestamp=time.time())

        selected = daemon._evaluation_drive_state(state, {"system": {"alerts": []}})

        assert selected.top_drive is goals
        assert state.top_drive is health

    def test_critical_system_alert_bypasses_drive_diversity(self):
        from pulse.src.core.daemon import PulseDaemon
        from pulse.src.drives.engine import Drive, DriveState

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.state = MagicMock()
        state = DriveState(
            drives=[Drive(name="health", category="health", pressure=4.0)],
            timestamp=time.time(),
        )

        selected = daemon._evaluation_drive_state(
            state,
            {"system": {"alerts": [{"severity": "high", "type": "disk"}]}},
        )

        assert selected is state
        daemon.state.recent_successful_top_drives.assert_not_called()

class TestShutdownWake:
    """Verify SIGTERM handler wakes the async loop instead of waiting full interval."""

    @pytest.mark.asyncio
    async def test_handle_shutdown_sets_event(self):
        from pulse.src.core.daemon import PulseDaemon

        daemon = PulseDaemon.__new__(PulseDaemon)
        daemon.running = True
        daemon._shutdown_event = None

        daemon._handle_shutdown()

        assert daemon.running is False

        event = __import__("asyncio").Event()
        daemon.running = True
        daemon._shutdown_event = event

        daemon._handle_shutdown()

        assert daemon.running is False
        assert event.is_set()
