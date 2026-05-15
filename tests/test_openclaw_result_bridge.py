import json
from types import SimpleNamespace

import pytest


class FakeRequest:
    remote = "127.0.0.1"

    def __init__(self, payload, *, token="secret"):
        self._payload = payload
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_openclaw_turn_result_saves_emotions_reply(monkeypatch, tmp_path):
    from pulse.src.core.health import HealthServer

    daemon = SimpleNamespace(
        config=SimpleNamespace(openclaw=SimpleNamespace(webhook_token="secret")),
        start_time=0,
        mutator=SimpleNamespace(get_state=lambda: {}),
    )
    server = HealthServer(daemon, port=9798)

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "ok", "thought_file": str(tmp_path / "thought.md")}),
            stderr="",
        )

    monkeypatch.setattr("pulse.src.core.health.subprocess.run", fake_run)
    monkeypatch.setattr("pulse.src.core.health.Path.exists", lambda self: True)

    response = await server._handle_openclaw_turn_result(
        FakeRequest(
            {
                "kind": "pulse.emotions.write_diary_note",
                "runId": "run-1",
                "status": "ok",
                "summary": "summary fallback",
                "outputText": "exact visible reply",
            }
        )
    )

    assert response.status == 200
    assert calls
    assert calls[0][1]["input"] == "exact visible reply\n"


@pytest.mark.asyncio
async def test_openclaw_turn_result_rejects_bad_token():
    from pulse.src.core.health import HealthServer

    daemon = SimpleNamespace(
        config=SimpleNamespace(openclaw=SimpleNamespace(webhook_token="secret")),
        start_time=0,
        mutator=SimpleNamespace(get_state=lambda: {}),
    )
    server = HealthServer(daemon, port=9797)

    response = await server._handle_openclaw_turn_result(
        FakeRequest(
            {
                "kind": "pulse.emotions.write_diary_note",
                "runId": "run-1",
                "status": "ok",
                "outputText": "exact visible reply",
            },
            token="wrong",
        )
    )

    assert response.status == 401


def test_openclaw_webhook_adds_result_callback_for_emotions(monkeypatch):
    from pulse.src.core.config import PulseConfig
    from pulse.src.core.webhook import OpenClawWebhook

    cfg = PulseConfig()
    cfg.openclaw.webhook_token = "secret"
    cfg.daemon.health_port = 9720
    hook = OpenClawWebhook(cfg)

    assert hook._result_callback_kind("EMOTIONAL LANDSCAPE\n- Mode: write_diary_note") == "pulse.emotions.write_diary_note"
    assert hook._result_callback_kind("HEALTH DAILY CHECK") is None
