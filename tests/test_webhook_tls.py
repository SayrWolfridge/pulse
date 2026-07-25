"""TLS policy for the OpenClaw webhook transport."""

from pulse.src.core.webhook import _ssl_for_url


def test_disables_tls_verification_for_https_loopback():
    assert _ssl_for_url("https://127.0.0.1:18789/hooks/agent") is False
    assert _ssl_for_url("https://localhost:18789/hooks/agent") is False
    assert _ssl_for_url("https://[::1]:18789/hooks/agent") is False


def test_keeps_default_tls_verification_for_non_loopback():
    assert _ssl_for_url("https://gateway.example/hooks/agent") is None
    assert _ssl_for_url("http://127.0.0.1:18789/hooks/agent") is None
