"""
sentry_ladder.py

Where an analysed Sentry issue goes. The agent fixes it, Sam is asked, an
engineer gets it, or Sam is told. Nothing is parked.

WHY THIS EXISTS
---------------
Sam, 2026-10-20: the Agent Fixes list held 11 Sentry tasks at `queued`,
unassigned, out of the engineers' Priority Queue. Every one had a diagnosis
and no fix, because the fix bar was "low risk" and almost nothing is. Nobody
was going to pick them up. His rule: "Either it should be routed to an
engineer or fixed by the agent", the way #bug-reports already works.

THE LADDER (`decide`)
---------------------
    needs a migration, or infrastructure    -> ENGINEER
    confidence low (or unrated)             -> REPORT: Sam is told in
                                               #sentry-automations, no task
    a cause but no concrete fix or files    -> ENGINEER
    confidence medium/high, risk low/medium -> FIX: a PR into the dev branch,
                                               Agent Fixes task at in progress
    confidence medium/high, risk high       -> GREENLIGHT: Sam is asked in
                                               the thread, nothing is built

A FIX that does not work out (the fix author declines, the patch guard
refuses it, it does not apply) or that would pass the daily PR cap becomes a
GREENLIGHT with the reason. A greenlit build that does not work out goes to
an engineer: Sam already said it should be fixed.

One task per root cause still holds: an issue whose cause already has a task
(`sentry_agent_fixes.existing_task`) is a comment on that task, nothing else.
Past SENTRY_AGENT_FIXES_DAILY_CAP new tasks in a day, or with filing switched
off, an issue is a REPORT, so Sam still hears about it and can `route` it.

SAM'S REPLIES
-------------
In the issue's thread in #sentry-automations, from Sam only:

    yes / fix / build it      build it (risk bar lifted), PR into dev
    route [backend|frontend]  Priority Queue task for OnlyG or Sanjay
    close / no                close it, nothing more happens

THE PARKED BACKLOG
------------------
`sweep_parked` runs after the 08:15 backfill. Any `[Sentry]` task still at
`queued` with no lane (filed before this module) is analysed again against
today's code and put through the ladder; the old task is closed with a
pointer to where the work went. Capped per run, so the first day is bounded.

SWITCHES
--------
    SENTRY_FIX_DAILY_CAP     agent PRs per day, default 3.
    SENTRY_SWEEP_PER_RUN     parked tasks re-run per morning, default 12.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx

import sentry_agent_fixes
import sentry_ledger
import sentry_notify
import sentry_slack
from vomeos import store

HANDLER_KEY = "engineering.sentry_reply"
PARKED_KEY = "engineering.sentry_parked"
QUEUE = "engineering"
# Replies are read only where they were asked for. Unset means none are:
# sentry_notify.channel() falls back to channels other handlers own.
CHANNEL = os.environ.get("SENTRY_AUTOMATIONS", "")

FIX, GREENLIGHT, ENGINEER, REPORT = "fix", "greenlight", "engineer", "report"
GROUPED, CLOSED = "grouped", "closed"

SAM_SLACK = "U01B9FQNSBU"
SAM_CLICKUP = sentry_agent_fixes.CLICKUP_USER_SAM
PRIORITY_QUEUE = "901113386257"
CLICKUP_BASE = sentry_agent_fixes.CLICKUP_BASE

# The fix bar Sam set on 2026-10-20. A person still reviews and merges every
# PR, which is what makes medium risk acceptable here.
FIX_RISK = ("low", "medium")
FIX_CONFIDENCE = ("medium", "high")

_ROUTE = re.compile(r"^route\b\s*(backend|frontend)?", re.I)
_CLOSE = re.compile(r"^(close|no|nope|ignore)\b", re.I)


def fix_daily_cap() -> int:
    return int(os.environ.get("SENTRY_FIX_DAILY_CAP", "3"))


def sweep_per_run() -> int:
    return int(os.environ.get("SENTRY_SWEEP_PER_RUN", "12"))


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def decide(issue: dict, analysis: dict) -> tuple[str, str]:
    """(lane, why). Deterministic: the analyst rates, this decides."""
    if analysis.get("needs_migration"):
        return ENGINEER, ("it needs a migration, and migrations are "
                          "generated on deploy, never written by an agent")
    if issue.get("infrastructure"):
        return ENGINEER, ("it is hosting or settings work, which agents "
                          "never change")
    confidence = str(analysis.get("confidence") or "").lower()
    if confidence not in FIX_CONFIDENCE:
        return REPORT, "the analyst's confidence in the cause is low"
    if not (analysis.get("proposed_fix") and analysis.get("files")):
        return ENGINEER, ("the cause is clear but no concrete fix was "
                          "found in the code")
    risk = str(analysis.get("risk") or "").lower()
    if risk in FIX_RISK:
        return FIX, f"{risk} risk, {confidence} confidence"
    return GREENLIGHT, f"the analyst rates the fix {risk or 'unrated'} risk"


# ---------------------------------------------------------------------------
# Entry point from sentry_handler
# ---------------------------------------------------------------------------

def place(issue: dict, analysis: dict, *, source: str = "",
          parked_task: str = "") -> dict:
    """Put one analysed issue in its lane. Never raises.

    `issue` carries repo, ref, host and owner as sentry_handler builds it,
    plus slack_ts/slack_channel when the issue already has a thread.
    `parked_task` is an old queued task being re-run by `sweep_parked`.
    """
    issue_id = str(issue.get("issue_id") or "")
    sentry_ledger.update(issue_id, analysis=analysis)
    try:
        if not parked_task:
            existing = sentry_agent_fixes.existing_task(issue, analysis)
            if existing:
                sentry_agent_fixes.comment(existing, (
                    f"Another Sentry issue with the same cause: "
                    f"{issue.get('title')}\n"
                    f"{issue.get('permalink') or issue_id}"))
                sentry_ledger.update(issue_id, clickup_task_id=existing,
                                     lane=GROUPED)
                return {"lane": GROUPED, "task_id": existing}

        lane, why = decide(issue, analysis)
        if lane != REPORT and not sentry_agent_fixes.enabled():
            lane, why = REPORT, "filing is switched off"
        elif (lane != REPORT and not parked_task
              and sentry_agent_fixes.filed_today()
              >= sentry_agent_fixes.daily_cap()):
            lane, why = REPORT, (f"today's cap of "
                                 f"{sentry_agent_fixes.daily_cap()} new "
                                 "Sentry tasks is reached")
        if lane == FIX:
            out = _build(issue, analysis, source, greenlit=False, why=why)
        elif lane == GREENLIGHT:
            out = _ask(issue, analysis, why)
        elif lane == ENGINEER:
            out = _route(issue, analysis, why)
        else:
            out = _report(issue, analysis, why)
        if parked_task:
            _retire_parked(parked_task, issue_id, out)
        print(f"[SENTRY] ladder {issue_id}: {out.get('lane')} "
              f"{out.get('task_id') or out.get('pr') or ''} ({why})")
        return out
    except Exception as exc:
        print(f"[SENTRY] ladder failed for {issue_id}: {exc}")
        return {"lane": "", "error": str(exc)}


# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------

def _build(issue: dict, analysis: dict, source: str, *, greenlit: bool,
           why: str = "") -> dict:
    """A PR into the dev branch, then the Agent Fixes task that tracks it."""
    import sentry_fix

    issue_id = str(issue.get("issue_id") or "")
    if not greenlit and _fixes_today() >= fix_daily_cap():
        return _ask(issue, analysis,
                    f"today's {fix_daily_cap()} agent PRs are already open")

    context = {"source": source, "repo": issue.get("repo", ""),
               "branch": issue.get("ref", ""), "issue_id": issue_id}
    gate = dict(analysis)
    if greenlit:
        # Sam's yes is the bar the risk gate stands for (as in #bug-reports).
        gate["risk"] = "low"
        context["greenlight"] = (
            "Sam read the problem, the fix and the risk in "
            "#sentry-automations and said yes. Risk as the analyst saw it: "
            f"{analysis.get('risk') or 'unrated'}. He reviews the pull "
            "request before it merges.")
    proposal = sentry_fix.propose(gate, context, allow_risk=FIX_RISK)
    pull_request = {"opened": False, "reason": proposal.get("reason", "")}
    if proposal.get("diff"):
        pull_request = sentry_fix.open_pull_request(issue, proposal, {
            "repo": issue.get("repo", ""), "branch": issue.get("ref", ""),
            "host": issue.get("host", "bitbucket"),
            "owner": issue.get("owner", ""),
            "root_cause": analysis.get("root_cause", ""),
        })
    sentry_ledger.update(
        issue_id,
        fix_diff=(proposal.get("diff") or "")[:12000] or None,
        fix_reason=(proposal.get("reason") or "")[:500] or None,
    )

    if not pull_request.get("opened"):
        failed = (pull_request.get("reason") or proposal.get("reason")
                  or "no fix came out of it")
        if greenlit:
            return _route(issue, analysis,
                          f"the agent could not build the fix ({failed})")
        return _ask(issue, analysis, f"I tried and could not build it "
                                     f"({failed})")

    old_task = _row_task(issue_id)
    task_id = sentry_agent_fixes.create_task(
        issue, analysis, pull_request, proposal,
        note=("**Agent fix from the Sentry pipeline.** Not reviewed by a "
              "person. Merge into the dev branch only after review."))
    if greenlit and old_task and task_id and old_task != task_id:
        # The decision task is answered; the fix task carries the work.
        _close_task(old_task,
                    f"Sam said yes. Agent fix: {_task_url(task_id)}")
        _repoint(old_task, task_id)
    sentry_ledger.update(issue_id, lane=FIX)
    _say(issue, "\n".join(p for p in (
        f":hammer_and_wrench: *Fixed by the agent* ({why}). PR into the dev "
        f"branch: {pull_request['url']}",
        f"Tracked on <{_task_url(task_id)}|ClickUp>. When it merges, the "
        "release watcher follows it to production." if task_id else "",
    ) if p))
    return {"lane": FIX, "pr": pull_request["url"], "task_id": task_id}


def _ask(issue: dict, analysis: dict, why: str) -> dict:
    """Sam decides. One Agent Fixes task assigned to him, one thread post."""
    issue_id = str(issue.get("issue_id") or "")
    _say(issue, "\n".join([
        f"<@{SAM_SLACK}> *Build this?* I won't without your yes ({why}).",
        f"*Problem* {issue.get('triage_summary') or issue.get('title')}",
        f"*Cause* {analysis.get('root_cause') or '?'}",
        f"*Fix* {analysis.get('proposed_fix') or '?'}",
        f"_risk {analysis.get('risk') or '?'}, confidence "
        f"{analysis.get('confidence') or '?'}_",
        "Reply `yes` to build it, `route` to send it to an engineer, or "
        "`close`.",
    ]))
    note = ("**Waiting on Sam's decision** in #sentry-automations "
            f"({_thread_link(issue)}): reply `yes`, `route` or `close`.")
    task_id = _row_task(issue_id)
    if task_id:
        # A parked task becomes his: assigned, and told why.
        _clickup_put(task_id, {"assignees": {"add": [SAM_CLICKUP]}})
        sentry_agent_fixes.comment(task_id, f"{note} Why: {why}.")
    else:
        task_id = sentry_agent_fixes.create_task(
            issue, analysis, {"opened": False}, {"reason": why},
            assignees=[SAM_CLICKUP], note=note)
    sentry_ledger.update(issue_id, lane=GREENLIGHT)
    return {"lane": GREENLIGHT, "task_id": task_id, "why": why}


def _route(issue: dict, analysis: dict, why: str, team: str = "") -> dict:
    """A Priority Queue task for the engineer, a post in their channel."""
    import sprint_thread

    issue_id = str(issue.get("issue_id") or "")
    team = team or ("frontend" if issue.get("host") == "github"
                    else "backend")
    key = "sanjay" if team == "frontend" else "onlyg"
    clickup_id, slack_id, label, channel = sprint_thread.ENGINEERS[key]
    name = {"onlyg": "OnlyG", "sanjay": "Sanjay"}[key]

    old_task = _row_task(issue_id)
    task_id = sentry_agent_fixes.create_task(
        issue, analysis, {"opened": False}, {"reason": why},
        list_id=PRIORITY_QUEUE, assignees=[clickup_id],
        note=(f"**Routed to {name} by the Sentry pipeline** because {why}. "
              f"Thread: {_thread_link(issue)}"))
    if old_task and task_id and old_task != task_id:
        _close_task(old_task, f"Routed to {name}: {_task_url(task_id)}")
        _repoint(old_task, task_id)

    headline = issue.get("triage_summary") or issue.get("title") or ""
    sentry_slack._post(channel, "\n".join(p for p in (
        f"<@{slack_id}> New {label} bug from Sentry: *{headline}*",
        f"Cause, fix and evidence are on ClickUp: {_task_url(task_id)}"
        if task_id else str(analysis.get("root_cause") or ""),
        issue.get("permalink") or "",
    ) if p))
    sentry_ledger.update(issue_id, lane=ENGINEER)
    _say(issue, (f":white_check_mark: Sent to {name} ({why}). "
                 + (f"<{_task_url(task_id)}|ClickUp>" if task_id else
                    f"<@{SAM_SLACK}> the ClickUp task could not be saved.")))
    return {"lane": ENGINEER, "task_id": task_id, "engineer": key}


def _report(issue: dict, analysis: dict, why: str) -> dict:
    """For Sam's awareness only. No task: nobody would work it."""
    issue_id = str(issue.get("issue_id") or "")
    _say(issue, "\n".join([
        f":eyes: *For your awareness, not fixing* ({why}).",
        f"*Problem* {issue.get('triage_summary') or issue.get('title')}",
        f"*Best guess* {analysis.get('root_cause') or '?'}",
        "Reply `route` to send it to an engineer, `yes` to have me try a "
        "fix anyway, or `close`.",
    ]))
    sentry_ledger.update(issue_id, lane=REPORT)
    return {"lane": REPORT, "why": why}


# ---------------------------------------------------------------------------
# Sam's replies
# ---------------------------------------------------------------------------

def handle_message(event: dict) -> None:
    """Web side: one message in #sentry-automations. Only Sam's thread
    replies matter; everything else (the bot's own posts, top-level
    messages) is ignored. Cheap, never raises."""
    ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or "")
    if event.get("user") != SAM_SLACK or not thread_ts or thread_ts == ts:
        return
    try:
        from vomeos.worker import enqueue_event

        enqueue_event(HANDLER_KEY, {
            "thread_ts": thread_ts, "ts": ts,
            "text": str(event.get("text") or "").strip(),
        }, queue=QUEUE)
    except Exception as exc:
        print(f"[SENTRY] could not queue reply {ts}: {exc}")


def handle_reply(message: dict) -> dict:
    """Worker side. Matches the thread to its ledger row and acts."""
    import bug_reports

    thread_ts = str((message or {}).get("thread_ts") or "")
    text = str((message or {}).get("text") or "").strip()
    rows = store.query(
        f"SELECT * FROM {sentry_ledger.TABLE} WHERE slack_ts = :ts "
        "ORDER BY last_seen_at DESC LIMIT 1", {"ts": thread_ts})
    if not rows:
        return {"status": "ignored", "reason": "not an issue thread"}
    row = rows[0]
    issue = _issue_from_row(row)
    analysis = row.get("analysis") or {}
    if isinstance(analysis, str):
        import json

        analysis = json.loads(analysis or "{}")
    lane = row.get("lane") or ""
    plain = re.sub(r"<[^>]*>", " ", text).strip()

    route = _ROUTE.match(plain)
    if route:
        if lane == ENGINEER:
            return {"status": "ignored", "reason": "already routed"}
        return _route(issue, analysis or _bare(row), "Sam routed it",
                      team=(route.group(1) or "").lower())
    if _CLOSE.match(plain):
        if row.get("clickup_task_id") and lane in (GREENLIGHT, FIX):
            _close_task(row["clickup_task_id"], "Closed by Sam in "
                                                "#sentry-automations.")
        sentry_ledger.update(issue["issue_id"], lane=CLOSED)
        _say(issue, ":white_check_mark: Closed. Nothing more happens on "
                    "this one.")
        return {"status": "ok", "lane": CLOSED}
    if plain.lower() == "fix" or bug_reports._is_yes(plain):
        if lane == FIX:
            return {"status": "ignored", "reason": "already has a PR"}
        if not analysis:
            return _route(issue, _bare(row), "Sam said yes, but the "
                          "analysis was not stored to build from")
        _say(issue, "On it. Building the fix now.")
        return _build(issue, analysis, _source(issue), greenlit=True,
                      why="Sam said yes")
    _say(issue, "I act on `yes`, `route` (or `route frontend`), or `close` "
                "here.")
    return {"status": "ok", "action": "hint"}


# ---------------------------------------------------------------------------
# The parked backlog
# ---------------------------------------------------------------------------

def sweep_parked(limit: int = 0) -> dict:
    """Queue queued `[Sentry]` tasks that predate the ladder, one job each
    (`place_parked`), so one slow fix cannot time the sweep out. Never
    raises."""
    from vomeos.worker import enqueue_event

    limit = limit or sweep_per_run()
    try:
        tasks = _parked_tasks()
    except Exception as exc:
        print(f"[SENTRY] sweep could not list Agent Fixes: {exc}")
        return {"status": "error", "error": str(exc)}
    queued, skipped = [], []
    for task in tasks:
        if len(queued) >= limit:
            break
        if not _parked_row(task["id"]):
            skipped.append(task["id"])
            continue
        enqueue_event(PARKED_KEY, {"task_id": task["id"]}, queue=QUEUE)
        queued.append(task["id"])
    if queued:
        print("[SENTRY] parked sweep queued: " + ", ".join(queued))
    return {"status": "ok", "queued": queued, "skipped": skipped,
            "remaining": max(len(tasks) - len(queued) - len(skipped), 0)}


def place_parked(message: dict) -> dict:
    """One parked task. Analyses again rather than trusting the stored
    diagnosis: some are days old, the code may have moved, and the old rows
    never kept the analyst's risk and confidence."""
    import sentry_handler

    task_id = str((message or {}).get("task_id") or "")
    row = _parked_row(task_id)
    if not row:
        return {"status": "ignored", "reason": "already placed"}
    return sentry_handler.reinvestigate(row, parked_task=task_id)


def _parked_row(task_id: str) -> dict | None:
    """The busiest issue filed on the task, while it has no lane yet."""
    rows = store.query(
        f"SELECT * FROM {sentry_ledger.TABLE} WHERE clickup_task_id = "
        ":task ORDER BY times_seen DESC NULLS LAST LIMIT 1",
        {"task": task_id})
    return rows[0] if rows and not rows[0].get("lane") else None


def _parked_tasks() -> list[dict]:
    resp = httpx.get(
        f"{CLICKUP_BASE}/list/{sentry_agent_fixes.AGENT_FIXES_LIST}/task",
        params={"statuses[]": "queued", "include_closed": "false"},
        headers={"Authorization": sentry_agent_fixes._token()},
        timeout=30,
    )
    resp.raise_for_status()
    return [t for t in resp.json().get("tasks") or []
            if str(t.get("name") or "").startswith("[Sentry]")
            and not t.get("assignees")]


def _retire_parked(old_task: str, issue_id: str, out: dict) -> None:
    """The old queued task closes and points at where the work went. A
    greenlight that reused it (same task id) stays open: it is now Sam's."""
    new_task = out.get("task_id") or ""
    if new_task == old_task or out.get("lane") == ENGINEER or not out.get(
            "lane"):
        # Reused as Sam's, already closed by _route, or the ladder failed
        # (left as it is for the next sweep).
        return
    where = {
        FIX: f"Agent fix opened: {out.get('pr')}",
        ENGINEER: f"Routed to an engineer: {_task_url(new_task)}",
        REPORT: "Reported to Sam in #sentry-automations (low confidence, "
                "nobody works it unless he routes it).",
        GREENLIGHT: f"Waiting on Sam: {_task_url(new_task)}",
    }.get(out.get("lane") or "", "Re-run by the Sentry ladder.")
    _close_task(old_task, where)
    if new_task:
        _repoint(old_task, new_task)
    elif out.get("lane") == REPORT:
        store.execute(
            f"UPDATE {sentry_ledger.TABLE} SET clickup_task_id = NULL "
            "WHERE clickup_task_id = :old", {"old": old_task})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _say(issue: dict, text: str) -> None:
    """Into the issue's thread, starting one if it has none, so every
    decision Sam can answer has a thread to answer in."""
    if issue.get("slack_ts"):
        sentry_slack.reply_in_thread(issue, text)
        return
    if not sentry_slack._enabled():
        return
    channel = issue.get("slack_channel") or sentry_notify.channel()
    severity = (issue.get("severity") or "s?").upper()
    head = (f"*{severity}* {issue.get('triage_summary') or issue.get('title')}"
            + (f"\n{issue['permalink']}" if issue.get("permalink") else ""))
    ts = sentry_slack._post(channel, head)
    if not ts:
        return
    issue.update(slack_ts=ts, slack_channel=channel)
    sentry_ledger.update(str(issue.get("issue_id") or ""), slack_ts=ts,
                         slack_channel=channel)
    sentry_slack._post(channel, text, thread_ts=ts)


def _fixes_today() -> int:
    rows = store.query(
        f"SELECT COUNT(*) AS n FROM {sentry_ledger.TABLE} WHERE lane = :fix "
        "AND filed_at >= :start",
        {"fix": FIX, "start": datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None)})
    return int(rows[0]["n"]) if rows else 0


def _row_task(issue_id: str) -> str:
    row = sentry_ledger.get(issue_id) or {}
    return str(row.get("clickup_task_id") or "")


def _repoint(old_task: str, new_task: str) -> None:
    """Later issues with the same cause comment on the live task."""
    store.execute(
        f"UPDATE {sentry_ledger.TABLE} SET clickup_task_id = :new "
        "WHERE clickup_task_id = :old", {"new": new_task, "old": old_task})


def _close_task(task_id: str, note: str) -> None:
    sentry_agent_fixes.comment(task_id, note)
    # `Closed`, never `on prod`: that status emails clients.
    _clickup_put(task_id, {"status": "Closed"})


def _clickup_put(task_id: str, body: dict) -> bool:
    try:
        resp = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            headers={"Authorization": sentry_agent_fixes._token(),
                     "Content-Type": "application/json"},
            json=body, timeout=30)
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[SENTRY] could not update {task_id}: {exc}")
        return False


def _task_url(task_id: str) -> str:
    return f"https://app.clickup.com/t/{task_id}" if task_id else ""


def _thread_link(issue: dict) -> str:
    ts = str(issue.get("slack_ts") or "")
    channel = issue.get("slack_channel") or sentry_notify.channel()
    if not ts:
        return "#sentry-automations"
    return (f"https://vomevolunteer.slack.com/archives/{channel}/"
            f"p{ts.replace('.', '')}")


def _issue_from_row(row: dict) -> dict:
    import sentry_projects

    project = sentry_projects.route(row.get("project") or "")
    return {
        "issue_id": str(row.get("issue_id") or ""),
        "project": row.get("project") or "",
        "title": row.get("title") or "",
        "culprit": row.get("culprit") or "",
        "exception_type": row.get("exception_type") or "",
        "permalink": row.get("permalink") or "",
        "severity": row.get("severity") or "",
        "triage_summary": row.get("triage_summary") or "",
        "infrastructure": bool(row.get("infrastructure")),
        "repo": row.get("repo") or (project.repo if project else ""),
        "ref": project.ref if project else "",
        "host": project.host if project else "bitbucket",
        "owner": project.owner if project else "",
        "stack": row.get("stack") or "",
        "slack_ts": row.get("slack_ts") or "",
        "slack_channel": row.get("slack_channel") or "",
    }


def _bare(row: dict) -> dict:
    """What an engineer task can say when no analysis was stored."""
    return {"root_cause": row.get("root_cause") or "",
            "proposed_fix": row.get("proposed_fix") or ""}


def _source(issue: dict) -> str:
    """The code a greenlit build reads, gathered fresh from the trace."""
    import sentry_code
    import sentry_event

    trace = sentry_event.fetch_trace(issue["issue_id"])
    code = sentry_code.gather(
        trace, issue.get("repo", ""), issue.get("ref", ""),
        host=issue.get("host", "bitbucket"), owner=issue.get("owner", ""),
        message=issue.get("title", ""))
    return code.get("files", "")
