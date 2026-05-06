import asyncio
import json
from pathlib import Path

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import ConversationSensor


def _message_line(ts_ms: int) -> str:
    return json.dumps({"type": "message", "message": {"role": "user", "timestamp": ts_ms}})


def _runtime_context(sender_id: str = "312058326") -> str:
    return json.dumps({
        "type": "custom_message",
        "customType": "openclaw.runtime-context",
        "content": json.dumps({"sender_id": sender_id}),
    })


def test_conversation_sensor_updates_lisa_state_for_fresh_lisa_message(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    now = 1_778_080_000.0
    lisa_ts = now - 30
    session.write_text("\n".join([_message_line(int(lisa_ts * 1000)), _runtime_context()]), encoding="utf-8")

    writer = tmp_path / "update-lisa-state.mjs"
    writer.write_text("", encoding="utf-8")
    state = tmp_path / "lisa-state.json"
    state.write_text(json.dumps({"last_human_signal_at": "2026-05-06T17:00:00+03:00"}), encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("pulse.src.sensors.manager.subprocess.run", fake_run)
    monkeypatch.setattr("pulse.src.sensors.manager.time.time", lambda: now)

    sensor = ConversationSensor(PulseConfig())
    sensor.LISA_STATE_WRITER = writer
    sensor.LISA_STATE_PATH = state
    sensor._latest_session_file = lambda: session

    result = asyncio.run(sensor.read())

    assert result["active"] is True
    assert calls
    assert calls[0][:3] == ["node", str(writer), "--awake-from-message"]
    assert "--signal-at" in calls[0]


def test_conversation_sensor_decays_lisa_state_only_outside_cooldown(tmp_path, monkeypatch):
    writer = tmp_path / "update-lisa-state.mjs"
    writer.write_text("", encoding="utf-8")
    state = tmp_path / "lisa-state.json"
    state.write_text(json.dumps({"last_human_signal_at": "2026-05-06T17:00:00+03:00"}), encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("pulse.src.sensors.manager.subprocess.run", fake_run)

    sensor = ConversationSensor(PulseConfig())
    sensor.LISA_STATE_WRITER = writer
    sensor.LISA_STATE_PATH = state
    sensor._last_human_activity = 1_778_080_000.0 - 600

    sensor._maybe_decay_lisa_state_from_silence(now=1_778_080_000.0, in_cooldown=False)

    assert calls
    assert calls[0][:3] == ["node", str(writer), "--decay-from-silence"]

    calls.clear()
    sensor._maybe_decay_lisa_state_from_silence(now=1_778_080_100.0, in_cooldown=False)
    assert calls == []

    sensor._last_lisa_state_decay = 0.0
    sensor._maybe_decay_lisa_state_from_silence(now=1_778_080_200.0, in_cooldown=True)
    assert calls == []
