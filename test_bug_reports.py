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
    # Cards here carry no "read" list; looking again finds nothing new.
    monkeypatch.setattr(bug_reports, "_refresh_card", lambda ts: {})
    # Failures go to Agent Workflows; record them instead of calling ClickUp.
    r.logged = []
    monkeypatch.setattr(bug_reports, "_log_failure",
                        lambda ts, card, reason, what="build": r.logged.append(
                            (what, reason)) or "https://app.clickup.com/t/AW1")
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


def test_not_a_bug_closes_ron_task_as_declined(rec):
    """No `declined` column (Sam, 2026-10-22): Closed, Resolution Declined."""
    bug_reports.act(TS, _row(), _certain("not_a_bug"))
    assert ("PUT", "/task/868mg8nce", {"status": "Closed"}) in rec.calls
    assert ("POST", f"/task/868mg8nce/field/{bug_reports.FIELD_RESOLUTION}",
            {"value": bug_reports.RESOLUTION_DECLINED}) in rec.calls
    assert "Marked closed on ClickUp" in rec.posts[-1][2]


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


def test_yes_on_a_card_saved_before_read_files_looks_again(rec,
                                                           monkeypatch):
    """The third exemption yes (2026-10-19) built from a card saved before
    the analysis recorded the files it opened, so no references went to the
    fix author."""
    _greenlight_waiting(monkeypatch)  # GUEST has no "read" key
    refreshed = []
    monkeypatch.setattr(bug_reports, "_refresh_card",
                        lambda ts: refreshed.append(ts)
                        or {**GUEST, "read": ["core:a/b.py:1"]})
    out = bug_reports._on_reply(TS, _sam("yes"))
    assert refreshed == [TS] and out.get("pr_urls")


def test_yes_on_a_current_card_builds_without_looking_again(
        rec, monkeypatch):
    _greenlight_waiting(monkeypatch, card={**GUEST, "read": []})
    monkeypatch.setattr(bug_reports, "_refresh_card",
                        lambda ts: pytest.fail("should not re-run"))
    assert bug_reports._on_reply(TS, _sam("yes")).get("pr_urls")


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


# ---------------------------------------------------------------------------
# Every failure is logged in Agent Workflows (Sam, 2026-10-20)
# ---------------------------------------------------------------------------

def test_a_failed_greenlit_build_is_logged_and_linked(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    rec.pr_opens = False
    bug_reports._on_reply(TS, _sam("yes"))
    assert rec.logged and "patch does not apply" in rec.logged[0][1]
    assert ("Logged for review in Agent Workflows: "
            "<https://app.clickup.com/t/AW1|task>") in rec.posts[-1][2]


def test_a_partly_built_fix_logs_the_rest(rec, monkeypatch):
    _greenlight_waiting(monkeypatch)
    rec.diff["web"] = ""
    bug_reports._on_reply(TS, _sam("yes"))
    assert rec.logged[0][0] == "Part of the fix was not built"
    assert "Agent Workflows" in rec.posts[-1][2]


def test_the_agents_own_small_fix_failing_is_logged(rec):
    rec.diff["core"] = ""
    out = bug_reports.act(TS, _row(), BUG)
    assert out["verdict"] == bug_reports.GREENLIGHT
    assert rec.logged[0][0] == "The agent's own small fix failed"
    assert "Agent Workflows" in rec.posts[-1][2]


def test_a_fix_with_a_data_update_beside_it_builds_and_routes_the_rest(
        rec, monkeypatch):
    """Ron's cancellation policy report (2026-10-20): a web fix plus a
    one-off data update. The fix is built; the data part goes to backend."""
    import sprint_thread

    card = {**GUEST, "also_needs": "one-off update: blank policies become "
                                   "a 12-hour window"}
    out = bug_reports.act(TS, _row(), card)
    assert "*Also needs an engineer:* one-off update" in rec.posts[-1][2]
    rec.posts.clear()
    _greenlight_waiting(monkeypatch, card=card)
    out = bug_reports._on_reply(TS, _sam("yes"))
    assert out.get("pr_urls")
    created = [c for c in rec.calls
               if c[1] == f"/list/{bug_reports.PRIORITY_QUEUE}/task"]
    assert len(created) == 1 and created[0][2]["assignees"] == [49257687]
    assert "one-off update" in created[0][2]["name"]
    assert any(p[0] == sprint_thread.ENG_BACKEND for p in rec.posts)
    thread = rec.posts[-1][2]
    assert thread.startswith(":white_check_mark: Fixed by the agent.")
    assert "Also logged on ClickUp for OnlyG" in thread


def test_a_hard_limit_is_a_refusal_not_a_failure(rec, monkeypatch):
    _greenlight_waiting(monkeypatch, card={**GUEST,
                                           "hard_limit": "migration"})
    bug_reports._on_reply(TS, _sam("yes"))
    assert rec.logged == []


def test_a_crash_is_logged(monkeypatch):
    logged, posts = [], []
    monkeypatch.setattr(bug_reports.store, "ensure_table", lambda t: True)
    monkeypatch.setattr(bug_reports, "_row", lambda ts: {})
    monkeypatch.setattr(bug_reports, "analyse",
                        lambda ts: (_ for _ in ()).throw(KeyError("x")))
    monkeypatch.setattr(bug_reports, "_save", lambda ts, **f: None)
    monkeypatch.setattr(bug_reports, "_post",
                        lambda text, thread_ts="", channel="":
                        posts.append(text))
    monkeypatch.setattr(bug_reports, "_log_failure",
                        lambda ts, card, reason, what="":
                        logged.append(what) or "https://app.clickup.com/t/X")
    out = bug_reports.handle({"ts": TS})
    assert out["status"] == "error"
    assert logged == ["The bug report workflow crashed"]
    assert "Agent Workflows" in posts[-1]


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


@pytest.mark.parametrize("text", [
    "Ship it!", "yes, do it", "Yes build it", "go ahead and build it",
    "build the PR", "yes please", "ok build it", "LGTM", "yep",
    f"<@{bug_reports.SAM_SLACK}> yes build it",
])
def test_yes_parsing_accepts_plain_approval(text):
    # "Yes build it" (2026-10-19) was read as a conversation by the first,
    # phrase-only version and the build never ran.
    assert bug_reports._is_yes(text)


@pytest.mark.parametrize("text", [
    "yes but use option 2", "why is the risk high?", "don't build it",
    "yes but only the backend", "ok", "thanks", "not yet",
    "build it without the 403 part",
])
def test_yes_parsing_leaves_conversation_alone(text):
    assert not bug_reports._is_yes(text)


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
    read = []
    monkeypatch.setattr(bug_reports, "_digest",
                        lambda ts, row, reply: read.append(reply["text"])
                        or {"status": "ok", "kind": "chat"})
    bug_reports._on_reply(TS, {"ts": "2.0", "text": "fix",
                               "user": "U01B6D235N1"})
    assert rec.opened == [] and read == ["fix"]


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


# ---------------------------------------------------------------------------
# The icon on each report (Sam, 2026-10-19)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,icon", [
    (bug_reports.CHECKING, "eyes"),
    (bug_reports.GREENLIGHT, "hourglass_flowing_sand"),
    (bug_reports.NEEDS_INFO, "hourglass_flowing_sand"),
    (bug_reports.ROUTED, "white_check_mark"),
    (bug_reports.FIXING, "white_check_mark"),
    (bug_reports.CLOSED, "white_check_mark"),
    (bug_reports.ERROR, "warning"),
])
def test_saving_a_status_sets_the_reports_icon(monkeypatch, status, icon):
    marked = []
    monkeypatch.setattr(bug_reports, "_mark",
                        lambda ts, state: marked.append(
                            bug_reports.MARKS[state]))
    monkeypatch.setattr(bug_reports.store, "ensure_table", lambda t: False)
    bug_reports._save(TS, status=status)
    assert marked == [icon]


def test_saving_without_a_status_leaves_the_icon(monkeypatch):
    monkeypatch.setattr(bug_reports, "_mark",
                        lambda *a: pytest.fail("should not mark"))
    monkeypatch.setattr(bug_reports.store, "ensure_table", lambda t: False)
    bug_reports._save(TS, last_reply_ts="2.0")


def test_marking_swaps_only_the_bots_own_icons(monkeypatch):
    calls = []

    class Client:
        def reactions_remove(self, **kw):
            calls.append(("remove", kw["name"]))
            if kw["name"] != "eyes":
                raise RuntimeError("no_reaction")

        def reactions_add(self, **kw):
            calls.append(("add", kw["name"]))

    monkeypatch.setattr(bug_reports, "_slack", Client)
    bug_reports._mark(TS, "done")
    assert ("add", "white_check_mark") in calls
    removed = {name for verb, name in calls if verb == "remove"}
    assert removed == set(bug_reports.MARKS.values()) - {"white_check_mark"}


def test_a_new_report_gets_eyes_straight_away(monkeypatch):
    import vomeos.worker

    marked = []
    monkeypatch.setattr(bug_reports, "_post", lambda *a, **k: "1")
    monkeypatch.setattr(bug_reports, "_mark",
                        lambda ts, state: marked.append((ts, state)))
    monkeypatch.setattr(vomeos.worker, "enqueue_event", lambda *a, **k: None)
    bug_reports.handle_message({"ts": TS, "text": "a bug", "user": "U1"})
    assert marked == [(TS, "checking")]


# ---------------------------------------------------------------------------
# The reporter hears when the fix merges and when it is live
# ---------------------------------------------------------------------------

@pytest.fixture
def progress(monkeypatch):
    import ticket_fix

    p = type("P", (), {})()
    p.rows, p.states, p.live, p.posts, p.saved, p.offered = [], {}, False, \
        [], [], []
    monkeypatch.setattr(bug_reports.store, "ensure_table", lambda t: True)
    monkeypatch.setattr(bug_reports.store, "query",
                        lambda sql, params=None, **k: p.rows)
    monkeypatch.setattr(ticket_fix, "_pr_state", lambda url: p.states[url])
    monkeypatch.setattr(bug_reports, "_is_live", lambda task: p.live)
    monkeypatch.setattr(bug_reports, "_reporter_mention",
                        lambda ts: "<@U01B6D235N1> ")
    monkeypatch.setattr(bug_reports, "_post",
                        lambda text, thread_ts="", channel="":
                        p.posts.append((thread_ts, text)))
    monkeypatch.setattr(bug_reports, "_save",
                        lambda ts, **f: p.saved.append((ts, f)))
    monkeypatch.setattr(bug_reports, "_offer_route",
                        lambda ts, card, reason, dry_run=False:
                        p.offered.append((ts, reason)))
    return p


def _fixing(pr_url="https://pr/core", status=bug_reports.FIXING):
    return {"ts": TS, "status": status, "pr_url": pr_url,
            "fix_task": "F1", "card": "{}"}


def test_merged_fix_tells_the_reporter_to_test(progress):
    progress.rows = [_fixing()]
    progress.states = {"https://pr/core": ("merged", "core", "abc1234")}
    out = bug_reports.notify_progress()
    assert out["told"] == [f"{TS} merged"]
    thread, text = progress.posts[0]
    assert thread == TS
    assert text.startswith("<@U01B6D235N1> Merged into `development` "
                           "(`core@abc1234`). Ready to test there")
    assert progress.saved == [(TS, {"status": bug_reports.MERGED})]


def test_an_open_pr_says_nothing_yet(progress):
    progress.rows = [_fixing()]
    progress.states = {"https://pr/core": ("open", "core", "")}
    bug_reports.notify_progress()
    assert progress.posts == [] and progress.saved == []


def test_multi_repo_waits_for_every_pr(progress):
    progress.rows = [_fixing("https://pr/core https://pr/web")]
    progress.states = {"https://pr/core": ("merged", "core", "abc"),
                       "https://pr/web": ("open", "web", "")}
    bug_reports.notify_progress()
    assert progress.posts == []
    progress.states["https://pr/web"] = ("merged", "web", "def")
    bug_reports.notify_progress()
    assert "`core@abc`, `web@def`" in progress.posts[0][1]


def test_a_pr_closed_unmerged_goes_back_to_sam(progress):
    progress.rows = [_fixing()]
    progress.states = {"https://pr/core": ("closed", "core", "")}
    bug_reports.notify_progress()
    assert progress.offered and "closed without merging" in \
        progress.offered[0][1]
    assert progress.posts == []


def test_live_fix_tells_the_thread_once(progress):
    progress.rows = [_fixing(status=bug_reports.MERGED)]
    progress.live = False
    bug_reports.notify_progress()
    assert progress.posts == []
    progress.live = True
    bug_reports.notify_progress()
    assert progress.posts == [(TS, "<@U01B6D235N1> Live in production now. "
                                   ":rocket:")]
    assert progress.saved == [(TS, {"status": bug_reports.LIVE})]


def test_merged_and_live_have_their_own_icons():
    assert bug_reports.MARKS[bug_reports.STATUS_MARK[bug_reports.MERGED]] \
        == "test_tube"
    assert bug_reports.MARKS[bug_reports.STATUS_MARK[bug_reports.LIVE]] \
        == "rocket"


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


# ---------------------------------------------------------------------------
# Every other reply is read (Sam, 2026-10-21): Ron's "one more on this" on a
# merged fix used to be dropped as chat on a finished report.
# ---------------------------------------------------------------------------

RON = "U01B6D235N1"
RON_FOLLOW_UP = (
    "@Sammy one more on this: the sequence completion email sends the same "
    "template, so it has the bold custom message and English footer too.")


@pytest.fixture
def reader(rec, monkeypatch):
    """The reader's answer, and everything around it, faked."""
    import types

    import vomeos

    r = types.SimpleNamespace(data={"kind": "chat"}, ok=True, runs=[],
                              analysed=[], lessons=[], children=[])

    def run_agent(name, context=None, **kw):
        r.runs.append((name, context))
        return types.SimpleNamespace(ok=r.ok, data=r.data,
                                     why=lambda: "timed out")

    monkeypatch.setattr(vomeos, "run_agent", run_agent)
    monkeypatch.setattr(bug_reports, "_read_report", lambda ts: {
        "text": "the report", "reporter": "Ron", "reporter_id": RON,
        "thread": "agent: Merged into development", "task_id": "868mg8nce"})
    monkeypatch.setattr(bug_reports, "_name", lambda u: "Ron")
    monkeypatch.setattr(bug_reports, "_follow_ups", lambda ts: r.children)
    monkeypatch.setattr(bug_reports, "analyse",
                        lambda ts, **k: r.analysed.append(ts)
                        or {"status": "ok", "verdict": "agent_fix"})
    saved = []
    monkeypatch.setattr(bug_reports, "_save",
                        lambda ts, **f: saved.append((ts, f)))
    r.saved = saved
    _stored(monkeypatch, status=bug_reports.MERGED)
    rows = {TS: bug_reports._row(TS)}
    monkeypatch.setattr(bug_reports, "_row", lambda ts: rows.get(ts, {}))
    r.rows = rows

    from vomeos import lessons

    monkeypatch.setattr(lessons, "add", lambda agent, rule, source, **k:
                        r.lessons.append((agent, rule, k)) or 41)
    return r


def _ron(text, ts="3.0"):
    return {"ts": ts, "text": text, "user": RON}


def test_a_follow_up_on_a_merged_fix_becomes_its_own_report(rec, reader):
    reader.data = {"kind": "follow_up",
                   "follow_up": "Sequence completion email uses the same "
                                "template and English-only text."}
    out = bug_reports._on_reply(TS, _ron(RON_FOLLOW_UP))
    assert out["follow_up"] == "3.0" and reader.analysed == ["3.0"]
    child = dict(reader.saved)["3.0"]
    assert child["parent_ts"] == TS and child["reporter_id"] == RON
    assert child["status"] == bug_reports.CHECKING
    assert "Sequence completion email" in child["card"]
    assert rec.posts == [("bug", TS, "Looking into this follow-up. I'll "
                          "check it against the code and update this "
                          "thread.")]
    # The reader saw where the report stands, so it never calls it live.
    context = reader.runs[0][1]
    assert "merged into the dev branch, not in production" in \
        context["status"]
    assert context["new_reply"].startswith("Ron: @Sammy one more")


def test_a_follow_up_seen_before_is_not_checked_twice(rec, reader):
    reader.data = {"kind": "follow_up", "follow_up": "x"}
    reader.rows["3.0"] = {"ts": "3.0", "parent_ts": TS}
    out = bug_reports._on_reply(TS, _ron(RON_FOLLOW_UP))
    assert out["reason"] == "follow-up already handled"
    assert reader.analysed == []


def test_thanks_needs_no_model_and_no_post(rec, reader):
    out = bug_reports._on_reply(TS, _ron("thanks!"))
    assert out["reason"] == "acknowledgement"
    assert reader.runs == [] and rec.posts == []


def test_chat_stays_quiet(rec, reader):
    out = bug_reports._on_reply(TS, _ron("OnlyG is on it tomorrow"))
    assert out["action"] == "read, stayed quiet" and rec.posts == []


def test_a_question_is_answered_in_the_thread(rec, reader):
    reader.data = {"kind": "question",
                   "reply": "Merged into development, not in production "
                            "yet."}
    bug_reports._on_reply(TS, _ron("Is this live yet?"))
    assert rec.posts == [("bug", TS, "Merged into development, not in "
                          "production yet.")]


def test_detail_goes_on_the_task_and_the_thread_gets_the_link(rec, reader):
    reader.data = {"kind": "info", "task_note": "Org: Maison Fraternite."}
    bug_reports._on_reply(TS, _ron("Use Maison Fraternite to test"))
    assert "Org: Maison Fraternite." in _comments(rec)
    assert "Added in the #bug-reports thread by Ron" in _comments(rec)
    assert rec.posts[-1][2] == ("Added to the ClickUp task: "
                                "<https://app.clickup.com/t/868mg8nce|task>")


def test_ron_teaching_the_analyst_tags_sam(rec, reader):
    reader.data = {"kind": "feedback", "reply": "Noted.",
                   "lesson": "Route the part a fix leaves out.",
                   "lesson_agent": "bug_report_analyst"}
    bug_reports._on_reply(TS, _ron("you should route the leftover part"))
    agent, rule, extra = reader.lessons[0]
    assert agent == "engineering.bug_report_analyst"
    assert extra["evidence"] == "Ron: you should route the leftover part"
    text = rec.posts[-1][2]
    assert text.startswith("Noted.\n<@U01B9FQNSBU> :brain: Learned *L41*")
    assert "Say `forget 41` to undo." in text


def test_sam_teaching_does_not_tag_himself(rec, reader):
    reader.data = {"kind": "feedback", "lesson": "Route leftovers.",
                   "lesson_agent": "fix_author"}
    bug_reports._on_reply(TS, _sam("route leftovers next time"))
    assert reader.lessons[0][0] == "engineering.fix_author"
    assert rec.posts[-1][2].startswith(":brain: Learned *L41*")


def test_a_correction_that_leaves_work_learns_and_checks_it(rec, reader):
    """Sam's "route the leftover part straight away" is a rule for next
    time and undone work in this thread."""
    reader.data = {"kind": "follow_up", "follow_up": "Route the French "
                   "footer part.", "lesson": "Route what a fix leaves out.",
                   "lesson_agent": "bug_report_analyst"}
    bug_reports._on_reply(TS, _sam("route the leftover part straight away"))
    assert reader.analysed == ["2.0"]
    assert reader.lessons[0][0] == "engineering.bug_report_analyst"
    ack = rec.posts[-1][2]
    assert ack.startswith("Looking into this follow-up.")
    assert "\n:brain: Learned *L41*" in ack


def test_a_lesson_for_any_other_agent_is_refused(rec, reader):
    reader.data = {"kind": "feedback", "lesson": "Always apologise.",
                   "lesson_agent": "reply_drafter"}
    bug_reports._on_reply(TS, _ron("be nicer"))
    assert reader.lessons == [] and rec.posts == []


def test_a_failed_read_stays_out_of_the_thread(rec, reader):
    reader.ok = False
    out = bug_reports._on_reply(TS, _ron("one more on this"))
    assert out["reason"] == "reply read failed" and rec.posts == []


def test_sams_command_answers_the_waiting_follow_up(rec, reader,
                                                    monkeypatch):
    reader.children = [{"ts": "3.0", "parent_ts": TS,
                        "status": bug_reports.DECISION, "card": "{}",
                        "task_id": "868mg8nce"}]
    routed = []
    monkeypatch.setattr(bug_reports, "_route",
                        lambda ts, row, card, team, reason="":
                        routed.append((ts, team)) or {"status": "ok"})
    bug_reports._on_reply(TS, _sam("route backend"))
    assert routed == [("3.0", "backend")]


def test_an_answer_reruns_the_waiting_follow_up(rec, reader):
    reader.children = [{"ts": "3.0", "parent_ts": TS,
                        "status": bug_reports.NEEDS_INFO, "card": "{}"}]
    bug_reports._on_reply(TS, _ron("the French sequence", ts="4.0"))
    assert reader.analysed == ["3.0"] and reader.runs == []


def test_forget_retires_the_lesson(rec, reader, monkeypatch):
    from vomeos import lessons

    retired = []
    monkeypatch.setattr(lessons, "get", lambda i: {"rule": "Route it."})
    monkeypatch.setattr(lessons, "retire",
                        lambda i, note="": retired.append(i))
    bug_reports._on_reply(TS, _sam("forget 41"))
    assert retired == [41]
    assert rec.posts[-1][2] == ":wastebasket: Forgot L41: Route it."


def test_a_follow_up_reads_as_its_own_report(monkeypatch):
    """The reply is the report, the earlier report heads the thread, and
    the task is the earlier report's unless the reply links one."""
    import json

    rows = {"3.0": {"ts": "3.0", "parent_ts": TS,
                    "card": json.dumps({"follow_up": "Same template."})}}
    monkeypatch.setattr(bug_reports, "_row", lambda ts: rows.get(ts, {}))
    monkeypatch.setattr(bug_reports, "_name", lambda u: u)

    class Client:
        def conversations_replies(self, channel, ts, limit):
            assert ts == TS
            return {"messages": [
                {"ts": TS, "user": "R", "text":
                    "bold email https://app.clickup.com/t/868parent"},
                {"ts": "2.0", "bot_id": "B", "text": "Merged."},
                {"ts": "3.0", "user": RON, "text": RON_FOLLOW_UP},
                {"ts": "4.0", "user": "S", "text": "nice catch"}]}

    monkeypatch.setattr(bug_reports, "_slack", Client)
    report = bug_reports._read_report("3.0")
    assert report["text"].startswith(bug_reports.FOLLOW_UP_NOTE)
    assert "In short: Same template." in report["text"]
    assert report["text"].endswith(RON_FOLLOW_UP)
    assert report["reporter_id"] == RON and report["task_id"] == "868parent"
    assert report["thread"].startswith("Earlier report by R: bold email")
    assert "agent: Merged." in report["thread"]
    assert RON_FOLLOW_UP not in report["thread"]


def test_a_follow_ups_posts_go_in_the_reports_thread(monkeypatch):
    posted = []

    class Client:
        def chat_postMessage(self, **kw):
            posted.append(kw["thread_ts"])
            return {"ts": "9.0"}

    monkeypatch.setattr(bug_reports, "_slack", Client)
    monkeypatch.setattr(bug_reports, "_row", lambda ts: {
        "3.0": {"parent_ts": TS}}.get(ts, {}))
    bug_reports._post("hi", thread_ts="3.0")
    bug_reports._post("hi", thread_ts=TS)
    assert posted == [TS, TS]


def test_merged_notice_tags_who_raised_the_follow_up(monkeypatch):
    monkeypatch.setattr(bug_reports, "_row",
                        lambda ts: {"reporter_id": "UFOLLOW"})
    assert bug_reports._reporter_mention("3.0") == "<@UFOLLOW> "


# =====================================================================
# A report posted with a screenshot (2026-10-22)
#
# Ron typed "Can't save admin role. Says Invalid Modules" in Slack with an
# image. Slack sent it as subtype "file_share" and the webhook dropped every
# subtype, so the report got no reply. It must reach the channel handler.
# =====================================================================

def _main():
    import os as _os

    _os.environ.setdefault("DATABASE_URL", "")
    import main
    return main


def test_a_report_with_a_screenshot_reaches_the_handler():
    main = _main()
    event = {"type": "message", "subtype": "file_share",
             "channel": bug_reports.CHANNEL, "user": "URON",
             "text": "Can't save admin role", "ts": "5.0"}
    assert not main._ignored_event(event)


def test_other_subtypes_and_bots_are_still_ignored():
    main = _main()
    for event in ({"subtype": "message_changed",
                   "channel": bug_reports.CHANNEL},
                  {"subtype": "file_share", "bot_id": "B1",
                   "channel": bug_reports.CHANNEL},
                  {"subtype": "file_share", "channel": "CTICKETS"}):
        assert main._ignored_event(event)
    assert not main._ignored_event({"channel": "CTICKETS", "text": "hi"})


def test_backfill_includes_a_report_with_a_screenshot(monkeypatch):
    class Client:
        def conversations_history(self, **kw):
            return {"messages": [
                {"ts": "6.0", "subtype": "channel_join", "text": "joined"},
                {"ts": "5.0", "subtype": "file_share", "user": "URON",
                 "text": "Can't save admin role"}]}

    seen = []
    monkeypatch.setattr(bug_reports, "_slack", Client)
    monkeypatch.setattr(bug_reports, "analyse",
                        lambda ts, dry_run=False: seen.append(ts) or {})
    bug_reports.backfill(dry_run=True)
    assert seen == ["5.0"]


def test_run_one_skips_a_report_already_handled(monkeypatch):
    monkeypatch.setattr(bug_reports, "_row",
                        lambda ts: {"status": bug_reports.ROUTED})
    monkeypatch.setattr(bug_reports, "analyse",
                        lambda ts, dry_run=False: pytest.fail("reran"))
    assert bug_reports.run_one(TS, dry_run=False)["status"] == "skipped"


def test_run_one_acks_and_analyses_a_missed_report(monkeypatch):
    calls = []
    monkeypatch.setattr(bug_reports, "_row", lambda ts: {})
    monkeypatch.setattr(bug_reports, "_post",
                        lambda text, thread_ts: calls.append(("post", thread_ts)))
    monkeypatch.setattr(bug_reports, "_mark",
                        lambda ts, status: calls.append(("mark", status)))
    monkeypatch.setattr(bug_reports, "analyse",
                        lambda ts, dry_run=False: calls.append(("analyse", ts))
                        or {"status": "ok"})
    bug_reports.run_one(TS, dry_run=False)
    assert calls == [("post", TS), ("mark", "checking"), ("analyse", TS)]
