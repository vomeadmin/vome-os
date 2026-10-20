"""
The existing-work search reads the strongest candidates, not just titles.

#9560 (2026-10-20): the matching open task's description already named the
line that skipped the coordinator, but triage only saw the task name, which
said "alerts" where the client said "notification", and called the ticket
vague. The client's open tasks now come with their description and latest
comments, whatever their title words.
"""

import clickup_search as C


def _task(tid, name, status="queued"):
    return {"id": tid, "name": name, "status": status, "list": "Priority Queue",
            "url": "", "zoho_ticket_id": ""}


def _setup(monkeypatch, tasks, briefs):
    monkeypatch.setattr(C, "CLICKUP_API_TOKEN", "test-token")
    monkeypatch.setattr(C, "LISTS", {"Priority Queue": "1"})
    monkeypatch.setattr(C, "_fetch_list", lambda list_id, name: tasks)
    monkeypatch.setattr(C, "search_tasks_by_text", lambda q, limit=5: [])
    monkeypatch.setattr(C, "_task_brief", lambda tid: briefs.get(tid, ""))


def test_same_client_task_is_briefed_even_without_shared_title_words(monkeypatch):
    tasks = [_task("A1", "Sarasota County Government -- Recruitment Workflow "
                         "admin alerts missing")]
    _setup(monkeypatch, tasks, {
        "A1": "    Description: coordinators skipped `if not shift_coordinators:`"})
    out = C.describe_existing_work(
        "New Application Notification Issue", ticket_number="9560",
        account="Sarasota County Government")
    assert "if not shift_coordinators" in out
    assert "verify it on the live branch" in out


def test_shipped_client_tasks_are_not_briefed(monkeypatch):
    tasks = [_task("S1", "Sarasota County Government -- old fix", "Closed")]
    _setup(monkeypatch, tasks, {"S1": "    Description: should not appear"})
    out = C.describe_existing_work("anything", account="Sarasota County Government")
    assert "should not appear" not in out


def test_briefs_are_capped(monkeypatch):
    tasks = [_task(f"T{i}", f"Sarasota County Government -- task {i}")
             for i in range(10)]
    _setup(monkeypatch, tasks, {f"T{i}": f"    Description: brief {i}"
                                for i in range(10)})
    out = C.describe_existing_work("x", account="Sarasota County Government")
    assert out.count("Description:") == C._BRIEFS_PER_SEARCH
