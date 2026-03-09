"""Tests for the Discord sensor (Phase 3 integration)."""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pulse.src.core.config import DiscordSensorConfig, PulseConfig
from pulse.src.sensors.discord_sensor import DiscordSensor, _parse_discord_ts


# ---- Fixtures ----


@pytest.fixture
def discord_config():
    """Build a PulseConfig with Discord sensor enabled."""
    config = PulseConfig()
    config.sensors.discord = DiscordSensorConfig(
        enabled=True,
        channels=["111111111111111111", "222222222222222222"],
        silence_threshold_minutes=60,
        bot_token="test-bot-token-fake",
        bot_token_env="DISCORD_BOT_TOKEN",
        channel_thresholds={"222222222222222222": 120},
        request_timeout=5,
    )
    return config


@pytest.fixture
def sensor(discord_config, tmp_path):
    discord_config.state.dir = str(tmp_path / "state")
    return DiscordSensor(discord_config)


# ---- Token resolution ----


class TestTokenResolution:
    def test_direct_config_token(self, sensor):
        assert sensor._resolve_token() == "test-bot-token-fake"

    def test_env_var_token(self):
        config = PulseConfig()
        config.sensors.discord = DiscordSensorConfig(
            enabled=True, bot_token="", bot_token_env="MY_DISCORD_TOKEN"
        )
        sensor = DiscordSensor(config)
        with patch.dict(os.environ, {"MY_DISCORD_TOKEN": "from-env"}):
            assert sensor._resolve_token() == "from-env"

    def test_no_token_returns_none(self):
        config = PulseConfig()
        config.sensors.discord = DiscordSensorConfig(
            enabled=True, bot_token="", bot_token_env="NONEXISTENT_VAR_12345"
        )
        sensor = DiscordSensor(config)
        # Ensure env var doesn't exist
        os.environ.pop("NONEXISTENT_VAR_12345", None)
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        assert sensor._resolve_token() is None

    def test_fallback_default_env_var(self):
        config = PulseConfig()
        config.sensors.discord = DiscordSensorConfig(
            enabled=True, bot_token="", bot_token_env=""
        )
        sensor = DiscordSensor(config)
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fallback-token"}):
            assert sensor._resolve_token() == "fallback-token"


# ---- Timestamp parsing ----


class TestTimestampParsing:
    def test_full_iso_with_offset(self):
        ts = _parse_discord_ts("2026-03-08T20:15:00.000000+00:00")
        assert ts is not None
        assert isinstance(ts, float)
        # Should be March 8 2026 20:15 UTC
        assert ts > 1772000000  # sanity

    def test_iso_without_microseconds(self):
        ts = _parse_discord_ts("2026-03-08T20:15:00+00:00")
        assert ts is not None

    def test_empty_string_returns_none(self):
        assert _parse_discord_ts("") is None

    def test_none_returns_none(self):
        assert _parse_discord_ts(None) is None

    def test_garbage_returns_none(self):
        assert _parse_discord_ts("not-a-date") is None


# ---- Read (with mocked API) ----


class TestRead:
    @pytest.mark.asyncio
    async def test_read_no_token_no_persisted(self, sensor):
        """Without API or persisted data, channels report unknown."""
        sensor._token = None
        sensor._session = None
        result = await sensor.read()
        assert "silent_agents" in result
        assert "channel_silences" in result
        assert result["channels_monitored"] == 2
        # All channels should be unknown → silent_agents = False (no false positive)
        assert result["silent_agents"] is False
        for ch in result["channel_silences"]:
            assert ch["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_read_active_channel(self, sensor):
        """Channel with recent message → not silent."""
        sensor._token = None
        sensor._session = None
        now = time.time()
        # Simulate persisted ts from 10 minutes ago (threshold is 60 min)
        sensor._persist_ts("111111111111111111", now - 600)
        result = await sensor.read()
        ch1 = [c for c in result["channel_silences"] if c["channel_id"] == "111111111111111111"][0]
        assert ch1["silent"] is False
        assert ch1["status"] == "active"
        assert ch1["silence_minutes"] < 15

    @pytest.mark.asyncio
    async def test_read_silent_channel(self, sensor):
        """Channel with old message → silent."""
        sensor._token = None
        sensor._session = None
        now = time.time()
        # 2 hours ago, threshold is 60 min for channel 1
        sensor._persist_ts("111111111111111111", now - 7200)
        result = await sensor.read()
        ch1 = [c for c in result["channel_silences"] if c["channel_id"] == "111111111111111111"][0]
        assert ch1["silent"] is True
        assert ch1["status"] == "silent"
        assert result["silent_agents"] is True

    @pytest.mark.asyncio
    async def test_per_channel_threshold(self, sensor):
        """Channel 2 has 120-min threshold override."""
        sensor._token = None
        sensor._session = None
        now = time.time()
        # 90 minutes ago — over default (60) but under channel 2's 120-min threshold
        sensor._persist_ts("222222222222222222", now - 5400)
        result = await sensor.read()
        ch2 = [c for c in result["channel_silences"] if c["channel_id"] == "222222222222222222"][0]
        assert ch2["silent"] is False  # 90 min < 120 threshold
        assert ch2["threshold_minutes"] == 120


# ---- File persistence ----


class TestFilePersistence:
    def test_persist_and_load(self, sensor, tmp_path):
        """Round-trip persist → load."""
        sensor.config.state = MagicMock()
        sensor.config.state.path = str(tmp_path)
        ts = time.time()
        sensor._persist_ts("123456", ts)
        loaded = sensor._load_persisted_ts("123456")
        assert loaded is not None
        assert abs(loaded - ts) < 0.01

    def test_load_nonexistent(self, sensor, tmp_path):
        sensor.config.state = MagicMock()
        sensor.config.state.path = str(tmp_path)
        assert sensor._load_persisted_ts("999999") is None


# ---- Lifecycle ----


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_with_token(self, sensor):
        """Initialize creates aiohttp session when token present."""
        await sensor.initialize()
        assert sensor._token == "test-bot-token-fake"
        assert sensor._session is not None
        await sensor.stop()
        assert sensor._session is None

    @pytest.mark.asyncio
    async def test_initialize_without_token(self):
        """Initialize without token → no session, just warning."""
        config = PulseConfig()
        config.sensors.discord = DiscordSensorConfig(
            enabled=True, channels=["111"], bot_token="", bot_token_env="NONEXISTENT_99"
        )
        s = DiscordSensor(config)
        os.environ.pop("NONEXISTENT_99", None)
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        await s.initialize()
        assert s._token is None
        assert s._session is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, sensor):
        """Stop when no session → no error."""
        sensor._session = None
        await sensor.stop()  # should not raise


# ---- Config parsing ----


class TestConfigParsing:
    def test_discord_config_from_yaml(self, tmp_path):
        """YAML → DiscordSensorConfig round-trip."""
        yaml_content = """\
sensors:
  discord:
    enabled: true
    channels:
      - "1473418272551469240"
    silence_threshold_minutes: 240
    bot_token_env: MY_TOKEN
    channel_thresholds:
      "1473418272551469240": 300
    request_timeout: 15
"""
        config_file = tmp_path / "pulse.yaml"
        config_file.write_text(yaml_content)
        config = PulseConfig.load(str(config_file))
        assert config.sensors.discord.enabled is True
        assert "1473418272551469240" in config.sensors.discord.channels
        assert config.sensors.discord.silence_threshold_minutes == 240
        assert config.sensors.discord.bot_token_env == "MY_TOKEN"
        assert config.sensors.discord.channel_thresholds["1473418272551469240"] == 300
        assert config.sensors.discord.request_timeout == 15

    def test_discord_config_defaults(self):
        """Default DiscordSensorConfig has sane values."""
        cfg = DiscordSensorConfig()
        assert cfg.enabled is False
        assert cfg.channels == []
        assert cfg.silence_threshold_minutes == 180
        assert cfg.bot_token == ""
        assert cfg.bot_token_env == "DISCORD_BOT_TOKEN"
        assert cfg.channel_thresholds == {}
        assert cfg.request_timeout == 10


# ---- Drive engine integration (mock) ----


class TestDriveEngineIntegration:
    def test_sensor_data_feeds_drives(self, tmp_path):
        """Verify drive engine reads discord.silent_agents correctly."""
        from pulse.src.drives.engine import DriveEngine, Drive
        from pulse.src.state.persistence import StatePersistence
        config = PulseConfig()
        config.state.dir = str(tmp_path / "state")
        state = StatePersistence(config)
        engine = DriveEngine(config, state)
        # Ensure social drive exists
        engine.drives["social"] = Drive(name="social", category="social", weight=1.0)
        initial_pressure = engine.drives["social"].pressure

        # Feed sensor data with silent_agents=True
        sensor_data = {"discord": {"silent_agents": True}}
        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["social"].pressure > initial_pressure
