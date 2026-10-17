"""
ENABLE_AUTO_ACK: the off switch for the instant new-ticket acknowledgment.

Unset must mean on, so deploying the flag changes nothing until someone
explicitly turns it off.
"""

import os
# Dummy creds so module-level client constructors don't raise on import.
# None of these make network calls at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import pytest  # noqa: E402

import agent  # noqa: E402


def test_unset_means_on(monkeypatch):
    monkeypatch.delenv("ENABLE_AUTO_ACK", raising=False)
    assert agent.auto_ack_enabled() is True


@pytest.mark.parametrize("value", ["true", "TRUE", "on", "1", "yes", ""])
def test_on_values(monkeypatch, value):
    monkeypatch.setenv("ENABLE_AUTO_ACK", value)
    assert agent.auto_ack_enabled() is True


@pytest.mark.parametrize("value", ["false", "FALSE", " False ", "off", "0", "no"])
def test_off_values(monkeypatch, value):
    monkeypatch.setenv("ENABLE_AUTO_ACK", value)
    assert agent.auto_ack_enabled() is False
