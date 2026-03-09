"""Tests for PEER_SYNC — multi-agent coordination module."""

import json
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pulse.src.peer_sync import (
    PeerInfo,
    PeerSync,
    _fetch_json,
    get_status,
    init,
    get_instance,
    STALE_SECONDS,
)


class TestPeerInfo(unittest.TestCase):
    """PeerInfo dataclass defaults and construction."""

    def test_defaults(self):
        p = PeerInfo(name="scout", url="http://localhost:9721")
        self.assertEqual(p.name, "scout")
        self.assertFalse(p.reachable)
        self.assertEqual(p.mood, "unknown")
        self.assertEqual(p.energy, 1.0)
        self.assertFalse(p.available)
        self.assertEqual(p.drives, {})
        self.assertEqual(p.consecutive_failures, 0)

    def test_custom_values(self):
        p = PeerInfo(name="edge", url="http://10.0.0.5:9720", role="trader", reachable=True)
        self.assertEqual(p.role, "trader")
        self.assertTrue(p.reachable)

    def test_drive_dict_mutable(self):
        p = PeerInfo(name="a", url="http://x")
        p.drives["curiosity"] = 3.5
        self.assertEqual(p.drives["curiosity"], 3.5)


class TestPeerSyncInit(unittest.TestCase):
    """Constructor and peer registry behavior."""

    def test_empty_config(self):
        ps = PeerSync(peers_config=[])
        self.assertEqual(ps.get_peer_names(), [])

    def test_valid_peers_registered(self):
        ps = PeerSync(peers_config=[
            {"name": "scout", "url": "http://localhost:9721", "token": "tok1", "role": "researcher"},
            {"name": "edge", "url": "http://localhost:9722"},
        ])
        self.assertEqual(sorted(ps.get_peer_names()), ["edge", "scout"])

    def test_invalid_peers_skipped(self):
        ps = PeerSync(peers_config=[
            {"name": "", "url": "http://localhost:9721"},  # missing name
            {"name": "ok", "url": ""},  # missing url
            {"name": "valid", "url": "http://localhost:9723"},
        ])
        self.assertEqual(ps.get_peer_names(), ["valid"])

    def test_poll_interval(self):
        ps = PeerSync(peers_config=[], poll_interval=120)
        self.assertEqual(ps._poll_interval, 120)

    def test_should_poll_initially_true(self):
        ps = PeerSync(peers_config=[])
        self.assertTrue(ps.should_poll())

    def test_should_poll_false_after_poll(self):
        ps = PeerSync(peers_config=[], poll_interval=60)
        ps._last_poll = time.time()
        self.assertFalse(ps.should_poll())


class TestFetchJson(unittest.TestCase):
    """HTTP helper _fetch_json."""

    @patch("pulse.src.peer_sync.urllib.request.urlopen")
    def test_successful_fetch(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"version": "0.5.3"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _fetch_json("http://localhost:9720/status")
        self.assertEqual(result, {"version": "0.5.3"})

    @patch("pulse.src.peer_sync.urllib.request.urlopen", side_effect=OSError("timeout"))
    def test_failed_fetch_returns_none(self, _):
        result = _fetch_json("http://unreachable:9720/status")
        self.assertIsNone(result)

    @patch("pulse.src.peer_sync.urllib.request.urlopen")
    def test_invalid_json_returns_none(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _fetch_json("http://localhost:9720/status")
        self.assertIsNone(result)


class TestPollPeer(unittest.TestCase):
    """Polling individual peers and updating PeerInfo."""

    def _make_sync(self):
        return PeerSync(
            peers_config=[{"name": "scout", "url": "http://localhost:9721", "token": "tok1"}],
            poll_interval=60,
        )

    @patch("pulse.src.peer_sync._fetch_json")
    def test_successful_poll_dict_drives(self, mock_fetch):
        mock_fetch.return_value = {
            "version": "0.5.3",
            "uptime_seconds": 3600.0,
            "drives": {"curiosity": 2.5, "goals": 1.2, "system": 0.8},
            "aura": {"mood": "focused", "energy": 0.9, "available": True, "focus": 0.85},
        }
        ps = self._make_sync()
        ps._save = MagicMock()  # skip file writes in test
        ps.poll_all()

        peer = ps._peers["scout"]
        self.assertTrue(peer.reachable)
        self.assertEqual(peer.version, "0.5.3")
        self.assertEqual(peer.top_drive, "curiosity")
        self.assertAlmostEqual(peer.top_pressure, 2.5)
        self.assertEqual(peer.mood, "focused")
        self.assertAlmostEqual(peer.energy, 0.9)
        self.assertTrue(peer.available)
        self.assertEqual(peer.consecutive_failures, 0)

    @patch("pulse.src.peer_sync._fetch_json")
    def test_successful_poll_list_drives(self, mock_fetch):
        mock_fetch.return_value = {
            "version": "0.5.0",
            "drives": [
                {"name": "goals", "pressure": 4.1},
                {"name": "curiosity", "pressure": 1.0},
            ],
        }
        ps = self._make_sync()
        ps._save = MagicMock()
        ps.poll_all()

        peer = ps._peers["scout"]
        self.assertTrue(peer.reachable)
        self.assertEqual(peer.top_drive, "goals")
        self.assertAlmostEqual(peer.top_pressure, 4.1)

    @patch("pulse.src.peer_sync._fetch_json", return_value=None)
    def test_unreachable_peer(self, _):
        ps = self._make_sync()
        ps._save = MagicMock()
        ps.poll_all()

        peer = ps._peers["scout"]
        self.assertFalse(peer.reachable)
        self.assertEqual(peer.consecutive_failures, 1)
        self.assertIn("unreachable", peer.last_error)

    @patch("pulse.src.peer_sync._fetch_json", return_value=None)
    def test_consecutive_failures_increment(self, _):
        ps = self._make_sync()
        ps._save = MagicMock()
        ps.poll_all()
        ps.poll_all()
        ps.poll_all()

        peer = ps._peers["scout"]
        self.assertEqual(peer.consecutive_failures, 3)

    @patch("pulse.src.peer_sync._fetch_json")
    def test_recovery_resets_failures(self, mock_fetch):
        ps = self._make_sync()
        ps._save = MagicMock()

        # First: unreachable
        mock_fetch.return_value = None
        ps.poll_all()
        self.assertEqual(ps._peers["scout"].consecutive_failures, 1)

        # Second: recovered
        mock_fetch.return_value = {"version": "0.5.3", "drives": {}}
        ps.poll_all()
        self.assertEqual(ps._peers["scout"].consecutive_failures, 0)
        self.assertTrue(ps._peers["scout"].reachable)

    @patch("pulse.src.peer_sync._fetch_json")
    def test_aura_fallback_to_top_level(self, mock_fetch):
        """When peer doesn't have aura key, read mood/energy from top level."""
        mock_fetch.return_value = {
            "version": "0.5.0",
            "mood": "content",
            "energy": 0.7,
            "available": False,
            "focus": 0.3,
            "drives": {},
        }
        ps = self._make_sync()
        ps._save = MagicMock()
        ps.poll_all()

        peer = ps._peers["scout"]
        self.assertEqual(peer.mood, "content")
        self.assertAlmostEqual(peer.energy, 0.7)
        self.assertFalse(peer.available)


class TestThalamusInjection(unittest.TestCase):
    """THALAMUS signal injection from peer state."""

    def _make_sync_with_peer(self, **overrides):
        ps = PeerSync(
            peers_config=[{"name": "scout", "url": "http://localhost:9721"}],
        )
        peer = ps._peers["scout"]
        peer.reachable = True
        peer.last_seen = time.time()
        peer.available = True
        peer.top_pressure = 1.0
        peer.top_drive = "curiosity"
        peer.mood = "focused"
        peer.energy = 0.8
        peer.focus = 0.7
        peer.role = "researcher"
        for k, v in overrides.items():
            setattr(peer, k, v)
        return ps

    @patch("pulse.src.thalamus.append")
    def test_available_peer_injects_signal(self, mock_append):
        ps = self._make_sync_with_peer()
        ps.inject_thalamus_signals()

        calls = mock_append.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertIn("peer_available", types)
        self.assertIn("peer_mood_shift", types)

    @patch("pulse.src.thalamus.append")
    def test_busy_peer_injects_busy_signal(self, mock_append):
        ps = self._make_sync_with_peer(top_pressure=4.5)
        ps.inject_thalamus_signals()

        calls = mock_append.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertIn("peer_busy", types)
        # Should NOT inject peer_available when pressure >= 3.0
        self.assertNotIn("peer_available", types)

    @patch("pulse.src.thalamus.append")
    def test_offline_peer_injects_offline_signal(self, mock_append):
        ps = self._make_sync_with_peer(reachable=False, consecutive_failures=3)
        ps.inject_thalamus_signals()

        calls = mock_append.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertIn("peer_offline", types)

    @patch("pulse.src.thalamus.append")
    def test_stale_peer_treated_as_offline(self, mock_append):
        ps = self._make_sync_with_peer(
            last_seen=time.time() - STALE_SECONDS - 10,
            consecutive_failures=5,
        )
        ps.inject_thalamus_signals()

        calls = mock_append.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertIn("peer_offline", types)
        self.assertNotIn("peer_available", types)

    @patch("pulse.src.thalamus.append")
    def test_neutral_mood_no_contagion(self, mock_append):
        ps = self._make_sync_with_peer(mood="neutral")
        ps.inject_thalamus_signals()

        calls = mock_append.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertNotIn("peer_mood_shift", types)

    @patch("pulse.src.thalamus.append")
    def test_salience_levels(self, mock_append):
        ps = self._make_sync_with_peer(top_pressure=4.0, mood="anxious")
        ps.inject_thalamus_signals()

        for call in mock_append.call_args_list:
            signal = call[0][0]
            if signal["type"] == "peer_busy":
                self.assertEqual(signal["salience"], 0.20)
            elif signal["type"] == "peer_mood_shift":
                self.assertEqual(signal["salience"], 0.08)

    @patch("pulse.src.thalamus.append")
    def test_peer_data_in_signal(self, mock_append):
        ps = self._make_sync_with_peer(top_pressure=4.0, top_drive="goals")
        ps.inject_thalamus_signals()

        for call in mock_append.call_args_list:
            signal = call[0][0]
            if signal["type"] == "peer_busy":
                self.assertEqual(signal["data"]["peer"], "scout")
                self.assertEqual(signal["data"]["drive"], "goals")
                self.assertAlmostEqual(signal["data"]["pressure"], 4.0)
                self.assertEqual(signal["data"]["role"], "researcher")

    def test_no_thalamus_graceful(self):
        """Injection is no-op when THALAMUS isn't available."""
        ps = self._make_sync_with_peer()
        # Temporarily break the import path
        import sys
        real = sys.modules.get("pulse.src.thalamus")
        sys.modules["pulse.src.thalamus"] = None  # force ImportError
        try:
            # Should not raise
            ps.inject_thalamus_signals()
        finally:
            if real is not None:
                sys.modules["pulse.src.thalamus"] = real
            else:
                sys.modules.pop("pulse.src.thalamus", None)


class TestSerialization(unittest.TestCase):
    """Persist / load peer state."""

    def test_save_creates_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "pulse_test"
            ps = PeerSync(
                peers_config=[{"name": "edge", "url": "http://localhost:9722"}],
                state_dir=state_dir,
            )
            ps._peers["edge"].reachable = True
            ps._peers["edge"].mood = "focused"
            ps._save()

            peers_file = state_dir / "peers.json"
            self.assertTrue(peers_file.exists())
            data = json.loads(peers_file.read_text())
            self.assertIn("edge", data)
            self.assertTrue(data["edge"]["reachable"])
            self.assertEqual(data["edge"]["mood"], "focused")

    def test_save_excludes_tokens(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "pulse_test"
            ps = PeerSync(
                peers_config=[{"name": "edge", "url": "http://localhost:9722", "token": "secret123"}],
                state_dir=state_dir,
            )
            ps._save()

            data = json.loads((state_dir / "peers.json").read_text())
            # Token should NOT be in persisted state
            self.assertNotIn("token", data.get("edge", {}))
            raw_text = (state_dir / "peers.json").read_text()
            self.assertNotIn("secret123", raw_text)


class TestGetSummary(unittest.TestCase):
    """Summary output for observation API."""

    def test_empty_summary(self):
        ps = PeerSync(peers_config=[])
        s = ps.get_summary()
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["reachable"], 0)
        self.assertEqual(s["peers"], [])

    def test_summary_with_peers(self):
        ps = PeerSync(peers_config=[
            {"name": "scout", "url": "http://localhost:9721"},
            {"name": "edge", "url": "http://localhost:9722"},
        ])
        ps._peers["scout"].reachable = True
        ps._peers["scout"].last_seen = time.time()
        ps._peers["scout"].top_drive = "curiosity"
        ps._peers["scout"].top_pressure = 2.1
        ps._peers["scout"].mood = "focused"

        s = ps.get_summary()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["reachable"], 1)
        self.assertEqual(len(s["peers"]), 2)

        scout_entry = next(p for p in s["peers"] if p["name"] == "scout")
        self.assertTrue(scout_entry["reachable"])
        self.assertEqual(scout_entry["top_drive"], "curiosity")
        self.assertAlmostEqual(scout_entry["top_pressure"], 2.1, places=2)

    def test_stale_peer_not_reachable(self):
        ps = PeerSync(peers_config=[{"name": "scout", "url": "http://localhost:9721"}])
        ps._peers["scout"].reachable = True
        ps._peers["scout"].last_seen = time.time() - STALE_SECONDS - 10

        s = ps.get_summary()
        self.assertEqual(s["reachable"], 0)
        scout_entry = s["peers"][0]
        self.assertFalse(scout_entry["reachable"])


class TestModuleSingleton(unittest.TestCase):
    """Module-level singleton helpers."""

    def test_get_status_before_init(self):
        import pulse.src.peer_sync as ps_mod
        old = ps_mod._instance
        ps_mod._instance = None
        try:
            s = get_status()
            self.assertFalse(s["enabled"])
            self.assertEqual(s["total"], 0)
        finally:
            ps_mod._instance = old

    def test_init_and_get_instance(self):
        instance = init(
            peers_config=[{"name": "test", "url": "http://localhost:9999"}],
            poll_interval=30,
        )
        self.assertIsNotNone(instance)
        self.assertIs(get_instance(), instance)

        s = get_status()
        self.assertTrue(s["enabled"])
        self.assertEqual(s["total"], 1)


class TestConfigIntegration(unittest.TestCase):
    """PeersConfig dataclass in config module."""

    def test_peer_config_defaults(self):
        from pulse.src.core.config import PeerConfig, PeersConfig
        pc = PeersConfig()
        self.assertFalse(pc.enabled)
        self.assertEqual(pc.poll_interval_seconds, 60)
        self.assertEqual(pc.peers, [])

    def test_peer_config_construction(self):
        from pulse.src.core.config import PeerConfig, PeersConfig
        p = PeerConfig(name="scout", url="http://localhost:9721", token="tok", role="researcher")
        cfg = PeersConfig(enabled=True, peers=[p])
        self.assertTrue(cfg.enabled)
        self.assertEqual(len(cfg.peers), 1)
        self.assertEqual(cfg.peers[0].name, "scout")

    def test_pulse_config_has_peers(self):
        from pulse.src.core.config import PulseConfig
        cfg = PulseConfig()
        self.assertIsNotNone(cfg.peers)
        self.assertFalse(cfg.peers.enabled)


class TestMultiPeerScenarios(unittest.TestCase):
    """Multi-peer coordination edge cases."""

    @patch("pulse.src.peer_sync._fetch_json")
    def test_mixed_reachability(self, mock_fetch):
        """One peer up, one down."""
        def side_effect(url, **kwargs):
            if "9721" in url:
                return {"version": "0.5.3", "drives": {"curiosity": 2.0}}
            return None

        mock_fetch.side_effect = side_effect
        ps = PeerSync(peers_config=[
            {"name": "scout", "url": "http://localhost:9721"},
            {"name": "edge", "url": "http://localhost:9722"},
        ])
        ps._save = MagicMock()
        ps.poll_all()

        self.assertTrue(ps._peers["scout"].reachable)
        self.assertFalse(ps._peers["edge"].reachable)

        s = ps.get_summary()
        self.assertEqual(s["reachable"], 1)

    @patch("pulse.src.thalamus.append")
    @patch("pulse.src.peer_sync._fetch_json")
    def test_no_signals_for_low_failure_offline(self, mock_fetch, mock_append):
        """Peer with <3 consecutive failures shouldn't trigger peer_offline."""
        mock_fetch.return_value = None
        ps = PeerSync(peers_config=[{"name": "scout", "url": "http://localhost:9721"}])
        ps._save = MagicMock()
        ps.poll_all()  # failure #1
        ps.poll_all()  # failure #2

        ps.inject_thalamus_signals()
        types = [c[0][0]["type"] for c in mock_append.call_args_list]
        self.assertNotIn("peer_offline", types)

    @patch("pulse.src.thalamus.append")
    def test_empty_peers_no_signals(self, mock_append):
        ps = PeerSync(peers_config=[])
        ps.inject_thalamus_signals()
        mock_append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
