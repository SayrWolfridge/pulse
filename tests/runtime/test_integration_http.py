"""Integration tests for the Pulse v2 runtime HTTP surface.

Why this exists
---------------
We already have good unit coverage for individual engines (ResponseEngine,
ChannelBridge, etc). What was missing was a single end-to-end test that proves
HypostasRuntime's HTTP handlers wire everything together correctly.

This test suite validates the full path:
  HTTP POST /runtime/bridge/receive
    -> ChannelBridge.receive
    -> ResponseEngine.respond (with Ollama client mocked)
    -> JSON response

And the "deliver" path:
  HTTP POST /runtime/bridge/receive {deliver:true}
    -> ChannelBridge.receive_and_send
    -> LocalHandler capture + bridge stats update

The goal is deterministic, offline tests (no live Ollama required).
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pulse.src.runtime import HypostasRuntime


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_post_json(url: str, payload: dict, timeout: float = 2.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _http_get_json(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


@pytest.fixture
def started_rt(tmp_path: Path):
    port = _free_port()
    r = HypostasRuntime(state_dir=tmp_path, port=port)

    # Patch Ollama client so ResponseEngine is deterministic and offline.
    mock_client = MagicMock()
    mock_client.model = "iris-70b-mock"
    mock_client.available.return_value = True
    mock_client.chat.return_value = ("Mock reply.", 2)
    r.response._client = mock_client

    r.start()
    time.sleep(0.1)
    yield r
    if r._running:
        r.stop()


class TestIntegrationHTTP:
    def test_bridge_receive_http_routes_to_response_engine(self, started_rt: HypostasRuntime):
        base = f"http://127.0.0.1:{started_rt._port}"

        # Baseline stats
        st0 = _http_get_json(base + "/runtime/bridge/status")
        assert st0["stats"]["inbound_count"] == 0
        assert "local" in st0["channels"]

        # Receive a message (deliver=false)
        resp = _http_post_json(
            base + "/runtime/bridge/receive",
            {"message": "Hello", "person": "josh", "channel": "local", "deliver": False},
        )
        assert resp["text"] == "Mock reply."
        assert resp["person"] == "josh"
        assert resp["channel"] == "local"

        st1 = _http_get_json(base + "/runtime/bridge/status")
        assert st1["stats"]["inbound_count"] == 1
        assert st1["stats"]["inbound_failures"] == 0

    def test_bridge_receive_http_deliver_sends_via_local_handler(self, started_rt: HypostasRuntime):
        base = f"http://127.0.0.1:{started_rt._port}"

        # Receive+send (deliver=true)
        resp = _http_post_json(
            base + "/runtime/bridge/receive",
            {"message": "Ping", "person": "josh", "channel": "local", "deliver": True},
        )
        assert resp["text"] == "Mock reply."
        assert resp["person"] == "josh"
        assert resp["channel"] == "local"
        assert resp["delivered"] is True

        # LocalHandler captured the outbound message
        assert len(started_rt.channel_bridge.local.sent) >= 1
        last = started_rt.channel_bridge.local.sent[-1]
        assert getattr(last, "message") == "Mock reply."
        assert getattr(last, "person") == "josh"

        st = _http_get_json(base + "/runtime/bridge/status")
        assert st["stats"]["inbound_count"] >= 1
        assert st["stats"]["outbound_count"] >= 1
