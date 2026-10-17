"""
test_sentry_agent_fixes.py

Item two of the agent roadmap. On 2026-10-17 the pipeline had analysed 61
issues, written 0 diffs and filed 0 tasks: the analyst could not see the code
for logged errors, and nothing it concluded reached a place a person works.

These cases pin the three fixes: finding code from a log message, filing one
task per root cause, and the daily backfill of issues the webhook never sent.
"""

import pytest

import sentry_agent_fixes as saf
import sentry_code
import sentry_handler
from vomeos.integrations.base import IntegrationResult

BIRTHDAY = ('Error sending Vome birthday email: null value in column '
            '"content_mode" of relation "email_integration_app_sentemail" '
            'violates not-null constraint')
INTEGRITY = ('IntegrityError: null value in column "content_mode" of '
             'relation "email_integration_app_sentemail" violates not-null '
             'constraint')
CORE = "vomedjango-restored-core-app"


# ---------------------------------------------------------------------------
# Finding code for a logged error
# ---------------------------------------------------------------------------

def test_anchor_is_the_logged_literal():
    assert sentry_code.message_anchor(BIRTHDAY) == (
        "Error sending Vome birthday email"
    )


@pytest.mark.parametrize("title", [
    INTEGRITY,
    "KeyError: 'site'",
    "short: thing",
    "",
])
def test_exceptions_and_short_heads_have_no_anchor(title):
    assert sentry_code.message_anchor(title) == ""


class FakeRepo:
    def __init__(self, body, search_paths=("notification_app/birthday.py",)):
        self.body = body
        self.search_paths = search_paths
        self.reads = []

    def configured(self, **kwargs):
        return True

    def search_code(self, query, repo="", **kwargs):
        return IntegrationResult("bitbucket", "search_code", True, data={
            "values": [{"file": {"path": p}} for p in self.search_paths],
        })

    def read_file(self, repo, path, ref="", max_bytes=0, **kwargs):
        self.reads.append((path, ref, max_bytes))
        return IntegrationResult("bitbucket", "read_file", True,
                                 data=self.body)

    def recent_commits(self, repo, path="", limit=8, ref="", **kwargs):
        return IntegrationResult("bitbucket", "recent_commits", True,
                                 data={"values": []})


def test_logged_error_is_located_and_windowed(monkeypatch):
    lines = ["x = 1"] * 1700
    lines[1624] = (
        '        logger.error(f"Error sending Vome birthday email: {e}")'
    )
    repo = FakeRepo("\n".join(lines))
    monkeypatch.setattr(sentry_code, "_client_for", lambda host: repo)
    out = sentry_code.gather("", CORE, "master", message=BIRTHDAY)
    assert out["read"] == ["notification_app/birthday.py:1625"]
    assert ">> 1625 |" in out["files"]
    assert "located by the logged message" in out["files"]
    # Read at the branch that ran, and past the whole-file cap.
    assert repo.reads[0][1] == "master"
    assert repo.reads[0][2] == sentry_code.READ_BYTES


def test_frames_win_over_the_message(monkeypatch):
    repo = FakeRepo("a\nb\nc")
    monkeypatch.setattr(sentry_code, "_client_for", lambda host: repo)
    trace = 'File "chat_app/tasks.py", line 2, in run'
    out = sentry_code.gather(trace, CORE, "master", message=BIRTHDAY)
    assert out["read"] == ["chat_app/tasks.py:2"]


def test_deep_frames_are_read_past_the_default_cap(monkeypatch):
    repo = FakeRepo("a\n" * 3000)
    monkeypatch.setattr(sentry_code, "_client_for", lambda host: repo)
    sentry_code.gather('File "big/module.py", line 2500, in f', CORE,
                       "master")
    assert repo.reads[0][2] == sentry_code.READ_BYTES


def test_read_file_cap_is_raisable():
    from vomeos.integrations import code_search

    big = IntegrationResult("bitbucket", "read_file", True,
                            data="x" * (code_search.MAX_FILE_BYTES + 10))
    assert "truncated" in code_search._truncate(big).data
    big = IntegrationResult("bitbucket", "read_file", True,
                            data="x" * (code_search.MAX_FILE_BYTES + 10))
    assert "truncated" not in code_search._truncate(big, 10**7).data


# ---------------------------------------------------------------------------
# One task per root cause
# ---------------------------------------------------------------------------

def test_the_content_mode_issues_share_a_key():
    """Seven issues on Oct 16, one fault. One task."""
    assert saf.group_key(CORE, BIRTHDAY) == saf.group_key(CORE, INTEGRITY)


def test_numbers_and_ids_do_not_split_a_group():
    a = saf.group_key(CORE, "ValueError: shift 12345 has no site 9")
    b = saf.group_key(CORE, "ValueError: shift 678 has no site 10")
    assert a == b


def test_generic_messages_keep_their_call_site():
    title = "AttributeError: 'NoneType' object has no attribute 'user'"
    a = saf.group_key(CORE, title, "chat_app.tasks in send")
    b = saf.group_key(CORE, title, "opportunity_app.api in book")
    assert a != b


def test_different_repos_never_merge():
    assert saf.group_key("a", BIRTHDAY) != saf.group_key("b", BIRTHDAY)


@pytest.fixture
def clickup(monkeypatch):
    calls = []
    ledger = {}

    def fake_clickup(path, body):
        calls.append((path, body))
        return {"id": "86new"} if path.endswith("/task") else {}

    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_test")
    monkeypatch.delenv("SENTRY_AGENT_FIXES_ENABLED", raising=False)
    monkeypatch.setattr(saf, "_clickup", fake_clickup)
    monkeypatch.setattr(saf.sentry_ledger, "update",
                        lambda issue_id, **f: ledger.setdefault(
                            issue_id, {}).update(f) or True)
    monkeypatch.setattr(saf, "_group_task", lambda key, issue_id: "")
    monkeypatch.setattr(saf, "_filed_today", lambda: 0)
    return calls, ledger


def _issue(**over):
    issue = {
        "issue_id": "7796380886", "title": BIRTHDAY,
        "culprit": "ProcessScheduledBirthdayEmailsTask", "repo": CORE,
        "ref": "master", "severity": "s2", "stack": "backend",
        "permalink": "https://sentry.io/issues/7796380886/",
    }
    issue.update(over)
    return issue


ANALYSIS = {"root_cause": "SentEmail row written without content_mode",
            "evidence": "birthday_tasks.py:1625", "proposed_fix": "...",
            "risk": "high", "confidence": "medium",
            "needs_migration": False, "duplicate_of": "", "files": []}


def test_files_a_new_task_in_agent_fixes(clickup):
    calls, ledger = clickup
    out = saf.file_issue(_issue(), ANALYSIS, {"opened": False},
                         {"reason": "risk is high, not low"})
    assert out == {"action": "created", "task_id": "86new"}
    path, body = calls[0]
    assert path == f"/list/{saf.AGENT_FIXES_LIST}/task"
    assert body["name"].startswith("[Sentry] Error sending Vome birthday")
    assert body["status"] == "queued"
    assert body["priority"] == 2
    assert "risk is high" in body["markdown_description"]
    assert "Not verified" in body["markdown_description"]
    assert chr(0x2014) not in body["markdown_description"]
    # No engineer is volunteered for work nobody has proposed a fix for.
    assert not any(f["id"] == saf.FIELD_MERGE_OWNER
                   for f in body["custom_fields"])
    assert ledger["7796380886"]["clickup_task_id"] == "86new"


def test_an_open_pr_names_the_merge_owner(clickup):
    calls, _ = clickup
    saf.file_issue(_issue(), {**ANALYSIS, "risk": "low"},
                   {"opened": True, "url": "https://bb/pr/9"}, {})
    body = calls[0][1]
    assert body["status"] == "in progress"
    owner = [f for f in body["custom_fields"]
             if f["id"] == saf.FIELD_MERGE_OWNER][0]
    assert owner["value"] == {"add": [saf.CLICKUP_USER_ONLYG]}
    assert "https://bb/pr/9" in body["markdown_description"]


def test_a_second_issue_of_the_same_fault_comments(monkeypatch, clickup):
    calls, ledger = clickup
    monkeypatch.setattr(saf, "_group_task", lambda key, issue_id: "86first")
    out = saf.file_issue(_issue(issue_id="2", title=INTEGRITY), ANALYSIS,
                         {}, {})
    assert out == {"action": "commented", "task_id": "86first"}
    assert calls[0][0] == "/task/86first/comment"
    assert ledger["2"]["clickup_task_id"] == "86first"


def test_analyst_duplicate_comments_on_the_existing_task(clickup):
    calls, _ = clickup
    saf.file_issue(_issue(), {**ANALYSIS, "duplicate_of": "868mabc12"},
                   {}, {})
    assert calls[0][0] == "/task/868mabc12/comment"


def test_daily_cap(monkeypatch, clickup):
    calls, _ = clickup
    monkeypatch.setattr(saf, "_filed_today", lambda: 5)
    out = saf.file_issue(_issue(), ANALYSIS, {}, {})
    assert out["action"] == "skipped" and "cap" in out["reason"]
    assert calls == []


def test_switched_off(monkeypatch, clickup):
    calls, _ = clickup
    monkeypatch.setenv("SENTRY_AGENT_FIXES_ENABLED", "false")
    assert saf.file_issue(_issue(), ANALYSIS, {}, {})["action"] == "skipped"
    assert calls == []


def test_clickup_failure_is_reported_not_raised(monkeypatch, clickup):
    def boom(path, body):
        raise RuntimeError("ClickUp 500")

    monkeypatch.setattr(saf, "_clickup", boom)
    out = saf.file_issue(_issue(), ANALYSIS, {}, {})
    assert out["action"] == "skipped" and "500" in out["reason"]


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def test_backfill_admits_only_unknown_issues(monkeypatch):
    from vomeos.integrations import sentry as sentry_api

    class FakeSentry:
        def configured(self):
            return True

        def list_issues(self, project, limit=25, **kw):
            return IntegrationResult("sentry", "list_issues", True, data=[
                {"id": "1", "title": "known", "project": {"slug": project}},
                {"id": "2", "title": "new one", "project": {"slug": project},
                 "metadata": {"type": "ValueError"}},
            ])

    admitted = []
    monkeypatch.setattr(sentry_api, "Sentry", FakeSentry)
    monkeypatch.setattr(sentry_handler.sentry_projects, "triaged_slugs",
                        lambda: ("prod-vome",))
    monkeypatch.setattr(sentry_handler.sentry_ledger, "get",
                        lambda i: {"issue_id": i} if i == "1" else None)
    monkeypatch.setattr(
        sentry_handler, "_admit",
        lambda s, p: admitted.append(s) or {"status": "queued"},
    )
    out = sentry_handler.run_backfill()
    assert out["admitted"] == 1 and out["known"] == 1
    assert admitted[0].issue_id == "2"
    assert admitted[0].reason == "backfill"


def test_backfill_reports_an_unreadable_project(monkeypatch):
    from vomeos.integrations import sentry as sentry_api

    class FakeSentry:
        def configured(self):
            return True

        def list_issues(self, project, limit=25, **kw):
            return IntegrationResult("sentry", "list_issues", False,
                                     error="HTTP 403")

    monkeypatch.setattr(sentry_api, "Sentry", FakeSentry)
    monkeypatch.setattr(sentry_handler.sentry_projects, "triaged_slugs",
                        lambda: ("prod-vome",))
    out = sentry_handler.run_backfill()
    assert out["status"] == "partial" and "403" in out["errors"][0]


def test_backfill_is_registered_before_the_report():
    import engineering_jobs  # noqa: F401
    from vomeos.worker import schedule as job_schedule

    engineering_jobs.register_all()
    job = job_schedule.get_job("engineering.sentry_backfill")
    assert job.cron == "15 8 * * *"
