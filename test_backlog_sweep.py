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


# ---------------------------------------------------------------------------
# The audit (Sam, 2026-10-24: Zoho and ClickUp in sync)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
DAY_MS = 86400 * 1000
NOW_MS = int(NOW.timestamp() * 1000)


def _audit_row(**over):
    row = {"ticket_id": "5694", "ticket_number": "9600",
           "zoho_status": "In Progress", "zoho_owner": "onlyg",
           "pq_open": [{"id": "868t1", "status": "queued",
                        "assignees": [49257687],
                        "updated": str(NOW_MS - DAY_MS)}],
           "pq_done": [], "fix_parents": [], "fix_children": [],
           "accepted": [], "sprint_open": False, "last_in": "",
           "last_out": "", "carded_after_cutoff": True}
    row.update(over)
    return row


def _cls(**over):
    return bs.classify_row(_audit_row(**over), NOW)["drift"]


def test_an_engineer_task_with_matching_owner_and_a_fresh_card_is_healthy():
    assert _cls() == 12


def test_a_task_waiting_on_the_client_is_still_on_the_engineers_plate():
    # 38 tasks at `awaiting client response` were never swept (2026-10-24).
    task = {"id": "868t1", "status": "awaiting client response",
            "assignees": [4434086], "updated": str(NOW_MS - DAY_MS)}
    assert _cls(pq_open=[task], zoho_owner="sanjay",
                carded_after_cutoff=False) == 10


def test_a_done_task_the_client_never_heard_about_is_class_1():
    done = {"id": "868t2", "status": "on prod", "assignees": [49257687],
            "updated": str(NOW_MS - 5 * DAY_MS),
            "closed": str(NOW_MS - 5 * DAY_MS)}
    row = bs.classify_row(_audit_row(
        pq_open=[], pq_done=[done], last_out="2026-10-01T10:00:00Z"), NOW)
    assert row["drift"] == 1
    told = _cls(pq_open=[], pq_done=[done], last_out="2026-10-23T10:00:00Z")
    assert told == 2  # answered, but Zoho still says In Progress


def test_zoho_closed_with_open_clickup_work_is_an_orphan():
    assert _cls(zoho_status="Closed") == 8


def test_a_deleted_ticket_the_sprint_still_lists_is_class_4():
    # #9539 returned 404 and sprint_queue still showed it.
    assert _cls(zoho_status=bs.ZOHO_GONE, pq_open=[], sprint_open=True) == 4


def test_a_copied_engineer_task_beside_an_agent_fixes_child_is_a_duplicate():
    child = {"id": "868c1", "status": "in progress", "assignees": [],
             "parent": "868p1"}
    assert _cls(fix_children=[child], zoho_owner="agent_fixes") == 9


def test_answered_but_still_processing_is_class_5_and_the_reverse_is_6():
    assert _cls(zoho_status="Processing", zoho_owner="sam",
                last_in="2026-10-20T10:00:00Z",
                last_out="2026-10-21T10:00:00Z") == 5
    assert _cls(zoho_status="Awaiting Client Response",
                last_in="2026-10-22T10:00:00Z",
                last_out="2026-10-21T10:00:00Z") == 6


def test_owner_must_follow_the_clickup_work():
    assert _cls(zoho_owner="sanjay") == 7
    parent = {"id": "868p1", "status": "in progress", "assignees": [],
              "check": "waiting"}
    assert _cls(pq_open=[], fix_parents=[parent], zoho_owner="sam") == 7
    assert _cls(pq_open=[], fix_parents=[parent],
                zoho_owner="agent_fixes") == 11
    assert _cls(pq_open=[], zoho_owner="agent_fixes") == 7


def test_a_stalled_or_unassigned_task_is_class_3():
    old = {"id": "868t1", "status": "queued", "assignees": [49257687],
           "updated": str(NOW_MS - 20 * DAY_MS)}
    assert _cls(pq_open=[old]) == 3
    nobody = {"id": "868t1", "status": "queued", "assignees": [],
              "updated": str(NOW_MS)}
    assert _cls(pq_open=[nobody], zoho_owner="sam") == 3


def test_in_progress_with_nothing_behind_it_is_class_2():
    assert _cls(pq_open=[], zoho_owner="sam") == 2


def test_join_matches_by_link_and_number_and_keeps_ticketless_work():
    link = ("https://desk.zoho.com/agent/vomevolunteer/vome-volunteer/"
            "tickets/details/5694")
    pq = [
        {"id": "868a", "name": "[#9600] Acme -- form", "status":
         {"status": "queued"}, "assignees": [{"id": 49257687}],
         "custom_fields": [{"id": "4776215b-c725-4d79-8f20-c16f0f0145ac",
                            "value": link}]},
        {"id": "868b", "name": "Backend: add sites field", "status":
         {"status": "queued"}, "assignees": [{"id": 49257687}]},
    ]
    zoho = [{"id": "5694", "ticketNumber": "9600", "status": "In Progress",
             "assigneeId": "569440000023160001", "subject": "form"}]
    rows = bs.join_audit(zoho, {}, {}, pq, [], [], [], set())
    by_task = {r["pq_open"][0]["id"]: r for r in rows}
    assert by_task["868a"]["ticket_id"] == "5694"
    assert by_task["868a"]["zoho_owner"] == "onlyg"
    assert by_task["868b"]["zoho_status"] == ""
    assert bs.classify_row(by_task["868b"], NOW)["drift"] == 10


def test_the_audit_sheet_has_no_em_dashes():
    audit = {"generated_at": "2026-10-24T18:00:00+00:00",
             "cutoff": bs.DEEP_CUTOFF, "unit_cost": {},
             "rows": [bs.classify_row(_audit_row(zoho_owner="sanjay"), NOW)]}
    assert "—" not in bs.render_audit(audit)


def test_the_on_prod_email_sent_hours_before_the_close_counts_as_told():
    # #7922: the reply went out the day the task closed. That is the
    # resolution, not an earlier answer.
    done = {"id": "868t2", "status": "closed", "assignees": [49257687],
            "closed": str(NOW_MS - 5 * DAY_MS)}
    hours_before = datetime.fromtimestamp(
        (NOW_MS - 5 * DAY_MS) / 1000 - 3 * 3600, timezone.utc).isoformat()
    assert _cls(pq_open=[], pq_done=[done], last_out=hours_before) == 2


def test_a_fix_that_moved_the_task_in_does_not_close_it(monkeypatch):
    import ticket_fix

    monkeypatch.setattr(ticket_fix, "propose_for_ticket",
                        lambda *a, **k: {"pr_url": "https://pr/1",
                                         "parent": "P1",
                                         "child": "868abc123"})
    monkeypatch.setattr(bs, "release_task",
                        lambda *a: pytest.fail("the moved task was closed"))
    out = bs.apply_one("fix", _entry(), _record(lane="agent_fix"))
    assert out["ok"] and "moved in" in out["did"]
