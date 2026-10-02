"""
sentry_slack.py

What a person actually sees. One thread per issue, a hard daily budget, and
nobody tagged unless it is worth interrupting them.

THE THREE RULES, AND WHY THEY ARE THE WHOLE DESIGN
--------------------------------------------------
**One issue, one thread, forever.** The first report is a top-level message
and its `ts` goes in the ledger. Every later update on that issue (it
regressed, a diff was proposed, it got worse) is a reply inside that thread.
A channel with forty issues in it has forty messages, not four hundred. This
is the single biggest anti-bombardment measure and it depends entirely on
`slack_ts` being stored, so a failure to store it is treated as serious.

**A severity bar for interrupting anyone.** Only `s1` posts immediately and
only `s1` mentions a person. Everything else accumulates and leaves in the
09:00 digest. An alert that arrives at 03:00 for a form validation bug trains
people to mute the channel, and a muted channel costs more than the bug.

**A hard daily cap on top-level posts.** Default five. Number six onward
rolls into the digest whatever its severity. If the cap is hit regularly
something upstream is broken, and the cap makes that visible instead of
burying it under its own output.

ROUTING IS BY MENTION, NOT BY CHANNEL
-------------------------------------
Everything goes to #eng-all. Splitting into #eng-backend and #eng-frontend
sounds tidier and produces two channels too quiet to open, plus an argument
about where a full-stack issue belongs. `sentry_notify` decides who gets
tagged. See its module docstring.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import sentry_ledger
import sentry_notify

# Top-level posts per day. The sixth issue is not less important than the
# fifth; it is just that six interruptions in a day is how a channel dies.
MAX_POSTS_PER_DAY = int(
    os.environ.get("SENTRY_SLACK_MAX_POSTS_PER_DAY", "5")
)

# Severities that may interrupt. Everything else waits for the digest.
INTERRUPT_AT = tuple(
    s.strip() for s in
    os.environ.get("SENTRY_SLACK_INTERRUPT_AT", "s1").split(",")
    if s.strip()
)


def _enabled() -> bool:
    """Posting switch, separate from the pipeline switch.

    Defaults OFF. Phase 2 ran for a week recording verdicts and saying
    nothing, and the step from "recording" to "interrupting people" should be
    a deliberate act rather than a side effect of a deploy.
    """
    return os.environ.get("SENTRY_SLACK_ENABLED", "false").lower() == "true"


def _post(channel: str, text: str, thread_ts: str = "") -> str:
    """Send one message. Returns its `ts`, or "" on failure.

    Never raises. Slack being down must not lose an analysis that has already
    been paid for; the ledger keeps the verdict either way.
    """
    try:
        from slack import client

        kwargs = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = client.chat_postMessage(**kwargs)
        return str(response.get("ts") or "")
    except Exception as exc:
        print(f"[SENTRY-SLACK] could not post: {exc}")
        return ""


def posts_today() -> int:
    """Top-level posts already made today, from the ledger.

    Counted in Postgres rather than in memory because the web process, the
    worker and the digest job are three processes, and a budget that resets
    on deploy is not a budget.
    """
    rows = sentry_ledger.recent(limit=500)
    today = datetime.now(timezone.utc).date()
    count = 0
    for row in rows:
        if not row.get("slack_ts"):
            continue
        stamp = row.get("last_seen_at")
        try:
            if stamp and stamp.date() == today:
                count += 1
        except AttributeError:
            continue
    return count


def _format_issue(issue: dict, analysis: dict | None) -> str:
    """The message body. Written for someone skimming forty of these."""
    severity = (issue.get("severity") or "s?").upper()
    summary = issue.get("triage_summary") or issue.get("title") or ""
    lines = [f"*{severity}* {summary}"]

    if issue.get("repo"):
        where = issue["repo"]
        if issue.get("ref"):
            where += f"@{issue['ref']}"
        lines.append(f"`{where}`  {issue.get('culprit') or ''}".rstrip())

    if analysis:
        root = analysis.get("root_cause") or ""
        fix = analysis.get("proposed_fix") or ""
        risk = analysis.get("risk") or "?"
        confidence = analysis.get("confidence") or "?"
        if root:
            lines += ["", f"*Cause* {root}"]
        if fix:
            lines.append(f"*Fix* {fix}")
        lines.append(f"_risk {risk}, confidence {confidence}_")
        if analysis.get("needs_migration"):
            lines.append(
                "_Needs a migration, so this cannot be automated: "
                "migrations are generated on deploy here._"
            )
        if analysis.get("duplicate_of"):
            lines.append(
                f"_Possibly already covered by ClickUp task "
                f"{analysis['duplicate_of']}._"
            )

    if issue.get("permalink"):
        lines += ["", issue["permalink"]]
    return "\n".join(lines)


def report_issue(issue: dict, analysis: dict | None = None) -> dict:
    """Post one issue, or decide not to.

    Returns what happened and why, so the digest can say "three issues were
    held back by the daily cap" rather than quietly dropping them.
    """
    issue_id = str(issue.get("issue_id") or "")
    severity = (issue.get("severity") or "").lower()

    if not _enabled():
        return {"posted": False, "reason": "slack posting disabled"}

    # Already has a thread. Anything further is a reply, never a new message.
    existing = issue.get("slack_ts")
    if existing:
        return {"posted": False, "reason": "already has a thread",
                "thread_ts": existing}

    if severity not in INTERRUPT_AT:
        return {"posted": False, "reason": f"{severity or 'unrated'} waits "
                                           f"for the digest"}

    used = posts_today()
    if used >= MAX_POSTS_PER_DAY:
        return {"posted": False,
                "reason": f"daily cap reached ({used}/{MAX_POSTS_PER_DAY})"}

    recipients = sentry_notify.route(
        issue.get("project", ""),
        stack=issue.get("stack", ""),
        exception_type=issue.get("exception_type", ""),
        culprit=issue.get("culprit", ""),
        infra=(
            analysis.get("infrastructure") if analysis
            else issue.get("infrastructure")
        ),
    )
    text = f"{recipients.mentions()} {_format_issue(issue, analysis)}"
    ts = _post(recipients.channel, text)
    if not ts:
        return {"posted": False, "reason": "slack rejected the message"}

    # Storing the ts is what makes every later update a reply. Losing it
    # means the next update becomes a second top-level message, which is the
    # failure this whole module exists to prevent.
    stored = sentry_ledger.update(
        issue_id,
        status=sentry_ledger.STATUS_REPORTED,
        slack_channel=recipients.channel,
        slack_ts=ts,
    )
    if not stored:
        print(
            f"[SENTRY-SLACK] WARNING posted {issue_id} but could not store "
            "its thread id. Further updates will start a new thread."
        )
    return {"posted": True, "thread_ts": ts, "channel": recipients.channel,
            "tagged": list(recipients.owners), "reason": recipients.reason}


def reply_in_thread(issue: dict, text: str) -> bool:
    """Add to an issue's existing thread. Does nothing without one.

    Deliberately does not fall back to a new top-level message. An update
    with nowhere to go is better lost than turned into a second alert about
    an issue somebody has already been told about.
    """
    if not _enabled():
        return False
    thread_ts = issue.get("slack_ts")
    channel = issue.get("slack_channel") or sentry_notify.channel()
    if not thread_ts:
        return False
    return bool(_post(channel, text, thread_ts=thread_ts))


def describe() -> dict:
    return {
        "enabled": _enabled(),
        "channel": sentry_notify.channel(),
        "max_posts_per_day": MAX_POSTS_PER_DAY,
        "posts_today": posts_today() if _enabled() else 0,
        "interrupt_at": list(INTERRUPT_AT),
    }
