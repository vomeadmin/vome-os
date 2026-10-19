"""
#bug-reports (built 2026-10-19): the verdict decides what happens, Sam's
thread commands act, and code tools read only the branches allowed.

No database and no network: every outside call is replaced.
"""

from __future__ import annotations

import pytest

import bug_reports
from vomeos import tools

TS = "1792430491.698039"

BUG = {
    "headline": "Exemption rules save with no institution",
    "verdict": "agent_fix", "team": "backend", "found_on": "dev_branch",
    "analysis": "The old lookup returns None for admins.",
    "engineer_summary": "Use get_admin_institution_v2.",
    "code_check": {
        "verdict": "bug", "finding": "old lookup",
        "where": "core:sequence_app/step_exemption/views.py:14",
        "fix": "swap in the sibling lookup", "fix_risk": "low",
        "confidence": "high",
    },
}


# A bug with no workable fix: nothing to build, so it routes on its own.
ENGINEER = {**BUG, "verdict": "engineer",
            "code_check": {**BUG["code_check"], "fix": "", "where": ""}}

# The guest shift report from the 2026-10-19 dry run: a concrete fix, high
# risk. It auto-routed to both engineers; Sam wants a greenlight instead.
GUEST = {
    "headline": "Guest submissions refuse shift picks",
    "verdict": "greenlight", "team": "both", "hard_limit": "",
    "risk_plain": "guests could be booked into shifts they are not "
                  "eligible for",
    "analysis": "Server refuses opportunity_roles on guest submissions.",
    "code_check": {
        "verdict": "bug", "finding": "guest shift picks refused",
        "where": "core:form_app/generic_volunteer/serializers.py:1296",
        "files": [
            "core:form_app/generic_volunteer/serializers.py:734",
            "web:src/views/me/volunteerForms/VolunteerShiftForm.js:1256",
        ],
        "fix": "validate and reserve shifts on the guest profile",
        "fix_risk": "high", "confidence": "high",
    },
}

# The auth cookie report: a real bug, but hosting and settings work.
COOKIES = {
    "headline": "Auth cookies are third-party", "verdict": "engineer",
    "team": "backend", "hard_limit": "infrastructure",
    "analysis": "API on a different site from the web app.",
    "code_check": {"verdict": "bug", "finding": "host-only cookies",
                   "where": "core:user_app/auth_cookies.py:18",
                   "fix": "serve the API from a subdomain",
                   "fix_risk": "high", "confidence": "medium"},
}

RON_CLICKUP = 4434980


class FakeRepo:
    def __init__(self, alias):
        self.alias, self.slug = alias, f"{alias}-repo"
        self.host, self.owner = "bitbucket", ""


class Recorder:
    def __init__(self):
        self.posts, self.calls, self.saved = [], [], {}
        self.proposed, self.opened = [], []
        # What GET /task returns: Ron's task, in Priority Queue, queued.
        self.task = {"id": "868mg8nce", "creator": {"id": RON_CLICKUP},
                     "list": {"id": bug_reports.PRIORITY_QUEUE},
                     "status": {"status": "queued"}, "custom_fields": []}
        # Per repo: what the fix author returns, and whether the PR opens.
        self.diff = {"core": "--- a\n+++ b", "web": "--- a\n+++ b"}
        self.rationale: dict = {}
        self.greenlit_builds: list = []
        self.pr_opens = True


@pytest.fixture
def rec(monkeypatch):
    import sentry_fix

    r = Recorder()

    def propose(ts, card, alias, places, several=False, greenlit=False):
        r.proposed.append((alias, sorted({p for p, _l in places})))
        r.greenlit_builds.append(greenlit)
        diff = r.diff.get(alias, "")
        return ({"diff": diff, "attempted": bool(diff),
                 "rationale": r.rationale.get(alias, ""),
                 "reason": "" if diff else "the fix author declined"},
                FakeRepo(alias))

    def open_pr(issue, proposal, context):
        r.opened.append(context["repo"])
        if not r.pr_opens:
            return {"opened": False, "reason": "patch does not apply"}
        return {"opened": True, "url": f"https://pr/{context['repo']}"}

    monkeypatch.setattr(bug_reports, "_propose", propose)
    monkeypatch.setattr(sentry_fix, "open_pull_request", open_pr)
    monkeypatch.setattr(bug_reports, "_post",
                        lambda text, thread_ts="", channel="":
                        r.posts.append((channel or "bug", thread_ts, text))
                        or "1.1")
    monkeypatch.setattr(bug_reports, "_permalink", lambda ts: "https://s/p")

    def clickup(method, path, body=None):
        r.calls.append((method, path, body))
        if method == "GET":
            return r.task
        return {"id": "newtask"}

    monkeypatch.setattr(bug_reports, "_clickup", clickup)
    monkeypatch.setattr(bug_reports, "_save",
                        lambda ts, **f: r.saved.update(f))
    monkeypatch.setattr(bug_reports, "_opened_today", lambda: 0)
    return r


def _row(**over):
    row = {"ts": TS, "task_id": "868mg8nce", "reporter": "Ron",
           "reporter_id": "U01B6D235N1", "rounds": 1}
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def _comments(rec) -> str:
    return "\n".join(c[2]["comment_text"] for c in rec.calls
                     if c[1].endswith("/comment"))


def test_engineer_adopts_the_reporters_task_and_posts_in_their_channel(rec):
    import sprint_thread

    out = bug_reports.act(TS, _row(), ENGINEER)
    assert out["verdict"] == "engineer"
    # Ron's own task: OnlyG in, Sam out, still queued, no second task.
    assert ("PUT", "/task/868mg8nce",
            {"assignees": {"add": [49257687],
                           "rem": [bug_reports.SAM_CLICKUP]}}) in rec.calls
    assert not any(c[1].startswith("/list/") for c in rec.calls)
    assert ("POST", f"/task/868mg8nce/field/{bug_reports.FIELD_SOURCE}",
            {"value": bug_reports.SOURCE_SLACK}) in rec.calls
    # Ron set no Type, so it becomes Bug.
    assert ("POST", f"/task/868mg8nce/field/{bug_reports.FIELD_TYPE}",
            {"value": bug_reports.TYPE_BUG}) in rec.calls
    # The analysis lives on ClickUp; Slack carries the short message.
    assert "The old lookup returns None" in _comments(rec)
    assert "no workable fix" in _comments(rec)
    eng = [p for p in rec.posts if p[0] == sprint_thread.ENG_BACKEND][0][2]
    assert "app.clickup.com/t/868mg8nce" in eng
    assert "The old lookup" not in eng
    thread = rec.posts[-1][2]
    assert thread.startswith(
        ":white_check_mark: Logged on ClickUp for OnlyG and sent in "
        f"<#{sprint_thread.ENG_BACKEND}> for communication about it.")
    assert "The old lookup" not in thread
    assert rec.saved["status"] == bug_reports.ROUTED


def test_routing_keeps_the_type_ron_chose(rec):
    rec.task["custom_fields"] = [{"id": bug_reports.FIELD_TYPE, "value": 1}]
    bug_reports.act(TS, _row(), ENGINEER)
    assert not any(c[1].endswith(bug_reports.FIELD_TYPE) for c in rec.calls)


def test_routing_reopens_a_done_task_as_queued(rec):
    rec.task["status"] = {"status": "declined"}
    bug_reports.act(TS, _row(), ENGINEER)
    put = [c for c in rec.calls if c[0] == "PUT"][0]
    assert put[2]["status"] == "queued"


def test_task_outside_priority_queue_gets_a_priority_queue_task(rec):
    rec.task["list"] = {"id": "somewhere-else"}
    out = bug_reports.act(TS, _row(), ENGINEER)
    created = [c for c in rec.calls
               if c[1] == f"/list/{bug_reports.PRIORITY_QUEUE}/task"]
    assert len(created) == 1 and created[0][2]["assignees"] == [49257687]
    assert ("POST", "/task/868mg8nce/link/newtask", None) in rec.calls
    assert out["task"] == "newtask"


def test_engineer_without_a_linked_task_creates_one_marked_slack(rec):
    bug_reports.act(TS, _row(task_id=""), {**ENGINEER, "team": "both"})
    created = [c for c in rec.calls
               if c[1] == f"/list/{bug_reports.PRIORITY_QUEUE}/task"]
    assert len(created) == 1
    body = created[0][2]
    assert {"id": bug_reports.FIELD_SOURCE,
            "value": bug_reports.SOURCE_SLACK} in body["custom_fields"]
    assert sorted(body["assignees"]) == [4434086, 49257687]


def test_needs_info_asks_the_reporter(rec):
    bug_reports.act(TS, _row(), {"verdict": "needs_info", "team": "none",
                                 "question": "Which page?",
                                 "analysis": "No page named."})
    text = rec.posts[-1][2]
    assert "<@U01B6D235N1> Which page?" in text
    assert rec.saved["status"] == bug_reports.NEEDS_INFO


def test_needs_info_stops_asking_after_max_rounds(rec):
    bug_reports.act(TS, _row(rounds=bug_reports.MAX_ROUNDS),
                    {"verdict": "needs_info", "question": "Which page?"})
    assert f"<@{bug_reports.SAM_SLACK}>" in rec.posts[-1][2]
    assert rec.saved["status"] == bug_reports.DECISION


def test_decision_asks_sam_and_routes_nothing(rec):
    bug_reports.act(TS, _row(), {"verdict": "decision", "question": "Keep?",
                                 "analysis": "Works as built."})
    assert f"<@{bug_reports.SAM_SLACK}>" in rec.posts[-1][2]
    assert not any(c[0] == "PUT" for c in rec.calls)


@pytest.mark.parametrize("verdict", ["not_a_bug", "already_fixed",
                                     "reporter_fix"])
def test_closing_verdicts_file_nothing(rec, verdict):
    bug_reports.act(TS, _row(task_id=""), {"verdict": verdict,
                                           "analysis": "x"})
    assert rec.calls == []
    assert rec.saved["status"] == bug_reports.CLOSED


def _certain(verdict, confidence="high"):
    return {"verdict": verdict, "analysis": "On development already.",
            "code_check": {"verdict": "works_as_designed",
                           "confidence": confidence}}


def test_already_fixed_closes_ron_task_as_completed(rec):
    bug_reports.act(TS, _row(), _certain("already_fixed"))
    assert ("PUT", "/task/868mg8nce", {"status": "Closed"}) in rec.calls
    assert ("POST", f"/task/868mg8nce/field/{bug_reports.FIELD_RESOLUTION}",
            {"value": bug_reports.RESOLUTION_COMPLETED}) in rec.calls
    assert "On development already." in _comments(rec)
    assert "Marked closed on ClickUp" in rec.posts[-1][2]


def test_not_a_bug_declines_ron_task(rec):
    bug_reports.act(TS, _row(), _certain("not_a_bug"))
    assert ("PUT", "/task/868mg8nce", {"status": "declined"}) in rec.calls


def test_unsure_close_leaves_the_task_open_for_sam(rec):
    bug_reports.act(TS, _row(), _certain("already_fixed", "medium"))
    assert not any(c[0] == "PUT" for c in rec.calls)
    assert f"<@{bug_reports.SAM_SLACK}> to confirm" in rec.posts[-1][2]


def test_reporter_fix_hands_the_task_back_to_ron(rec):
    bug_reports.act(TS, _row(), _certain("reporter_fix"))
    assert ("PUT", "/task/868mg8nce", {"assignees": {
        "add": [RON_CLICKUP], "rem": [bug_reports.SAM_CLICKUP]}}) in rec.calls


def _engineer_assigned(rec) -> bool:
    return any(c[0] == "PUT" and set((c[2].get("assignees") or {})
                                     .get("add") or []) & {49257687, 4434086}
               for c in rec.calls)


def test_high_risk_bug_asks_sam_to_greenlight(rec):
    out = bug_reports.act(TS, _row(), GUEST)
    assert out["verdict"] == bug_reports.GREENLIGHT
    post = rec.posts[-1][2]
    assert post.startswith(f"<@{bug_reports.SAM_SLACK}> Greenlight?")
    for part in ("*Problem:* Guest submissions refuse shift picks",
                 "*Fix:* validate and reserve shifts on the guest profile "
                 "(in core, web)",
                 "*Risk:* guests could be booked", "`yes`",
                 "`route backend|frontend|both`", "`close`",
                 "app.clickup.com/t/868mg8nce"):
        assert part in post
    # No engineer, no PR; the task stays with Sam, analysis on ClickUp.
    assert not _engineer_assigned(rec) and rec.opened == []
    assert ("PUT", "/task/868mg8nce", {"assignees": {
        "add": [bug_reports.SAM_CLICKUP], "rem": []}}) in rec.calls
    assert "Server refuses opportunity_roles" in _comments(rec)
    assert rec.saved["status"] == bug_reports.GREENLIGHT


def test_engineer_verdict_with_a_workable_fix_still_asks_sam(rec):
    out = bug_reports.act(TS, _row(), {**GUEST, "verdict": "engineer"})
    assert out["verdict"] == bug_reports.GREENLIGHT
    assert not _engineer_assigned(rec)


def test_daily_cap_asks_sam_instead_of_routing(rec, monkeypatch):
    monkeypatch.setattr(bug_reports, "_opened_today", lambda: 99)
    out = bug_reports.act(TS, _row(), BUG)
    assert out["verdict"] == bug_reports.GREENLIGHT
    assert "daily pull request cap" in rec.posts[-1][2]


def test_auth_cookie_report_still_routes_to_an_engineer(rec):
    import sprint_thread

    out = bug_reports.act(TS, _row(), COOKIES)
    assert out["verdict"] == "engineer"
    assert _engineer_assigned(rec)
    assert "hosting, DNS" in _comments(rec)
    assert any(p[0] == sprint_thread.ENG_BACKEND for p in rec.posts)
    assert "Greenlight" not in rec.posts[-1][2]


def test_low_risk_fix_that_fails_alone_asks_for_a_greenlight(rec):
    rec.diff["core"] = ""
    out = bug_reports.act(TS, _row(), BUG)
    assert out["verdict"] == bug_reports.GREENLIGHT
    assert "the fix author declined" in rec.posts[-1][2]
    assert not _engineer_assigned(rec)


def test_dry_run_shows_the_greenlight_step(rec, capsys):
    bug_reports.act(TS, _row(), GUEST, dry_run=True)
    out = capsys.readouterr().out
    assert ("WOULD: ask Sam to greenlight (risk: high), then PR into "
            "core, web dev branch") in out
    assert rec.posts == [] and rec.calls == []


def test_no_code_read_asks_sam_instead_of_routing(rec):
    card = {**BUG, "verdict": "engineer",
            "code_check": {"verdict": "not_checked"}}
    out = bug_reports.act(TS, _row(), card)
    assert out["verdict"] == bug_reports.DECISION
    assert "`route backend`" in rec.posts[-1][2]
    assert not any(c[0] == "PUT" for c in rec.calls)


def test_agent_fix_opens_a_pr_and_files_agent_fixes(rec):
    out = bug_reports.act(TS, _row(), BUG)
    assert out["pr_urls"] == ["https://pr/core-repo"]
    # On its own the agent works on `where` only.
    assert rec.proposed == [("core",
                             ["sequence_app/step_exemption/views.py"])]
    fix = [c for c in rec.calls if c[1].endswith("/task")
           and c[0] == "POST"][0]
    assert "Fix PR: https://pr/core-repo" in fix[2]["markdown_description"]
    assert {"id": bug_reports.FIELD_SOURCE,
            "value": bug_reports.SOURCE_SLACK} in fix[2]["custom_fields"]
    # Ron's task is linked to the fix and told about it.
    assert ("POST", "/task/868mg8nce/link/newtask", None) in rec.calls
    assert rec.saved["status"] == bug_reports.FIXING


# ---------------------------------------------------------------------------
# Thread replies
# ---------------------------------------------------------------------------

def _stored(monkeypatch, **over):
    row = {**_row(), "status": bug_reports.DECISION, "card": "{}",
           "last_reply_ts": ""}
    row.update(over)
    monkeypatch.setattr(bug_reports, "_row", lambda ts: row)


def test_sam_route_command_routes(rec, monkeypatch):
    _stored(monkeypatch)
    out = bug_reports._on_reply(TS, {"ts": "2.0", "text": "route frontend",
                                     "user": bug_reports.SAM_SLACK})
    assert out["team"] == "frontend"


def _greenlight_waiting(monkeypatch, card=GUEST, **over):
    import json

    _stored(monkeypatch, status=bug_reports.GREENLIGHT,
            card=json.dumps(card), **over)


def _sam(text):
    return {"ts": "2.0", "text": text, "user": bug_reports.SAM_SLACK}


def test_sams_yes_builds_one_pr_per_repo(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    out = bug_reports._on_reply(TS, _sam("yes"))
    assert out["pr_urls"] == ["https://pr/core-repo", "https://pr/web-repo"]
    # Every file the fix names, grouped by repo.
    assert rec.proposed == [
        ("core", ["form_app/generic_volunteer/serializers.py"]),
        ("web", ["src/views/me/volunteerForms/VolunteerShiftForm.js"])]
    tasks = [c for c in rec.calls if c[0] == "POST" and c[1].endswith("/task")]
    parent, children = tasks[0][2], tasks[1:]
    desc = parent["markdown_description"]
    assert desc.count(bug_reports.MULTI_PR_LABEL) == 2
    assert "Fix PR:" not in desc  # left to run_rollup, not promote_merged
    assert len(children) == 2
    assert all(c[2]["parent"] == "newtask" for c in children)
    thread = rec.posts[-1][2]
    assert thread.startswith(
        ":white_check_mark: Fixed by the agent. Pull request into the dev "
        "branch, waiting for Sam to review and merge.")
    assert "https://pr/web-repo" in thread
    assert not _engineer_assigned(rec)


@pytest.mark.parametrize("text", ["yes", "Go", "do it", "execute",
                                  "ship it!", "fix", "yes, do it"])
def test_every_form_of_sams_yes_builds(rec, monkeypatch, text):
    _greenlight_waiting(monkeypatch)
    out = bug_reports._on_reply(TS, _sam(text))
    assert out.get("pr_urls")


def test_a_yes_from_anyone_else_does_nothing(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    out = bug_reports._on_reply(TS, {"ts": "2.0", "text": "yes",
                                     "user": "U01B6D235N1"})
    assert out["reason"] == "only Sam can greenlight"
    assert rec.opened == [] and rec.posts == []


def test_sams_question_reruns_the_analysis_instead_of_building(rec,
                                                               monkeypatch):
    _greenlight_waiting(monkeypatch)
    ran = []
    monkeypatch.setattr(bug_reports, "analyse",
                        lambda ts, greenlit=False: ran.append(greenlit)
                        or {"status": "ok"})
    bug_reports._on_reply(TS, _sam("yes but only the backend part"))
    assert ran == [False] and rec.opened == []


def test_a_fix_needing_a_migration_is_refused_even_with_sams_yes(
        rec, monkeypatch):
    _greenlight_waiting(monkeypatch, card={**GUEST,
                                           "hard_limit": "migration"})
    out = bug_reports._on_reply(TS, _sam("yes"))
    assert out["action"] == "route offered"
    post = rec.posts[-1][2]
    assert "migration" in post and "`route both`" in post
    assert "`yes`" not in post
    assert rec.opened == [] and not _engineer_assigned(rec)


def test_a_greenlit_build_that_fails_offers_route_and_never_routes(
        rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    rec.pr_opens = False
    out = bug_reports._on_reply(TS, _sam("yes"))
    assert out["action"] == "route offered"
    assert "patch does not apply" in rec.posts[-1][2]
    assert not _engineer_assigned(rec)
    assert rec.saved["status"] == bug_reports.GREENLIT


def test_part_built_says_what_is_left(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    rec.diff["web"] = ""
    out = bug_reports._on_reply(TS, _sam("yes"))
    assert out["pr_urls"] == ["https://pr/core-repo"]
    assert out["not_built"] == ["web"]
    assert "Not built: web: the fix author declined" in rec.posts[-1][2]
    assert "`route both`" in rec.posts[-1][2]


def test_yes_builds_with_the_greenlight_on(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    bug_reports._on_reply(TS, _sam("yes"))
    assert rec.greenlit_builds == [True, True]


def test_low_risk_build_on_its_own_is_not_greenlit(rec):
    bug_reports.act(TS, _row(), BUG)
    assert rec.greenlit_builds == [False]


def test_what_the_fix_author_left_out_is_named(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    rec.rationale["core"] = ("Swaps the lookup. Left out: the 403 on the "
                             "volunteer list, its view is not in this file.")
    out = bug_reports._on_reply(TS, _sam("yes"))
    assert out["not_built"] == ["core"]
    post = rec.posts[-1][2]
    assert "Not built: core: left out the 403 on the volunteer list" in post
    assert ".." not in post


def test_refusal_reason_ending_in_a_period_reads_cleanly(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    rec.diff = {"core": "", "web": ""}
    bug_reports._on_reply(TS, _sam("yes"))
    assert ".." not in rec.posts[-1][2]


def test_greenlit_proposal_gets_whole_files_references_and_the_greenlight(
        monkeypatch):
    """The first greenlit build (exemption rules) failed because the fix
    author saw a 60-line window, no copy of the sibling module, and no word
    that Sam had approved it."""
    import sentry_fix
    from vomeos.integrations import code_search

    views = "\n".join(f"line {n}" for n in range(1, 301))
    files = {"sequence_app/step_exemption/views.py": views,
             "sequence_app/step_status/views.py": "def _institution():\n"
                                                  "    return v2()"}

    class Reader:
        def read_file(self, repo, path, ref="", max_bytes=0, **kw):
            class R:
                ok, error, data = True, "", files[path]
            return R()

    seen = {}
    monkeypatch.setattr(code_search, "Bitbucket", Reader)
    monkeypatch.setattr(sentry_fix, "propose",
                        lambda analysis, ctx: seen.update(ctx) or {})
    card = {**GUEST, "code_check": {
        **GUEST["code_check"],
        "where": "core:sequence_app/step_exemption/views.py:34",
        "references": ["core:sequence_app/step_status/views.py:12"]}}
    bug_reports._propose(TS, card, "core",
                         [("sequence_app/step_exemption/views.py", 34)],
                         greenlit=True)
    assert "line 300" in seen["source"]  # whole file, not a window
    assert "REFERENCE, read only" in seen["source"]
    assert "return v2()" in seen["source"]
    assert "said yes" in seen["greenlight"]


def test_files_the_analyst_opened_become_references():
    """Second exemption build (2026-10-19): the analyst read
    step_status/views.py but did not name it, so the fix author never saw
    it. What it opened is passed on regardless."""
    calls = [
        {"name": "search_code", "input": {"repo": "core", "query": "x"}},
        {"name": "read_code_file", "input": {
            "repo": "core", "path": "sequence_app/step_exemption/views.py"}},
        {"name": "read_code_file", "input": {
            "repo": "core", "path": "sequence_app/step_status/views.py",
            "start_line": 48, "end_line": 85}},
        {"name": "read_code_file", "input": {"repo": "core",
                                             "path": "sequence_app/"}},
        {"name": "read_code_file", "input": {
            "repo": "core", "path": "user_app/utils.py", "start_line": 143}},
    ]
    read = bug_reports._files_read(calls)
    assert read == ["core:sequence_app/step_exemption/views.py:1",
                    "core:sequence_app/step_status/views.py:48",
                    "core:user_app/utils.py:143"]
    card = {"read": read, "code_check": {
        "references": ["core:sequence_app/step_status/views.py:48",
                       "web:src/x.js:1"]}}
    assert bug_reports._references(card, "core") == [
        ("sequence_app/step_status/views.py", 48),
        ("sequence_app/step_exemption/views.py", 1),
        ("user_app/utils.py", 143)]
    assert bug_reports._references(card, "web") == [("src/x.js", 1)]


def test_the_stored_card_keeps_what_the_analyst_read(monkeypatch):
    import types

    result = types.SimpleNamespace(
        ok=True, data={"verdict": "greenlight"},
        tool_calls=[{"name": "read_code_file", "input": {
            "repo": "core", "path": "user_app/utils.py"}}])
    monkeypatch.setattr(bug_reports, "_task_text", lambda t: "")
    monkeypatch.setattr(bug_reports, "_existing", lambda t: "")
    card = bug_reports._analyst_card(
        TS, {"text": "r", "reporter": "Ron", "thread": "", "task_id": ""},
        lambda *a, **k: result)
    assert card["read"] == ["core:user_app/utils.py:1"]


def test_run_agent_reports_the_tool_calls_it_made(monkeypatch):
    from vomeos import runner

    def fake_loop(manifest, system, user, model):
        runner._TOOL_CALLS.get().append(
            {"name": "read_code_file", "input": {"repo": "core",
                                                 "path": "a.py"}})
        return '{"verdict": "decision"}', 1, 1, 1, 0, 0

    monkeypatch.setattr(runner, "_call_with_tools", fake_loop)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    result = runner.run_agent(bug_reports.AGENT, context={
        "report": "r", "reporter": "Ron", "thread": "t",
        "linked_task": "", "existing_tasks": ""})
    assert result.tool_calls == [{"name": "read_code_file", "input": {
        "repo": "core", "path": "a.py"}}]
    assert runner._TOOL_CALLS.get() is None  # reset after the run


def test_after_a_greenlight_a_rerun_never_routes_alone(rec):
    row = _row(greenlit=True)
    out = bug_reports.act(TS, row, COOKIES)
    assert out["action"] == "route offered"
    assert not _engineer_assigned(rec)


def test_sam_fix_outside_a_greenlight_lifts_the_risk_bar(rec, monkeypatch):
    import json

    risky = {**BUG, "code_check": {**BUG["code_check"], "fix_risk": "high"}}
    _stored(monkeypatch, card=json.dumps(risky))
    out = bug_reports._on_reply(TS, _sam("fix"))
    assert out["pr_urls"] == ["https://pr/core-repo"]


def test_yes_parsing():
    assert bug_reports._is_yes("Ship it!")
    assert bug_reports._is_yes("yes, do it")
    assert not bug_reports._is_yes("yes but use option 2")
    assert not bug_reports._is_yes("why is the risk high?")


# ---------------------------------------------------------------------------
# Fixes across repos: one Fix Commit once every PR has merged
# ---------------------------------------------------------------------------

def _multi_tasks():
    parent = {"id": "P1", "description":
              f"{bug_reports.MULTI_PR_LABEL} https://bitbucket.org/x/core/"
              f"pull-requests/1\n{bug_reports.MULTI_PR_LABEL} "
              "https://github.com/x/web/pull/2", "custom_fields": []}
    return [parent, {"id": "C1", "parent": "P1"},
            {"id": "C2", "parent": "P1"}]


def test_rollup_waits_for_every_pr_then_lists_all_commits(rec, monkeypatch):
    import ticket_fix

    monkeypatch.setattr(ticket_fix, "list_in_progress", _multi_tasks)
    states = {"1": ("merged", "core", "abc123"), "2": ("open", "web", "")}
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: states[url.rstrip("/")[-1]])
    assert bug_reports.run_rollup()["changed"] == []
    states["2"] = ("merged", "web", "def456")
    out = bug_reports.run_rollup()
    assert out["changed"] == ["P1 merged as core@abc123; web@def456"]
    assert ("POST", f"/task/P1/field/{ticket_fix.FIELD_FIX_COMMIT}",
            {"value": "core@abc123; web@def456"}) in rec.calls
    moved = {c[1] for c in rec.calls if c[2] == {"status": "on dev"}}
    assert moved == {"/task/P1", "/task/C1", "/task/C2"}


def test_rollup_closes_the_fix_when_a_pr_is_closed_unmerged(rec,
                                                            monkeypatch):
    import ticket_fix

    handed = []
    monkeypatch.setattr(ticket_fix, "list_in_progress", _multi_tasks)
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: ("closed", "web", "")
                        if url.endswith("2") else ("merged", "core", "abc"))
    monkeypatch.setattr(bug_reports, "_hand_back",
                        lambda task, closed: handed.append(task))
    bug_reports.run_rollup()
    closed = {c[1] for c in rec.calls if c[2] == {"status": "Closed"}}
    assert closed == {"/task/P1", "/task/C1", "/task/C2"}
    assert handed == ["P1"]
    assert not any(c[2] == {"status": "on prod"} for c in rec.calls)


def test_someone_else_saying_fix_is_not_a_command(rec, monkeypatch):
    _stored(monkeypatch, status=bug_reports.ROUTED)
    out = bug_reports._on_reply(TS, {"ts": "2.0", "text": "fix",
                                     "user": "U01B6D235N1"})
    assert out["status"] == "skipped"


def test_reporter_answer_reruns_a_waiting_report(rec, monkeypatch):
    _stored(monkeypatch, status=bug_reports.NEEDS_INFO)
    ran = []
    monkeypatch.setattr(bug_reports, "analyse",
                        lambda ts: ran.append(ts) or {"status": "ok"})
    bug_reports._on_reply(TS, {"ts": "2.0", "text": "the Forms page",
                               "user": "U01B6D235N1"})
    assert ran == [TS]


def test_same_reply_twice_runs_once(rec, monkeypatch):
    _stored(monkeypatch, last_reply_ts="2.0")
    out = bug_reports._on_reply(TS, {"ts": "2.0", "text": "close",
                                     "user": bug_reports.SAM_SLACK})
    assert out["reason"] == "reply already handled"


def test_linked_task_id_is_read_from_the_report():
    text = ("_Task:_ <https://app.clickup.com/t/868mg8nce|Exemption rules "
            "can't be created>")
    assert bug_reports._TASK_LINK.search(text).group(1) == "868mg8nce"


# ---------------------------------------------------------------------------
# Code tools on other branches
# ---------------------------------------------------------------------------

def test_tools_read_an_allowed_branch(monkeypatch):
    monkeypatch.setenv("VOMEOS_CODE_EXTRA_BRANCHES",
                       "web=develop|RON_DEVELOP")
    repo = tools.repo_map()["web"]
    assert tools._on_branch(repo, "RON_DEVELOP").branch == "RON_DEVELOP"
    assert tools._on_branch(repo, "").branch == repo.branch


def test_tools_refuse_a_branch_not_listed(monkeypatch):
    monkeypatch.setenv("VOMEOS_CODE_EXTRA_BRANCHES", "web=develop")
    out = tools.execute("read_code_file", {"repo": "core", "path": "x.py",
                                           "branch": "development"})
    assert "not readable on core" in out


def test_branch_is_offered_in_the_tool_schema(monkeypatch):
    monkeypatch.setenv("VOMEOS_CODE_EXTRA_BRANCHES", "core=development")
    defs = tools.definitions(["read_code_file"])
    assert "branch" in defs[0]["input_schema"]["properties"]
