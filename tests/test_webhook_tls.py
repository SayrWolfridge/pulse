"""OpenClaw webhook transport and session-contract tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pulse.src.core.config import PulseConfig
from pulse.src.core.daemon import PulseDaemon
from pulse.src.core.webhook import OpenClawWebhook, _ssl_for_url
from pulse.src.metrics import PulseMetrics


class _FakeResponse:
    def __init__(self, status, json_body=None, text_body=""):
        self.status = status
        self._json_body = json_body
        self._text_body = text_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        if isinstance(self._json_body, Exception):
            raise self._json_body
        return self._json_body

    async def text(self):
        return self._text_body


class _FailingContext:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    closed = False

    def __init__(self, outcomes):
        self._outcomes = iter(outcomes)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            return _FailingContext(outcome)
        return outcome


async def _async_value(value):
    return value


def test_disables_tls_verification_for_https_loopback():
    assert _ssl_for_url("https://127.0.0.1:18789/hooks/agent") is False
    assert _ssl_for_url("https://localhost:18789/hooks/agent") is False
    assert _ssl_for_url("https://[::1]:18789/hooks/agent") is False


def test_keeps_default_tls_verification_for_non_loopback():
    assert _ssl_for_url("https://gateway.example/hooks/agent") is None
    assert _ssl_for_url("http://127.0.0.1:18789/hooks/agent") is None


def test_main_mode_uses_current_persistent_hook_contract():
    config = PulseConfig()
    config.openclaw.session_mode = "main"
    config.openclaw.session_key = "agent:main:telegram:default:direct:312058326"

    payload = OpenClawWebhook(config)._build_payload("hello", "Pulse")

    assert payload["sessionMode"] == "persistent"
    assert payload["sessionKey"] == config.openclaw.session_key
    assert payload["channel"] == "telegram"
    assert payload["to"] == "312058326"
    assert "isolated" not in payload


def test_isolated_mode_uses_current_isolated_hook_contract():
    config = PulseConfig()
    config.openclaw.session_mode = "isolated"
    config.openclaw.isolated_model = "test/model"

    payload = OpenClawWebhook(config)._build_payload("hello", "Pulse")

    assert payload["sessionMode"] == "isolated"
    assert payload["model"] == "test/model"
    assert "sessionKey" not in payload
    assert "isolated" not in payload


@pytest.mark.asyncio
async def test_timeout_replays_once_with_same_key_then_accepts(monkeypatch):
    hook = OpenClawWebhook(PulseConfig())
    session = _FakeSession(
        [asyncio.TimeoutError(), _FakeResponse(202, {"runId": "run-123"})]
    )
    monkeypatch.setattr(hook, "_get_session", lambda: _async_value(session))

    result = await hook.trigger("hello")

    assert result is True
    keys = [call[1]["headers"]["Idempotency-Key"] for call in session.calls]
    assert len(keys) == 2
    assert keys[0] == keys[1]


@pytest.mark.asyncio
async def test_explicit_rejection_is_definite_failure_without_replay(monkeypatch):
    hook = OpenClawWebhook(PulseConfig())
    session = _FakeSession([_FakeResponse(503, {"ok": False}, "not admitted")])
    monkeypatch.setattr(hook, "_get_session", lambda: _async_value(session))

    result = await hook.trigger("hello")

    assert result is False
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_two_timeouts_remain_ambiguous_with_same_key(monkeypatch):
    hook = OpenClawWebhook(PulseConfig())
    session = _FakeSession([asyncio.TimeoutError(), asyncio.TimeoutError()])
    monkeypatch.setattr(hook, "_get_session", lambda: _async_value(session))

    result = await hook.trigger("hello")

    assert result is None
    keys = [call[1]["headers"]["Idempotency-Key"] for call in session.calls]
    assert len(keys) == 2
    assert keys[0] == keys[1]


def test_ambiguous_delivery_does_not_change_drive_pressure():
    daemon = PulseDaemon.__new__(PulseDaemon)
    daemon.drives = Mock()
    decision = Mock()
    decision.top_drive = None

    daemon._apply_trigger_drive_outcome(decision, delivery_success=None)

    daemon.drives.on_trigger_success.assert_not_called()
    daemon.drives.on_trigger_failure.assert_not_called()


def test_ambiguous_delivery_has_its_own_metric():
    metrics = PulseMetrics(SimpleNamespace(start_time=None))

    metrics.record_trigger("drive_pressure", None)
    text = metrics.collect()

    assert 'pulse_trigger_ambiguous_total{reason="drive_pressure"} 1' in text
    assert 'pulse_trigger_failures_total{reason="drive_pressure"}' not in text
