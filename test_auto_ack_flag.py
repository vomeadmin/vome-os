"""
ENABLE_AUTO_ACK: the instant new-ticket acknowledgment, retired 2026-10-17.

Unset now means OFF. Shadow triage drafts a read, guarded first reply for
Sam instead of sending a blind one seconds after the ticket arrives. Setting
ENABLE_AUTO_ACK=true brings the old behaviour back.
"""

import os
# Dummy creds so module-level client constructors don't raise on import.
# None of these make network calls at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import pytest  # noqa: E402

import agent  # noqa: E402


def test_unset_means_off(monkeypatch):
    monkeypatch.delenv("ENABLE_AUTO_ACK", raising=False)
    assert agent.auto_ack_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", " on ", "1", "yes"])
def test_on_values(monkeypatch, value):
    monkeypatch.setenv("ENABLE_AUTO_ACK", value)
    assert agent.auto_ack_enabled() is True


@pytest.mark.parametrize("value", ["false", "off", "0", "no", ""])
def test_off_values(monkeypatch, value):
    monkeypatch.setenv("ENABLE_AUTO_ACK", value)
    assert agent.auto_ack_enabled() is False
