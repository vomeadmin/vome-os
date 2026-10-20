"""Regression test for the auth fast path on ticket 9549 (Sarasota County).

An admin forwarded "FW: THIRD Failure to Send New Volunteer Log-in" about a
volunteer's missing invite. The fast path read it as an auth problem, checked
the admin's own email (active) and auto-sent "We checked your account and it's
already active" 18 seconds later. Pinned here:
  1. the classifier tells the sender's own sign-in problem (SELF) from one
     about someone else (OTHER), and a forward is never SELF,
  2. OTHER from an active admin gets Sam's invitation FAQ, OTHER from anyone
     else goes to triage,
  3. SELF keeps the volunteer flow (check, activate or answer) unchanged.

These tests make no network calls.
"""
import os
from types import SimpleNamespace
# Dummy creds so module-level client constructors don't raise on import.
# None of these make network calls at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import pytest  # noqa: E402

import agent  # noqa: E402

TICKET_9549 = "569440000043176144"
ACTIVE_ADMIN = {"found": True, "is_active": True, "is_bypassable": False,
                "is_offline_profile": False, "user_type": "organization_admin"}


def _model_says(monkeypatch, answer):
    reply = SimpleNamespace(content=[SimpleNamespace(text=answer)])
    monkeypatch.setattr(agent.client.messages, "create", lambda **_kw: reply)


@pytest.mark.parametrize("answer,expected", [
    ("SELF", agent.AUTH_TICKET_SELF),
    ("OTHER", agent.AUTH_TICKET_OTHER),
    ("NO", None),
    ("", None),
])
def test_classifier_maps_the_model_answer(monkeypatch, answer, expected):
    _model_says(monkeypatch, answer)
    assert agent._classify_auth_ticket("Can't log in", "I never got my code") == expected


@pytest.mark.parametrize("subject", [
    "FW: THIRD Failure to Send New Volunteer Log-in",
    "Fwd: verification code",
    "RE: FW: can't log in",
    "TR: code d'activation",
])
def test_a_forward_is_never_the_senders_own_problem(monkeypatch, subject):
    _model_says(monkeypatch, "SELF")
    assert agent._classify_auth_ticket(subject, "I didn't get my code") is None


def test_a_forward_about_a_volunteer_is_other(monkeypatch):
    _model_says(monkeypatch, "OTHER")
    assert agent._classify_auth_ticket(
        "FW: THIRD Failure to Send New Volunteer Log-in",
        "We have been having some trouble with the re-send invitations feature.",
    ) == agent.AUTH_TICKET_OTHER


@pytest.fixture
def sent(monkeypatch):
    replies = []
    monkeypatch.setattr(agent, "_send_auth_reply",
                        lambda ticket_id, to, content: replies.append((to, content)))
    monkeypatch.setattr(agent, "_run_auth_bypass", lambda email: {"bypassed": True})
    return replies


def _admin_help(name="Stephanie Sielaty", lang=None):
    return agent._handle_admin_signin_help_ticket(
        ticket_id=TICKET_9549,
        contact_email="ssielaty@scgov.net",
        contact_name=name,
        detected_lang=lang,
    )


def test_admin_about_a_volunteer_gets_the_invitation_faq(monkeypatch, sent):
    monkeypatch.setattr(agent, "_run_auth_check", lambda email: ACTIVE_ADMIN)
    assert _admin_help() is True
    assert len(sent) == 1
    to, content = sent[0]
    assert to == "ssielaty@scgov.net"
    assert content.startswith("Hi Stephanie,")
    assert "same email address that you invited them from" in content
    assert agent._INVITE_FAQ_URL["en"] in content
    assert agent._LOGIN_HELP_FAQ_URL["en"] in content
    assert "already active" not in content


def test_admin_faq_in_french(monkeypatch, sent):
    monkeypatch.setattr(agent, "_run_auth_check", lambda email: ACTIVE_ADMIN)
    assert _admin_help(lang="French") is True
    content = sent[0][1]
    assert content.startswith("Bonjour Stephanie,")
    assert agent._INVITE_FAQ_URL["fr"] in content
    assert agent._LOGIN_HELP_FAQ_URL["fr"] in content


@pytest.mark.parametrize("auth_result", [
    {**ACTIVE_ADMIN, "user_type": "volunteer"},
    {**ACTIVE_ADMIN, "user_type": "guardian"},
    {**ACTIVE_ADMIN, "is_active": False},
    {"found": False},
    None,
])
def test_someone_else_from_a_non_admin_goes_to_triage(monkeypatch, sent, auth_result):
    monkeypatch.setattr(agent, "_run_auth_check", lambda email: auth_result)
    assert _admin_help() is False
    assert sent == []


def _self_check(auth_result, monkeypatch):
    monkeypatch.setattr(agent, "_run_auth_check", lambda email: auth_result)
    return agent._handle_auth_bypass_email_ticket(
        ticket_id="1",
        contact_email="volunteer@example.com",
        contact_name="Jim Cranston",
        detected_lang=None,
    )


@pytest.mark.parametrize("auth_result,phrase", [
    ({"found": True, "is_bypassable": True}, "activate your account"),
    ({"found": True, "is_active": True}, "already active"),
    ({"found": True, "is_offline_profile": True}, "already exists in our system"),
    ({"found": False}, "weren't able to find an account"),
])
def test_volunteer_flow_is_unchanged(monkeypatch, sent, auth_result, phrase):
    assert _self_check(auth_result, monkeypatch) is True
    assert len(sent) == 1
    assert sent[0][0] == "volunteer@example.com"
    assert phrase in sent[0][1]


def test_process_ticket_routes_an_admin_report_to_the_faq(monkeypatch, sent):
    # #9549 end to end: forwarded admin report, model says OTHER.
    _model_says(monkeypatch, "OTHER")
    monkeypatch.setattr(agent, "_dedup_check", lambda ticket_id: False)
    monkeypatch.setattr(agent, "fetch_ticket_from_zoho", lambda ticket_id: None)
    monkeypatch.setattr(agent, "fetch_ticket_conversations", lambda ticket_id: None)
    monkeypatch.setattr(agent, "_run_auth_check", lambda email: ACTIVE_ADMIN)
    called = []
    monkeypatch.setattr(agent, "_handle_auth_bypass_email_ticket",
                        lambda **kw: called.append(kw) or True)
    result = agent.process_ticket({
        "ticket_id": TICKET_9549,
        "contact_name": "Stephanie Sielaty",
        "contact_email": "ssielaty@scgov.net",
        "subject": "FW: THIRD Failure to Send New Volunteer Log-in",
        "body": "We have been having some trouble with the re-send invitations feature.",
    })
    assert result == "admin_signin_help_sent"
    assert called == [], "the admin's own account must not go through the volunteer flow"
    assert "same email address that you invited them from" in sent[0][1]
