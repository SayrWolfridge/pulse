"""Regression tests for PulseDaemon initialization behavior."""

import time
from unittest.mock import MagicMock, patch


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
        assert daemon.last_trigger_time <= after + 0.1, (
            f"last_trigger_time ({daemon.last_trigger_time}) is suspiciously in the future."
        )

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
