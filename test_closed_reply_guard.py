"""Regression test for the auto-close bug on ticket 7905 (#...38755009).

A client replied "still unable to see any shift times ... screenshots attached"
to a resolved ticket and it was silently re-closed, because:
  1. the conversations list endpoint returns `summary` (not `content`) on reply
     threads, so the classifiers judged empty text, and
  2. a closed ticket re-closed on a bare "ack" classification.

These tests pin the fix without hitting the network.
"""
import os
# Dummy creds so module-level client constructors don't raise on import.
# None of these make network calls at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import agent  # noqa: E402

# The real reply thread object as Zoho's getTicketConversations returned it:
# `summary` present, NO `content` key, 3 attachments.
REAL_REPLY = {
    "summary": (
        "Hi Sam, I just took a look and am still unable to see any shift "
        "times from the opportunity dashboard. I'll attach a few screenshots "
        "of my view of one deactivated opportunity- perha..."
    ),
    "hasAttach": True,
    "attachmentCount": "3",
    "author": {"type": "END_USER", "email": "volunteers@sasklegion.ca"},
}


def test_reply_text_is_read_from_summary_when_content_missing():
    text = agent._extract_reply_text(REAL_REPLY)
    assert text, "reply text must not be empty (regression: was '' -> '(no text content)')"
    assert "still unable" in text.lower()


def test_action_signal_fires_on_real_reply():
    text = agent._extract_reply_text(REAL_REPLY)
    assert agent._ACTION_SIGNAL_RE.search(text), "'still'/'unable' must register as action-needed"


def test_real_reply_is_not_a_confident_ack():
    # Attachments alone disqualify it; the model is never consulted here.
    text = agent._extract_reply_text(REAL_REPLY)
    assert agent._is_confident_ack(text, has_attachment=True) is False
    # And even without attachments, the action signal disqualifies it.
    assert agent._is_confident_ack(text, has_attachment=False) is False


def test_empty_or_unreadable_reply_is_never_a_confident_ack():
    assert agent._is_confident_ack("", has_attachment=False) is False
    assert agent._is_confident_ack("   ", has_attachment=False) is False


def test_no_action_guard_also_rejects_the_real_reply():
    text = agent._extract_reply_text(REAL_REPLY)
    assert agent._is_no_action_reply(text, has_attachment=True) is False


def test_status_change_is_not_treated_as_a_reply():
    # Sam manually closes a ticket: Zoho fires a ticket-field update. The
    # payload carries only `id` (the ticket), no separate reply `ticketId`,
    # and eventType is a ticket update. Must NOT trigger reply-handling, or it
    # would restore the status he just set (the Closed<->Processing loop).
    assert agent.is_zoho_reply_event("Ticket_Update", "569440000038666806", "") is False
    assert agent.is_zoho_reply_event("Ticket_Update", "TID", "TID") is False
    assert agent.is_zoho_reply_event("unknown", "TID", "") is False


def test_client_reply_is_treated_as_a_reply():
    # A thread-add: reply ID in `id`, real ticket ID in `ticketId` (differs).
    assert agent.is_zoho_reply_event("Ticket_Thread_Add", "REPLY_ID", "TICKET_ID") is True
    # eventType signal alone is enough even if the shape is ambiguous.
    assert agent.is_zoho_reply_event("Ticket_Thread_Add", "TID", "") is True
    # Shape signal alone is enough even if eventType is missing/unknown.
    assert agent.is_zoho_reply_event("unknown", "REPLY_ID", "TICKET_ID") is True


def test_confident_ack_still_allows_a_genuine_thanks(monkeypatch):
    # A real "thank you!" with no attachments/signals: the model is consulted
    # and, if it agrees, this remains a confident ack (so genuine acks still
    # auto-close and don't clutter the queue).
    monkeypatch.setattr(agent, "_classify_no_action_reply", lambda t: True)
    assert agent._is_confident_ack("Thank you so much!", has_attachment=False) is True
    # ...but attachments override even a model "ack".
    assert agent._is_confident_ack("Thank you so much!", has_attachment=True) is False


# #9526 (2026-10-20): our answer closed the ticket, Allie's out-of-office came
# back, and Zoho's reply workflow flipped it Closed -> Processing.
OOO_REPLY = {"createdTime": "2026-10-20T10:38:22.000Z",
             "summary": "I will be out of the office Oct 20-21"}
REPLY_WORKFLOW_FLIP = {"time": "2026-10-20T10:38:31.000Z", "from": "Closed",
                       "to": "Processing", "actor_type": "Workflow",
                       "actor": "Customer Reply to Processing Status"}
OUR_CLOSE = {"time": "2026-10-20T10:38:14.000Z", "from": "In Progress",
             "to": "Closed", "actor_type": "Agent", "actor": "Support Agent"}


def _restore_with(monkeypatch, changes):
    import zoho_desk_api

    set_to = []
    monkeypatch.setattr(zoho_desk_api, "status_history", lambda t: changes)
    monkeypatch.setattr(agent, "_set_zoho_ticket_status",
                        lambda t, s: set_to.append(s) or True)
    return agent._restore_status_after_auto_reply("T", OOO_REPLY), set_to


def test_an_out_of_office_gets_its_status_back(monkeypatch):
    restored, set_to = _restore_with(monkeypatch,
                                     [REPLY_WORKFLOW_FLIP, OUR_CLOSE])
    assert restored == "Closed"
    assert set_to == ["Closed"]


def test_awaiting_client_response_comes_back_too(monkeypatch):
    flip = dict(REPLY_WORKFLOW_FLIP, **{"from": "Awaiting Client Response"})
    restored, set_to = _restore_with(monkeypatch, [flip])
    assert set_to == ["Awaiting Client Response"]


def test_a_status_a_person_set_since_is_left_alone(monkeypatch):
    sam = {"time": "2026-10-20T10:40:00.000Z", "from": "Processing",
           "to": "In Progress", "actor_type": "Agent", "actor": "Sam"}
    restored, set_to = _restore_with(monkeypatch,
                                     [sam, REPLY_WORKFLOW_FLIP, OUR_CLOSE])
    assert restored is None and set_to == []


def test_an_older_workflow_change_is_not_undone(monkeypatch):
    # The workflow had not run for this reply; its newest change is a day old.
    old = dict(REPLY_WORKFLOW_FLIP, time="2026-10-19T09:00:00.000Z")
    restored, set_to = _restore_with(monkeypatch, [old])
    assert restored is None and set_to == []


def test_unreadable_history_changes_nothing(monkeypatch):
    restored, set_to = _restore_with(monkeypatch, None)
    assert restored is None and set_to == []


def test_the_out_of_office_guard_schedules_the_restore(monkeypatch):
    conversations = {"data": [dict(OOO_REPLY, author={"type": "END_USER",
                                                      "email": "a@b.org"})]}
    scheduled = []
    monkeypatch.setattr(agent, "fetch_ticket_conversations",
                        lambda t: conversations)
    monkeypatch.setattr(agent, "_restore_status_after_auto_reply_later",
                        lambda t, r: scheduled.append(t))
    monkeypatch.setattr(agent, "_processing_updates", {})
    assert agent.process_ticket_update("T9526") is None
    assert scheduled == ["T9526"]


if __name__ == "__main__":
    # Minimal runner so this works without pytest installed.
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            import inspect
            kwargs = {"monkeypatch": _MP()} if "monkeypatch" in inspect.signature(fn).parameters else {}
            fn(**kwargs)
            print(f"PASS {name}")
            passed += 1
    print(f"\n{passed} passed")


# 2026-10-24: agent fixes sit on the Zoho "Agent Fixes" team with no agent.
# The owner rules read that as "unassigned" and closed the ClickUp task the
# release watcher needs to tell the client.
def _sync_with(monkeypatch, ticket):
    import database
    import zoho_actions

    closed = []
    monkeypatch.setattr(database, "get_thread_by_ticket_id",
                        lambda t: ("TS", {"clickup_task_id": "CU1"}))
    monkeypatch.setattr(database, "update_thread", lambda *a, **k: None)
    monkeypatch.setattr(agent, "fetch_ticket_from_zoho", lambda t: ticket)
    monkeypatch.setattr(agent, "_unwrap_mcp_result", lambda r: r)
    monkeypatch.setattr(agent, "close_clickup_task", closed.append)
    agent.sync_zoho_to_clickup("T")
    return closed, zoho_actions.AGENT_FIXES_TEAM


def test_an_agent_fix_ticket_keeps_its_clickup_task(monkeypatch):
    import zoho_actions

    closed, _ = _sync_with(monkeypatch, {
        "status": "In Progress", "assigneeId": None,
        "teamId": zoho_actions.AGENT_FIXES_TEAM})
    assert closed == []


def test_an_unassigned_ticket_off_the_team_still_closes(monkeypatch):
    closed, _ = _sync_with(monkeypatch, {
        "status": "In Progress", "assigneeId": None, "teamId": None})
    assert closed == ["CU1"]


# 2026-10-24: a ticket closed in Zoho takes every engineer task with it,
# also when it has no thread record or a second task.
def test_a_closed_ticket_closes_its_other_queue_tasks(monkeypatch):
    import ticket_reconciler

    swept = []
    monkeypatch.setattr(ticket_reconciler, "close_tasks_for_ticket",
                        lambda t: swept.append(t) or [])
    closed, _ = _sync_with(monkeypatch, {"status": "Closed"})
    assert closed == ["CU1"]
    assert swept == ["T"]


def test_a_closed_ticket_with_no_thread_still_closes_its_tasks(monkeypatch):
    import database
    import ticket_reconciler
    import zoho_actions

    swept = []
    monkeypatch.setattr(database, "get_thread_by_ticket_id", lambda t: None)
    monkeypatch.setattr(zoho_actions, "ticket_status", lambda t: "Closed")
    monkeypatch.setattr(ticket_reconciler, "close_tasks_for_ticket",
                        lambda t: swept.append(t) or [])
    agent.sync_zoho_to_clickup("T")
    assert swept == ["T"]


def test_an_open_ticket_with_no_thread_closes_nothing(monkeypatch):
    import database
    import ticket_reconciler
    import zoho_actions

    swept = []
    monkeypatch.setattr(database, "get_thread_by_ticket_id", lambda t: None)
    monkeypatch.setattr(zoho_actions, "ticket_status",
                        lambda t: "In Progress")
    monkeypatch.setattr(ticket_reconciler, "close_tasks_for_ticket",
                        lambda t: swept.append(t) or [])
    agent.sync_zoho_to_clickup("T")
    assert swept == []


# 2026-10-25: a task released off the engineer queue was reopened when the
# ticket was then parked at Awaiting Client Response (#9536).
def _park_with(monkeypatch, task_closed):
    parked = []
    monkeypatch.setattr(agent, "_clickup_task_closed", lambda t: task_closed)
    monkeypatch.setattr(agent, "_update_clickup_task_status",
                        lambda t, s: parked.append(t))
    _sync_with(monkeypatch, {"status": "Awaiting Client Response"})
    return parked


def test_a_closed_task_stays_closed_when_the_ticket_is_parked(monkeypatch):
    assert _park_with(monkeypatch, task_closed=True) == []


def test_an_open_task_is_parked_with_the_ticket(monkeypatch):
    assert _park_with(monkeypatch, task_closed=False) == ["CU1"]
