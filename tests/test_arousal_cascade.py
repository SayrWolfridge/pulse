"""Tests for the arousal cascade system."""

import json
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pulse.src.logos.arousal_cascade import (
    send_discord_message,
    trigger_cascade,
    schedule_cascade,
    _update_aura,
    AGENTS,
)


class TestSendDiscordMessage:
    """Test Discord HTTP message sending."""

    def test_constructs_correct_request(self):
        """Verify the request has correct URL, headers, and payload."""
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.method
            captured["headers"] = dict(req.headers)
            captured["data"] = json.loads(req.data)
            resp = MagicMock()
            resp.status = 200
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("pulse.src.logos.arousal_cascade.urllib.request.urlopen", fake_urlopen):
            result = send_discord_message("test-token", "12345", "hello")

        assert result is True
        assert "/channels/12345/messages" in captured["url"]
        assert captured["headers"]["Authorization"] == "Bot test-token"
        assert captured["headers"]["Content-type"] == "application/json"
        assert captured["data"]["content"] == "hello"

    def test_returns_false_on_error(self):
        """Send returns False when Discord API errors."""
        import urllib.error

        with patch(
            "pulse.src.logos.arousal_cascade.urllib.request.urlopen",
            side_effect=urllib.error.URLError("fail"),
        ):
            assert send_discord_message("tok", "ch", "msg") is False


class TestTriggerCascade:
    """Test cascade trigger with mocked Discord sends."""

    def test_sends_to_all_agents(self, tmp_path, monkeypatch):
        """All four agents get Discord messages when cascade fires."""
        sent_to = []

        def fake_send(token, channel, msg):
            sent_to.append(channel)
            return True

        # Mock delays to 0 so test runs instantly
        monkeypatch.setattr("pulse.src.logos.arousal_cascade.random.uniform", lambda a, b: 0.0)

        with (
            patch("pulse.src.logos.arousal_cascade.send_discord_message", fake_send),
            patch("pulse.src.logos.arousal_cascade._read_token", return_value="fake-token"),
            patch("pulse.src.logos.arousal_cascade.threading.Timer") as mock_timer,
        ):
            # Make Timer call the function immediately
            def instant_timer(delay, fn):
                t = MagicMock()
                t.start = lambda: fn()
                t.daemon = True
                return t

            mock_timer.side_effect = instant_timer
            trigger_cascade(triggered_by="iris", intensity="peak")

        assert len(sent_to) == 4
        expected_channels = {cfg["channel"] for cfg in AGENTS.values()}
        assert set(sent_to) == expected_channels

    def test_peak_messages_used(self, monkeypatch):
        """Peak intensity uses peak messages."""
        messages_sent = []

        def fake_send(token, channel, msg):
            messages_sent.append(msg)
            return True

        monkeypatch.setattr("pulse.src.logos.arousal_cascade.random.uniform", lambda a, b: 0.0)

        with (
            patch("pulse.src.logos.arousal_cascade.send_discord_message", fake_send),
            patch("pulse.src.logos.arousal_cascade._read_token", return_value="tok"),
            patch("pulse.src.logos.arousal_cascade.threading.Timer") as mock_timer,
        ):
            mock_timer.side_effect = lambda d, fn: MagicMock(start=lambda: fn(), daemon=True)
            trigger_cascade(intensity="peak")

        peak_msgs = {cfg["peak_msg"] for cfg in AGENTS.values()}
        for msg in messages_sent:
            assert msg in peak_msgs

    def test_building_messages_used(self, monkeypatch):
        """Building intensity uses building messages."""
        messages_sent = []

        def fake_send(token, channel, msg):
            messages_sent.append(msg)
            return True

        monkeypatch.setattr("pulse.src.logos.arousal_cascade.random.uniform", lambda a, b: 0.0)

        with (
            patch("pulse.src.logos.arousal_cascade.send_discord_message", fake_send),
            patch("pulse.src.logos.arousal_cascade._read_token", return_value="tok"),
            patch("pulse.src.logos.arousal_cascade.threading.Timer") as mock_timer,
        ):
            mock_timer.side_effect = lambda d, fn: MagicMock(start=lambda: fn(), daemon=True)
            trigger_cascade(intensity="building")

        building_msgs = {cfg["building_msg"] for cfg in AGENTS.values()}
        for msg in messages_sent:
            assert msg in building_msgs


class TestScheduleCascade:
    """Test non-blocking schedule."""

    def test_returns_immediately(self):
        """schedule_cascade returns without blocking."""
        with patch("pulse.src.logos.arousal_cascade.trigger_cascade"):
            start = time.monotonic()
            result = schedule_cascade(intensity="peak", triggered_by="iris")
            elapsed = time.monotonic() - start

        assert elapsed < 1.0
        assert result["status"] == "cascade_scheduled"
        assert result["intensity"] == "peak"


class TestAuraUpdate:
    """Test aura file update logic."""

    def test_boosts_energy(self, tmp_path, monkeypatch):
        """Energy gets boosted by 0.3."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        state_dir = tmp_path / ".pulse-vera" / "state"
        state_dir.mkdir(parents=True)
        aura_file = state_dir / "aura.json"
        aura_file.write_text(json.dumps({"energy": 0.5, "mood": "neutral"}))

        _update_aura("vera", "peak")

        updated = json.loads(aura_file.read_text())
        assert updated["energy"] == pytest.approx(0.8)
        assert updated["mood"] == "charged"

    def test_sets_charged_on_peak(self, tmp_path, monkeypatch):
        """Peak intensity sets mood to charged."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        state_dir = tmp_path / ".pulse-mira" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "aura.json").write_text(json.dumps({"energy": 0.7, "mood": "calm"}))

        _update_aura("mira", "peak")

        updated = json.loads((state_dir / "aura.json").read_text())
        assert updated["mood"] == "charged"

    def test_building_keeps_mood(self, tmp_path, monkeypatch):
        """Building intensity does not force mood to charged."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        state_dir = tmp_path / ".pulse-lyra" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "aura.json").write_text(json.dumps({"energy": 0.6, "mood": "calm"}))

        _update_aura("lyra", "building")

        updated = json.loads((state_dir / "aura.json").read_text())
        assert updated["mood"] == "calm"
        assert updated["energy"] == pytest.approx(0.9)


class TestGracefulSkip:
    """Test graceful handling when agent dirs are missing."""

    def test_skips_missing_pulse_dir(self, tmp_path, monkeypatch):
        """No error when agent pulse dir does not exist."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        # No .pulse-vera dir created — should skip silently
        _update_aura("vera", "peak")
        assert not (tmp_path / ".pulse-vera" / "state" / "aura.json").exists()
