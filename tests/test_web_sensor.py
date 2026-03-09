"""Tests for the Web sensor (Phase 3 integration)."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pulse.src.core.config import PulseConfig, WebSensorConfig
from pulse.src.drives.engine import DriveEngine, Drive
from pulse.src.state.persistence import StatePersistence
from pulse.src.sensors.web_sensor import (
    WebSensor,
    _parse_feed,
    _extract_rss_items,
    _extract_atom_items,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def web_config(tmp_path):
    config = PulseConfig()
    config.sensors.web = WebSensorConfig(
        enabled=True,
        feeds=["https://example.com/feed.xml", "https://example.com/blog.rss"],
        check_interval_minutes=30,
        max_items_per_feed=20,
        request_timeout=5,
    )
    config.state.dir = str(tmp_path / "state")
    return config


@pytest.fixture
def sensor(web_config):
    return WebSensor(web_config)


# ---------------------------------------------------------------------------
# Sample XML fixtures
# ---------------------------------------------------------------------------

RSS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <guid>https://example.com/post-3</guid>
      <title>Post 3</title>
      <pubDate>Mon, 09 Mar 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>https://example.com/post-2</guid>
      <title>Post 2</title>
      <pubDate>Sun, 08 Mar 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>https://example.com/post-1</guid>
      <title>Post 1</title>
      <pubDate>Sat, 07 Mar 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

ATOM_SAMPLE = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test Feed</title>
  <entry>
    <id>https://example.com/atom-entry-2</id>
    <title>Atom Entry 2</title>
    <updated>2026-03-09T10:00:00Z</updated>
  </entry>
  <entry>
    <id>https://example.com/atom-entry-1</id>
    <title>Atom Entry 1</title>
    <updated>2026-03-08T10:00:00Z</updated>
  </entry>
</feed>"""

MALFORMED_XML = b"<this is not valid XML >>><"

RSS_NO_GUID = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <link>https://example.com/via-link</link>
      <title>Link-only item</title>
    </item>
  </channel>
</rss>"""


# ---------------------------------------------------------------------------
# XML parsing tests
# ---------------------------------------------------------------------------


class TestParseFeed:
    def test_parse_rss_extracts_items(self):
        items = _parse_feed(RSS_SAMPLE, max_items=10)
        assert len(items) == 3
        assert items[0]["uid"] == "https://example.com/post-3"
        assert items[2]["uid"] == "https://example.com/post-1"

    def test_parse_rss_respects_max_items(self):
        items = _parse_feed(RSS_SAMPLE, max_items=2)
        assert len(items) == 2

    def test_parse_atom_extracts_entries(self):
        items = _parse_feed(ATOM_SAMPLE, max_items=10)
        assert len(items) == 2
        assert items[0]["uid"] == "https://example.com/atom-entry-2"

    def test_parse_rss_fallback_to_link(self):
        items = _parse_feed(RSS_NO_GUID, max_items=10)
        assert len(items) == 1
        assert items[0]["uid"] == "https://example.com/via-link"

    def test_parse_malformed_xml_raises(self):
        with pytest.raises(ValueError, match="XML parse error"):
            _parse_feed(MALFORMED_XML, max_items=10)

    def test_parse_rss_empty_channel(self):
        xml = b"<rss version='2.0'><channel></channel></rss>"
        items = _parse_feed(xml, max_items=10)
        assert items == []

    def test_parse_atom_pub_dates(self):
        items = _parse_feed(ATOM_SAMPLE, max_items=10)
        assert items[0]["pub"] == "2026-03-09T10:00:00Z"
        assert items[1]["pub"] == "2026-03-08T10:00:00Z"

    def test_parse_rss_pub_dates(self):
        items = _parse_feed(RSS_SAMPLE, max_items=10)
        assert "Mar 2026" in items[0]["pub"]


# ---------------------------------------------------------------------------
# New-item counting tests
# ---------------------------------------------------------------------------


class TestCountNew:
    def _items(self, uids):
        return [{"uid": u, "pub": ""} for u in uids]

    def test_first_run_no_bookmark_returns_zero(self):
        items = self._items(["a", "b", "c"])
        assert WebSensor._count_new(items, "") == 0

    def test_no_new_items(self):
        items = self._items(["a", "b", "c"])
        assert WebSensor._count_new(items, "a") == 0

    def test_one_new_item(self):
        items = self._items(["new", "a", "b"])
        assert WebSensor._count_new(items, "a") == 1

    def test_two_new_items(self):
        items = self._items(["newest", "new", "a"])
        assert WebSensor._count_new(items, "a") == 2

    def test_bookmark_not_found_returns_all(self):
        items = self._items(["x", "y", "z"])
        assert WebSensor._count_new(items, "old-bookmark") == 3

    def test_empty_items(self):
        assert WebSensor._count_new([], "some-uid") == 0


# ---------------------------------------------------------------------------
# State persistence tests
# ---------------------------------------------------------------------------


class TestStatePersistence:
    @pytest.mark.asyncio
    async def test_save_and_load_roundtrip(self, sensor, tmp_path):
        await sensor.initialize()

        url = "https://example.com/feed.xml"
        sensor._feed_states[url].last_seen_uid = "https://example.com/post-99"
        sensor._feed_states[url].last_checked = 1741500000.0

        sensor._save_state()

        # Fresh sensor from same config
        sensor2 = WebSensor(sensor.config)
        await sensor2.initialize()

        assert sensor2._feed_states[url].last_seen_uid == "https://example.com/post-99"
        assert sensor2._feed_states[url].last_checked == 1741500000.0

    @pytest.mark.asyncio
    async def test_load_state_missing_file_is_silent(self, sensor, tmp_path):
        await sensor.initialize()
        for state in sensor._feed_states.values():
            assert state.last_seen_uid == ""
            assert state.last_checked == 0.0

    @pytest.mark.asyncio
    async def test_load_state_ignores_unknown_urls(self, sensor, tmp_path):
        await sensor.initialize()
        state_file = Path(sensor.config.state.dir) / "web_sensor_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "https://unknown.example.com/rss": {
                        "last_seen_uid": "uid-x",
                        "last_checked": 123.0,
                    }
                }
            )
        )
        sensor._load_state()
        for state in sensor._feed_states.values():
            assert state.last_seen_uid == ""


# ---------------------------------------------------------------------------
# Read cycle tests
# ---------------------------------------------------------------------------


class TestReadCycle:
    def _make_mock_poll(self, items_by_url):
        async def _poll(url, max_items, timeout):
            val = items_by_url.get(url)
            if isinstance(val, Exception):
                raise val
            return val if val is not None else []

        return _poll

    @pytest.mark.asyncio
    async def test_all_feeds_inside_interval_skipped(self, sensor, tmp_path):
        await sensor.initialize()
        now = time.time()
        for state in sensor._feed_states.values():
            state.last_checked = now - 60  # 1 min ago, interval=30 min

        result = await sensor.read()
        assert result["new_content"] is False
        assert result["feeds_skipped"] == 2
        assert result["feeds_checked"] == 0

    @pytest.mark.asyncio
    async def test_new_content_detected(self, sensor, tmp_path):
        await sensor.initialize()
        url = "https://example.com/feed.xml"
        url2 = "https://example.com/blog.rss"
        sensor._feed_states[url].last_seen_uid = "https://example.com/post-1"
        sensor._feed_states[url].last_checked = 0.0
        sensor._feed_states[url2].last_checked = 0.0

        new_items = [
            {"uid": "https://example.com/post-3", "pub": ""},
            {"uid": "https://example.com/post-2", "pub": ""},
            {"uid": "https://example.com/post-1", "pub": ""},
        ]

        with patch.object(
            sensor, "_poll_feed",
            side_effect=self._make_mock_poll({url: new_items, url2: []}),
        ):
            result = await sensor.read()

        assert result["new_content"] is True
        assert result["new_items_count"] == 2
        assert result["feeds_checked"] == 2
        assert sensor._feed_states[url].last_seen_uid == "https://example.com/post-3"

    @pytest.mark.asyncio
    async def test_first_run_no_new_content(self, sensor, tmp_path):
        await sensor.initialize()
        url = "https://example.com/feed.xml"
        url2 = "https://example.com/blog.rss"
        sensor._feed_states[url].last_checked = 0.0
        sensor._feed_states[url2].last_checked = 0.0

        fresh_items = [{"uid": "uid-a", "pub": ""}, {"uid": "uid-b", "pub": ""}]

        with patch.object(
            sensor, "_poll_feed",
            side_effect=self._make_mock_poll({url: fresh_items, url2: fresh_items}),
        ):
            result = await sensor.read()

        assert result["new_content"] is False
        assert result["new_items_count"] == 0
        assert result["feeds_checked"] == 2

    @pytest.mark.asyncio
    async def test_errored_feed_counted_separately(self, sensor, tmp_path):
        await sensor.initialize()
        url = "https://example.com/feed.xml"
        url2 = "https://example.com/blog.rss"
        sensor._feed_states[url].last_checked = 0.0
        sensor._feed_states[url2].last_checked = 0.0

        with patch.object(
            sensor, "_poll_feed",
            side_effect=self._make_mock_poll(
                {url: ConnectionError("timeout"), url2: []}
            ),
        ):
            result = await sensor.read()

        assert result["feeds_errored"] == 1
        assert result["feeds_checked"] == 1
        assert result["new_content"] is False

    @pytest.mark.asyncio
    async def test_empty_feed_does_not_crash(self, sensor, tmp_path):
        await sensor.initialize()
        for state in sensor._feed_states.values():
            state.last_checked = 0.0

        with patch.object(
            sensor, "_poll_feed",
            side_effect=self._make_mock_poll({}),
        ):
            result = await sensor.read()

        assert result["new_content"] is False
        assert result["new_items_count"] == 0

    @pytest.mark.asyncio
    async def test_state_persisted_after_read(self, sensor, tmp_path):
        await sensor.initialize()
        url = "https://example.com/feed.xml"
        sensor._feed_states[url].last_seen_uid = "uid-old"
        sensor._feed_states[url].last_checked = 0.0
        sensor._feed_states["https://example.com/blog.rss"].last_checked = time.time()

        new_items = [{"uid": "uid-new", "pub": ""}, {"uid": "uid-old", "pub": ""}]

        with patch.object(
            sensor, "_poll_feed",
            side_effect=self._make_mock_poll({url: new_items}),
        ):
            await sensor.read()

        state_file = Path(sensor.config.state.dir) / "web_sensor_state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data[url]["last_seen_uid"] == "uid-new"


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_state_dir(self, sensor, tmp_path):
        await sensor.initialize()
        assert Path(sensor.config.state.dir).exists()

    @pytest.mark.asyncio
    async def test_stop_saves_state(self, sensor, tmp_path):
        await sensor.initialize()
        url = "https://example.com/feed.xml"
        sensor._feed_states[url].last_seen_uid = "saved-on-stop"
        await sensor.stop()
        state_file = Path(sensor.config.state.dir) / "web_sensor_state.json"
        data = json.loads(state_file.read_text())
        assert data[url]["last_seen_uid"] == "saved-on-stop"

    @pytest.mark.asyncio
    async def test_no_feeds_configured_read_returns_empty(self, tmp_path):
        config = PulseConfig()
        config.sensors.web = WebSensorConfig(enabled=True, feeds=[])
        config.state.dir = str(tmp_path / "state")
        s = WebSensor(config)
        await s.initialize()
        result = await s.read()
        assert result["new_content"] is False
        assert result["feeds_checked"] == 0


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_default_web_config(self):
        config = PulseConfig()
        assert config.sensors.web.enabled is False
        assert config.sensors.web.feeds == []
        assert config.sensors.web.check_interval_minutes == 30
        assert config.sensors.web.max_items_per_feed == 20
        assert config.sensors.web.request_timeout == 10

    def test_yaml_parsing(self, tmp_path):
        yaml_content = """\
sensors:
  web:
    enabled: true
    feeds:
      - https://hnrss.org/frontpage
      - https://blog.langchain.dev/rss/
    check_interval_minutes: 15
    max_items_per_feed: 10
    request_timeout: 8
"""
        cfg_file = tmp_path / "pulse.yaml"
        cfg_file.write_text(yaml_content)
        config = PulseConfig.load(str(cfg_file))
        assert config.sensors.web.enabled is True
        assert len(config.sensors.web.feeds) == 2
        assert "hnrss.org" in config.sensors.web.feeds[0]
        assert config.sensors.web.check_interval_minutes == 15
        assert config.sensors.web.max_items_per_feed == 10
        assert config.sensors.web.request_timeout == 8


# ---------------------------------------------------------------------------
# Drive engine integration
# ---------------------------------------------------------------------------


class TestDriveEngineIntegration:
    def _make_engine(self):
        config = PulseConfig()
        td = tempfile.mkdtemp()
        config.state.dir = td
        sp = StatePersistence(config)
        engine = DriveEngine(config, sp)
        engine.drives["curiosity"] = Drive(
            name="curiosity", category="curiosity", weight=1.0
        )
        return engine

    def test_new_content_spikes_curiosity(self):
        engine = self._make_engine()
        before = engine.drives["curiosity"].pressure

        sensor_data = {"web": {"new_content": True, "new_items_count": 3}}
        engine._apply_sensor_spikes(sensor_data)

        after = engine.drives["curiosity"].pressure
        assert after > before

    def test_no_new_content_no_spike(self):
        engine = self._make_engine()
        before = engine.drives["curiosity"].pressure

        sensor_data = {"web": {"new_content": False, "new_items_count": 0}}
        engine._apply_sensor_spikes(sensor_data)

        after = engine.drives["curiosity"].pressure
        assert after == before

    def test_missing_web_data_no_crash(self):
        engine = self._make_engine()
        before = engine.drives["curiosity"].pressure

        # No web key at all in sensor data
        engine._apply_sensor_spikes({"filesystem": {"changes": []}})

        after = engine.drives["curiosity"].pressure
        assert after == before
