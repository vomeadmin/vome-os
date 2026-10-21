"""
test_backlog_sweep.py

Sam, 2026-10-20: the work filed with OnlyG and Sanjay before triage existed
gets one pass, and what an answer or an agent fix settles comes off their
plates. What matters here: only work actually on an engineer's plate is
swept, each card lands in the group that matches what would happen to it,
a task an engineer has started is never taken without asking, apply never
runs by accident, and releasing a task never sets a status that emails the
client.
"""

import pytest

import backlog_sweep as bs


def _task(**over):
    task = {"id": "868abc123", "name": "[#9512] Sarasota -- form not saving",
            "url": "https://app.clickup.com/t/868abc123",
            "status": {"status": "queued"},
            "assignees": [{"id": 49257687, "username": "OnlyG"}],
            "date_created": "1760000000000", "date_updated": "1760900000000",
            "custom_fields": [{"id": "4776215b-c725-4d79-8f20-c16f0f0145ac",
                               "value": "https://support.vomevolunteer.com/"
                                        "support/vomevolunteer/ShowHomePage"
                                        ".do#Cases/dv/569440000043050400"}]}
    task.update(over)
    return task


def _entry(**over):
    entry = bs.task_entry(_task())
    entry.update(over)
    return entry


def _record(**card):
    base = {"category": "clear_bug", "lane": "engineer", "confidence": "high",
            "escalation_reason": "schema_or_migration",
            "code_check": {"verdict": "bug", "fix_risk": "high",
                           "where": "core:form_app/v.py:9"}}
    base.update(card)
    return {"status": "ok", "card": base, "zoho_status": "In Progress"}


# ---------------------------------------------------------------------------
# What is on an engineer's plate
# ---------------------------------------------------------------------------

def test_a_queued_engineer_task_is_swept_with_its_ticket():
    entry = bs.task_entry(_task())
    assert entry["key"] == "868abc123"
    assert entry["engineers"] == ["onlyg"]
    assert entry["zoho_ticket_id"] == "569440000043050400"
    assert entry["ticket_number"] == "9512"
    assert entry["started"] is False


def test_in_progress_is_swept_but_flagged_started():
    entry = bs.task_entry(_task(status={"status": "in progress"}))
    assert entry["started"] is True


@pytest.mark.parametrize("status", ["on dev", "escalated", "needs client info",
                                    "awaiting client response", "sleeping"])
def test_work_off_the_plate_is_not_swept(status):
    """on dev is finished, escalated is Sam's, the rest wait on a client."""
    assert bs.task_entry(_task(status={"status": status})) is None


def test_a_task_with_no_engineer_is_not_swept():
    assert bs.task_entry(_task(assignees=[{"id": 3691763}])) is None


def test_zoho_in_progress_with_an_engineering_team_is_swept():
    row = {"id": "5694400000431", "ticketNumber": "9400",
           "status": "In Progress", "assigneeId": next(
               k for k, v in bs.ZOHO_TEAMS.items() if v == "sanjay"),
           "subject": "Kiosk blank"}
    entry = bs.zoho_entry(row)
    assert entry["key"] == "zoho:5694400000431"
    assert entry["engineers"] == ["sanjay"]


def test_zoho_in_progress_with_sam_is_not_swept():
    """In Progress with Sam is an agent fix waiting on his merge."""
    row = {"id": "1", "status": "In Progress",
           "assigneeId": "569440000000139001"}
    assert bs.zoho_entry(row) is None


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

def test_engineer_lane_stays_with_the_engineer():
    assert bs.bucket(_entry(), _record())[0] == "engineer"


def test_works_as_designed_is_an_answer_even_in_the_engineer_lane():
    record = _record(code_check={"verdict": "works_as_designed"},
                     category="user_education", lane="answer")
    assert bs.bucket(_entry(), record)[0] == "answer"


def test_a_small_low_risk_bug_is_an_agent_fix():
    record = _record(lane="agent_fix", escalation_reason="",
                     code_check={"verdict": "bug", "fix_risk": "low",
                                 "confidence": "medium",
                                 "where": "web:src/x.js:10"})
    group, why = bs.bucket(_entry(), record)
    assert group == "agent_fix"
    assert "web:src/x.js:10" in why


def test_a_product_call_is_sams_never_the_engineers():
    record = _record(escalation_reason="product_decision")
    assert bs.bucket(_entry(), record)[0] == "sam_decides"
    design = _record(code_check={"verdict": "design_question"})
    assert bs.bucket(_entry(), design)[0] == "sam_decides"


def test_sam_ops_is_sams():
    assert bs.bucket(_entry(), _record(lane="sam_ops"))[0] == "sam_ops"


def test_a_live_fix_or_nothing_owed_closes():
    live = _record()
    live["release_live"] = True
    assert bs.bucket(_entry(), live)[0] == "close"
    assert bs.bucket(_entry(), _record(lane="housekeeping"))[0] == "close"
    assert bs.bucket(_entry(),
                     _record(category="awaiting_client"))[0] == "close"


def test_needs_human_needs_a_look():
    record = _record(category="needs_human", lane="engineer",
                     why_human="earlier reply looks wrong")
    group, why = bs.bucket(_entry(), record)
    assert group == "needs_look"
    assert "earlier reply" in why


def test_a_failed_triage_is_not_triaged():
    assert bs.bucket(_entry(), {"status": "error", "why": "timeout"}) == (
        "unreadable", "timeout")


# ---------------------------------------------------------------------------
# Apply never runs by accident
# ---------------------------------------------------------------------------

def _seed(tmp_path, monkeypatch, entry, record):
    monkeypatch.setattr(bs, "OUT_DIR", tmp_path)
    bs._save(bs.INVENTORY, {"entries": [entry]})
    bs._save(bs.CARDS, {entry["key"]: record})


def test_apply_refuses_outside_the_worker_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    _seed(tmp_path, monkeypatch, _entry(), _record())
    called = []
    monkeypatch.setattr(bs, "apply_one", lambda *a: called.append(a))
    assert bs.run_apply("note", ["868abc123"], True, False) == 1
    assert called == []


def test_apply_without_confirm_is_a_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    _seed(tmp_path, monkeypatch, _entry(), _record())
    called = []
    monkeypatch.setattr(bs, "apply_one", lambda *a: called.append(a))
    assert bs.run_apply("note", ["9512"], False, False) == 0
    assert called == []


def test_a_started_task_is_never_taken_without_asking(tmp_path, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    _seed(tmp_path, monkeypatch, _entry(started=True), _record())
    called = []
    monkeypatch.setattr(bs, "apply_one",
                        lambda *a: called.append(a) or {"ok": True})
    bs.run_apply("answer", ["868abc123"], True, False)
    assert called == []
    bs.run_apply("answer", ["868abc123"], True, True)
    assert len(called) == 1


def test_an_applied_item_is_not_applied_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    _seed(tmp_path, monkeypatch, _entry(), _record())
    called = []
    monkeypatch.setattr(bs, "apply_one",
                        lambda *a: called.append(a) or {"ok": True,
                                                        "did": "x"})
    bs.run_apply("note", ["868abc123"], True, False)
    bs.run_apply("note", ["868abc123"], True, False)
    assert len(called) == 1


# ---------------------------------------------------------------------------
# Releasing a task never emails the client
# ---------------------------------------------------------------------------

def test_release_closes_and_unassigns_only(monkeypatch):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_test")
    calls = []

    class _Resp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(bs.httpx, "post",
                        lambda url, **kw: calls.append(("post", url, kw))
                        or _Resp())
    monkeypatch.setattr(bs.httpx, "put",
                        lambda url, **kw: calls.append(("put", url, kw))
                        or _Resp())
    bs.release_task(_entry(), "A reply settles this.")
    put = [c for c in calls if c[0] == "put"][0][2]["json"]
    assert put["status"] == "Closed"
    assert put["status"].lower() not in ("user education", "needs client info",
                                         "on prod")
    assert put["assignees"] == {"add": [], "rem": [49257687]}
    comment = [c for c in calls if c[0] == "post"][0][2]["json"]
    assert comment["notify_all"] is False
    assert "approved by Sam" in comment["comment_text"]


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------

def test_the_sheet_groups_items_and_escapes_customer_text():
    entry = _entry(name="<script>alert(1)</script> form")
    record = _record(situation="Admin says <b>nothing</b> saves")
    page = bs.render_sheet({"entries": [entry]}, {entry["key"]: record}, {})
    assert 'id="engineer"' in page
    assert "<script>alert" not in page
    assert "&lt;b&gt;nothing&lt;/b&gt;" in page
    assert "\u2014" not in page


def test_a_feature_task_with_no_ticket_is_planned_work_not_sams_call():
    record = _record(lane="feature", category="feature_request")
    assert bs.bucket(_entry(zoho_ticket_id=""), record)[0] == "planned"
    assert bs.bucket(_entry(), record)[0] == "sam_decides"
