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
    calls["reactions"] = []
    monkeypatch.setattr(fd, "_react", lambda ts:
                        calls["reactions"].append(ts))
    monkeypatch.setattr(fd, "_row", lambda ts: {})
    monkeypatch.setattr(fd, "_name", lambda user: "Ron")
    monkeypatch.setattr(fd, "_create_task", lambda item, ts, row, **k:
                        calls["tasks"].append((item["n"], k)) or "T-NEW")
    calls["moves"], calls["updates"] = [], []
    calls["task_list"] = fd.PRIORITY_QUEUE

    def clickup(method, path, body=None):
        if method == "GET":
            return {"id": path.split("/")[-1], "status": {"status": "queued"},
                    "list": {"id": calls["task_list"], "name": "x"}}
        calls["updates"].append((path, body))
        return {}

    monkeypatch.setattr(fd, "_clickup", clickup)
    monkeypatch.setattr("clickup_tasks.move_clickup_task", lambda task, to:
                        calls["moves"].append((task, to)) or True)
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
    monkeypatch.delenv("FEATURE_DESK_AUTO_ANALYSE", raising=False)
    queued = []
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda key, payload, queue="": queued.append(payload))
    fd.handle_message({"ts": "1.1", "text": "x" * 200})
    assert desk["posts"][0] == fd.ASK_SAM_TEXT
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
    assert desk["posts"][0] == fd.ASK_SAM_TEXT
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
      "plan": "x"}, "more than 3 files"),
    ({"repo": "core", "anchors": [f"a.py:{n}" for n in range(1, 8)],
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


def test_a_helper_and_its_call_site_get_their_own_anchors():
    """2026-10-24: site and category filters needed _parse_request_data,
    a new helper and the _get_shifts call site 650 lines later in V4, plus
    Insights and V5. Three anchors could not reach the call site."""
    v4 = "opportunity_app/api_version/v4/api.py"
    item = _build_item(build={
        "repo": "core", "plan": "x",
        "anchors": [f"{v4}:680", f"{v4}:862", f"{v4}:1328",
                    "opportunity_app/api_version/v4/insights_api.py:362",
                    "opportunity_app/api_version/v5/api.py:1693"]})
    assert fd._can_build(item) == (True, "")


def test_log_with_a_linked_task_goes_on_that_task(desk):
    items = {"1": {"n": "1", "title": "Bulk duplicate shifts",
                   "verdict": "log", "analysis": "New endpoints + a field."}}
    fd.act("1.1", {"task_id": "T1"}, {"summary": "s"}, items)
    assert items["1"]["state"] == fd.LOGGED
    assert desk["comments"][0][0] == "T1"
    assert desk["tasks"] == []


def test_a_settled_thread_gets_a_check_mark_and_one_line(desk):
    """Sam, 2026-10-19: mark what is handled and on ClickUp."""
    items = {"1": {"n": "1", "state": fd.PR_OPEN, "recorded": True,
                   "fix_task": "AF1"},
             "2": {"n": "2", "state": fd.LOGGED}}
    fd._record("1.1", {"task_id": "T1"}, items)
    assert desk["reactions"] == ["1.1"]
    assert desk["comments"][0][0] == "T1"
    line = desk["posts"][-1]
    assert line.startswith(":white_check_mark: Handled: 1 PR open")
    assert "2 left in your Priority Queue" in line
    assert "app.clickup.com/t/T1" in line and "t/AF1" in line
    assert {"status": fd.SETTLED} in desk["saves"]


def test_no_check_mark_while_anything_is_open(desk):
    items = {"1": {"n": "1", "state": fd.LOGGED},
             "2": {"n": "2", "state": fd.WAITING_SAM}}
    fd._record("1.1", {"task_id": "T1"}, items)
    assert desk["reactions"] == [] and desk["comments"] == []


def test_the_check_mark_is_given_once(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: {"status": fd.SETTLED})
    fd._record("1.1", {"task_id": "T1"},
               {"1": {"n": "1", "state": fd.LOGGED, "recorded": True}})
    assert desk["reactions"] == [] and desk["posts"] == []


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


def test_a_refused_pull_request_waits_for_a_retry(monkeypatch, desk):
    """2026-10-19: Bitbucket refused the branch (token scope) and the item
    was logged, so `go 1` could never try again."""
    monkeypatch.setattr(fd, "_propose", lambda ts, item:
                        ({"diff": "--- a/x.py\n+++ b/x.py\n"}, None))
    monkeypatch.setattr(fd, "_open_pr", lambda *a: {
        "opened": False, "reason": "could not branch: " + fd._host_error(
            'HTTP 403: {"error": {"message": "Your credentials lack one or '
            'more required privilege scopes."}}')})
    items = {"1": {**_build_item(), "state": fd.HELD}}
    fd._build("1.1", {"task_id": "T1"}, items, "1", sam_approved=True)
    assert items["1"]["state"] == fd.HELD
    assert desk["comments"] == []
    assert "VOMEOS_BITBUCKET_WRITE_TOKEN" in desk["posts"][-1]
    assert "say `go 1` to try again" in desk["posts"][-1]


def test_go_retries_a_build_that_was_logged_by_a_failure(monkeypatch, desk):
    items = {"1": {**_build_item(), "state": fd.LOGGED}}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(items))
    monkeypatch.setattr(fd, "_build", lambda ts, row, items, n, **k:
                        desk["builds"].append(n))
    fd._on_reply("1.1", {"ts": "3", "user": SAM, "text": "go 1"})
    assert desk["builds"] == ["1"]


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


# The fix author's own words on profile status, 2026-10-21 (trimmed).
SHORT_OF_SOURCE = (
    "The change cannot be written safely from the source given. All three "
    "edits depend on names whose imports I cannot see: "
    "OrganizationOfflineVolunteerInvite in form_app/institution/serializers"
    ".py. The detail change relies on get_is_invited, which I was not shown "
    "and so cannot copy. To proceed I need: the import blocks of both files.")


@pytest.mark.parametrize("proposal", [
    {"reason": "the imports are outside the windows", "needs_context": True},
    {"reason": SHORT_OF_SOURCE},
])
def test_a_builder_short_of_source_waits_for_go(monkeypatch, desk, proposal):
    """2026-10-21: Sam said `go 1` on profile status, the builder could not
    see the imports or get_is_invited, and the item was logged. Missing
    source is fixable, so the item waits for `go 1` again."""
    monkeypatch.setattr(fd, "_propose", lambda ts, item: (proposal, None))
    items = {"1": {**_build_item(), "state": fd.HELD}}
    result = fd._build("1.1", {"task_id": "T1"}, items, "1",
                       sam_approved=True)
    assert result["built"] is False
    assert items["1"]["state"] == fd.HELD
    assert desk["comments"] == [] and desk["tasks"] == []
    assert "needs more source" in desk["posts"][-1]
    assert "say `go 1` to try again, or `log 1`" in desk["posts"][-1]


def test_the_source_a_builder_asks_for_comes_with_the_next_go(monkeypatch,
                                                              desk):
    """2026-10-24: the builder named v4/api.py:1328 as the span it needed,
    and the second `go 1` was shown the same windows and refused again."""
    import sentry_projects
    import vomeos.tools

    class Repo:
        slug, host, owner, branch, alias = "core-slug", "bitbucket", "", \
            "master", "core"

    v4 = "opportunity_app/api_version/v4/api.py"
    reasons = iter([
        f"The central edit is in source I was not shown: {v4}:1328-1330.",
        f"Still not shown: {v4}:1500 and {v4}:1328.",
    ])
    propose = fd._propose
    monkeypatch.setattr(fd, "_propose", lambda ts, item: (
        {"reason": next(reasons), "needs_context": True}, None))
    items = {"1": {**_build_item(), "state": fd.HELD}}
    fd._build("1.1", {"task_id": "T1"}, items, "1", sam_approved=True)
    fd._build("1.1", {"task_id": "T1"}, items, "1", sam_approved=True)
    assert items["1"]["source_asked"] == [f"{v4}:1328", f"{v4}:1500"]

    seen = {}
    monkeypatch.setattr(fd, "_propose", propose)
    monkeypatch.setattr(vomeos.tools, "repo_map", lambda: {"core": Repo})
    monkeypatch.setattr(sentry_projects, "fix_target",
                        lambda slug: "development")
    monkeypatch.setattr(sentry_code, "gather_for_build",
                        lambda edits, repo, ref, **k: seen.update(
                            refs=k["refs"]) or {"files": "SOURCE"})
    import sentry_fix
    monkeypatch.setattr(sentry_fix, "propose", lambda a, c: {"diff": ""})
    fd._propose("1.1", items["1"])
    assert (v4, 1328) in seen["refs"] and (v4, 1500) in seen["refs"]


def test_a_real_refusal_is_still_logged(monkeypatch, desk):
    monkeypatch.setattr(fd, "_propose", lambda ts, item: (
        {"reason": "it needs a migration", "needs_context": False}, None))
    items = {"1": {**_build_item(), "state": fd.HELD}}
    fd._build("1.1", {"task_id": "T1"}, items, "1", sam_approved=True)
    assert items["1"]["state"] == fd.LOGGED
    assert "builder declined" in desk["posts"][-1]


def test_an_unchecked_build_goes_to_sam_not_the_builder(monkeypatch, desk):
    """The architect wrote "Not yet checked" on option B and the build went
    ahead; the builder stopped on the same open question."""
    monkeypatch.setenv("FEATURE_DESK_PR_ENABLED", "true")
    monkeypatch.setattr(fd, "_build", lambda *a, **k:
                        desk["builds"].append(a))
    items = {"1": _build_item(analysis=(
        "Guests read as offline via volunteers.is_offline_volunteer. Not "
        "yet checked: whether is_offline_volunteer is cleared when a "
        "stand-in is claimed. The v4 list already annotates is_offline."))}
    fd.act("1.1", {"task_id": ""}, {"summary": "Profile status"}, items)
    assert desk["builds"] == []
    assert items["1"]["verdict"] == "decision"
    assert items["1"]["state"] == fd.WAITING_SAM
    assert items["1"]["question"].startswith("Not yet checked: whether")
    assert "Should I check that before building" in desk["posts"][0]


def test_a_checked_build_is_still_offered(monkeypatch, desk):
    monkeypatch.delenv("FEATURE_DESK_PR_ENABLED", raising=False)
    items = {"1": _build_item(analysis="Checked: a claim deletes the "
                                       "stand-in, so the flag never "
                                       "outlives it.")}
    fd.act("1.1", {"task_id": ""}, {"summary": "Profile status"}, items)
    assert items["1"]["state"] == fd.HELD


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
    saved = json.loads([s for s in desk["saves"] if "items" in s][-1]["items"])
    assert all(item["recorded"] for item in saved.values())
    fd._record("1.1", {"task_id": "T1"}, saved)
    assert len(desk["comments"]) == 1


def test_sams_log_moves_the_task_to_raw_intake(monkeypatch, desk):
    items = {"1": {"n": "1", "title": "Bulk", "verdict": "log",
                   "analysis": "New endpoints.", "state": fd.WAITING_SAM},
             "2": {**WAITING, "n": "2"}}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(items, task_id="T1"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "log 1"})
    assert desk["moves"] == [("T1", fd.RAW_INTAKE)]
    assert ("/task/T1", {"assignees": {"add": [],
                                       "rem": [fd.SAM_CLICKUP]}}) \
        in desk["updates"]
    assert [task for task, _ in desk["comments"]] == ["T1"]
    assert desk["tasks"] == []
    saved = json.loads([s for s in desk["saves"] if "items" in s][-1]["items"])
    assert saved["1"]["state"] == fd.LOGGED
    assert saved["1"]["recorded"] is True
    assert not saved["2"].get("recorded")
    assert "Raw Intake" in desk["posts"][-1]


# ---------------------------------------------------------------------------
# Sam places the request (2026-10-21): Build Now, Raw Intake, his Priority
# Queue, an engineer. Ron's task is moved, never copied.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,verb", [
    ("Move Ron's existing clickup task into Sam Build Now", "build"),
    ("build now", "build"),
    ("I'll handle this one", "build"),
    ("log this, I want to personally handle it", "build"),
    ("send it to OnlyG", "route"),
    ("route to backend", "route"),
    ("leave it in my priority queue for now", "leave"),
    ("leave it", "leave"),
    ("put it in the backlog", "log"),
    ("move it to Raw Intake", "log"),
])
def test_sam_places_it_in_his_own_words(text, verb):
    items = {"1": {"n": "1", "state": fd.WAITING_SAM}}
    assert fd.parse_placement(text, items)["verb"] == verb


@pytest.mark.parametrize("text", [
    "should this go in build now?",
    "Make it an option on the form step",
    "log it under the forms epic",
])
def test_a_question_or_shape_is_not_a_placement(text):
    assert fd.parse_placement(text, {"1": {"state": fd.WAITING_SAM}}) is None


def test_placement_commands_parse():
    items = {"1": {"state": fd.LOGGED}, "2": {"state": fd.PR_OPEN}}
    assert fd.parse_command("route 1 onlyg", items) == {
        "verb": "route", "targets": [("1", "")], "engineer": "onlyg"}
    assert fd.parse_command("mine", items) == {
        "verb": "build", "targets": [("1", "")]}
    assert fd.parse_command("leave 1", items)["verb"] == "leave"


def test_build_now_moves_rons_task_without_analysis(monkeypatch, desk):
    """Sam, 2026-10-21: "Move Ron's existing clickup task into Sam Build
    Now" was answered with "the task still needs moving" and the open
    question repeated. Nothing moved."""
    items = {"1": {**WAITING, "analysis": "Report qualifier."}}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(items, task_id="868mkqh95"))
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text":
                         "Move Ron's existing clickup task into Sam Build Now"})
    assert desk["moves"] == [("868mkqh95", fd.SAM_ACCEPTED)]
    assert ("/task/868mkqh95", {"assignees": {"add": [fd.SAM_CLICKUP],
                                              "rem": []}, "priority": 2}) \
        in desk["updates"]
    assert desk["tasks"] == []
    assert not any("Report qualifier" in text for _, text in desk["comments"])
    line = desk["posts"][-1]
    assert line.startswith(":white_check_mark: Handled: 1 moved to Sam Build "
                           "Now")
    assert "still needs" not in line and "?" not in line
    assert desk["reactions"] == ["1.1"]


def test_a_task_already_there_is_not_moved_again(monkeypatch, desk):
    desk["task_list"] = fd.SAM_ACCEPTED
    items = {"1": dict(WAITING)}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(items, task_id="T1"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "build 1"})
    assert desk["moves"] == []
    saved = json.loads([s for s in desk["saves"] if "items" in s][-1]["items"])
    assert saved["1"]["state"] == fd.BUILD_NOW


def test_a_settled_log_can_still_be_moved(monkeypatch, desk):
    """The architect's own `log` left the task in the Priority Queue and
    settled the thread; Sam's later `build 1` still moves it."""
    items = {"1": {"n": "1", "verdict": "log", "state": fd.LOGGED,
                   "recorded": True}}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        items, task_id="T1", status=fd.SETTLED))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "build 1"})
    assert desk["moves"] == [("T1", fd.SAM_ACCEPTED)]
    assert desk["posts"][-1].startswith("1: moved to Sam Build Now")


def test_route_assigns_the_engineer_alone(monkeypatch, desk):
    items = {"1": dict(WAITING)}
    monkeypatch.setattr(fd, "_row", lambda ts: _row(
        items, task_id="T1", requester="Ron"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "route 1 onlyg"})
    import sprint_thread

    onlyg = sprint_thread.ENGINEERS["onlyg"]
    assert desk["moves"] == []
    assert ("/task/T1", {"assignees": {"add": [onlyg[0]],
                                       "rem": [fd.SAM_CLICKUP]}}) \
        in desk["updates"]
    assert any(f"<@{onlyg[1]}>" in post for post in desk["posts"])
    assert "routed to OnlyG" in desk["posts"][-1]


def test_route_without_an_engineer_asks(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _row({"1": dict(WAITING)}))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "route 1"})
    assert desk["updates"] == [] and "onlyg" in desk["posts"][-1]


def test_leave_writes_nothing(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _row({"1": dict(WAITING)},
                                                    task_id="T1"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "leave 1"})
    assert desk["moves"] == [] and desk["updates"] == []
    assert desk["comments"] == []
    assert "left in Priority Queue" in desk["posts"][-1]


def test_a_task_outside_the_product_lists_is_left_alone(monkeypatch, desk):
    desk["task_list"] = "999"
    monkeypatch.setattr(fd, "_row", lambda ts: _row({"1": dict(WAITING)},
                                                    task_id="T1"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "build 1"})
    assert desk["moves"] == [] and desk["updates"] == []
    assert desk["posts"][-1].startswith(":warning: Nothing moved")


def test_build_now_with_no_task_creates_one_in_sam_build_now(monkeypatch,
                                                            desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _row({"1": dict(WAITING)}))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "build 1"})
    assert desk["tasks"] == [("1", {"list_id": fd.SAM_ACCEPTED,
                                    "assignees": [fd.SAM_CLICKUP],
                                    "priority": 2})]


def test_a_task_named_in_the_reply_is_the_one_moved(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _row({"1": dict(WAITING)}))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text":
                         "move it to build now, it's 868mkbdz6"})
    assert desk["moves"] == [("868mkbdz6", fd.SAM_ACCEPTED)]
    assert desk["tasks"] == []


# ---------------------------------------------------------------------------
# The answer key can read into the items list
# ---------------------------------------------------------------------------

def test_compare_reads_into_lists():
    actual = {"items": [{"verdict": "log"}, {"verdict": "decision"}]}
    assert compare({"items.1.verdict": "decision"}, actual) == []
    assert compare({"items.2.verdict": "log"}, actual) != []


# ---------------------------------------------------------------------------
# After the PR: merged, live, closed (Sam, 2026-10-19)
# ---------------------------------------------------------------------------

PR = ("https://bitbucket.org/vomedjango/vomedjango-restored-core-app/"
      "pull-requests/7")


def _built(monkeypatch, **over):
    items = {"1": {**_build_item(), "state": fd.PR_OPEN, "pr_url": PR,
                   "fix_task": "AF1", "recorded": True, **over}}
    monkeypatch.setattr(fd, "_items_with", lambda value: [
        ("1.1", {"ts": "1.1", "task_id": "T1"}, items)])
    monkeypatch.setattr(fd, "_read_post", lambda ts: {"requester_id": RON})
    return items


def test_merged_tells_the_requester_to_test_on_dev(monkeypatch, desk):
    items = _built(monkeypatch)
    fd.pr_merged(PR, "core@abc123")
    assert items["1"]["merged"] == "core@abc123"
    assert "Merged into development" in desk["posts"][0]
    assert f"<@{RON}>" in desk["posts"][0]
    assert "core@abc123" in desk["posts"][0]
    fd.pr_merged(PR, "core@abc123")
    assert len(desk["posts"]) == 1


def test_live_is_said_once(monkeypatch, desk):
    items = _built(monkeypatch, merged="core@abc123")
    fd.fix_live("AF1")
    fd.fix_live("AF1")
    assert len(desk["posts"]) == 1
    assert "Live in production" in desk["posts"][0]
    assert items["1"]["live"] is True


def test_a_pr_closed_unmerged_reopens_the_item(monkeypatch, desk):
    removed = []
    monkeypatch.setattr(fd, "_unreact", lambda ts: removed.append(ts))
    items = _built(monkeypatch)
    fd.pr_closed(PR)
    assert items["1"]["state"] == fd.HELD and items["1"]["pr_url"] == ""
    assert removed == ["1.1"]
    assert "`go 1` builds it again" in desk["posts"][0]
    assert desk["saves"][-1]["status"] == fd.OPEN


def test_the_release_watcher_promotion_tells_the_thread(monkeypatch):
    import ticket_fix

    told = []
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: ("merged", "core", "abc123"))
    monkeypatch.setattr(ticket_fix, "_clickup", lambda *a, **k: {})
    monkeypatch.setattr(ticket_fix, "_sprint_note", lambda *a: None)
    monkeypatch.setattr(fd, "pr_merged",
                        lambda url, commit: told.append((url, commit)) or [])
    ticket_fix.promote_merged([{"id": "AF1", "status": "in progress",
                                "custom_fields": [],
                                "description": f"Fix PR: {PR}"}])
    assert told == [(PR, "core@abc123")]


# ---------------------------------------------------------------------------
# What the builder is shown
# ---------------------------------------------------------------------------

import sentry_code  # noqa: E402

SERIALIZERS = "\n".join([
    '"""Form serializers."""',
    "import json",
    "",
    "from django.db.models import (Count,",
    "                              OuterRef)",
    "# models",
    "from form_app.models import Form",
    "",
    *[f"x{i} = {i}" for i in range(8, 300)],
    "from organization_app.models import OrganizationVolunteer  # noqa: E402",
    *[f"y{i} = {i}" for i in range(301, 700)],
    "    def helper(self):",
    "        from user_app.models import User",
    *[f"z{i} = {i}" for i in range(703, 1000)],
])
REFERENCE = "\n".join(f"r{i} = {i}" for i in range(1, 6000))


class _Read:
    def __init__(self, data):
        self.ok, self.data, self.error = bool(data), data, \
            "" if data else "HTTP 404"


class _Client:
    def __init__(self, files):
        self.files, self.reads = files, []

    def configured(self, **k):
        return True

    def read_file(self, repo, path, ref="", max_bytes=0, **k):
        self.reads.append(path)
        return _Read(self.files.get(path, ""))


def test_import_blocks_cover_header_and_later_top_level_imports():
    lines = SERIALIZERS.splitlines()
    blocks = sentry_code.import_blocks(lines)
    assert blocks[0] == (0, 7)            # docstring through Form
    assert blocks[1] == (300, 301)        # the E402 import, its own span
    assert len(blocks) == 2               # an import inside a def is not


def test_references_read_the_architects_where_line():
    where = ("core:form_app/institution/api_version/v4/api.py:282; "
             "core:organization_app/serializers.py:4836")
    assert sentry_code.references(where) == [
        ("form_app/institution/api_version/v4/api.py", 282),
        ("organization_app/serializers.py", 4836)]


def test_a_feature_build_sees_imports_anchors_and_read_only_precedent(
        monkeypatch):
    client = _Client({"form_app/serializers.py": SERIALIZERS,
                      "organization_app/serializers.py": REFERENCE})
    monkeypatch.setattr(sentry_code, "_client_for", lambda host: client)
    out = sentry_code.gather_for_build(
        [("form_app/serializers.py", 900)], "core", "development",
        refs=[("organization_app/serializers.py", 4848),
              ("form_app/serializers.py", 900)])
    text = out["files"]
    assert "--- form_app/serializers.py lines 1-7 ---" in text
    assert "from form_app.models import Form" in text
    assert "--- form_app/serializers.py lines 301-301 ---" in text
    assert ">> 900 |" in text
    edit, reference = text.split("=== READ-ONLY REFERENCE")
    assert "organization_app/serializers.py" not in edit
    assert "--- organization_app/serializers.py lines 4748-4948 ---" \
        in reference
    assert out["read"] == ["form_app/serializers.py:900",
                           "organization_app/serializers.py:4848"]


def test_a_feature_build_that_reads_nothing_says_so(monkeypatch):
    monkeypatch.setattr(sentry_code, "_client_for",
                        lambda host: _Client({}))
    out = sentry_code.gather_for_build([("a.py", 3)], "core", "development")
    assert out["files"].startswith("Could not read any source file")
    assert "a.py: HTTP 404" in out["files"]


def test_propose_hands_the_evidence_to_the_reader(monkeypatch):
    import sentry_fix
    import sentry_projects
    import vomeos.tools

    class Repo:
        slug, host, owner, branch, alias = "core-slug", "bitbucket", "", \
            "master", "core"

    seen = {}
    monkeypatch.setattr(vomeos.tools, "repo_map", lambda: {"core": Repo})
    monkeypatch.setattr(sentry_projects, "fix_target",
                        lambda slug: "development")
    monkeypatch.setattr(sentry_code, "gather_for_build",
                        lambda edits, repo, ref, **k: seen.update(
                            edits=edits, ref=ref, refs=k["refs"])
                        or {"files": "SOURCE"})
    monkeypatch.setattr(sentry_fix, "propose", lambda analysis, ctx:
                        seen.update(source=ctx["source"]) or {"diff": ""})
    item = _build_item(
        evidence="core:organization_app/serializers.py:4836",
        build={"repo": "core", "anchors": ["form_app/a.py:10"],
               "plan": "Copy get_is_invited (organization_app/"
                       "serializers.py:4848) onto the row.",
               "contract": "is_invited: bool"})
    fd._propose("1.1", item)
    assert seen["edits"] == [("form_app/a.py", 10)]
    assert seen["ref"] == "development"
    assert seen["refs"] == [("organization_app/serializers.py", 4836),
                            ("organization_app/serializers.py", 4848)]
    assert seen["source"] == "SOURCE"


# ---------------------------------------------------------------------------
# Ask first (Sam, 2026-10-25): many requests are not wanted yet, so nothing
# is analysed until Sam says so. Before that the request is one item, the
# post as written, and placing it costs no model call.
# ---------------------------------------------------------------------------

def _waiting_row(task_id=""):
    post = {"text": "*Bulk duplicate shifts*\nLet admins copy a week."}
    return _row({"1": fd._plain_item(post)}, status=fd.AWAITING,
                task_id=task_id)


def test_with_auto_analyse_on_the_post_is_analysed(monkeypatch, desk):
    monkeypatch.setenv("FEATURE_DESK_AUTO_ANALYSE", "true")
    monkeypatch.setattr("vomeos.worker.enqueue_event", lambda *a, **k: None)
    fd.handle_message({"ts": "1.1", "text": "x" * 200})
    assert desk["posts"][0] == fd.ANALYSING_TEXT


def test_a_new_post_is_recorded_without_the_architect(monkeypatch, desk):
    monkeypatch.delenv("FEATURE_DESK_AUTO_ANALYSE", raising=False)
    monkeypatch.setattr(fd.store, "ensure_table", lambda name: True)
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(fd, "_read_post", lambda ts: {
        "text": "<@U1> *Bulk duplicate shifts*\n<https://app.clickup.com/"
                "t/868abc123|spec>", "requester": "Ron",
        "requester_id": RON, "thread": "", "task_id": "868abc123"})
    result = fd.handle({"ts": "1.1"})
    assert result == {"status": "ok", "action": "waiting for Sam"}
    saved = desk["saves"][-1]
    assert saved["status"] == fd.AWAITING
    assert saved["task_id"] == "868abc123"
    item = json.loads(saved["items"])["1"]
    assert item["title"] == "Bulk duplicate shifts"
    assert item["recorded"] is True
    assert desk["posts"] == []


def test_a_bare_link_gets_a_plain_title():
    post = {"text": "<https://app.clickup.com/t/868mjq5gb>"}
    assert fd._plain_item(post)["title"] == "Feature request"


@pytest.mark.parametrize("user,text", [
    (RON, "Also it should keep the volunteers on each shift"),
    (SAM, "Ron, is this for the March release?"),
    (SAM, "check with Ron first"),
    (SAM, "no need to analyse, I'll think about it"),
])
def test_while_waiting_nothing_is_read(monkeypatch, desk, user, text):
    monkeypatch.setattr(fd, "_row", lambda ts: _waiting_row())
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    result = fd._on_reply("1.1", {"ts": "2", "user": user, "text": text})
    assert result == {"status": "skipped", "reason": "waiting for Sam"}
    assert desk["posts"] == []


@pytest.mark.parametrize("text,direction", [
    ("analyse", ""),
    ("Analyze this", ""),
    ("please analyse it", ""),
    ("go", ""),
    ("analyse it, focus on whether it is a step type",
     "focus on whether it is a step type"),
])
def test_sam_asks_for_the_analysis(monkeypatch, desk, text, direction):
    seen = {}
    monkeypatch.setattr(fd, "_row", lambda ts: _waiting_row())
    monkeypatch.setattr(fd, "analyse", lambda ts, **k: seen.update(k))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": text})
    assert seen == {"direction": direction}
    assert desk["posts"] == [fd.ANALYSING_TEXT]
    # The stand-in item goes, so only the architect's items remain.
    assert {"status": fd.ANALYSING, "items": "{}"} in desk["saves"]


def test_only_sam_starts_an_analysis(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _waiting_row())
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    result = fd._on_reply("1.1", {"ts": "2", "user": RON, "text": "analyse"})
    assert result["reason"] == "waiting for Sam"


def test_log_moves_rons_task_with_no_analysis(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _waiting_row(task_id="T1"))
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "log"})
    assert desk["moves"] == [("T1", fd.RAW_INTAKE)]
    assert desk["comments"] == []
    assert desk["tasks"] == []
    assert desk["reactions"] == ["1.1"]
    assert desk["posts"][-1].startswith(":white_check_mark: Handled: 1 moved "
                                        "to Raw Intake")


def test_log_without_a_task_files_the_request(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _waiting_row())
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "log"})
    assert desk["tasks"] == [("1", {})]


@pytest.mark.parametrize("text,where", [
    ("build", fd.SAM_ACCEPTED),
    ("Move Ron's task into Sam Build Now", fd.SAM_ACCEPTED),
    ("put it in the backlog", fd.RAW_INTAKE),
])
def test_sam_places_a_waiting_request(monkeypatch, desk, text, where):
    monkeypatch.setattr(fd, "_row", lambda ts: _waiting_row(task_id="T1"))
    monkeypatch.setattr(fd, "analyse", lambda *a, **k: pytest.fail("ran"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": text})
    assert desk["moves"] == [("T1", where)]


def test_no_settles_it_and_writes_nothing(monkeypatch, desk):
    monkeypatch.setattr(fd, "_row", lambda ts: _waiting_row(task_id="T1"))
    fd._on_reply("1.1", {"ts": "2", "user": SAM, "text": "no"})
    assert desk["comments"] == []
    assert desk["reactions"] == ["1.1"]
    assert "1 not building" in desk["posts"][-1]


def test_an_unanalysed_task_carries_the_request(monkeypatch):
    sent = {}
    monkeypatch.setattr(fd, "_permalink", lambda ts: "https://slack/x")
    monkeypatch.setattr(fd, "_read_post", lambda ts: {
        "text": "Let admins copy a week of shifts."})
    monkeypatch.setattr(fd, "_clickup", lambda method, path, body=None:
                        sent.update(body) or {"id": "T9"})
    item = fd._plain_item({"text": "Let admins copy a week of shifts."})
    assert fd._create_task(item, "1.1", {}) == "T9"
    assert sent["markdown_description"].startswith(
        "Let admins copy a week of shifts.")
    assert "Architect" not in sent["markdown_description"]
