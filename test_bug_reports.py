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


class Recorder:
    def __init__(self):
        self.posts, self.calls, self.saved = [], [], {}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(bug_reports, "_post",
                        lambda text, thread_ts="", channel="":
                        r.posts.append((channel or "bug", thread_ts, text))
                        or "1.1")
    monkeypatch.setattr(bug_reports, "_permalink", lambda ts: "https://s/p")

    def clickup(method, path, body=None):
        r.calls.append((method, path, body))
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

def test_engineer_adopts_the_reporters_task_and_posts_in_their_channel(rec):
    card = {**BUG, "verdict": "engineer"}
    out = bug_reports.act(TS, _row(), card)
    assert out["verdict"] == "engineer"
    # Ron's own task is assigned, not a second one created.
    assert ("PUT", "/task/868mg8nce",
            {"assignees": {"add": [49257687], "rem": []}}) in rec.calls
    assert not any(c[1].startswith("/list/") for c in rec.calls)
    assert any(c[1] == f"/task/868mg8nce/field/{bug_reports.FIELD_SOURCE}"
               for c in rec.calls)
    channels = [p[0] for p in rec.posts]
    import sprint_thread
    assert sprint_thread.ENG_BACKEND in channels
    assert rec.saved["status"] == bug_reports.ROUTED


def test_engineer_without_a_linked_task_creates_one_marked_slack(rec):
    bug_reports.act(TS, _row(task_id=""), {**BUG, "verdict": "engineer",
                                           "team": "both"})
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


def test_risky_agent_fix_falls_back_to_an_engineer(rec):
    card = {**BUG, "code_check": {**BUG["code_check"], "fix_risk": "medium"}}
    out = bug_reports.act(TS, _row(), card)
    assert out["verdict"] == "engineer"
    assert "fix risk is 'medium'" in rec.posts[-1][2]


def test_no_code_read_asks_sam_instead_of_routing(rec):
    card = {**BUG, "verdict": "engineer",
            "code_check": {"verdict": "not_checked"}}
    out = bug_reports.act(TS, _row(), card)
    assert out["verdict"] == bug_reports.DECISION
    assert "`route backend`" in rec.posts[-1][2]
    assert not any(c[0] == "PUT" for c in rec.calls)


def test_agent_fix_opens_a_pr_and_files_agent_fixes(rec, monkeypatch):
    import sentry_fix

    class Repo:
        alias, slug, host, owner = "core", "core-app", "bitbucket", ""

    monkeypatch.setattr(bug_reports, "_propose",
                        lambda ts, card: ({"diff": "--- a\n+++ b",
                                           "attempted": True}, Repo))
    monkeypatch.setattr(sentry_fix, "open_pull_request",
                        lambda *a: {"opened": True, "url": "https://pr/1"})
    out = bug_reports.act(TS, _row(), BUG)
    assert out["pr_url"] == "https://pr/1"
    fix = [c for c in rec.calls if c[1].endswith("/task")
           and c[0] == "POST"][0]
    assert "Fix PR: https://pr/1" in fix[2]["markdown_description"]
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


def test_sam_fix_on_a_risky_card_explains_instead_of_routing(rec,
                                                             monkeypatch):
    import json

    risky = {**BUG, "code_check": {**BUG["code_check"], "fix_risk": "high"}}
    _stored(monkeypatch, card=json.dumps(risky))
    out = bug_reports._on_reply(TS, {"ts": "2.0", "text": "fix",
                                     "user": bug_reports.SAM_SLACK})
    assert out["action"] == "fix refused"
    assert not any(c[0] == "PUT" for c in rec.calls)


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
