"""CC on Vic's client replies.

Anchor case: Zoho ticket #9313. A client types a second address into the
portal's CC box. Zoho does not put it on the ticket as ``cc``; it stores it as
a secondary contact id. Vic read only ``cc``, so every API reply dropped the
CC'd person while replies sent from the Zoho UI reached them.

No network: every Zoho call is monkeypatched.
"""
import os

# Dummy creds so module-level client constructors don't raise on import.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import pytest  # noqa: E402

import agent  # noqa: E402
import zoho_actions  # noqa: E402
from ops import zoho_sync  # noqa: E402

PRIMARY = "admin.team@riverbend.org"
SECONDARY_ID = "569440000099990001"
SECONDARY = "nora.personal@gmail.com"

# Shape of a real getTicket response for a portal ticket with a CC
# (anonymised): no "cc" key at all, the CC lives in secondaryContacts.
PORTAL_TICKET = {
    "id": "1",
    "subject": "Not receiving CC emails",
    "channel": "Web",
    "email": PRIMARY,
    "contact": {"firstName": "Riverbend", "lastName": "Admin", "email": PRIMARY},
    "secondaryContacts": [SECONDARY_ID],
}


@pytest.fixture(autouse=True)
def _no_thread_read(monkeypatch):
    monkeypatch.setattr(zoho_actions, "ticket_teammates", lambda tid: [])


def _contacts(monkeypatch, mapping):
    calls = []

    def fake_call(tool, args):
        assert tool == "ZohoDesk_getContact"
        cid = args["path_variables"]["contactId"]
        calls.append(cid)
        if cid not in mapping:
            return None
        return {"data": {"id": cid, "email": mapping[cid]}}

    monkeypatch.setattr(agent, "_zoho_desk_call", fake_call)
    return calls


def test_portal_cc_secondary_contact_is_copied(monkeypatch):
    _contacts(monkeypatch, {SECONDARY_ID: SECONDARY})
    fields = agent._extract_ticket_fields(PORTAL_TICKET)
    assert fields["contact_email"] == PRIMARY
    assert fields["cc_email"] == SECONDARY


def test_no_secondary_contacts_makes_no_lookup(monkeypatch):
    calls = _contacts(monkeypatch, {})
    ticket = {**PORTAL_TICKET, "secondaryContacts": []}
    assert agent._extract_ticket_fields(ticket)["cc_email"] == ""
    assert calls == []


def test_unresolvable_contact_does_not_break_the_rest(monkeypatch):
    _contacts(monkeypatch, {SECONDARY_ID: SECONDARY})
    ticket = {**PORTAL_TICKET, "secondaryContacts": ["404", SECONDARY_ID]}
    assert agent._extract_ticket_fields(ticket)["cc_email"] == SECONDARY


def test_primary_own_and_duplicate_addresses_are_dropped(monkeypatch):
    _contacts(monkeypatch, {
        "a": PRIMARY.upper(),
        "b": "support@vomevolunteer.zohodesk.com",
        "c": "admin@vomevolunteer.com",
        "d": SECONDARY,
    })
    ticket = {
        **PORTAL_TICKET,
        "cc": f'"Nora" <{SECONDARY}>',
        "secondaryContacts": ["a", "b", "c", "d"],
    }
    assert agent._extract_ticket_fields(ticket)["cc_email"] == SECONDARY


def test_legacy_cc_key_still_honoured(monkeypatch):
    _contacts(monkeypatch, {})
    ticket = {**PORTAL_TICKET, "secondaryContacts": [],
              "cc": ["one@x.org", "two@y.org"]}
    assert agent._extract_ticket_fields(ticket)["cc_email"] == "one@x.org,two@y.org"


def test_ops_send_looks_up_cc_when_not_given(monkeypatch):
    sent = {}
    monkeypatch.setattr(zoho_sync, "fetch_ticket_cc", lambda tid: SECONDARY)

    def fake_send(tool, args):
        sent.update(args["body"])
        return {"id": "t1"}

    monkeypatch.setattr(zoho_sync, "_zoho_desk_call", fake_send)
    zoho_sync.send_zoho_reply("1", "Hi Nora,", PRIMARY)
    assert sent["cc"] == SECONDARY


def test_ops_send_explicit_empty_cc_skips_lookup(monkeypatch):
    sent = {}

    def no_lookup(tid):
        raise AssertionError("cc lookup should not run")

    monkeypatch.setattr(zoho_sync, "fetch_ticket_cc", no_lookup)
    monkeypatch.setattr(
        zoho_sync, "_zoho_desk_call",
        lambda tool, args: sent.update(args["body"]) or {"id": "t1"},
    )
    zoho_sync.send_zoho_reply("1", "Hi Nora,", PRIMARY, cc_email="")
    assert "cc" not in sent


def test_a_teammate_the_client_wrote_to_is_copied(monkeypatch):
    """#9610: Ron was on the client's To line. Fix emails and other agent
    replies keep him, even though own addresses are dropped from cc."""
    _contacts(monkeypatch, {SECONDARY_ID: SECONDARY})
    monkeypatch.setattr(zoho_actions, "ticket_teammates",
                        lambda tid: ["r.segev@vomevolunteer.com"])
    fields = agent._extract_ticket_fields(PORTAL_TICKET)
    assert fields["cc_email"] == f"{SECONDARY},r.segev@vomevolunteer.com"
