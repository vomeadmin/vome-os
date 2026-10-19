"""
test_feature_desk.py

#feature-requests (Sam, 2026-10-19). The architect decides the shape; this
tests the deterministic half around it: which posts are picked up, what
Sam's replies mean, which plans may become a pull request, and that a later
analysis can never reopen what is already built or logged.
"""

import json

import pytest

import feature_desk as fd
from vomeos.evaluate import compare

SAM = fd.SAM_SLACK
RON = "U01B6D235N1"


def _build_item(**over):
    item = {
        "n": "1", "title": "Return address parts on the profile load",
        "verdict": "agent_build", "shape": "return an existing field",
        "analysis": "The columns exist on User; two serializers omit them.",
        "evidence": "core:volunteer_app/serializers.py:812",
        "build": {"repo": "core",
                  "anchors": [
                      "volunteer_app/serializers.py:812",
                      "volunteer_app/api_version/v2/serializers.py:40"],
                  "plan": "Add the four fields to both Meta.fields.",
                  "contract": "volunteer.address_city etc., strings or null"},
    }
    item.update(over)
    return item


@pytest.fixture
def desk(monkeypatch):
    """Every outward call recorded instead of made."""
    calls = {"posts": [], "saves": [], "comments": [], "builds": [],
             "analyses": [], "tasks": []}
    monkeypatch.setattr(fd, "_post", lambda text, thread_ts="", channel="":
                        calls["posts"].append(text) or "1.0")
    monkeypatch.setattr(fd, "_save", lambda ts, **f:
                        calls["saves"].append(f))
    monkeypatch.setattr(fd, "_comment", lambda task, text:
                        calls["comments"].append((task, text)))
    monkeypatch.setattr(fd, "_set_source", lambda task: None)
    monkeypatch.setattr(fd, "_permalink", lambda ts: "")
    monkeypatch.setattr(fd, "_name", lambda user: "Ron")
    monkeypatch.setattr(fd, "_create_task", lambda item, ts, row:
                        calls["tasks"].append(item["n"]) or "T-NEW")
    return calls


# ---------------------------------------------------------------------------
# Sam's words
# ---------------------------------------------------------------------------

OPEN = {"1": {"state": fd.WAITING_SAM}, "2": {"state": fd.HELD},
        "3": {"state": fd.PR_OPEN}}


@pytest.mark.parametrize("text,verb,targets", [
    ("go 2", "go", [("2", "")]),
    ("go 2B", "go", [("2", "B")]),
    ("go 2 b", "go", [("2", "B")]),
    ("go 1 and 2", "go", [("1", ""), ("2", "")]),
    ("`log 1`", "log", [("1", "")]),
    ("no 1, 2", "no", [("1", ""), ("2", "")]),
    ("go", "go", [("1", ""), ("2", "")]),
])
def test_exact_commands(text, verb, targets):
    assert fd.parse_command(text, OPEN) == {"verb": verb, "targets": targets}


@pytest.mark.parametrize("text", [
    "go with B but keep it on the role",
    "No, I think it should be a new step type",
    "log it under the forms epic",
    "make it a step setting",
])
def test_anything_else_is_direction(text):
    assert fd.parse_command(text, OPEN) is None


def test_a_bare_verb_never_touches_a_final_item():
    command = fd.parse_command("no", OPEN)
    assert ("3", "") not in command["targets"]


# ---------------------------------------------------------------------------
# The web side
# ---------------------------------------------------------------------------

def test_always_listening_with_no_switch_set(monkeypatch, desk):
    """Sam, 2026-10-19: no on switch. His first real post went unanswered
    because FEATURE_DESK_ENABLED was never set."""
    monkeypatch.delenv("FEATURE_DESK_ENABLED", raising=False)
    queued = []
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda key, payload, queue="": queued.append(payload))
    fd.handle_message({"ts": "1.1", "text": "x" * 200})
    assert desk["posts"][0].startswith("Analysing this")
    assert queued == [{"ts": "1.1"}]


def test_a_short_top_level_post_is_chat(monkeypatch, desk):
    queued = []
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda *a, **k: queued.append(a))
    fd.handle_message({"ts": "1.1", "text": "Amazing"})
    assert desk["posts"] == [] and queued == []


def test_a_request_is_acknowledged_then_queued(monkeypatch, desk):
    queued = []
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda key, payload, queue="": queued.append(payload))
    fd.handle_message({"ts": "1.1", "text": "Please return the address "
                       "parts on the volunteer profile load, thanks."})
    assert desk["posts"][0].startswith("Analysing this")
    assert queued == [{"ts": "1.1"}]


def test_a_bare_clickup_link_is_a_request(monkeypatch, desk):
    queued = []
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda key, payload, queue="": queued.append(payload))
    fd.handle_message({"ts": "1.1",
                       "text": "<https://app.clickup.com/t/868mjq5gb>"})
    assert queued == [{"ts": "1.1"}]


def test_a_priority_queue_task_gets_one_thread(monkeypatch, desk):
    monkeypatch.setattr(fd.store, "ensure_table", lambda name: True)
    monkeypatch.setattr(fd.store, "query", lambda sql, params:
                        [{"ts": "9.9"}])
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    result = fd.intake_task("868mjq5gb")
    assert result == {"status": "skipped", "reason": "already has a thread",
                      "ts": "9.9"}
    assert desk["posts"] == []


def test_a_reply_is_queued_against_its_post(monkeypatch, desk):
    queued = []
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda key, payload, queue="": queued.append(payload))
    fd.handle_message({"ts": "2.2", "thread_ts": "1.1", "user": SAM,
                       "text": "go 1"})
    assert desk["posts"] == []
    assert queued == [{"ts": "1.1", "reply": {"ts": "2.2", "user": SAM,
                                              "text": "go 1"}}]


# ---------------------------------------------------------------------------
# What each verdict does
# ---------------------------------------------------------------------------

def test_a_concrete_plan_in_core_is_built(monkeypatch, desk):
    monkeypatch.setenv("FEATURE_DESK_PR_ENABLED", "true")
    monkeypatch.setattr(fd, "_build", lambda ts, row, items, n, **k:
                        desk["builds"].append(n))
    items = {"1": _build_item()}
    fd.act("1.1", {"task_id": ""}, {"summary": "Address"}, items)
    assert desk["builds"] == ["1"]
    assert items["1"]["state"] == fd.BUILDING


def test_with_prs_off_the_reply_says_ready_not_building(monkeypatch, desk):
    """Sam, 2026-10-19: the reply said "Building it now" and the next
    message said it was not."""
    monkeypatch.delenv("FEATURE_DESK_PR_ENABLED", raising=False)
    monkeypatch.setattr(fd, "_build", lambda *a, **k: pytest.fail("built"))
    items = {"1": _build_item()}
    fd.act("1.1", {"task_id": ""}, {"summary": "Address"}, items)
    assert items["1"]["state"] == fd.HELD
    assert len(desk["posts"]) == 1
    assert "`go 1` opens the pull request" in desk["posts"][0]
    assert "Building it now" not in desk["posts"][0]


@pytest.mark.parametrize("build,why", [
    ({"repo": "web", "anchors": ["src/a.py:1"], "plan": "x"}, "repo"),
    ({"repo": "core", "anchors": [], "plan": "x"}, "anchors"),
    ({"repo": "core", "anchors": ["a.py:1", "b.py:2", "c.py:3", "d.py:4"],
      "plan": "x"}, "anchors"),
    ({"repo": "core", "anchors": ["volunteer_app/serializers.py"],
      "plan": "x"}, "anchors"),
    ({"repo": "core", "anchors": ["a.py:1"], "plan": ""}, "plan"),
])
def test_a_plan_that_cannot_be_written_is_logged(monkeypatch, desk, build,
                                                 why):
    monkeypatch.setattr(fd, "_build", lambda *a, **k:
                        desk["builds"].append(a))
    items = {"1": _build_item(build=build)}
    fd.act("1.1", {"task_id": "T1"}, {"summary": "s"}, items)
    assert desk["builds"] == []
    assert items["1"]["verdict"] == "log"
    assert items["1"]["state"] == fd.LOGGED
    assert why in items["1"]["analysis"]


def test_log_with_a_linked_task_goes_on_that_task(desk):
    items = {"1": {"n": "1", "title": "Bulk duplicate shifts",
                   "verdict": "log", "analysis": "New endpoints + a field."}}
    fd.act("1.1", {"task_id": "T1"}, {"summary": "s"}, items)
    assert items["1"]["state"] == fd.LOGGED
    assert desk["comments"][0][0] == "T1"
    assert desk["tasks"] == []


def test_log_without_a_task_waits_for_sam(desk):
    items = {"1": {"n": "1", "title": "Time off alert", "verdict": "log",
                   "analysis": "A new admin notification type."}}
    fd.act("1.1", {"task_id": ""}, {"summary": "s"}, items)
    assert items["1"]["state"] == fd.WAITING_SAM
    assert desk["tasks"] == []
    assert f"<@{SAM}>" in desk["posts"][0]


def test_a_decision_shows_its_options_and_asks_sam(desk):
    items = {"1": {"n": "1", "title": "References", "verdict": "decision",
                   "analysis": "Two shapes fit.",
                   "options": [
                       {"key": "A", "shape": "option on the form step",
                        "tradeoffs": "reuses guest submissions",
                        "agent_can_build": False},
                       {"key": "B", "shape": "new step type",
                        "tradeoffs": "fifteen registries",
                        "agent_can_build": False}],
                   "recommendation": "A", "question": "A or B?"}}
    fd.act("1.1", {"task_id": ""}, {"summary": "References"}, items)
    text = desk["posts"][0]
    assert "*A.* option on the form step" in text
    assert "_Recommend:_ A" in text and f"<@{SAM}>" in text
    assert items["1"]["state"] == fd.WAITING_SAM


def test_needs_info_turns_into_sam_after_too_many_rounds(desk):
    items = {"1": {"n": "1", "title": "Export field",
                   "verdict": "needs_info", "question": "Which field?"}}
    fd.act("1.1", {"task_id": "", "rounds": fd.MAX_ROUNDS,
                   "requester_id": RON}, {"summary": "s"}, items)
    assert items["1"]["state"] == fd.WAITING_SAM


def test_not_a_request_closes_quietly(desk):
    items = {}
    result = fd.act("1.1", {"task_id": ""},
                    {"summary": "A thank you.", "not_a_request": True}, items)
    assert result["verdict"] == "not_a_request"
    assert desk["posts"][0].startswith("No backend request here")


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def test_pull_requests_off_holds_the_item(monkeypatch, desk):
    monkeypatch.delenv("FEATURE_DESK_PR_ENABLED", raising=False)
    items = {"1": _build_item()}
    result = fd._build("1.1", {"task_id": ""}, items, "1")
    assert result["built"] is False
    assert items["1"]["state"] == fd.HELD
    assert "go 1" in desk["posts"][0]


def test_sams_go_builds_with_pull_requests_off(monkeypatch, desk):
    """Sam, 2026-10-19: `go 1` was refused with "say go 1". The switch only
    stops the agent opening PRs on its own."""
    monkeypatch.delenv("FEATURE_DESK_PR_ENABLED", raising=False)
    proposed = []
    monkeypatch.setattr(fd, "_propose", lambda ts, item:
                        proposed.append(item["n"]) or
                        ({"reason": "declined"}, None))
    items = {"1": {**_build_item(), "state": fd.HELD}}
    fd._build("1.1", {"task_id": "T1"}, items, "1", sam_approved=True)
    assert proposed == ["1"]
    assert not any("is ready to build" in p for p in desk["posts"])


def test_the_daily_cap_holds_but_sam_lifts_it(monkeypatch, desk):
    monkeypatch.setenv("FEATURE_DESK_PR_ENABLED", "true")
    monkeypatch.setattr(fd, "_opened_today", lambda: 99)
    monkeypatch.setattr(fd, "_propose", lambda ts, item:
                        ({"reason": "declined"}, None))
    items = {"1": _build_item()}
    held = fd._build("1.1", {"task_id": "T1"}, items, "1")
    assert "daily cap" in held["reason"]
    items = {"1": _build_item()}
    tried = fd._build("1.1", {"task_id": "T1"}, items, "1",
                      sam_approved=True)
    assert "builder declined" in tried["reason"]
    assert items["1"]["state"] == fd.LOGGED


# ---------------------------------------------------------------------------
# Re-analysis
# ---------------------------------------------------------------------------

def test_a_new_analysis_never_reopens_a_final_item():
    previous = {"1": {"n": "1", "verdict": "agent_build",
                      "state": fd.PR_OPEN, "pr_url": "https://pr/1"},
                "2": {"n": "2", "verdict": "decision",
                      "state": fd.WAITING_SAM}}
    fresh = [{"n": 1, "verdict": "decision"},
             {"n": 2, "verdict": "agent_build"}]
    merged = fd._merge(previous, fresh, approved={"2"})
    assert merged["1"]["state"] == fd.PR_OPEN
    assert merged["1"]["pr_url"] == "https://pr/1"
    assert merged["2"]["verdict"] == "agent_build"
    assert merged["2"]["sam_approved"] is True


def _row(items, **over):
    row = {"ts": "1.1", "items": json.dumps(items), "task_id": "",
           "last_reply_ts": "", "status": fd.OPEN}
    row.update(over)
    return row


def test_sam_saying_thanks_costs_nothing(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        {"1": {"n": "1", "state": fd.WAITING_SAM}}))
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    result = fd._on_reply("1.1", {"ts": "2", "user": SAM,
                                  "text": "thanks!"})
    assert result["reason"] == "acknowledgement"


def test_sam_free_text_is_direction(monkeypatch, desk):
    seen = {}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        {"1": {"n": "1", "state": fd.WAITING_SAM}}))
    monkeypatch.setattr(fd, "analyse", lambda ts, **k: seen.update(k))
    fd._on_reply("1.1", {"ts": "2", "user": SAM,
                         "text": "Make it an option on the form step"})
    assert seen["direction"] == "Make it an option on the form step"


def test_sam_go_on_a_held_build_builds_it(monkeypatch, desk):
    items = {"1": {**_build_item(), "state": fd.HELD}}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(items))
    monkeypatch.setattr(fd, "_build", lambda ts, row, items, n, **k:
                        desk["builds"].append((n, k)))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "go 1"})
    assert desk["builds"] == [("1", {"sam_approved": True})]


def test_sam_go_on_a_decision_reanalyses_with_his_choice(monkeypatch, desk):
    seen = {}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        {"2": {"n": "2", "verdict": "decision", "state": fd.WAITING_SAM}}))
    monkeypatch.setattr(fd, "analyse", lambda ts, **k: seen.update(k))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "go 2B"})
    assert seen["direction"] == "Sam: build item 2 using option B"
    assert seen["approved"] == ["2"]


def test_only_sam_can_command(monkeypatch, desk):
    seen = {}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        {"1": {**_build_item(), "state": fd.HELD}}))
    monkeypatch.setattr(fd, "_build", lambda *a, **k: pytest.fail("built"))
    monkeypatch.setattr(fd, "analyse", lambda ts, **k: seen.update(k))
    fd._on_reply("1.1", {"ts": "2", "user": RON, "text": "go 1"})
    assert seen == {"direction": "", "reply": "Ron: go 1"}


def test_ron_and_sam_talking_is_read(monkeypatch, desk):
    seen = {}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        {"1": {"n": "1", "state": fd.WAITING_SAM}}))
    monkeypatch.setattr(fd, "analyse", lambda ts, **k: seen.update(k))
    fd._on_reply("1.1", {"ts": "2", "user": RON,
                         "text": "I'll hold the frontend gate until then"})
    assert seen["reply"] == "Ron: I'll hold the frontend gate until then"
    assert seen["direction"] == ""


def test_a_closed_thread_is_not_read(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _row({}, status=fd.CLOSED))
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    result = fd._on_reply("1.1", {"ts": "2", "user": SAM,
                                  "text": "Actually make it a step type"})
    assert result["reason"] == "thread closed"


def test_the_requester_answering_a_question_reanalyses(monkeypatch, desk):
    ran = []
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        {"1": {"n": "1", "state": fd.NEEDS_INFO}}))
    monkeypatch.setattr(fd, "analyse", lambda ts, **k: ran.append(ts))
    fd._on_reply("1.1", {"ts": "2", "user": RON,
                         "text": "The opportunity export, the hours column"})
    assert ran == ["1.1"]


def test_a_reply_is_handled_once(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _row({}, last_reply_ts="2"))
    result = fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "go"})
    assert result["reason"] == "reply already handled"


# ---------------------------------------------------------------------------
# Reading the thread: chiming in only when there is something to add
# ---------------------------------------------------------------------------

WAITING = {"n": "1", "title": "Custom HTML for Trial orgs",
           "verdict": "decision", "state": fd.WAITING_SAM,
           "analysis": "Plan entitlement.", "question": "A or B?"}


def test_a_read_with_nothing_to_add_posts_nothing(desk):
    items = {"1": dict(WAITING)}
    result = fd.act("1.1", {"task_id": "T1"},
                    {"summary": "s", "chime_in": False}, items, only=[])
    assert result["action"] == "read, stayed quiet"
    assert desk["posts"] == [] and desk["comments"] == []


def test_chiming_in_posts_the_line_not_the_card(desk):
    items = {"1": dict(WAITING)}
    fd.act("1.1", {"task_id": ""},
           {"summary": "s", "chime_in": True,
            "reply": "The gate reads plan_type only."}, items, only=[])
    assert desk["posts"] == ["The gate reads plan_type only."]


def test_a_changed_verdict_is_said_on_its_own(desk):
    items = {"1": {**WAITING, "verdict": "already_exists", "state": "",
                   "analysis": "Trials keep plan 3 or 4."},
             "2": {**WAITING, "n": "2", "title": "Other"}}
    fd.act("1.1", {"task_id": ""},
           {"summary": "Whole card", "chime_in": True,
            "reply": "Agreed, nothing to build."}, items, only=["1"])
    text = desk["posts"][0]
    assert text.startswith("Agreed, nothing to build.")
    assert "Custom HTML for Trial orgs" in text and "Other" not in text
    assert "Whole card" not in text and f"<@{SAM}>" not in text
    assert items["1"]["state"] == fd.EXISTS


def test_a_reply_round_keeps_unchanged_items(monkeypatch, desk):
    previous = {"1": {**WAITING, "options": [{"key": "A"}]},
                "2": {**_build_item(n="2"), "state": fd.HELD}}

    class Result:
        ok = True
        data = {"summary": "s", "chime_in": False, "reply": "", "items": [
            {"n": 1, "verdict": "decision", "title": "reworded"},
            {"n": 2, "verdict": "agent_build", "title": "reworded"}]}

    acted = {}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(previous))
    monkeypatch.setattr(fd, "_read_post", lambda ts: {
        "text": "req", "requester": "Ron", "requester_id": RON,
        "thread": "Sam: later", "task_id": "T1"})
    monkeypatch.setattr(fd, "_task_text", lambda t: "")
    monkeypatch.setattr(fd, "_existing", lambda t: "")
    monkeypatch.setattr("vomeos.run_agent", lambda *a, **k: Result())
    monkeypatch.setattr(fd, "act", lambda ts, row, card, items, **k:
                        acted.update(items=items, **k))
    fd.analyse("1.1", reply="Sam: later")
    assert acted["only"] == []
    assert acted["items"]["1"]["title"] == WAITING["title"]
    assert acted["items"]["2"]["state"] == fd.HELD


def test_a_failed_read_stays_out_of_the_thread(monkeypatch, desk):
    class Result:
        ok = False
        data = None

        def why(self):
            return "timeout"

    monkeypatch.setattr(fd, "_row", lambda ts: _row({"1": dict(WAITING)}))
    monkeypatch.setattr(fd, "_read_post", lambda ts: {
        "text": "req", "requester": "Ron", "requester_id": RON,
        "thread": "", "task_id": ""})
    monkeypatch.setattr(fd, "_task_text", lambda t: "")
    monkeypatch.setattr(fd, "_existing", lambda t: "")
    monkeypatch.setattr("vomeos.run_agent", lambda *a, **k: Result())
    result = fd.analyse("1.1", reply="Ron: hm")
    assert result["reason"] == "reply read failed"
    assert desk["posts"] == []


# ---------------------------------------------------------------------------
# ClickUp is the record of where the thread landed
# ---------------------------------------------------------------------------

def test_nothing_goes_on_the_task_while_an_item_is_open(desk):
    items = {"1": {"n": "1", "title": "Bulk duplicate shifts",
                   "verdict": "log", "analysis": "New endpoints."},
             "2": {**WAITING, "n": "2", "state": ""}}
    fd.act("1.1", {"task_id": "T1"}, {"summary": "s"}, items)
    assert items["1"]["state"] == fd.LOGGED
    assert desk["comments"] == []


def test_the_outcome_goes_on_the_task_once_it_settles(monkeypatch, desk):
    items = {"1": {"n": "1", "title": "Bulk", "verdict": "log",
                   "state": fd.LOGGED},
             "2": {**WAITING, "n": "2"}}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(items, task_id="T1"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "no 2"})
    assert len(desk["comments"]) == 1
    task, text = desk["comments"][0]
    assert task == "T1" and "Bulk" in text and "Trial" in text
    saved = json.loads(desk["saves"][-1]["items"])
    assert all(item["recorded"] for item in saved.values())
    fd._record("1.1", {"task_id": "T1"}, saved)
    assert len(desk["comments"]) == 1


def test_sams_log_goes_on_the_task_straight_away(monkeypatch, desk):
    items = {"1": {"n": "1", "title": "Bulk", "verdict": "log",
                   "state": fd.WAITING_SAM},
             "2": {**WAITING, "n": "2"}}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(items, task_id="T1"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "log 1"})
    assert [task for task, _ in desk["comments"]] == ["T1"]
    saved = json.loads(desk["saves"][-1]["items"])
    assert saved["1"]["recorded"] is True
    assert not saved["2"].get("recorded")


# ---------------------------------------------------------------------------
# The answer key can read into the items list
# ---------------------------------------------------------------------------

def test_compare_reads_into_lists():
    actual = {"items": [{"verdict": "log"}, {"verdict": "decision"}]}
    assert compare({"items.1.verdict": "decision"}, actual) == []
    assert compare({"items.2.verdict": "log"}, actual) != []
