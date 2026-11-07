"""An edited reply is saved and taught only once it has gone out.

#9623 (2026-11-06): a guard held Sam's edit ("We've confirmed"), yet the
edit had already replaced the agent's draft and been learned as a lesson.
"""

import pytest

import sprint_store
import sprint_thread
import sprint_tools


ITEM = {"id": 7, "number": 119, "ticket_id": "Z1", "draft": "agent draft",
        "draft_status": "ok", "recipients": {"first_name": "Elinor"}}


def _patch(monkeypatch, guard="ok", result="*119.* sent to e@x.org."):
    calls = {"set_draft": [], "learn": [], "sent_draft": None}
    monkeypatch.setattr(sprint_tools, "_sam_only", lambda *a: None)
    monkeypatch.setattr(sprint_tools, "find", lambda t: ("Z1", dict(ITEM)))
    monkeypatch.setattr(sprint_thread, "_answered", lambda item: False)
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda *a, **k: (guard, "" if guard == "ok" else "no_confirmation"))
    monkeypatch.setattr(sprint_store, "set_draft",
                        lambda *a, **k: calls["set_draft"].append(a))
    monkeypatch.setattr(sprint_thread, "_learn",
                        lambda *a: calls["learn"].append(a) or "lesson")

    def send(item, **kw):
        calls["sent_draft"] = item["draft"]
        return result
    monkeypatch.setattr(sprint_thread, "_send", send)
    return calls


def test_a_held_edit_changes_nothing(monkeypatch):
    calls = _patch(monkeypatch, guard="held")
    with pytest.raises(Exception):
        sprint_tools.send_ticket_reply("9623", "send 1", "sam", text="We've confirmed it.")
    assert calls["set_draft"] == [] and calls["learn"] == []
    assert calls["sent_draft"] is None


def test_a_sent_edit_is_saved_and_taught(monkeypatch):
    calls = _patch(monkeypatch)
    out = sprint_tools.send_ticket_reply("9623", "send 1", "sam", text="The limit is 100 MB.")
    assert out["sent"]
    assert "The limit is 100 MB." in calls["sent_draft"]
    assert len(calls["set_draft"]) == 1 and len(calls["learn"]) == 1


def test_a_failed_send_keeps_the_agent_draft(monkeypatch):
    calls = _patch(monkeypatch, result="*119.* has no recipient.")
    out = sprint_tools.send_ticket_reply("9623", "send 1", "sam", text="The limit is 100 MB.")
    assert not out["sent"]
    assert calls["set_draft"] == [] and calls["learn"] == []


def test_a_portal_link_is_not_a_signature():
    body = sprint_thread._signed(ITEM, "See https://support.vomevolunteer.com/portal/en/kb/articles/x for the steps.")
    assert body.startswith("Hi Elinor,")
    assert body.endswith("Sam | Vome team\nsupport.vomevolunteer.com")


def test_an_existing_signature_is_not_doubled():
    body = sprint_thread._signed(ITEM, "Hi Elinor,\n\nDone.\n\nSam | Vome team\nsupport.vomevolunteer.com")
    assert body.count("Sam | Vome team") == 1
    body = sprint_thread._signed(ITEM, "Done.\n\nSam | Vome team")
    assert body.count("Sam | Vome team") == 1
    assert body.endswith("support.vomevolunteer.com")
