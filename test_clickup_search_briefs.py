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
    monkeypatch.setattr(C, "RELEASE_LISTS", {})
    monkeypatch.setattr(C, "search_tasks_by_text", lambda q, limit=5, **k: [])
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


# #9501 (2026-10-20): the release behind Sam's "upcoming release" promise
# named the ticket only in a comment, in a list the search never read.

def _release_setup(monkeypatch, release, text):
    monkeypatch.setattr(C, "CLICKUP_API_TOKEN", "test-token")
    monkeypatch.setattr(C, "LISTS", {"Priority Queue": "1"})
    monkeypatch.setattr(C, "RELEASE_LISTS", {"Releases": "9"})
    monkeypatch.setattr(
        C, "_fetch_list",
        lambda list_id, name: [release] if list_id == "9" else [])
    monkeypatch.setattr(C, "search_tasks_by_text", lambda q, limit=5, **k: [])
    monkeypatch.setattr(C, "_task_brief", lambda tid: "")
    monkeypatch.setattr(C, "_task_text", lambda tid: text)


def test_a_release_naming_the_ticket_in_a_comment_is_found(monkeypatch):
    release = {**_task("R1", "Delete a sequence, a sequence field", "Closed"),
               "list": "Releases"}
    _release_setup(monkeypatch, release,
                   "Requesting client: Sarasota, Zoho #9501. Notify her.")
    out = C.describe_existing_work(
        "Sequence Fields - Delete Option", ticket_number="9501")
    assert "A RELEASE names THIS ticket" in out
    assert "R1" in out and "SHIPPED" in out


def test_a_release_for_another_ticket_is_not_claimed(monkeypatch):
    release = {**_task("R2", "Bulk export", "Closed"), "list": "Releases"}
    _release_setup(monkeypatch, release, "Zoho #95010 and PR #9501x")
    assert C.release_tasks_for("9501", "") == []


def test_an_unreadable_release_list_is_never_none(monkeypatch):
    _release_setup(monkeypatch, {}, "")

    def broken(list_id, name):
        if list_id == "9":
            raise RuntimeError("503")
        return []

    monkeypatch.setattr(C, "_fetch_list", broken)
    out = C.describe_existing_work("x", ticket_number="9501")
    assert "could NOT be searched" in out
    assert "do not call it unbacked" in out
