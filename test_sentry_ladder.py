"""
test_sentry_ladder.py

Sam, 2026-10-20: eleven Sentry tasks sat at `queued` in Agent Fixes with a
diagnosis, no fix and no owner. "Either it should be routed to an engineer
or fixed by the agent." These cases pin the ladder that replaced parking.
"""

import pytest

import sentry_ladder as ladder

CORE = "vomedjango-restored-core-app"
GOOD = {"root_cause": "SentEmail row written without content_mode",
        "evidence": "birthday_tasks.py:1625",
        "proposed_fix": "set content_mode in send_vome_email",
        "risk": "medium", "confidence": "high", "needs_migration": False,
        "duplicate_of": "",
        "files": ["notifications_app/utils/email_utils.py"]}


def _issue(**over):
    issue = {"issue_id": "77", "title": "IntegrityError: content_mode",
             "culprit": "ProcessScheduledBirthdayEmailsTask", "repo": CORE,
             "ref": "master", "host": "bitbucket", "severity": "s2",
             "triage_summary": "Birthday emails fail to send",
             "permalink": "https://sentry.io/issues/77/",
             "slack_ts": "1.2", "slack_channel": "CSENTRY"}
    issue.update(over)
    return issue


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("over, lane", [
    ({}, ladder.FIX),
    ({"risk": "low", "confidence": "medium"}, ladder.FIX),
    ({"risk": "high"}, ladder.GREENLIGHT),
    ({"risk": ""}, ladder.GREENLIGHT),
    ({"confidence": "low"}, ladder.REPORT),
    ({"confidence": ""}, ladder.REPORT),
    ({"needs_migration": True}, ladder.ENGINEER),
    ({"files": []}, ladder.ENGINEER),
    ({"proposed_fix": ""}, ladder.ENGINEER),
])
def test_decide(over, lane):
    assert ladder.decide(_issue(), {**GOOD, **over})[0] == lane


def test_infrastructure_goes_to_an_engineer_even_when_easy():
    lane, why = ladder.decide(_issue(infrastructure=True),
                              {**GOOD, "risk": "low"})
    assert lane == ladder.ENGINEER and "settings" in why


def test_a_migration_outranks_low_confidence():
    lane, _ = ladder.decide(_issue(), {**GOOD, "confidence": "low",
                                       "needs_migration": True})
    assert lane == ladder.ENGINEER


# ---------------------------------------------------------------------------
# Lanes, with ClickUp, Slack and the fix author faked
# ---------------------------------------------------------------------------

@pytest.fixture
def world(monkeypatch):
    import sentry_fix
    import sprint_thread

    w = {"said": [], "posted": [], "tasks": [], "closed": [], "put": [],
         "comments": [], "ledger": {}, "proposal": {
             "attempted": True, "diff": "--- a\n+++ b\n", "reason": "ok"},
         "pr": {"opened": True, "url": "https://bb/pr/1", "branch": "b"},
         "filed_today": 0, "row_task": "",
         "proposed_with": []}

    monkeypatch.setattr(ladder, "_say", lambda issue, text:
                        w["said"].append(text))
    monkeypatch.setattr(ladder.sentry_slack, "_post",
                        lambda ch, text, thread_ts="":
                        w["posted"].append((ch, text)) or "9.9")
    # Engineer pings go through eng_priority; no store, so unthreaded.
    import eng_priority

    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", channel="",
                        broadcast=False:
                        w["posted"].append((channel, text)) or "9.8")
    monkeypatch.setattr(eng_priority.store, "ensure_table", lambda t: False)
    monkeypatch.setattr(ladder.sentry_agent_fixes, "create_task",
                        lambda *a, **k: w["tasks"].append((a, k))
                        or f"86t{len(w['tasks'])}")
    monkeypatch.setattr(ladder.sentry_agent_fixes, "comment",
                        lambda t, text: w["comments"].append((t, text))
                        or True)
    monkeypatch.setattr(ladder.sentry_agent_fixes, "existing_task",
                        lambda issue, analysis: "")
    monkeypatch.setattr(ladder.sentry_agent_fixes, "filed_today",
                        lambda: w["filed_today"])
    monkeypatch.setattr(ladder.sentry_agent_fixes, "enabled", lambda: True)
    monkeypatch.setattr(ladder, "_close_task",
                        lambda t, note: w["closed"].append((t, note)))
    monkeypatch.setattr(ladder, "_clickup_put",
                        lambda t, body: w["put"].append((t, body)) or True)
    monkeypatch.setattr(ladder, "_repoint", lambda old, new: None)
    monkeypatch.setattr(ladder, "_row_task", lambda i: w["row_task"])
    monkeypatch.setattr(ladder.sentry_ledger, "update",
                        lambda issue_id, **f: w["ledger"].setdefault(
                            issue_id, {}).update(f) or True)

    def propose(analysis, context, allow_risk=("low",)):
        w["proposed_with"].append((analysis, context, allow_risk))
        return w["proposal"]

    monkeypatch.setattr(sentry_fix, "propose", propose)
    monkeypatch.setattr(sentry_fix, "open_pull_request",
                        lambda issue, proposal, ctx: w["pr"])
    monkeypatch.setitem(sprint_thread.ENGINEERS, "onlyg",
                        (49257687, "UONLYG", "backend", "CBACKEND"))
    monkeypatch.setitem(sprint_thread.ENGINEERS, "sanjay",
                        (4434086, "USANJAY", "frontend", "CFRONTEND"))
    return w


def test_medium_risk_is_fixed_by_the_agent(world):
    out = ladder.place(_issue(), GOOD, source="src")
    assert out["lane"] == ladder.FIX and out["pr"] == "https://bb/pr/1"
    analysis, context, allow = world["proposed_with"][0]
    assert allow == ("low", "medium") and "greenlight" not in context
    assert world["tasks"][0][0][2]["opened"] is True
    assert world["ledger"]["77"]["lane"] == ladder.FIX
    assert "https://bb/pr/1" in world["said"][0]
    assert not any("<@" in s for s in world["said"])  # nobody interrupted


def test_high_risk_asks_sam_and_builds_nothing(world):
    out = ladder.place(_issue(), {**GOOD, "risk": "high"})
    assert out["lane"] == ladder.GREENLIGHT
    assert world["proposed_with"] == []
    assert world["said"][0].startswith(f"<@{ladder.SAM_SLACK}> *Build this?*")
    _, kwargs = world["tasks"][0]
    assert kwargs["assignees"] == [ladder.SAM_CLICKUP]


def test_a_fix_that_will_not_build_asks_sam_with_the_reason(world):
    world["proposal"] = {"attempted": False, "diff": "",
                         "reason": "patch refused: touches settings"}
    out = ladder.place(_issue(), GOOD)
    assert out["lane"] == ladder.GREENLIGHT
    assert "patch refused" in world["said"][0]


def test_low_confidence_is_reported_with_no_task(world):
    out = ladder.place(_issue(), {**GOOD, "confidence": "low"})
    assert out["lane"] == ladder.REPORT
    assert world["tasks"] == []
    assert "For your awareness" in world["said"][0]
    assert "`route`" in world["said"][0]


def test_a_migration_goes_to_onlyg_in_priority_queue(world):
    out = ladder.place(_issue(), {**GOOD, "needs_migration": True})
    assert out["lane"] == ladder.ENGINEER and out["engineer"] == "onlyg"
    _, kwargs = world["tasks"][0]
    assert kwargs["list_id"] == ladder.PRIORITY_QUEUE
    assert kwargs["assignees"] == [49257687]
    # S2 is high priority: no ping, in the morning list (Sam, 2026-10-27).
    assert world["posted"] == [] and out["pinged"] is False
    assert world["said"][0].startswith(
        ":white_check_mark: Sent to OnlyG (it needs a migration")
    assert "high priority, in the morning list" in world["said"][0]


def test_an_s1_pings_the_engineer(world):
    out = ladder.place(_issue(severity="s1"),
                       {**GOOD, "needs_migration": True})
    assert out["pinged"] is True
    channel, text = world["posted"][0]
    assert channel == "CBACKEND"
    assert text.startswith(":rotating_light: <@UONLYG> New backend bug "
                           "from Sentry")
    assert "pinged" in world["said"][0]


def test_a_frontend_issue_goes_to_sanjay(world):
    out = ladder.place(_issue(host="github", severity="s1"),
                       {**GOOD, "files": []})
    assert out["engineer"] == "sanjay"
    assert world["posted"][0][0] == "CFRONTEND"


def test_a_known_cause_only_comments(world, monkeypatch):
    monkeypatch.setattr(ladder.sentry_agent_fixes, "existing_task",
                        lambda issue, analysis: "86first")
    out = ladder.place(_issue(), GOOD)
    assert out == {"lane": ladder.GROUPED, "task_id": "86first"}
    assert world["comments"][0][0] == "86first"
    assert world["said"] == [] and world["proposed_with"] == []


def test_past_the_task_cap_sam_is_still_told(world):
    world["filed_today"] = 5
    out = ladder.place(_issue(), GOOD)
    assert out["lane"] == ladder.REPORT and "cap" in out["why"]
    assert world["tasks"] == []


def test_a_failure_is_reported_not_raised(world, monkeypatch):
    def boom(issue, analysis):
        raise RuntimeError("ClickUp 500")

    monkeypatch.setattr(ladder.sentry_agent_fixes, "existing_task", boom)
    assert "500" in ladder.place(_issue(), GOOD)["error"]


def test_no_em_dashes_in_anything_said(world):
    for analysis in (GOOD, {**GOOD, "risk": "high"},
                     {**GOOD, "confidence": "low"},
                     {**GOOD, "needs_migration": True}):
        ladder.place(_issue(), analysis)
    text = "\n".join(world["said"] + [t for _, t in world["posted"]])
    assert chr(0x2014) not in text


# ---------------------------------------------------------------------------
# Sam's replies
# ---------------------------------------------------------------------------

def test_only_sams_thread_replies_are_queued(monkeypatch):
    queued = []
    import vomeos.worker as worker

    monkeypatch.setattr(worker, "enqueue_event",
                        lambda key, payload, queue="": queued.append(payload))
    ladder.handle_message({"user": "UOTHER", "ts": "2", "thread_ts": "1",
                           "text": "yes"})
    ladder.handle_message({"user": ladder.SAM_SLACK, "ts": "1",
                           "thread_ts": "", "text": "yes"})
    ladder.handle_message({"user": ladder.SAM_SLACK, "ts": "2",
                           "thread_ts": "1", "text": "yes"})
    assert queued == [{"thread_ts": "1", "ts": "2", "text": "yes"}]


@pytest.fixture
def thread(world, monkeypatch):
    row = {"issue_id": "77", "project": "prod-vome", "title": "t",
           "slack_ts": "1.2", "slack_channel": "CSENTRY",
           "lane": ladder.GREENLIGHT, "clickup_task_id": "86ask",
           "analysis": {**GOOD, "risk": "high"}}
    monkeypatch.setattr(ladder.store, "query", lambda sql, params: [row])
    monkeypatch.setattr(ladder, "_source", lambda issue: "fresh src")
    return row


def test_yes_builds_with_the_risk_bar_lifted(world, thread):
    out = ladder.handle_reply({"thread_ts": "1.2", "text": "yes build it"})
    assert out["lane"] == ladder.FIX
    analysis, context, _ = world["proposed_with"][0]
    assert analysis["risk"] == "low" and "Sam" in context["greenlight"]
    assert context["source"] == "fresh src"


def test_a_greenlit_build_that_fails_goes_to_an_engineer(world, thread):
    world["pr"] = {"opened": False, "reason": "patch does not apply"}
    out = ladder.handle_reply({"thread_ts": "1.2", "text": "fix"})
    assert out["lane"] == ladder.ENGINEER
    assert "could not build" in world["said"][-1]


def test_route_frontend(world, thread):
    out = ladder.handle_reply({"thread_ts": "1.2",
                               "text": "route frontend"})
    assert out["engineer"] == "sanjay"


def test_close(world, thread):
    out = ladder.handle_reply({"thread_ts": "1.2", "text": "close"})
    assert out["lane"] == ladder.CLOSED
    assert world["closed"][0][0] == "86ask"
    assert world["ledger"]["77"]["lane"] == ladder.CLOSED


def test_a_conversation_gets_a_hint_and_no_action(world, thread):
    out = ladder.handle_reply({"thread_ts": "1.2",
                               "text": "yes but only for birthdays"})
    assert out["action"] == "hint" and world["proposed_with"] == []


def test_yes_on_a_fixed_issue_does_nothing(world, thread):
    thread["lane"] = ladder.FIX
    out = ladder.handle_reply({"thread_ts": "1.2", "text": "yes"})
    assert out["status"] == "ignored"


def test_a_thread_that_is_not_an_issue_is_ignored(monkeypatch):
    monkeypatch.setattr(ladder.store, "query", lambda sql, params: [])
    out = ladder.handle_reply({"thread_ts": "5.5", "text": "yes"})
    assert out["status"] == "ignored"


# ---------------------------------------------------------------------------
# The parked backlog
# ---------------------------------------------------------------------------

def test_a_parked_task_closes_and_points_at_the_fix(world):
    ladder.place(_issue(), GOOD, parked_task="86old")
    old, note = world["closed"][0]
    assert old == "86old" and "https://bb/pr/1" in note


def test_a_parked_task_reused_as_sams_decision_stays_open(world):
    world["row_task"] = "86old"
    out = ladder.place(_issue(), {**GOOD, "risk": "high"},
                       parked_task="86old")
    assert out["task_id"] == "86old"
    assert world["closed"] == []
    assert world["put"] == [("86old",
                             {"assignees": {"add": [ladder.SAM_CLICKUP]}})]


@pytest.fixture
def parked(monkeypatch):
    import sentry_handler
    import vomeos.worker as worker

    rows = {"86a": [{"issue_id": "1", "lane": "fix"}],
            "86b": [{"issue_id": "2", "lane": None}]}
    monkeypatch.setattr(ladder, "_parked_tasks",
                        lambda: [{"id": "86a"}, {"id": "86b"}])
    monkeypatch.setattr(ladder.store, "query",
                        lambda sql, params: rows[params["task"]])
    ran, queued = [], []
    monkeypatch.setattr(sentry_handler, "reinvestigate",
                        lambda row, parked_task="": ran.append(
                            (row["issue_id"], parked_task))
                        or {"lane": "engineer"})
    monkeypatch.setattr(worker, "enqueue_event",
                        lambda key, payload, queue="": queued.append(
                            (key, payload)))
    return ran, queued


def test_sweep_queues_one_job_per_unplaced_task(parked):
    ran, queued = parked
    out = ladder.sweep_parked()
    assert queued == [(ladder.PARKED_KEY, {"task_id": "86b"})]
    assert out["skipped"] == ["86a"] and ran == []


def test_a_parked_job_reruns_the_busiest_issue(parked):
    ran, _ = parked
    ladder.place_parked({"task_id": "86b"})
    assert ran == [("2", "86b")]
    assert ladder.place_parked({"task_id": "86a"})["status"] == "ignored"
