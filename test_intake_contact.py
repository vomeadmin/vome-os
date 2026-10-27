"""The widget ticket contact is the logged-in reporter, not the volunteer
they asked about (#9650)."""

import intake


def _run(monkeypatch, extracted, user_email="admin@org.ca"):
    calls = {}

    def fake_contact(email, first_name="", last_name="", account_id=None):
        calls["contact_email"] = email
        return {"id": "c1"}

    def fake_ticket(subject, description, email, contact_id, account_id):
        calls["ticket_email"] = email
        return {"id": "t1", "ticketNumber": "1"}

    def fake_comment(ticket_id, note):
        calls["note"] = note

    monkeypatch.setattr(intake, "zoho_find_or_create_account", lambda name: {"id": "a1"})
    monkeypatch.setattr(intake, "zoho_find_or_create_contact", fake_contact)
    monkeypatch.setattr(intake, "zoho_create_ticket", fake_ticket)
    monkeypatch.setattr(intake, "zoho_add_comment", fake_comment)
    monkeypatch.setattr(intake, "zoho_upload_attachment", lambda *a: None)
    monkeypatch.setattr(intake, "fetch_crm_account", lambda email: {})
    monkeypatch.setattr(intake, "send_ticket_brief", lambda **kw: None)
    monkeypatch.setattr(intake, "_generate_ticket_subject", lambda *a: "Subject")

    session = {
        "user_email": user_email,
        "user_role": "admin",
        "user_first_name": "Vol",
        "user_last_name": "Coordo",
        "org_name": "Depot",
    }
    result = intake._create_ticket_from_intake(extracted, session, [], [])
    assert result["ticket_created"]
    return calls


def test_volunteer_email_is_not_the_contact(monkeypatch):
    calls = _run(monkeypatch, {"affected_user_email": "volunteer@mail.ca"})
    assert calls["contact_email"] == "admin@org.ca"
    assert calls["ticket_email"] == "admin@org.ca"
    assert "Reported by: admin@org.ca (admin)" in calls["note"]
    assert "Affected user: volunteer@mail.ca" in calls["note"]


def test_self_report_uses_reporter(monkeypatch):
    calls = _run(monkeypatch, {"affected_user_email": "self"})
    assert calls["ticket_email"] == "admin@org.ca"


def test_falls_back_to_affected_when_no_session_email(monkeypatch):
    calls = _run(monkeypatch, {"affected_user_email": "volunteer@mail.ca"}, user_email="")
    assert calls["ticket_email"] == "volunteer@mail.ca"
