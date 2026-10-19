"""
test_agent_fixing.py

A ticket the agent is fixing (#9582, 2026-10-19). Once the agent opens a fix
PR the ticket is the agent's: the sprint says so and offers the
acknowledgment, then shows it as one line per sprint until the fix is live
(it comes back as a resolution) or the PR is closed unmerged (it comes back
as a decision). Zoho says the same thing the moment the PR opens.
"""

import os
from datetime import datetime, timedelta

import pytest

import sprint_batch
import sprint_store
import sprint_thread
import ticket_fix
import zoho_actions

NOW = datetime(2026, 10, 19, 18, 0)
PR = "https://github.com/vomeadmin/vome-react/pull/812"


def _item(**over):
    item = {
        "id": 58, "number": 58, "ticket_id": "569440000043239739",
        "ticket_number": "9582", "status": sprint_store.FIXING,
        "category": "clear_bug", "account": "CHU de Québec (high, 3900 CAD)",
        "important": True, "summary": "Vues",
        "draft": "Bonjour Isabelle,\n\nMerci pour les détails.",
        "draft_status": "ok", "draft_why": "", "last_thread_id": "T-LATEST",
        "recipients": {"to": "isabelle.is.savard@chudequebec.ca",
                       "first_name": "Isabelle",
                       "received_at": "2026-10-19T14:39:00.000Z"},
        "card": {"lane": "agent_fix", "live_question": "Views fail"},
        "fix_pr": PR, "decision": "agent fix PR open",
        "decided_at": NOW - timedelta(days=2), "sent_at": None,
        "arrived_at": NOW - timedelta(days=2),
    }
    item.update(over)
    return item


# ---------------------------------------------------------------------------
# Where it shows in the sprint
# ---------------------------------------------------------------------------

def test_before_the_acknowledgment_it_is_a_reply_to_send():
    assert sprint_batch.section(_item(), NOW) == sprint_batch.READY


def test_after_the_acknowledgment_it_needs_nobody():
    item = _item(sent_at=NOW - timedelta(days=1))
    assert sprint_batch.section(item, NOW) == sprint_batch.IN_FLIGHT


def test_a_held_draft_still_shows_as_fix_first():
    item = _item(draft_status="blocked", draft_why="names a file")
    assert sprint_batch.section(item, NOW) == sprint_batch.FIX


def test_handed_back_after_a_reply_is_a_decision_not_a_resend():
    # The PR was closed: the old acknowledgment must not be offered again.
    item = _item(status=sprint_store.OPEN, sent_at=NOW - timedelta(days=1),
                 decision=sprint_store.FIX_ABANDONED)
    assert sprint_batch.section(item, NOW) == sprint_batch.DECIDE


def test_the_card_says_the_agent_is_fixing_it():
    text = sprint_batch.detail(_item(), set(), NOW)
    assert "The agent is fixing this" in text
    assert "waiting on your merge into the dev branch" in text
    assert PR in text
    assert "`send 58`" in text


def test_a_closed_pr_says_nobody_is_fixing_it():
    item = _item(status=sprint_store.OPEN, sent_at=NOW,
                 decision=sprint_store.FIX_ABANDONED)
    text = sprint_batch.detail(item, set(), NOW)
    assert "closed without merging" in text
    assert "The agent is fixing this" not in text


def test_in_flight_items_get_one_line_and_do_not_count_as_needing_sam():
    fixing = _item(sent_at=NOW - timedelta(days=1))
    merged = _item(id=59, number=59, ticket_number="9583",
                   sent_at=NOW - timedelta(days=1),
                   decision="merged into dev as web@abc123456")
    picked = sprint_batch.select([fixing, merged], now=NOW)
    assert picked[sprint_batch.READY] == []
    top, _thread = sprint_batch.render(1, picked, {}, NOW)
    assert "*0 need you*" in top
    assert "AGENT IS FIXING (2)" in top
    assert "*58*" in top and "2d" in top
    assert "PR open, waiting on your merge" in top
    assert "merged into dev as web@abc123456, waiting on production" in top


def test_a_fresh_clear_does_not_retriage_what_the_agent_is_fixing(
        monkeypatch):
    item = _item(updated_at=NOW - timedelta(days=3))
    monkeypatch.setattr(sprint_store, "by_ticket", lambda tid: item)
    assert not sprint_batch._needs_triage({"id": item["ticket_id"]}, NOW)
    item["status"] = sprint_store.OPEN
    assert sprint_batch._needs_triage({"id": item["ticket_id"]}, NOW)


# ---------------------------------------------------------------------------
# Sending the acknowledgment keeps it the agent's
# ---------------------------------------------------------------------------

@pytest.fixture
def sends(monkeypatch):
    import shadow_triage

    class S:
        zoho = []
        decided = []
        kept = []
        live = False

    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda tid: {"id": "T-LATEST"})
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda tid, to, cc, c: S.zoho.append(c) or {})
    monkeypatch.setattr(zoho_actions, "set_status", lambda *a: {})
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: S.decided.append(s))
    monkeypatch.setattr(sprint_store, "keep_fixing",
                        lambda i: S.kept.append(i))
    monkeypatch.setattr(sprint_store, "record_sent", lambda i, t: None)
    monkeypatch.setattr(sprint_thread, "_post", lambda *a, **k: "ts")
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": S.live})
    return S


def test_the_acknowledgment_leaves_it_in_flight(sends):
    out = sprint_thread._send(_item())
    assert len(sends.zoho) == 1
    assert sends.kept == [58] and sends.decided == []
    assert "sent to isabelle" in out


def test_the_same_acknowledgment_is_never_sent_twice(sends):
    out = sprint_thread._send(_item(sent_at=NOW))
    assert sends.zoho == []
    assert "already acknowledged" in out


def test_once_the_fix_is_live_the_resolution_finishes_it(sends):
    # release_resolutions re-triages on Live, which reopens the item.
    sends.live = True
    sprint_thread._send(_item(status=sprint_store.OPEN))
    assert sends.decided == [sprint_store.SENT] and sends.kept == []


# ---------------------------------------------------------------------------
# Zoho
# ---------------------------------------------------------------------------

def test_zoho_shows_the_agent_has_it_without_emailing(monkeypatch):
    calls = []
    monkeypatch.setattr(zoho_actions, "_send",
                        lambda m, p, b: calls.append((m, p, b)) or {})
    problems = zoho_actions.mark_agent_fix("T1", PR, "https://cu/t/P1")
    assert problems == ""
    assert ("PATCH", "/tickets/T1",
            {"status": "In Progress",
             "assigneeId": zoho_actions.SAM_AGENT}) in calls
    assert ("POST", "/tickets/T1/associateTag",
            {"tags": ["agent-fix"]}) in calls
    note = next(b for m, p, b in calls if p.endswith("/comments"))
    assert note["isPublic"] is False and PR in note["content"]
    # No reply endpoint is ever touched.
    assert not any("sendReply" in p for _, p, _ in calls)


def test_a_zoho_failure_is_reported_not_raised(monkeypatch):
    def flaky(method, path, body):
        if path.endswith("associateTag"):
            raise RuntimeError("Zoho 422: bad tag")
        return {}

    monkeypatch.setattr(zoho_actions, "_send", flaky)
    problems = zoho_actions.mark_agent_fix("T1", PR)
    assert "tag not set" in problems and "422" in problems


def test_opening_the_pr_marks_zoho(monkeypatch):
    import sentry_code
    import sentry_fix

    marked = []
    monkeypatch.setattr(ticket_fix, "enabled", lambda: True)
    monkeypatch.setattr(ticket_fix, "should_attempt",
                        lambda card, sam_approved=False: (True, ""))
    monkeypatch.setattr(ticket_fix, "_record", lambda *a, **k: None)
    monkeypatch.setattr(ticket_fix, "_file_tasks", lambda *a: ("P1", "C1"))
    monkeypatch.setattr(sentry_code, "gather", lambda *a, **k: {"files": ""})
    monkeypatch.setattr(sentry_fix, "propose", lambda a, c: {"diff": "d"})
    monkeypatch.setattr(sentry_fix, "open_pull_request",
                        lambda *a: {"opened": True, "url": PR})
    monkeypatch.setattr(zoho_actions, "mark_agent_fix",
                        lambda t, u, task="": marked.append((t, u, task))
                        or "")
    card = {"code_check": {
        "where": "web:src/views/MyVolunteerUserList.js:6119",
        "finding": "f", "fix": "x", "confidence": "medium"},
        "live_question": "Views fail"}
    out = ticket_fix._propose("T1", "9582", "CHU", card, "zoho-url",
                              sam_approved=True)
    assert out["outcome"] == "pr_opened"
    assert marked == [("T1", PR, "https://app.clickup.com/t/P1")]


# ---------------------------------------------------------------------------
# Merged, or closed without merging
# ---------------------------------------------------------------------------

def _tasks():
    return [
        {"id": "P1", "parent": None, "status": {"status": "in progress"},
         "description": f"Fix PR: {PR}", "custom_fields": []},
        {"id": "C1", "parent": "P1", "status": {"status": "in progress"}},
    ]


@pytest.fixture
def tracked(monkeypatch):
    class T:
        clickup = []
        progress = []
        abandoned = []
        unmarked = []

    monkeypatch.setattr(ticket_fix, "_clickup",
                        lambda m, p, b=None: T.clickup.append((m, p, b)) or {})
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_store, "fix_progress",
                        lambda u, n: T.progress.append((u, n)) or 1)
    monkeypatch.setattr(
        sprint_store, "fix_abandoned",
        lambda u: T.abandoned.append(u) or [_item(status="open")])
    monkeypatch.setattr(zoho_actions, "unmark_agent_fix",
                        lambda t, u: T.unmarked.append((t, u)) or "")
    return T


def test_a_merge_moves_the_sprint_line_on(monkeypatch, tracked):
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: ("merged", "web", "abc123456"))
    ticket_fix.promote_merged(_tasks())
    assert tracked.progress == [(PR, "merged into dev as web@abc123456")]
    assert tracked.abandoned == []


def test_a_closed_pr_hands_the_ticket_back(monkeypatch, tracked):
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: ("closed", "web", ""))
    changed = ticket_fix.promote_merged(_tasks())
    assert tracked.abandoned == [PR]
    assert tracked.unmarked == [("569440000043239739", PR)]
    # Closed, never "on prod": on prod would email the client.
    assert ("PUT", "/task/P1", {"status": "Closed"}) in tracked.clickup
    assert ("PUT", "/task/C1", {"status": "Closed"}) in tracked.clickup
    assert not any(b == {"status": "on prod"} for _, _, b in tracked.clickup)
    assert any("handed back" in c for c in changed)


def test_an_open_pr_changes_nothing(monkeypatch, tracked):
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: ("open", "web", ""))
    assert ticket_fix.promote_merged(_tasks()) == []
    assert tracked.clickup == [] and tracked.abandoned == []


# ---------------------------------------------------------------------------
# The store (real Postgres; skipped without SPRINT_TEST_DATABASE_URL)
# ---------------------------------------------------------------------------

TEST_DB = os.environ.get("SPRINT_TEST_DATABASE_URL", "")


@pytest.fixture
def db(monkeypatch):
    from vomeos import config, store

    if not TEST_DB:
        pytest.skip("SPRINT_TEST_DATABASE_URL not set")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB)
    store.reset()
    store.execute(f"DROP TABLE IF EXISTS {sprint_store.ITEMS}")
    store.reset()
    assert sprint_store.ready()
    yield store
    store.reset()


def _stored(ticket="T1"):
    return sprint_store.upsert_ticket(
        ticket_id=ticket, ticket_number="9582", category="clear_bug",
        account="CHU", subject="Vues", summary="s", important=True,
        card={"lane": "agent_fix"}, draft="Bonjour", draft_status="ok",
        draft_why="", recipients={"to": "i@chu.ca"}, last_thread_id="T")


def test_store_lifecycle(db):
    item = _stored()
    sprint_store.set_fix_pr(item["id"], PR)
    item = sprint_store.get_item(item["id"])
    assert item["status"] == "fixing" and item["fix_pr"] == PR
    took = item["decided_at"]
    assert took is not None

    sprint_store.record_sent(item["id"], "Bonjour")
    sprint_store.keep_fixing(item["id"])
    item = sprint_store.get_item(item["id"])
    assert item["status"] == "fixing" and item["decided_at"] == took
    assert item["id"] in [i["id"] for i in sprint_store.working_set(0)]

    assert sprint_store.fix_progress(PR, "merged into dev as web@abc") == 1
    item = sprint_store.get_item(item["id"])
    assert item["decision"] == "merged into dev as web@abc"
    assert item["decided_at"] == took

    back = sprint_store.fix_abandoned(PR)
    assert [b["id"] for b in back] == [item["id"]]
    item = sprint_store.get_item(item["id"])
    assert item["status"] == "open"
    assert item["decision"] == sprint_store.FIX_ABANDONED
    assert item["fix_pr"] == PR


def test_fixing_carries_forward_to_the_next_day(db):
    item = _stored()
    sprint_store.set_fix_pr(item["id"], PR)
    db.execute(f"UPDATE {sprint_store.ITEMS} SET day = day - 1"
               " WHERE id = :i", {"i": item["id"]})
    assert sprint_store.carry_forward() == 1


def test_a_finished_item_is_not_reopened_by_a_late_pr(db):
    item = _stored()
    sprint_store.decide(item["id"], sprint_store.SENT, "sent by Sam")
    sprint_store.set_fix_pr(item["id"], PR)
    assert sprint_store.get_item(item["id"])["status"] == "sent"
