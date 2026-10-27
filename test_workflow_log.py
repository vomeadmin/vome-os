"""
"Log this on ClickUp" (built 2026-10-19): what triggers it, what the task
looks like, and that a thread logged twice gets one task.

No database and no network: every outside call is replaced.
"""

from __future__ import annotations

import pytest

import workflow_log as wl

SAM, RON, OTHER = wl.SAM_SLACK, wl.RON_SLACK, "U0OTHER"
BOT = "U0AMGEX92GG"


def _event(text, user=SAM, **over):
    event = {"user": user, "text": text, "channel": "C0CAJ269RAQ",
             "ts": "2.0", "thread_ts": "1.0"}
    event.update(over)
    return event


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Log this for us on ClickUp",
    "log this on clickup",
    "can you log this for later",
    "add it to agent workflows",
    "log this as an idea on Click Up",
])
def test_sam_asking_to_log_triggers(text, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_USER_ID", BOT)
    assert wl.wants(_event(text))


def test_ron_can_log(monkeypatch):
    assert wl.wants(_event("log this on clickup", user=RON))


def test_nobody_else_can_log():
    assert not wl.wants(_event("log this on clickup", user=OTHER))


@pytest.mark.parametrize("text", [
    "log 101",                         # the sprint's feature request command
    "log this",                        # no where, no mention
    "I'll check ClickUp later",        # no ask
    "route backend",
])
def test_other_messages_do_not_trigger(text, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_USER_ID", BOT)
    assert not wl.wants(_event(text))


def test_mentioning_the_bot_is_enough(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_USER_ID", BOT)
    assert wl.wants(_event(f"<@{BOT}> log this"))
    assert wl.wants(_event("log this"), mentioned=True)


def test_switch_off(monkeypatch):
    monkeypatch.setenv("WORKFLOW_LOG_ENABLED", "false")
    assert not wl.wants(_event("log this on clickup"))


def test_main_checks_it_before_any_channel_routing():
    """A "log this" reply in a #bug-reports or sprint thread must not also
    reach that channel's own handler."""
    src = open("main.py", encoding="utf-8").read()
    message = src.index('if event_type == "message":')
    check = src.index("workflow_log.wants(event)", message)
    assert check < src.index("sprint_thread.CHANNEL", message)
    assert check < src.index("bug_reports.CHANNEL", message)
    mention = src.index('if event_type == "app_mention":')
    assert src.index("workflow_log.wants(event, mentioned=True)",
                     mention) < src.index("handle_agent_mention(event)",
                                          mention)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, data, ok=True):
        self.data, self.ok = data, ok


CARD = {"title": "Greenlit build fails on a 60-line window", "type": "bug",
        "workflow": "bug reports",
        "summary": "Sam said yes and the build was refused.",
        "details": "- refused at line 94", "next_step": "Read whole files."}


@pytest.fixture
def rec(monkeypatch):
    import vomeos

    r = type("R", (), {})()
    r.posts, r.calls, r.claimed, r.existing, r.card = [], [], True, None, CARD
    r.contexts = []
    monkeypatch.setattr(wl, "_claim", lambda *a: r.claimed)
    monkeypatch.setattr(wl, "_existing_task", lambda *a: r.existing)
    monkeypatch.setattr(wl, "_record", lambda *a: None)
    monkeypatch.setattr(wl, "_read_thread",
                        lambda *a: "Ron: exemption bug\nagent: Greenlight?")
    monkeypatch.setattr(wl, "_channel_name", lambda c: "#bug-reports")
    monkeypatch.setattr(wl, "_permalink", lambda c, t: "https://s/thread")
    monkeypatch.setattr(wl, "_post",
                        lambda c, t, text: r.posts.append((c, t, text)))
    r.marked = []
    monkeypatch.setattr(wl, "_mark_logged",
                        lambda c, t: r.marked.append((c, t)))

    def clickup(method, path, body=None):
        r.calls.append((method, path, body))
        return {"id": "T1", "url": "https://app.clickup.com/t/T1"}

    monkeypatch.setattr(wl, "_clickup", clickup)
    monkeypatch.setattr(vomeos, "run_agent",
                        lambda agent, context, **kw: r.contexts.append(context)
                        or Result(r.card, ok=r.card is not None))
    wl._TAGS_READY.clear()
    return r


def test_logs_a_task_in_agent_workflows(rec):
    out = wl.handle(_event("Log this for us on ClickUp"))
    assert out == {"status": "ok", "task": "T1", "type": "bug"}
    create = [c for c in rec.calls if c[1] == f"/list/{wl.LIST}/task"][0][2]
    assert create["name"] == "[Bug] Greenlit build fails on a 60-line window"
    assert create["status"] == "to do"
    assert create["assignees"] == [wl.SAM_CLICKUP]
    desc = create["markdown_description"]
    assert "Sam said yes" in desc
    assert "[Slack thread](https://s/thread)" in desc
    assert "Logged by Sam from #bug-reports" in desc
    tagged = [c[1] for c in rec.calls if "/tag/" in c[1]]
    assert tagged == ["/task/T1/tag/bug", "/task/T1/tag/from%20slack"]
    # The thread goes in; the request is passed without the mention.
    assert rec.contexts[0]["request"] == "Log this for us on ClickUp"
    assert "Greenlight?" in rec.contexts[0]["thread"]
    # Slack: a check mark, one line and the link; posted in the thread.
    channel, thread, text = rec.posts[-1]
    assert thread == "1.0"
    assert text.startswith(":white_check_mark: Logged on ClickUp in Agent "
                           "Workflows: <https://app.clickup.com/t/T1|")
    # A clipboard on the thread's first message: visible without opening it.
    assert rec.marked == [("C0CAJ269RAQ", "1.0")]


def test_logged_by_ron_says_so(rec):
    wl.handle(_event("log this on clickup", user=RON))
    create = [c for c in rec.calls if c[1] == f"/list/{wl.LIST}/task"][0][2]
    assert "Logged by Ron" in create["markdown_description"]
    assert create["assignees"] == [wl.SAM_CLICKUP]


def test_same_thread_again_comments_on_its_task(rec):
    rec.existing = ("T0", "[Bug] Earlier log")
    out = wl.handle(_event("log this on clickup", ts="3.0"))
    assert out["action"] == "commented"
    assert not any(c[1] == f"/list/{wl.LIST}/task" for c in rec.calls)
    assert any(c[1] == "/task/T0/comment" for c in rec.calls)
    assert "Added to the existing task" in rec.posts[-1][2]
    assert rec.marked == [("C0CAJ269RAQ", "1.0")]


def test_a_message_already_claimed_is_skipped(rec):
    rec.claimed = False
    assert wl.handle(_event("log this on clickup"))["reason"] == \
        "already logged"
    assert rec.calls == [] and rec.posts == [] and rec.marked == []


def test_logger_failure_still_logs_a_rough_task(rec):
    rec.card = None
    out = wl.handle(_event("log this on clickup"))
    assert out["type"] == "question"
    create = [c for c in rec.calls if c[1] == f"/list/{wl.LIST}/task"][0][2]
    assert create["name"].startswith("[Question] log this on clickup")


def test_unknown_type_becomes_question(rec):
    rec.card = {**CARD, "type": "complaint"}
    assert wl.handle(_event("log this on clickup"))["type"] == "question"


# ---------------------------------------------------------------------------
# Logged by the agents themselves after a failure (Sam, 2026-10-20)
# ---------------------------------------------------------------------------

def test_log_issue_files_a_tagged_task_and_marks_the_thread(rec, monkeypatch):
    monkeypatch.setattr(wl.store, "ensure_table", lambda t: False)
    url = wl.log_issue("bug reports", "Build failed: cancellation policy",
                       "The greenlit build failed.", "- Reason: x",
                       channel="C0CAJ269RAQ", thread_ts="1.0")
    assert url == "https://app.clickup.com/t/T1"
    create = [c for c in rec.calls if c[1] == f"/list/{wl.LIST}/task"][0][2]
    assert create["name"] == "[Bug] Build failed: cancellation policy"
    assert create["assignees"] == [wl.SAM_CLICKUP]
    assert "Logged automatically by the bug reports workflow" in \
        create["markdown_description"]
    tagged = [c[1] for c in rec.calls if "/tag/" in c[1]]
    assert tagged == ["/task/T1/tag/bug", "/task/T1/tag/auto%20logged"]
    assert rec.marked == [("C0CAJ269RAQ", "1.0")]
    assert rec.posts == []  # the caller says it in its own message


def test_a_second_failure_in_the_thread_comments_instead(rec):
    rec.existing = ("T0", "[Bug] Build failed")
    url = wl.log_issue("bug reports", "t", "again", "- Reason: y",
                       channel="C1", thread_ts="1.0")
    assert url == "https://app.clickup.com/t/T0"
    assert not any(c[1] == f"/list/{wl.LIST}/task" for c in rec.calls)
    assert any(c[1] == "/task/T0/comment" for c in rec.calls)


def test_close_thread_closes_only_the_agents_tasks(rec, monkeypatch):
    seen = []
    monkeypatch.setattr(wl.store, "ensure_table", lambda t: True)
    monkeypatch.setattr(wl.store, "query",
                        lambda sql, p, **k: seen.append(sql)
                        or [{"task_id": "T0"}])
    assert wl.close_thread("C1", "1.0", "Sam closed it.") == ["T0"]
    assert "logged_by = 'agent'" in seen[0]
    assert ("PUT", "/task/T0", {"status": wl.DONE}) in rec.calls
    assert ("POST", "/task/T0/comment",
            {"comment_text": "Sam closed it.", "notify_all": False}) \
        in rec.calls


def test_close_thread_never_raises(rec, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(wl.store, "ensure_table", boom)
    assert wl.close_thread("C1", "1.0", "note") == []


def test_a_failure_after_close_reopens_the_task(rec, monkeypatch):
    rec.existing = ("T0", "[Bug] Build failed")
    clickup = wl._clickup

    def closed_task(method, path, body=None):
        if method == "GET":
            rec.calls.append((method, path, body))
            return {"status": {"status": "complete", "type": "closed"}}
        return clickup(method, path, body)

    monkeypatch.setattr(wl, "_clickup", closed_task)
    wl.log_issue("bug reports", "t", "again", "- Reason: y",
                 channel="C1", thread_ts="1.0")
    assert ("PUT", "/task/T0", {"status": "to do"}) in rec.calls


def test_log_issue_never_raises(rec, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("clickup down")

    monkeypatch.setattr(wl, "_clickup", boom)
    assert wl.log_issue("bug reports", "t", "s", "d") == ""


def test_github_writes_count_the_alias_token(monkeypatch):
    """Every GitHub agent PR was refused before it was sent: the token lives
    under its alias and the guard ignored aliases (2026-10-20)."""
    from vomeos.integrations import code_write

    for name in list(__import__("os").environ):
        if name.startswith("VOMEOS_GITHUB_WRITE_TOKEN") or \
                name == "VOMEOS_GITHUB_OWNER":
            monkeypatch.delenv(name, raising=False)
    writer = code_write.GitHubWriter()
    alias = code_write.OWNER_TOKEN_ALIASES["VOMEADMIN"][0]
    monkeypatch.delenv(alias, raising=False)
    assert not writer.configured()
    monkeypatch.setenv(alias, "token")
    assert writer.configured()
    assert writer.configured(owner="vomeadmin")


def test_top_level_message_threads_its_reply_under_itself(rec):
    wl.handle(_event("log this on clickup: an agent for demo notes",
                     thread_ts=""))
    assert rec.posts[-1][1] == "2.0"


# ---------------------------------------------------------------------------
# "Agent error" (Sam, 2026-10-27): flagged from his phone in the thread it
# went wrong in, with his note and the whole thread on the task.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "agent error: it re-asked go 3 after I said go",
    "Agent error",
    "log this as an agent error",
    "log it as agent failure, it never built the PR",
])
def test_agent_error_triggers(text):
    assert wl.wants(_event(text))


@pytest.mark.parametrize("text", [
    "was that an agent error?",       # talking about it, not asking
    "agent error",                    # someone else
])
def test_agent_error_needs_sam_or_ron_and_the_phrase_first(text):
    user = OTHER if text == "agent error" else SAM
    assert not wl.wants(_event(text, user=user))


def test_an_agent_error_is_a_bug_with_the_note_and_the_thread(rec):
    rec.card = {**CARD, "type": "improvement"}
    note = "agent error: multi-line go was read as direction"
    out = wl.handle(_event(note))
    assert out["type"] == "bug"
    create = [c for c in rec.calls if c[1] == f"/list/{wl.LIST}/task"][0][2]
    assert create["name"].startswith("[Bug] ")
    assert f"**Sam's note:** {note}" in create["markdown_description"]
    tagged = [c[1] for c in rec.calls if "/tag/" in c[1]]
    assert "/task/T1/tag/agent%20error" in tagged
    thread = [c[2]["comment_text"] for c in rec.calls
              if c[1] == "/task/T1/comment"]
    assert thread == ["Slack thread when logged:\n\n"
                      "Ron: exemption bug\nagent: Greenlight?"]


def test_a_long_thread_is_split_and_keeps_the_newest(rec, monkeypatch):
    long = "x" * (wl.THREAD_MAX + 5000) + "LAST"
    monkeypatch.setattr(wl, "_read_thread", lambda *a: long)
    wl.handle(_event("agent error"))
    parts = [c[2]["comment_text"] for c in rec.calls
             if c[1] == "/task/T1/comment"]
    assert len(parts) == 3 and parts[0].startswith(
        "Slack thread when logged (1/3)")
    assert parts[-1].endswith("LAST")
