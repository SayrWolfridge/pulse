"""Tests for the X/Twitter sensor (Phase 3 integration)."""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pulse.src.core.config import PulseConfig, TwitterSensorConfig
from pulse.src.sensors.twitter_sensor import TwitterSensor, _parse_twitter_ts


# ---- Fixtures ----


@pytest.fixture
def twitter_config():
    """Build a PulseConfig with Twitter sensor enabled."""
    config = PulseConfig()
    config.sensors.twitter = TwitterSensorConfig(
        enabled=True,
        username="iamIrisAI",
        silence_threshold_minutes=360,
        bearer_token="test-bearer-token-fake",
        bearer_token_env="TWITTER_BEARER_TOKEN",
        max_results=10,
        request_timeout=5,
    )
    return config


@pytest.fixture
def sensor(twitter_config, tmp_path):
    twitter_config.state.dir = str(tmp_path / "state")
    return TwitterSensor(twitter_config)


# ---- Token resolution ----


class TestTokenResolution:
    def test_direct_config_token(self, sensor):
        assert sensor._resolve_token() == "test-bearer-token-fake"

    def test_env_var_named_in_config(self):
        config = PulseConfig()
        config.sensors.twitter = TwitterSensorConfig(
            enabled=True,
            username="iamIrisAI",
            bearer_token="",
            bearer_token_env="MY_TWITTER_BEARER",
        )
        s = TwitterSensor(config)
        with patch.dict(os.environ, {"MY_TWITTER_BEARER": "from-named-env"}):
            assert s._resolve_token() == "from-named-env"

    def test_fallback_default_env_var(self):
        config = PulseConfig()
        config.sensors.twitter = TwitterSensorConfig(
            enabled=True,
            username="iamIrisAI",
            bearer_token="",
            bearer_token_env="",
        )
        s = TwitterSensor(config)
        # Wipe both env vars, then inject default
        env_patch = {k: "" for k in ("TWITTER_BEARER_TOKEN",)}
        with patch.dict(os.environ, {"TWITTER_BEARER_TOKEN": "fallback-default"}, clear=False):
            # Ensure named env var is absent
            os.environ.pop("", None)
            assert s._resolve_token() == "fallback-default"

    def test_no_token_returns_none(self):
        config = PulseConfig()
        config.sensors.twitter = TwitterSensorConfig(
            enabled=True,
            username="iamIrisAI",
            bearer_token="",
            bearer_token_env="NONEXISTENT_TWITTER_VAR_99",
        )
        s = TwitterSensor(config)
        os.environ.pop("NONEXISTENT_TWITTER_VAR_99", None)
        os.environ.pop("TWITTER_BEARER_TOKEN", None)
        assert s._resolve_token() is None


# ---- Timestamp parsing ----


class TestTimestampParsing:
    def test_twitter_z_suffix_format(self):
        ts = _parse_twitter_ts("2026-03-08T20:15:00.000Z")
        assert ts is not None
        assert isinstance(ts, float)
        # Should be March 8 2026 20:15 UTC (~1772000000+)
        assert ts > 1_700_000_000

    def test_twitter_without_milliseconds(self):
        ts = _parse_twitter_ts("2026-03-08T20:15:00Z")
        assert ts is not None

    def test_empty_string_returns_none(self):
        assert _parse_twitter_ts("") is None

    def test_none_returns_none(self):
        assert _parse_twitter_ts(None) is None

    def test_garbage_returns_none(self):
        assert _parse_twitter_ts("not-a-timestamp") is None

    def test_iso_with_explicit_offset(self):
        ts = _parse_twitter_ts("2026-03-08T20:15:00+00:00")
        assert ts is not None


# ---- Read flow ----


class TestReadFlow:
    @pytest.mark.asyncio
    async def test_read_active_when_recent_mentions(self, sensor, tmp_path):
        """Recent API mentions → silent_x=False."""
        sensor.config.state.dir = str(tmp_path / "state")
        recent_ts = time.time() - 60 * 30  # 30 min ago
        sensor._fetch_recent_mentions = AsyncMock(
            return_value={"count": 3, "last_ts": recent_ts}
        )
        result = await sensor.read()
        assert result["silent_x"] is False
        assert result["recent_mentions"] == 3
        assert result["username"] == "iamIrisAI"

    @pytest.mark.asyncio
    async def test_read_silent_when_no_mentions_for_threshold(self, sensor, tmp_path):
        """No mentions past threshold → silent_x=True."""
        sensor.config.state.dir = str(tmp_path / "state")
        old_ts = time.time() - 60 * 400  # 400 min ago (> 360 threshold)
        sensor._fetch_recent_mentions = AsyncMock(
            return_value={"count": 0, "last_ts": old_ts}
        )
        result = await sensor.read()
        assert result["silent_x"] is True

    @pytest.mark.asyncio
    async def test_read_falls_back_to_persisted_ts(self, sensor, tmp_path):
        """API returns no timestamp → falls back to persisted file."""
        sensor.config.state.dir = str(tmp_path / "state")
        old_ts = time.time() - 60 * 500  # very old
        sensor._persist_ts(old_ts)
        sensor._fetch_recent_mentions = AsyncMock(
            return_value={"count": 0, "last_ts": None}
        )
        result = await sensor.read()
        assert result["last_mention_ts"] is not None
        assert result["silent_x"] is True

    @pytest.mark.asyncio
    async def test_read_unknown_when_no_data_at_all(self, sensor, tmp_path):
        """No API data, no persisted file → unknown state, silent_x=False (no false positive)."""
        sensor.config.state.dir = str(tmp_path / "state")
        sensor._fetch_recent_mentions = AsyncMock(
            return_value={"count": 0, "last_ts": None}
        )
        result = await sensor.read()
        assert result["silent_x"] is False
        assert result["last_mention_ts"] is None
        assert result["silence_minutes"] is None


# ---- File persistence ----


class TestFilePersistence:
    def test_persist_and_load_roundtrip(self, sensor, tmp_path):
        sensor.config.state.dir = str(tmp_path / "state")
        ts = 1_772_200_000.5
        sensor._persist_ts(ts)
        loaded = sensor._load_persisted_ts()
        assert loaded == pytest.approx(ts, rel=1e-6)

    def test_load_returns_none_when_no_file(self, sensor, tmp_path):
        sensor.config.state.dir = str(tmp_path / "state")
        assert sensor._load_persisted_ts() is None


# ---- Lifecycle ----


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_sets_token(self, sensor):
        with patch("pulse.src.sensors.twitter_sensor._HAS_AIOHTTP", False):
            await sensor.initialize()
        assert sensor._token == "test-bearer-token-fake"

    @pytest.mark.asyncio
    async def test_stop_closes_session(self, sensor):
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        sensor._session = mock_session
        await sensor.stop()
        mock_session.close.assert_called_once()
        assert sensor._session is None


# ---- Config parsing ----


class TestConfigParsing:
    def test_twitter_config_defaults(self):
        cfg = TwitterSensorConfig()
        assert cfg.enabled is False
        assert cfg.username == ""
        assert cfg.silence_threshold_minutes == 360
        assert cfg.bearer_token == ""
        assert cfg.bearer_token_env == "TWITTER_BEARER_TOKEN"
        assert cfg.max_results == 10
        assert cfg.request_timeout == 10

    def test_twitter_config_roundtrip_via_dict(self):
        from pulse.src.core.config import PulseConfig
        import yaml, io

        yaml_text = """
sensors:
  twitter:
    enabled: true
    username: iamIrisAI
    silence_threshold_minutes: 180
    bearer_token_env: MY_BEARER
    max_results: 5
"""
        data = yaml.safe_load(yaml_text)
        config = PulseConfig._from_dict(data)
        tw = config.sensors.twitter
        assert tw.enabled is True
        assert tw.username == "iamIrisAI"
        assert tw.silence_threshold_minutes == 180
        assert tw.bearer_token_env == "MY_BEARER"
        assert tw.max_results == 5


# ---- Drive engine integration ----


class TestDriveEngineIntegration:
    def test_twitter_silence_spikes_social_drive(self):
        """twitter.silent_x=True → social drive receives a spike."""
        from pulse.src.drives.engine import Drive, DriveEngine
        from pulse.src.state.persistence import StatePersistence
        import tempfile

        config = PulseConfig()
        with tempfile.TemporaryDirectory() as td:
            config.state.dir = td
            sp = StatePersistence(config)
            engine = DriveEngine(config, sp)

            # Inject a social drive at zero pressure
            engine.drives["social"] = Drive(name="social", category="social", weight=1.0)
            before = engine.drives["social"].pressure

            sensor_data = {"twitter": {"silent_x": True}}
            engine._apply_sensor_spikes(sensor_data)

            assert engine.drives["social"].pressure > before
