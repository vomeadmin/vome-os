"""Regression test for the auth fast path on ticket 9549 (Sarasota County).

An admin forwarded "FW: THIRD Failure to Send New Volunteer Log-in" about a
volunteer's missing invite. The fast path read it as an auth problem, checked
the admin's own email (active) and auto-sent "We checked your account and it's
already active" 18 seconds later. Two fixes are pinned here:
  1. a forwarded ticket never enters the fast path, and
  2. only an activation (a fix) emails the client; every other auth result
     goes to triage, which drafts for Sam.

These tests make no network calls.
"""
import os
# Dummy creds so module-level client constructors don't raise on import.
# None of these make network calls at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import pytest  # noqa: E402

import agent  # noqa: E402


@pytest.mark.parametrize("subject", [
    "FW: THIRD Failure to Send New Volunteer Log-in",
    "Fwd: verification code",
    "RE: FW: can't log in",
    "TR: code d'activation",
])
def test_forwarded_ticket_skips_the_fast_path_without_asking_the_model(monkeypatch, subject):
    def _no_model(**_kwargs):
        raise AssertionError("the model must not be asked about a forwarded ticket")
    monkeypatch.setattr(agent.client.messages, "create", _no_model)
    assert agent._detect_auth_bypass_issue(subject, "I didn't get my code") is False


@pytest.fixture
def sent(monkeypatch):
    replies = []
    monkeypatch.setattr(agent, "_send_auth_reply",
                        lambda ticket_id, to, content: replies.append((to, content)))
    monkeypatch.setattr(agent, "_run_auth_bypass",
                        lambda email: {"bypassed": True})
    return replies


@pytest.mark.parametrize("auth_result", [
    {"found": True, "is_active": True, "is_bypassable": False},
    {"found": True, "is_offline_profile": True, "is_bypassable": False},
    {"found": False},
    {"found": True},
])
def test_answers_are_left_for_triage(monkeypatch, sent, auth_result):
    monkeypatch.setattr(agent, "_run_auth_check", lambda email: auth_result)
    handled = agent._handle_auth_bypass_email_ticket(
        ticket_id="569440000043176144",
        contact_email="ssielaty@scgov.net",
        contact_name="Stephanie Sielaty",
        detected_lang=None,
    )
    assert handled is False
    assert sent == [], "nothing may be emailed when nothing was fixed"


def test_activation_still_sends_the_fix_email(monkeypatch, sent):
    monkeypatch.setattr(agent, "_run_auth_check",
                        lambda email: {"found": True, "is_bypassable": True})
    handled = agent._handle_auth_bypass_email_ticket(
        ticket_id="1",
        contact_email="volunteer@example.com",
        contact_name="Jim Cranston",
        detected_lang=None,
    )
    assert handled is True
    assert len(sent) == 1
    to, content = sent[0]
    assert to == "volunteer@example.com"
    assert content.startswith("Hi Jim,")
    assert "activate your account" in content


def test_failed_activation_sends_nothing(monkeypatch, sent):
    monkeypatch.setattr(agent, "_run_auth_check",
                        lambda email: {"found": True, "is_bypassable": True})
    monkeypatch.setattr(agent, "_run_auth_bypass", lambda email: {"bypassed": False})
    handled = agent._handle_auth_bypass_email_ticket(
        ticket_id="1",
        contact_email="volunteer@example.com",
        contact_name="Jim Cranston",
        detected_lang=None,
    )
    assert handled is False
    assert sent == []
