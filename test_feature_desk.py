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
                  "anchors": ["volunteer_app/serializers.py:812",
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
    monkeypatch.setenv("FEATURE_DESK_ENABLED", "true")
    monkeypatch.setattr(fd, "_post", lambda text, thread_ts="", channel="":
                        calls["posts"].append(text) or "1.0")
    monkeypatch.setattr(fd, "_save", lambda ts, **f:
                        calls["saves"].append(f))
    monkeypatch.setattr(fd, "_comment", lambda task, text:
                        calls["comments"].append((task, text)))
    monkeypatch.setattr(fd, "_set_source", lambda task: None)
    monkeypatch.setattr(fd, "_permalink", lambda ts: "")
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

def test_off_by_default(monkeypatch, desk):
    monkeypatch.delenv("FEATURE_DESK_ENABLED", raising=False)
    fd.handle_message({"ts": "1.1", "text": "x" * 200})
    assert desk["posts"] == []


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
    monkeypatch.setattr(fd, "_build", lambda ts, row, items, n, **k:
                        desk["builds"].append(n))
    items = {"1": _build_item()}
    fd.act("1.1", {"task_id": ""}, {"summary": "Address"}, items)
    assert desk["builds"] == ["1"]
    assert items["1"]["state"] == fd.BUILDING


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
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        {"1": {**_build_item(), "state": fd.HELD}}))
    monkeypatch.setattr(fd, "_build", lambda *a, **k: pytest.fail("built"))
    result = fd._on_reply("1.1", {"ts": "2", "user": RON, "text": "go 1"})
    assert result["status"] == "skipped"


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
# The answer key can read into the items list
# ---------------------------------------------------------------------------

def test_compare_reads_into_lists():
    actual = {"items": [{"verdict": "log"}, {"verdict": "decision"}]}
    assert compare({"items.1.verdict": "decision"}, actual) == []
    assert compare({"items.2.verdict": "log"}, actual) != []
