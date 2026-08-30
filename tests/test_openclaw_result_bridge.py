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


@pytest.mark.asyncio
async def test_openclaw_turn_result_marks_growth_item_awaiting_lisa(monkeypatch, tmp_path):
    from pulse.src.core.health import HealthServer

    material_path = tmp_path / "growth-material.json"
    material_path.write_text(
        '{"items":[{"id":"g1","status":"candidate","title":"one question"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr("pulse.src.core.health.GROWTH_MATERIAL_PATH", material_path)
    daemon = SimpleNamespace(
        config=SimpleNamespace(openclaw=SimpleNamespace(webhook_token="secret")),
        start_time=0,
        mutator=SimpleNamespace(get_state=lambda: {}),
    )
    server = HealthServer(daemon, port=9796)

    response = await server._handle_openclaw_turn_result(
        FakeRequest(
            {
                "kind": "pulse.conversation.growth:g1",
                "runId": "run-growth-1",
                "status": "ok",
                "outputText": "Я написал Лисе и задал один вопрос.",
            }
        )
    )

    payload = json.loads(response.text)
    item = json.loads(material_path.read_text(encoding="utf-8"))["items"][0]
    assert response.status == 200
    assert payload["conversation"]["status"] == "awaiting_lisa"
    assert item["status"] == "awaiting_lisa"
    assert item["offered_run_id"] == "run-growth-1"


def test_openclaw_webhook_adds_result_callback_for_emotions(monkeypatch):
    from pulse.src.core.config import PulseConfig
    from pulse.src.core.webhook import OpenClawWebhook

    cfg = PulseConfig()
    cfg.openclaw.webhook_token = "secret"
    cfg.daemon.health_port = 9720
    hook = OpenClawWebhook(cfg)

    assert hook._result_callback_kind("EMOTIONAL LANDSCAPE\n- Mode: write_diary_note") == "pulse.emotions.write_diary_note"
    assert hook._result_callback_kind(
        "GROWTH CONVERSATION\nPULSE_CONVERSATION_CALLBACK_KIND=pulse.conversation.growth:g1"
    ) == "pulse.conversation.growth:g1"
    assert hook._result_callback_kind("HEALTH DAILY CHECK") is None
