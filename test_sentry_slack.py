"""
test_sentry_slack.py

The three rules that stop this becoming the thing it was built to prevent:
one thread per issue, a severity bar for interrupting, and a hard daily cap.

Every test here is about NOT posting. Posting is easy; the engineering is in
the restraint, and a regression in any of these turns a useful channel into
one people mute, at which point the whole pipeline is worth nothing.
"""

import pytest

import sentry_ledger
import sentry_slack


@pytest.fixture
def wired(monkeypatch):
    sent, updates = [], []
    monkeypatch.setenv("SENTRY_SLACK_ENABLED", "true")
    monkeypatch.setenv("SAM_SLACK_USER_ID", "U01B9FQNSBU")
    monkeypatch.setattr(
        sentry_slack, "_post",
        lambda channel, text, thread_ts="": (
            sent.append({"channel": channel, "text": text,
                         "thread": thread_ts})
            or f"ts-{len(sent)}"
        ),
    )
    monkeypatch.setattr(sentry_slack, "posts_today", lambda: 0)
    monkeypatch.setattr(
        sentry_ledger, "update",
        lambda issue_id, **f: updates.append((issue_id, f)) or True,
    )
    return sent, updates


def _issue(**over):
    issue = {
        "issue_id": "900",
        "project": "prod-vome",
        "severity": "s1",
        "triage_summary": "Admins cannot accept an invite.",
        "culprit": "/api/organization/admin/invite-register/{token}/",
        "exception_type": "ValueError",
        "repo": "vomedjango-restored-core-app",
        "ref": "master",
        "stack": "backend",
        "permalink": "https://sentry.io/x/900/",
    }
    issue.update(over)
    return issue


# ---------------------------------------------------------------------------
# Posting is off until somebody turns it on
# ---------------------------------------------------------------------------

def test_posting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SENTRY_SLACK_ENABLED", raising=False)
    result = sentry_slack.report_issue(_issue())
    assert result["posted"] is False
    assert "disabled" in result["reason"]


# ---------------------------------------------------------------------------
# One issue, one thread, forever
# ---------------------------------------------------------------------------

def test_an_issue_with_a_thread_never_posts_again(wired):
    sent, _ = wired
    result = sentry_slack.report_issue(_issue(slack_ts="ts-earlier"))
    assert result["posted"] is False
    assert "already has a thread" in result["reason"]
    assert sent == []


def test_the_thread_id_is_stored(wired):
    # Everything later depends on this. Losing it turns the next update into
    # a second top-level alert about an issue somebody was already told about.
    sent, updates = wired
    result = sentry_slack.report_issue(_issue())
    assert result["posted"] is True
    issue_id, fields = updates[-1]
    assert issue_id == "900"
    assert fields["slack_ts"] == result["thread_ts"]
    assert fields["status"] == sentry_ledger.STATUS_REPORTED


def test_an_update_goes_in_the_thread(wired):
    sent, _ = wired
    assert sentry_slack.reply_in_thread(
        _issue(slack_ts="ts-1", slack_channel="C1"), "a patch was written"
    )
    assert sent[0]["thread"] == "ts-1"
    assert sent[0]["channel"] == "C1"


def test_an_update_with_no_thread_says_nothing(wired):
    # Deliberately does NOT fall back to a new message. An update with
    # nowhere to go is better lost than turned into a second alert.
    sent, _ = wired
    assert sentry_slack.reply_in_thread(_issue(), "an update") is False
    assert sent == []


# ---------------------------------------------------------------------------
# The severity bar
# ---------------------------------------------------------------------------

def test_only_s1_interrupts(wired):
    sent, _ = wired
    for severity in ("s2", "s3", "", "unrated"):
        result = sentry_slack.report_issue(_issue(severity=severity))
        assert result["posted"] is False, severity
        assert "digest" in result["reason"]
    assert sent == []


def test_s1_does_interrupt(wired):
    sent, _ = wired
    assert sentry_slack.report_issue(_issue(severity="s1"))["posted"] is True
    assert len(sent) == 1


def test_the_bar_is_configurable(monkeypatch, wired):
    monkeypatch.setattr(sentry_slack, "INTERRUPT_AT", ("s1", "s2"))
    assert sentry_slack.report_issue(_issue(severity="s2"))["posted"] is True


# ---------------------------------------------------------------------------
# The daily cap
# ---------------------------------------------------------------------------

def test_the_cap_holds_back_the_sixth(monkeypatch, wired):
    sent, _ = wired
    monkeypatch.setattr(sentry_slack, "posts_today", lambda: 5)
    result = sentry_slack.report_issue(_issue())
    assert result["posted"] is False
    assert "daily cap" in result["reason"]
    assert sent == []


def test_the_cap_applies_even_to_s1(monkeypatch, wired):
    # Six interruptions in a day is how a channel dies, and the sixth issue
    # is not less important than the fifth. Making this visible beats
    # burying it under its own output.
    sent, _ = wired
    monkeypatch.setattr(sentry_slack, "posts_today", lambda: 99)
    assert sentry_slack.report_issue(
        _issue(severity="s1")
    )["posted"] is False


# ---------------------------------------------------------------------------
# Who gets tagged
# ---------------------------------------------------------------------------

def test_a_backend_issue_tags_onlyg(wired):
    sent, _ = wired
    sentry_slack.report_issue(_issue())
    assert "<@U0APZ7JUHRD>" in sent[0]["text"]


def test_an_infrastructure_issue_tags_siraj_and_copies_sam(wired):
    sent, _ = wired
    sentry_slack.report_issue(
        _issue(), {"infrastructure": True, "risk": "high"}
    )
    text = sent[0]["text"]
    assert "<@U0ATX30F3TL>" in text          # Siraj
    assert "<@U0APZ7JUHRD>" in text          # OnlyG
    assert "cc <@U01B9FQNSBU>" in text       # Sam


def test_everything_goes_to_one_channel(wired):
    sent, _ = wired
    sentry_slack.report_issue(_issue())
    assert sent[0]["channel"] == "C0BR5RZ51B3"


# ---------------------------------------------------------------------------
# What the message says
# ---------------------------------------------------------------------------

def test_the_analysis_is_included_when_there_is_one(wired):
    sent, _ = wired
    sentry_slack.report_issue(_issue(), {
        "root_cause": "The invite is saved before the user it points at.",
        "proposed_fix": "Save outgoing_user before assigning it.",
        "risk": "low", "confidence": "high",
    })
    text = sent[0]["text"]
    assert "saved before the user" in text
    assert "risk low" in text


def test_a_migration_is_called_out(wired):
    # The reader has to know immediately that this one can never be
    # automated, whatever else the message says.
    sent, _ = wired
    sentry_slack.report_issue(_issue(), {
        "root_cause": "The column is too short.",
        "needs_migration": True, "risk": "high",
    })
    assert "migration" in sent[0]["text"]


def test_a_known_clickup_task_is_named(wired):
    sent, _ = wired
    sentry_slack.report_issue(_issue(), {"duplicate_of": "86abc1234"})
    assert "86abc1234" in sent[0]["text"]


def test_a_slack_failure_does_not_store_a_thread(monkeypatch, wired):
    # Storing a ts for a message that was never sent would silence every
    # later update on the issue.
    _, updates = wired
    monkeypatch.setattr(
        sentry_slack, "_post", lambda channel, text, thread_ts="": ""
    )
    result = sentry_slack.report_issue(_issue())
    assert result["posted"] is False
    assert updates == []
