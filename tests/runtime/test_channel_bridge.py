"""Tests for ChannelBridge — Pulse v2 Day 17.

We test the bridge without any external messaging integration:
- LocalHandler captures outbound messages in-memory.
- ResponseEngine is real but has its Ollama client patched.

Surfaces:
- Channel registration + handler resolution
- Inbound receive() routes to ResponseEngine
- receive_and_send() sends via channel
- Outbound send() records episodic trace (non-fatal)
- deliver_proactive() runs dispatcher + sends
- status() shape
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pulse.src.runtime import HypostasRuntime


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def started_rt(tmp_path: Path):
    r = HypostasRuntime(state_dir=tmp_path, port=_free_port())

    # Patch Ollama client so ResponseEngine is deterministic and offline.
    mock_client = MagicMock()
    mock_client.model = "iris-70b"
    mock_client.available.return_value = True
    mock_client.chat.return_value = ("Mock reply.", 2)
    r.response._client = mock_client

    r.start()
    time.sleep(0.05)
    yield r
    if r._running:
        r.stop()


class TestChannelBridge:
    def test_bridge_present_on_runtime(self, started_rt):
        assert hasattr(started_rt, "channel_bridge")

    def test_default_local_handler_registered(self, started_rt):
        st = started_rt.channel_bridge.status()
        assert "local" in st["channels"]

    def test_register_channel_adds_handler(self, started_rt):
        handler = started_rt.channel_bridge.local  # reuse local
        started_rt.channel_bridge.register_channel("local2", handler)
        assert "local2" in started_rt.channel_bridge.handlers()

    def test_receive_routes_to_response_engine(self, started_rt):
        text = started_rt.channel_bridge.receive("Hello", person="josh", channel="local")
        assert text == "Mock reply."

    def test_receive_rejects_empty(self, started_rt):
        with pytest.raises(ValueError):
            started_rt.channel_bridge.receive("   ")

    def test_send_delivers_to_local_handler(self, started_rt):
        ok = started_rt.channel_bridge.send("Hi", channel="local", person="josh")
        assert ok is True
        assert len(started_rt.channel_bridge.local.sent) >= 1

    def test_send_unknown_channel_raises(self, started_rt):
        with pytest.raises(ValueError):
            started_rt.channel_bridge.send("x", channel="does_not_exist")

    def test_receive_and_send_round_trip(self, started_rt):
        before = len(started_rt.channel_bridge.local.sent)
        result = started_rt.channel_bridge.receive_and_send("Ping", person="josh", channel="local")
        assert result["delivered"] is True
        assert result["text"] == "Mock reply."
        assert len(started_rt.channel_bridge.local.sent) == before + 1

    def test_deliver_proactive_returns_204_when_no_candidate(self, started_rt):
        # Force ProactiveEngine to have no candidates
        started_rt.proactive._candidates_cache = []
        started_rt.proactive._cache_ts = time.time()
        started_rt.proactive.top_candidate = MagicMock(return_value=None)

        res = started_rt.channel_bridge.deliver_proactive(person="josh")
        assert res["dispatched"] is False

    def test_deliver_proactive_sends_when_candidate_exists(self, started_rt):
        # Make dispatcher return a synthetic dispatch
        fake = MagicMock()
        fake.dispatched = True
        fake.text = "Proactive hello."
        fake.kind = "morning_checkin"
        fake.fallback = False
        fake.episode_id = "ep1"
        fake.elapsed_ms = 10
        fake.error = None

        started_rt.dispatcher.dispatch = MagicMock(return_value=fake)

        before = len(started_rt.channel_bridge.local.sent)
        res = started_rt.channel_bridge.deliver_proactive(person="josh")
        assert res["dispatched"] is True
        assert res["delivered"] is True
        assert "Proactive hello." in res["text"]
        assert len(started_rt.channel_bridge.local.sent) == before + 1

    def test_openclaw_channel_registered(self, started_rt):
        assert "openclaw" in started_rt.channel_bridge.handlers()

    def test_openclaw_handler_queues_to_state(self, started_rt):
        ok = started_rt.channel_bridge.send("Hello from bridge", channel="openclaw", person="josh")
        assert ok is True
        queue = started_rt.state.get("proactive.openclaw_outbound")
        assert isinstance(queue, list)
        assert len(queue) >= 1
        entry = queue[-1]
        assert entry["text"] == "Hello from bridge"
        assert entry["person"] == "josh"
        assert entry["status"] == "pending"

    def test_openclaw_handler_appends_multiple(self, started_rt):
        started_rt.channel_bridge.send("msg1", channel="openclaw", person="josh")
        started_rt.channel_bridge.send("msg2", channel="openclaw", person="josh")
        queue = started_rt.state.get("proactive.openclaw_outbound")
        assert len(queue) >= 2

    def test_status_json_serialisable(self, started_rt):
        st = started_rt.channel_bridge.status()
        json.dumps(st)
