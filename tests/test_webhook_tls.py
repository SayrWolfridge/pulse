"""OpenClaw webhook transport and session-contract tests."""

from pulse.src.core.config import PulseConfig
from pulse.src.core.webhook import OpenClawWebhook, _ssl_for_url


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
