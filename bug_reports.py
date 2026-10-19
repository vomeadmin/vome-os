"""
bug_reports.py

#bug-reports, end to end. A teammate posts a bug (usually Ron, writing with
Claude from his frontend work); the agent checks it against the code and
answers in the thread: fixed with a pull request, sent to an engineer, a
question for Sam, a question for the reporter, or no bug after all.

WHY THIS EXISTS
---------------
Sam, 2026-10-19: Ron's reports are exhaustive, but each one still waited for
Sam to read it, check the code and decide who takes it. The sprint machinery
already does that for customer tickets (code check, fix ladder, engineer
routing), so this points it at the channel.

FLOW
----
    top-level message (web)  ->  "Looking into this." in the thread
                             ->  enqueue engineering.bug_report  ->  worker:
      1. the report, its thread, and the ClickUp task the reporter linked
      2. engineering.bug_report_analyst reads the code (production, the dev
         branches, and RON_DEVELOP) and returns a verdict
      3. a bug with a fix (agent_fix, greenlight, or engineer with one):
           low risk, under the daily cap -> PR into the dev branch on its
                                            own, Agent Fixes task
           otherwise, or the small PR did not work out -> GREENLIGHT: Sam is
                        @-mentioned with the problem, the fix and its repos,
                        and the risk in plain words. Nothing routes.
         engineer    -> only with no workable fix (none described, no exact
                        location) or a hard limit (migration, infrastructure
                        or settings, data). The reporter's task (or a new
                        Priority Queue task) to OnlyG / Sanjay, a post in
                        their channel.
         decision    -> Sam is asked in the thread
         needs_info  -> the reporter is asked in the thread
         not_a_bug, already_fixed, reporter_fix -> see below

GREENLIGHT (Sam, 2026-10-19)
----------------------------
The guest shift report had a confirmed bug and a concrete fix, and went
straight to OnlyG and Sanjay because the fix was high risk. Sam wants to say
yes once instead. His yes ("yes", "go", "do it", "execute", "ship it",
`fix`; only his, only whole answers) lifts the risk bar and widens the
work to every file the fix names, one PR per repo, each repo's PR its own
child task under one Agent Fixes parent. It never lifts the hard limits or
the patch safety guard (3 files, 60 lines per repo). A part that cannot be
built is named, with `route` offered for it. Any other reply from him is a
conversation: the analysis runs again with his note and asks again. Once he
has said yes, nothing routes without his word (status GREENLIT).

A fix spanning repos lists its PRs as "Bug report PR:" lines, which
ticket_fix.promote_merged does not read; `run_rollup` (every 15 minutes)
writes every commit into Fix Commit once all of them merge, so the release
watcher waits for all of them.

Replies in the thread: from Sam, `fix`, `route backend|frontend|both` and
`close` act anywhere; any other reply to a report waiting on an answer runs
the analysis again with the thread.

THE REPORTER'S OWN TASK
-----------------------
Ron files a ClickUp task for every report and links it (Sam chose to adopt
it rather than file a second one). ClickUp is the record and Slack is the
conversation (Sam, 2026-10-19): the analysis goes on the task as a comment,
and Slack gets a check mark, one line and the link.

    engineer       his task, kept in Priority Queue: the engineer assigned,
                   Sam removed, an open status (queued), Source = Slack,
                   Type Bug if he set none. Outside Priority Queue, or no
                   task linked: a new Priority Queue task, linked to his.
    already_fixed  Closed, Resolution Completed  } only when the code check
    not_a_bug      declined, Resolution Declined } is high confidence
    reporter_fix   assigned back to the reporter, Sam removed

SWITCHES
--------
    BUG_REPORTS_ENABLED          default on. "false" ignores the channel.
    BUG_REPORT_FIX_DAILY_CAP     pull requests per day, default 3.
    SLACK_CHANNEL_BUG_REPORTS    default C0CAJ269RAQ.

Backfill (Sam asked for a dry run of the first reports before anything
posts): `py bug_reports.py --backfill --dry-run`, then `--backfill`.
"""

from __future__ import annotations

import json
import os
import re

import httpx

from vomeos import store

CHANNEL = os.environ.get("SLACK_CHANNEL_BUG_REPORTS", "C0CAJ269RAQ")
HANDLER_KEY = "engineering.bug_report"
QUEUE = "engineering"
AGENT = "engineering.bug_report_analyst"

SAM_SLACK = "U01B9FQNSBU"
SAM_CLICKUP = 3691763
PRIORITY_QUEUE = "901113386257"
FIELD_SOURCE = "857e1262-cb5c-4c22-b8da-770a8fcfa82e"
SOURCE_SLACK = "e62588d7-4b72-45a8-a9ab-a2c20a860903"
FIELD_TYPE = "e0e439f5-397d-432d-addd-e90fbf50cd30"
TYPE_BUG = "d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df"
FIELD_RESOLUTION = "63ef3458-cfa6-4a0b-ae44-18858cd555f0"
RESOLUTION_COMPLETED = "c51c9782-0bca-4c40-a4fe-a272427dc347"
RESOLUTION_DECLINED = "8600a963-ab55-430f-a86f-2b1d0f911156"

# Statuses an engineer is already working from. Anything else (a done
# status on a reopened report) goes back to `queued` when it is routed.
# Never `on prod`: that status emails clients through the on-prod handler.
ENGINEER_OPEN = ("queued", "in progress", "escalated")
COMPLETE = {"status": "Closed", "resolution": RESOLUTION_COMPLETED}
DECLINE = {"status": "declined", "resolution": RESOLUTION_DECLINED}

# A report that keeps coming back for more information stops asking after
# this many runs and goes to Sam.
MAX_ROUNDS = 4

_TASK_LINK = re.compile(r"app\.clickup\.com/t/(?:\d+/)?([a-z0-9]+)")
_ROUTE = re.compile(r"^route\s+(backend|frontend|both)\b", re.I)
# "core:form_app/s.py:120", as ticket_fix requires of a fix location.
_EXACT_WHERE = re.compile(r"^[\w-]+:[^\s:()]+(:\d+(-\d+)?)?$")
# A fix spanning repos lists its PRs this way, so ticket_fix.promote_merged
# (which reads "Fix PR:") leaves it to run_rollup.
MULTI_PR_LABEL = "Bug report PR:"
_MULTI_PR = re.compile(r"Bug report PR:\s*(\S+)")

# Sam's greenlight. Only his replies count, and only whole answers: "yes but
# use option 2" is a conversation, not a yes.
YES = {"yes", "y", "yep", "yeah", "go", "go ahead", "do it", "execute",
       "ship it", "fix", "build it", "please", "yes please", "approved"}

# What a greenlight never lifts (Sam, 2026-10-19). The analyst names one in
# `hard_limit`; these are the words the thread shows.
HARD_LIMITS = {
    "migration": "it needs a migration (a model or schema change), and "
                 "migrations are generated on deploy, never written by hand",
    "infrastructure": "it is hosting, DNS, deployment or settings work, "
                      "which agents never change",
    "data": "it is a data, billing or account operation, which agents "
            "never do",
}

CHECKING, NEEDS_INFO, DECISION = "checking", "needs_info", "decision"
ROUTED, FIXING, CLOSED, ERROR = "routed", "fixing", "closed", "error"
# GREENLIGHT: waiting for Sam's yes. GREENLIT: he said yes and the build
# could not finish; from here only Sam routes it.
GREENLIGHT, GREENLIT = "greenlight", "greenlit"
WAITING = (NEEDS_INFO, DECISION)

LEDGER = "vomeos_bug_reports"
store.register_table(
    LEDGER,
    f"""
    CREATE TABLE IF NOT EXISTS {LEDGER} (
        ts            VARCHAR PRIMARY KEY,
        reporter      VARCHAR,
        task_id       VARCHAR,
        status        VARCHAR,
        verdict       VARCHAR,
        team          VARCHAR,
        pr_url        TEXT,
        fix_task      VARCHAR,
        card          TEXT,
        rounds        INTEGER NOT NULL DEFAULT 0,
        last_reply_ts VARCHAR,
        created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)


def enabled() -> bool:
    value = os.environ.get("BUG_REPORTS_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def daily_cap() -> int:
    return int(os.environ.get("BUG_REPORT_FIX_DAILY_CAP", "3"))


# ---------------------------------------------------------------------------
# Web side: the Slack event
# ---------------------------------------------------------------------------

def handle_message(event: dict) -> None:
    """One human message in #bug-reports. Cheap, never raises."""
    if not enabled():
        return
    ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or "")
    text = str(event.get("text") or "").strip()
    if not ts or not text:
        return
    try:
        from vomeos.worker import enqueue_event

        if not thread_ts or thread_ts == ts:
            _post("Looking into this. I'll check it against the code and "
                  "update this thread.", thread_ts=ts)
            enqueue_event(HANDLER_KEY, {"ts": ts}, queue=QUEUE)
        else:
            enqueue_event(HANDLER_KEY, {
                "ts": thread_ts,
                "reply": {"ts": ts, "user": event.get("user") or "",
                          "text": text},
            }, queue=QUEUE)
    except Exception as exc:
        print(f"[BUG REPORTS] could not queue {ts}: {exc}")


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------

def handle(message: dict) -> dict:
    """One report, or one reply in its thread. Never raises."""
    ts = str(message.get("ts") or "")
    if not ts or not enabled():
        return {"status": "skipped", "reason": "disabled or no message"}
    if not store.ensure_table(LEDGER):
        return {"status": "skipped", "reason": "no database"}
    try:
        reply = message.get("reply")
        if reply:
            return _on_reply(ts, reply)
        row = _row(ts)
        if row and row.get("status") != ERROR:
            return {"status": "skipped", "reason": "already handled"}
        return analyse(ts)
    except Exception as exc:
        print(f"[BUG REPORTS] {ts} failed: {type(exc).__name__}: {exc}")
        _save(ts, status=ERROR)
        _post(f"I couldn't finish checking this ({type(exc).__name__}). "
              f"<@{SAM_SLACK}> it needs a look.", thread_ts=ts)
        return {"status": "error", "error": str(exc)}


def _on_reply(ts: str, reply: dict) -> dict:
    row = _row(ts)
    if not row:
        return {"status": "skipped", "reason": "not a report we handled"}
    if row.get("last_reply_ts") == reply.get("ts"):
        return {"status": "skipped", "reason": "reply already handled"}
    _save(ts, last_reply_ts=reply.get("ts"))
    text = str(reply.get("text") or "").strip()
    card = _card(row)

    status = row.get("status")
    asking = status in (GREENLIGHT, GREENLIT)

    if reply.get("user") == SAM_SLACK:
        word = text.lower().rstrip(".! ")
        route = _ROUTE.match(text)
        if route:
            return _route(ts, row, card, route.group(1).lower(),
                          reason="Sam's call")
        if word in ("close", "no"):
            _save(ts, status=CLOSED)
            _post("Closed. Nothing filed.", thread_ts=ts)
            return {"status": "ok", "action": "closed"}
        # `fix` works anywhere; the other yeses only answer a greenlight.
        if word == "fix" or (asking and _is_yes(text)):
            _save(ts, status=GREENLIT)
            return _build(ts, {**row, "greenlit": True}, card, greenlit=True)
        if asking:
            # A question or a change to the fix: think again with his note
            # in the thread, then ask again.
            return analyse(ts, greenlit=status == GREENLIT)
    elif asking:
        return {"status": "skipped", "reason": "only Sam can greenlight"}

    if status in WAITING:
        return analyse(ts)
    return {"status": "skipped", "reason": "chat on a finished report"}


def analyse(ts: str, dry_run: bool = False, greenlit: bool = False) -> dict:
    """Run the analyst on the report and act on its verdict. `greenlit`:
    Sam already said yes once, so nothing routes without his word."""
    from vomeos import run_agent

    report = _read_report(ts)
    if not report:
        return {"status": "skipped", "reason": "message unreadable"}
    row = {} if dry_run else _row(ts)
    rounds = int(row.get("rounds") or 0) + 1
    task_id = report["task_id"]
    context = {
        "report": report["text"],
        "reporter": report["reporter"],
        "thread": report["thread"] or "(no replies)",
        "linked_task": _task_text(task_id),
        "existing_tasks": _existing(report["text"]),
    }
    result = run_agent(AGENT, context=context,
                       subject_type="slack_bug_report", subject_id=ts)
    if not result.ok:
        card = {"verdict": DECISION, "team": "none",
                "headline": "The analysis did not finish",
                "analysis": f"The analyst returned nothing usable "
                            f"({result.why()}).",
                "question": "Can you take a look at this one?"}
    else:
        card = result.data or {}
    if not dry_run:
        _save(ts, reporter=report["reporter"], task_id=task_id,
              verdict=str(card.get("verdict") or ""),
              team=str(card.get("team") or ""), card=json.dumps(card),
              rounds=rounds, status=GREENLIT if greenlit else CHECKING)
    row = {**row, "ts": ts, "task_id": task_id, "text": report["text"],
           "reporter": report["reporter"], "greenlit": greenlit,
           "reporter_id": report["reporter_id"], "rounds": rounds}
    return act(ts, row, card, dry_run=dry_run)


def act(ts: str, row: dict, card: dict, dry_run: bool = False) -> dict:
    verdict = str(card.get("verdict") or "")
    if verdict == "needs_info" and int(row.get("rounds") or 0) >= MAX_ROUNDS:
        verdict = DECISION
        card = {**card, "question": "I've asked for more information "
                                    f"{MAX_ROUNDS} times. Can you take it?"}
    # Auto-routing assumes the agent checked the code. When it could not
    # read any (a token lapsed, a repo down), routing would only pass the
    # reporter's theory on unverified: Sam decides instead.
    blind = (card.get("code_check") or {}).get("verdict") == "not_checked"
    if blind and verdict in ("agent_fix", GREENLIGHT, "engineer"):
        verdict = DECISION
        card = {**card, "question": "I couldn't read the code, so this is "
                                    "unverified. Route it as is "
                                    f"(`route {_team(card)}`) or `close`?"}
    if verdict in ("agent_fix", GREENLIGHT, "engineer"):
        return _fix_or_route(ts, row, card, dry_run=dry_run)

    # ClickUp holds the record; Slack carries a short message and the link
    # (Sam, 2026-10-19). The analysis only goes in the thread when there is
    # no task to put it on.
    task_id = row.get("task_id") or ""
    url = f"https://app.clickup.com/t/{task_id}" if task_id else ""
    details = (f"Details on ClickUp: <{url}|task>" if url
               else str(card.get("analysis") or ""))
    question = str(card.get("question") or "")
    # Closing a teammate's task on the agent's word needs a certain call.
    certain = (card.get("code_check") or {}).get("confidence") == "high"
    task_change: dict = {}
    if verdict == "needs_info":
        who = row.get("reporter_id") or ""
        text = f"{f'<@{who}> ' if who else ''}{question}"
        status = NEEDS_INFO
    elif verdict in ("not_a_bug", "already_fixed"):
        label = ("Not a bug" if verdict == "not_a_bug" else "Already fixed")
        if url and certain:
            task_change = (DECLINE if verdict == "not_a_bug" else COMPLETE)
            done = ("declined" if verdict == "not_a_bug" else "closed")
            text = f":white_check_mark: {label}. Marked {done} on ClickUp: " \
                   f"<{url}|task>"
        elif url:
            text = (f":white_check_mark: {label}, I think. Left open on "
                    f"ClickUp for <@{SAM_SLACK}> to confirm: <{url}|task>")
        else:
            text = f":white_check_mark: {label}. {details}"
        status = CLOSED
    elif verdict == "reporter_fix":
        if url:
            task_change = {"to_reporter": True}
            text = (":white_check_mark: This only exists in your branch, so "
                    f"it's yours to fix. Assigned back to you on ClickUp: "
                    f"<{url}|task>")
        else:
            text = (":white_check_mark: This only exists in your branch, so "
                    f"it's yours to fix. {details}")
        status = CLOSED
    else:
        text = (f"<@{SAM_SLACK}> Needs your call: {question}\n{details}\n"
                "Reply here with your answer, or `fix`, "
                "`route backend|frontend|both`, `close`.")
        status = DECISION
    if dry_run:
        return _dry(ts, card, [f"thread: {text}",
                               f"ClickUp: {task_change or 'comment only'}"])
    if task_id:
        _comment(task_id, _comment_text(card, ts))
        _set_source(task_id)
        _apply(task_id, task_change)
    _post(text, thread_ts=ts)
    _save(ts, status=status)
    return {"status": "ok", "verdict": verdict}


# ---------------------------------------------------------------------------
# Agent fix
# ---------------------------------------------------------------------------

def _fix_or_route(ts: str, row: dict, card: dict,
                  dry_run: bool = False) -> dict:
    """A bug with a fix: build it, or ask Sam to greenlight it. Engineers
    get it on the agent's own call only when there is no workable fix
    (Sam, 2026-10-19: the guest shift report had a concrete fix and went
    straight to OnlyG and Sanjay because its risk was high)."""
    import ticket_fix

    limit = _hard_limit(card)
    if limit:
        return _auto_route(ts, row, card, limit, dry_run)
    if not _workable(card):
        return _auto_route(ts, row, card, "there is no workable fix: "
                           + _why_unworkable(card), dry_run)
    ok, why = ticket_fix.should_attempt(card)
    if ok and not dry_run and _opened_today() >= daily_cap():
        ok, why = False, "the daily pull request cap is reached"
    if not ok:
        return _ask_greenlight(ts, row, card, why, dry_run)
    return _build(ts, row, card, greenlit=False, dry_run=dry_run)


def _hard_limit(card: dict) -> str:
    """Why no one's yes can make this an agent fix, or ""."""
    return HARD_LIMITS.get(str(card.get("hard_limit") or "").lower(), "")


def _workable(card: dict) -> bool:
    return not _why_unworkable(card)


def _why_unworkable(card: dict) -> str:
    code = card.get("code_check") or {}
    if code.get("verdict") != "bug":
        return f"the code check says {code.get('verdict') or 'nothing'}"
    if not str(code.get("fix") or "").strip():
        return "no fix is described"
    if not _spots(card, greenlit=True):
        return "no exact location in the code"
    return ""


def _spots(card: dict, greenlit: bool) -> dict[str, list[tuple[str, int]]]:
    """repo alias -> [(path, line)] the fix changes. On its own the agent
    works on `where` only; after Sam's yes, on every file the fix needs."""
    import ticket_fix
    from vomeos.tools import repo_map

    code = card.get("code_check") or {}
    places = [code.get("where")]
    if greenlit:
        places += list(code.get("files") or [])
    repos = repo_map()
    out: dict[str, list[tuple[str, int]]] = {}
    for place in places:
        place = str(place or "").strip()
        if not _EXACT_WHERE.match(place):
            continue
        alias, path, line = ticket_fix.parse_where(place)
        if alias in repos and path and (path, line) not in out.get(alias,
                                                                   []):
            out.setdefault(alias, []).append((path, line))
    return out


def _is_yes(text: str) -> bool:
    """"yes", "go", "ship it", "yes, do it". Anything with more in it ("yes
    but use option 2") is a conversation, not a greenlight."""
    parts = re.split(r"[,.!]+|\s+and\s+", text.lower())
    parts = [p.strip() for p in parts if p.strip()]
    return bool(parts) and all(p in YES for p in parts)


def _ask_greenlight(ts: str, row: dict, card: dict, why: str,
                    dry_run: bool = False) -> dict:
    """@-mention Sam: the problem, the fix, the risk, and how to answer.
    The reporter's task stays with Sam until he decides."""
    code = card.get("code_check") or {}
    repos = ", ".join(sorted(_spots(card, greenlit=True))) or "unknown"
    risk = str(card.get("risk_plain") or "").strip() or \
        f"fix risk {code.get('fix_risk') or 'unknown'}"
    task_id = row.get("task_id") or ""
    url = f"https://app.clickup.com/t/{task_id}" if task_id else ""
    lines = [f"<@{SAM_SLACK}> Greenlight? I can build this fix but won't "
             f"without your yes ({why})."]
    if card.get("answer"):
        lines.append(f"*Your question:* {card['answer']}")
    lines += [
        f"*Problem:* {card.get('headline') or ''}",
        f"*Fix:* {code.get('fix') or ''} (in {repos})",
        f"*Risk:* {risk}",
        "Reply `yes` to build the pull request, "
        "`route backend|frontend|both` to send it to an engineer, "
        "or `close`.",
    ]
    if url:
        lines.append(f"Details on ClickUp: <{url}|task>")
    text = "\n".join(lines)
    if dry_run:
        return _dry(ts, card, [
            f"ask Sam to greenlight (risk: {code.get('fix_risk')}), then PR "
            f"into {repos} dev branch",
            f"thread: {text}",
        ])
    if task_id:
        _comment(task_id, _comment_text(card, ts))
        _set_source(task_id)
        try:
            _clickup("PUT", f"/task/{task_id}",
                     {"assignees": {"add": [SAM_CLICKUP], "rem": []}})
        except Exception as exc:
            print(f"[BUG REPORTS] could not keep {task_id} with Sam: {exc}")
    _post(text, thread_ts=ts)
    _save(ts, status=GREENLIGHT)
    return {"status": "ok", "verdict": GREENLIGHT}


def _auto_route(ts: str, row: dict, card: dict, reason: str,
                dry_run: bool = False) -> dict:
    """Route on the agent's own call, unless Sam has already greenlit this
    report: then only Sam routes it."""
    if not row.get("greenlit"):
        return _route(ts, row, card, _team(card), reason=reason,
                      dry_run=dry_run)
    return _offer_route(ts, card, reason, dry_run)


def _offer_route(ts: str, card: dict, reason: str,
                 dry_run: bool = False) -> dict:
    text = (f"<@{SAM_SLACK}> I can't build this: {reason}. Say "
            f"`route {_team(card)}` to send it to an engineer, or `close`.")
    if dry_run:
        return _dry(ts, card, [f"thread: {text}"])
    _post(text, thread_ts=ts)
    _save(ts, status=GREENLIT)
    return {"status": "ok", "action": "route offered", "reason": reason}


def _build(ts: str, row: dict, card: dict, greenlit: bool,
           dry_run: bool = False) -> dict:
    """One pull request per repo into its dev branch, and the Agent Fixes
    task(s). Sam's yes (`greenlit`) lifts the risk bar and widens the work
    to every file the fix names; it never lifts the hard limits or the
    patch safety guard."""
    row = {**row, "greenlit": row.get("greenlit") or greenlit}
    limit = _hard_limit(card)
    if limit:
        return _offer_route(ts, card, limit, dry_run)
    spots = _spots(card, greenlit)
    if not spots:
        return _offer_route(ts, card, "there is no exact location in the "
                            "code to change", dry_run)

    import sentry_fix

    headline = str(card.get("headline") or "")[:60]
    opened, failed, previews = [], [], []
    for alias, places in spots.items():
        proposal, repo = _propose(ts, card, alias, places, len(spots) > 1)
        if not proposal.get("diff"):
            failed.append((alias, proposal.get("reason") or "no diff"))
            continue
        if dry_run:
            previews.append(f"PR into {alias} dev branch:\n"
                            f"{proposal['diff'][:3000]}")
            continue
        pr = sentry_fix.open_pull_request(
            {"issue_id": _issue_key(ts, alias if len(spots) > 1 else ""),
             "triage_summary": headline,
             "source_label": f"#bug-reports, reported by "
                             f"{row.get('reporter') or 'a teammate'}",
             "severity": "internal report"},
            proposal,
            {"repo": repo.slug, "host": repo.host, "owner": repo.owner,
             "root_cause": (card.get("code_check") or {}).get("finding")
             or ""},
        )
        if pr.get("opened"):
            opened.append((repo, pr["url"]))
        else:
            failed.append((alias, pr.get("reason") or "the PR did not open"))

    if dry_run:
        notes = [f"not built in {a}: {r}" for a, r in failed]
        return _dry(ts, card, previews + notes)
    if not opened:
        reason = "; ".join(f"{a}: {r}" for a, r in failed)
        if not greenlit:
            # The small fix the agent tried alone did not work out. Sam can
            # still say yes to the whole fix, across every file it names.
            return _ask_greenlight(ts, row, card, f"I tried the small "
                                   f"version and it did not work ({reason})")
        return _offer_route(ts, card, reason)

    fix_task = _file_fix_task(card, opened, ts)
    task_url = f"https://app.clickup.com/t/{fix_task}" if fix_task else ""
    urls = [url for _repo, url in opened]
    if row.get("task_id"):
        _comment(row["task_id"],
                 "Agent fix proposed: " + " ".join(urls) + "\n"
                 f"Agent Fixes task: {task_url}\n\n"
                 + _comment_text(card, ts))
        _set_source(row["task_id"])
        _link(row["task_id"], fix_task)
    _save(ts, status=FIXING, pr_url=" ".join(urls), fix_task=fix_task)
    links = " · ".join(f"<{url}|{repo.alias} pull request>"
                       for repo, url in opened)
    lines = [":white_check_mark: Fixed by the agent. Pull request into the "
             "dev branch, waiting for Sam to review and merge.", links]
    lines.append(f"Logged on ClickUp: <{task_url}|Agent Fixes task>"
                 if task_url else
                 f"<@{SAM_SLACK}> the ClickUp task could not be saved.")
    if failed:
        rest = "; ".join(f"{a}: {r}" for a, r in failed)
        lines.append(f"Not built: {rest}. Say `route {_team(card)}` to send "
                     "the rest to an engineer.")
    _post("\n".join(lines), thread_ts=ts)
    return {"status": "ok", "verdict": "agent_fix", "pr_urls": urls,
            "not_built": [a for a, _r in failed]}


def _propose(ts: str, card: dict, alias: str, places: list,
             several: bool = False):
    """The fix author's diff for one repo, written against the branch it
    will merge into. Same ladder as ticket_fix._propose, minus the Zoho
    ticket, across every place in this repo."""
    import sentry_code
    import sentry_fix
    import sentry_projects
    from vomeos.tools import repo_map

    code = card.get("code_check") or {}
    repo = repo_map().get(alias)
    if not repo:
        return {"reason": f"unknown repo {alias!r}"}, repo
    ref = sentry_projects.fix_target(repo.slug) or repo.branch
    trace = "\n".join(f'File "{path}", line {line or 1}, in bug_report'
                      for path, line in places)
    gathered = sentry_code.gather(trace, repo.slug, ref, host=repo.host,
                                  owner=repo.owner)
    fix = str(code.get("fix") or "")
    if several:
        fix += f" (this pull request covers the {alias} part only)"
    proposal = sentry_fix.propose({
        "root_cause": code.get("finding") or "",
        "proposed_fix": fix,
        # sentry_fix's gate refuses anything not low risk. Reaching here
        # means the code check called it low, or Sam greenlit it: his yes is
        # exactly the bar this lifts. Migrations stay refused (hard limit
        # above, and the patch guard after).
        "risk": "low",
        "confidence": code.get("confidence") or "medium",
        "needs_migration": False,
        "files": list(dict.fromkeys(path for path, _line in places)),
    }, {"source": gathered.get("files", ""), "repo": repo.slug,
        "branch": ref, "issue_id": _issue_key(ts, alias if several else "")})
    return proposal, repo


def _file_fix_task(card: dict, prs: list, ts: str) -> str:
    """The Agent Fixes parent, plus one child per pull request when the fix
    spans repos.

    One PR: its "Fix PR:" line is what ticket_fix.promote_merged follows to
    `on dev`. Several: the parent lists them as "Bug report PR:" lines
    instead, which promote_merged does not read, and `run_rollup` moves it
    to `on dev` with every commit in Fix Commit once ALL of them merge, so
    the release watcher waits for all of them.
    """
    import ticket_fix as tf

    code = card.get("code_check") or {}
    several = len(prs) > 1
    label = MULTI_PR_LABEL if several else "Fix PR:"
    description = "\n".join([
        "**Agent fix proposed from #bug-reports.** Not reviewed by a person. "
        "Merge into the dev branch only after review.",
        "",
        *[f"{label} {url}" for _repo, url in prs],
        "**Repos:** " + ", ".join(f"{r.alias} ({r.slug})" for r, _u in prs)
        + ", into the dev branch",
        f"**Report:** {_permalink(ts)}",
        "",
        "## Engineer summary",
        f"**Likely cause:** {code.get('finding') or ''}",
        f"**Where:** `{code.get('where') or ''}`",
        f"**Potential fix:** {code.get('fix') or ''}",
        f"**Confidence:** {code.get('confidence')} | "
        f"**Risk:** {code.get('fix_risk')}",
        "",
        "_When the PR merges, Fix Commit is filled in automatically and the "
        "release watcher takes it from there._" if not several else
        "_When every PR has merged, Fix Commit lists all of them and the "
        "release watcher waits for each one to be live._",
    ])
    try:
        task = _clickup("POST", f"/list/{tf.AGENT_FIXES_LIST}/task", {
            "name": f"[Bug report] {card.get('headline') or ''}"[:180],
            "markdown_description": description,
            "status": "in progress",
            "custom_fields": [
                {"id": tf.FIELD_TAGS, "value": [tf.TAG_AGENT]},
                {"id": tf.FIELD_TYPE, "value": tf.TYPE_BUG},
                {"id": tf.FIELD_MERGE_OWNER, "value": {"add": [SAM_CLICKUP]}},
                {"id": tf.FIELD_RELEASE_CHECK, "value": tf.RELEASE_WAITING},
                {"id": FIELD_SOURCE, "value": SOURCE_SLACK},
            ],
        })
    except Exception as exc:
        print(f"[BUG REPORTS] Agent Fixes task not filed for {ts}: {exc}")
        return ""
    parent = str(task.get("id") or "")
    if several and parent:
        for repo, url in prs:
            try:
                _clickup("POST", f"/list/{tf.AGENT_FIXES_LIST}/task", {
                    "name": f"[Bug report] {repo.alias}: "
                            f"{card.get('headline') or ''}"[:180],
                    "markdown_description": f"Pull request: {url}",
                    "parent": parent,
                    "status": "in progress",
                })
            except Exception as exc:
                print(f"[BUG REPORTS] child for {url} not filed: {exc}")
    return parent


def run_rollup() -> dict:
    """Fixes that span repos: once every PR on the parent has merged, write
    them all into Fix Commit (`repo@sha; repo@sha`) and move the parent and
    its children to `on dev`, where the release watcher waits for each
    commit to be live. A PR closed unmerged closes the fix and hands the
    report back to Sam. Every 15 minutes; never sets `on prod`."""
    import ticket_fix as tf

    changed = []
    try:
        tasks = tf.list_in_progress()
    except Exception as exc:
        return {"error": str(exc)}
    for task in tasks:
        if task.get("parent"):
            continue
        urls = _MULTI_PR.findall(task.get("description")
                                 or task.get("text_content") or "")
        if not urls or _field_value(task, tf.FIELD_FIX_COMMIT):
            continue
        states = [(url, *tf._pr_state(url)) for url in urls]
        children = [t for t in tasks if t.get("parent") == task["id"]]
        closed = [url for url, state, _a, _s in states if state == "closed"]
        try:
            if closed:
                for target in [task] + children:
                    _clickup("PUT", f"/task/{target['id']}",
                             {"status": "Closed"})
                _hand_back(task["id"], closed)
                changed.append(f"{task['id']} closed: {closed[0]} unmerged")
            elif all(state == "merged" and sha
                     for _u, state, _a, sha in states):
                value = "; ".join(f"{a}@{sha}" for _u, _st, a, sha in states)
                _clickup("POST",
                         f"/task/{task['id']}/field/{tf.FIELD_FIX_COMMIT}",
                         {"value": value})
                for target in [task] + children:
                    _clickup("PUT", f"/task/{target['id']}",
                             {"status": "on dev"})
                changed.append(f"{task['id']} merged as {value}")
        except Exception as exc:
            changed.append(f"{task['id']} roll-up failed: {exc}")
    return {"changed": changed}


def _hand_back(fix_task: str, closed: list) -> None:
    """A PR of a multi-repo fix closed unmerged: tell Sam in the report's
    thread, and offer the engineers."""
    if not store.ensure_table(LEDGER):
        return
    rows = store.query(f"SELECT * FROM {LEDGER} WHERE fix_task = :f",
                       {"f": fix_task})
    if not rows:
        return
    row = dict(rows[0])
    _offer_route(row["ts"], _card(row),
                 f"a pull request was closed without merging ({closed[0]}), "
                 "so the fix will not ship")


def _opened_today() -> int:
    rows = store.query(
        f"SELECT COUNT(*) AS n FROM {LEDGER} WHERE pr_url <> ''"
        " AND updated_at >= date_trunc('day', NOW())"
    )
    return int(rows[0]["n"]) if rows else 0


# ---------------------------------------------------------------------------
# Engineer
# ---------------------------------------------------------------------------

def _team(card: dict) -> str:
    team = str(card.get("team") or "").lower()
    return team if team in ("backend", "frontend", "both") else "backend"


def _engineers(team: str) -> list[tuple]:
    import sprint_thread

    keys = {"backend": ["onlyg"], "frontend": ["sanjay"],
            "both": ["onlyg", "sanjay"]}[team]
    return [(key, *sprint_thread.ENGINEERS[key]) for key in keys]


def _names(engineers: list[tuple]) -> str:
    pretty = {"onlyg": "OnlyG", "sanjay": "Sanjay"}
    return " and ".join(pretty.get(e[0], e[0]) for e in engineers)


def _route(ts: str, row: dict, card: dict, team: str, reason: str = "",
           dry_run: bool = False) -> dict:
    """The engineer's Priority Queue task, then a short post in their
    channel and in the report's thread. Sam chose auto-routing, and that
    ClickUp holds the record while Slack carries the message (2026-10-19).
    """
    engineers = _engineers(team)
    names = _names(engineers)
    channels = " and ".join(f"<#{e[4]}>" for e in engineers)
    headline = str(card.get("headline") or "Bug report")
    if dry_run:
        task = row.get("task_id") or "a new Priority Queue task"
        because = f", because {reason}" if reason else ""
        return _dry(ts, card, [
            f"route to {names} ({team}){because}",
            f"ClickUp: {task} in Priority Queue, assigned to {names} only, "
            "queued, Source = Slack, analysis as a comment",
            f"thread: :white_check_mark: Logged on ClickUp for {names} and "
            f"sent in {channels} for communication about it.",
        ])

    task_id = _engineer_task(row, card, ts, [e[1] for e in engineers],
                             reason)
    task_url = f"https://app.clickup.com/t/{task_id}" if task_id else ""
    link = _permalink(ts)
    for _key, _clickup_id, slack_id, label, channel in engineers:
        _post("\n".join(p for p in (
            f"<@{slack_id}> New {label} bug from #bug-reports "
            f"({row.get('reporter') or 'a teammate'}): *{headline}*",
            f"Details and the agent's analysis are on ClickUp: {task_url}"
            if task_url else str(card.get("engineer_summary") or ""),
            f"Report: {link}" if link else "",
        ) if p), channel=channel)

    _save(ts, status=ROUTED, team=team, task_id=task_id)
    if task_url:
        text = (f":white_check_mark: Logged on ClickUp for {names} and sent "
                f"in {channels} for communication about it.\n"
                f"<{task_url}|{headline}>")
    else:
        text = (f":warning: Sent to {names} in {channels}, but the ClickUp "
                f"task could not be saved. <@{SAM_SLACK}> it needs logging.")
    _post(text, thread_ts=ts)
    return {"status": "ok", "verdict": "engineer", "team": team,
            "task": task_id}


def _engineer_task(row: dict, card: dict, ts: str, ids: list,
                   reason: str) -> str:
    """The reporter's task when it is in Priority Queue, otherwise a new
    Priority Queue task linked to it. Either way: assigned to the engineers
    alone, an open engineer status, Source = Slack, Type kept if the
    reporter set one, and the analysis as a comment."""
    task_id = row.get("task_id") or ""
    task = {}
    if task_id:
        try:
            task = _clickup("GET", f"/task/{task_id}")
        except Exception as exc:
            print(f"[BUG REPORTS] could not read {task_id}: {exc}")
    if not task or (task.get("list") or {}).get("id") != PRIORITY_QUEUE:
        new = _create_task(card, ts, ids, reason, row.get("text") or "")
        if task_id and new:
            _link(task_id, new)
            _comment(task_id, "Routed to an engineer. Work is tracked on "
                              f"https://app.clickup.com/t/{new}")
        return new or task_id

    status = str((task.get("status") or {}).get("status") or "").lower()
    body: dict = {"assignees": {
        "add": ids, "rem": [SAM_CLICKUP] if SAM_CLICKUP not in ids else []}}
    if status not in ENGINEER_OPEN:
        body["status"] = "queued"
    try:
        _clickup("PUT", f"/task/{task_id}", body)
    except Exception as exc:
        print(f"[BUG REPORTS] could not assign {task_id}: {exc}")
    _set_source(task_id)
    if not _field_value(task, FIELD_TYPE):
        _set_field(task_id, FIELD_TYPE, TYPE_BUG)
    _comment(task_id, _comment_text(card, ts, reason))
    return task_id


def _field_value(task: dict, field_id: str):
    for field in task.get("custom_fields") or []:
        if field.get("id") == field_id:
            return field.get("value")
    return None


def _create_task(card: dict, ts: str, assignees: list, reason: str,
                 report: str = "") -> str:
    description = _comment_text(card, ts, reason)
    if report:
        description += f"\n\n## The report\n{report}"
    try:
        task = _clickup("POST", f"/list/{PRIORITY_QUEUE}/task", {
            "name": str(card.get("headline") or "Bug report")[:180],
            "markdown_description": description,
            "assignees": assignees,
            "status": "queued",
            "custom_fields": [
                {"id": FIELD_SOURCE, "value": SOURCE_SLACK},
                {"id": FIELD_TYPE, "value": TYPE_BUG},
            ],
        })
        return str(task.get("id") or "")
    except Exception as exc:
        print(f"[BUG REPORTS] task not created for {ts}: {exc}")
        return ""


def _apply(task_id: str, change: dict) -> None:
    """Close, decline, or hand a teammate's task back to them."""
    if not change:
        return
    try:
        if change.get("to_reporter"):
            task = _clickup("GET", f"/task/{task_id}")
            creator = (task.get("creator") or {}).get("id")
            if creator and creator != SAM_CLICKUP:
                _clickup("PUT", f"/task/{task_id}", {"assignees": {
                    "add": [creator], "rem": [SAM_CLICKUP]}})
            return
        _clickup("PUT", f"/task/{task_id}", {"status": change["status"]})
        _set_field(task_id, FIELD_RESOLUTION, change["resolution"])
    except Exception as exc:
        print(f"[BUG REPORTS] could not update {task_id}: {exc}")


def _comment_text(card: dict, ts: str, reason: str = "") -> str:
    code = card.get("code_check") or {}
    lines = [
        f"**Agent analysis** ({card.get('verdict')}, team "
        f"{card.get('team')}, found on {card.get('found_on')})",
        str(card.get("analysis") or ""),
    ]
    if card.get("engineer_summary"):
        lines += ["", f"**What to change:** {card['engineer_summary']}"]
    if code.get("where"):
        lines += [f"**Where:** `{code['where']}` (confidence "
                  f"{code.get('confidence')}, fix risk "
                  f"{code.get('fix_risk')})"]
    if card.get("question"):
        lines += [f"**Open question:** {card['question']}"]
    if reason:
        lines += [f"**Routed because:** {reason}"]
    link = _permalink(ts)
    if link:
        lines += ["", f"Slack report: {link}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading the report
# ---------------------------------------------------------------------------

def _read_report(ts: str) -> dict:
    client = _slack()
    replies = client.conversations_replies(channel=CHANNEL, ts=ts,
                                           limit=50).get("messages") or []
    if not replies:
        return {}
    parent = replies[0]
    reporter_id = str(parent.get("user") or "")
    thread = []
    for msg in replies[1:]:
        who = "agent" if msg.get("bot_id") else _name(msg.get("user"))
        thread.append(f"{who}: {msg.get('text') or ''}")
    text = str(parent.get("text") or "")
    match = _TASK_LINK.search(text)
    return {"text": text, "reporter_id": reporter_id,
            "reporter": _name(reporter_id), "thread": "\n\n".join(thread),
            "task_id": match.group(1) if match else ""}


def _name(user_id) -> str:
    if not user_id:
        return "someone"
    try:
        profile = _slack().users_info(user=user_id)["user"]
        return (profile.get("real_name") or profile.get("name")
                or str(user_id))
    except Exception:
        return str(user_id)


def _task_text(task_id: str) -> str:
    if not task_id:
        return "No linked ClickUp task."
    try:
        task = _clickup("GET", f"/task/{task_id}?include_markdown_"
                               "description=true")
    except Exception as exc:
        return f"Linked task {task_id} could not be read ({exc})."
    body = task.get("markdown_description") or task.get("text_content") or ""
    status = (task.get("status") or {}).get("status") or ""
    return (f"{task.get('name')} (status {status})\n\n{body}")[:8000]


def _existing(text: str) -> str:
    try:
        import clickup_search

        return clickup_search.describe_existing_work(text[:300])
    except Exception as exc:
        return (f"Existing-task search failed ({exc}). Treat existing work "
                "as UNKNOWN, not as none.")


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _issue_key(ts: str, alias: str = "") -> str:
    """Names the PR branch; one per repo when a fix spans repos."""
    key = "slack-" + ts.replace(".", "-")
    return f"{key}-{alias}" if alias else key


def _row(ts: str) -> dict:
    rows = store.query(f"SELECT * FROM {LEDGER} WHERE ts = :t", {"t": ts})
    return dict(rows[0]) if rows else {}


def _card(row: dict) -> dict:
    try:
        return json.loads(row.get("card") or "{}")
    except ValueError:
        return {}


def _save(ts: str, **fields) -> None:
    if not store.ensure_table(LEDGER):
        return
    cols = ["ts"] + list(fields)
    params = {"ts": ts, **fields}
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in fields)
    store.execute(
        f"INSERT INTO {LEDGER} ({', '.join(cols)}) VALUES "
        f"({', '.join(':' + c for c in cols)}) ON CONFLICT (ts) DO UPDATE "
        f"SET {updates + ', ' if updates else ''}updated_at = NOW()",
        params,
    )


def _dry(ts: str, card: dict, actions: list[str]) -> dict:
    print(f"\n=== {ts} | {card.get('verdict')} | {card.get('team')} | "
          f"found on {card.get('found_on')}")
    print(f"headline: {card.get('headline')}")
    print(f"analysis: {card.get('analysis')}")
    if card.get("question"):
        print(f"question: {card.get('question')}")
    print(f"code_check: {json.dumps(card.get('code_check') or {})}")
    for action in actions:
        print(f"WOULD: {action}")
    return {"status": "dry_run", "verdict": card.get("verdict"),
            "actions": actions}


def _slack():
    from slack_sdk import WebClient

    return WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))


def _post(text: str, thread_ts: str = "", channel: str = "") -> str:
    try:
        kwargs = {"channel": channel or CHANNEL, "text": text,
                  "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return str(_slack().chat_postMessage(**kwargs).get("ts") or "")
    except Exception as exc:
        print(f"[BUG REPORTS] Slack post failed: {exc}")
        return ""


def _permalink(ts: str) -> str:
    try:
        return str(_slack().chat_getPermalink(
            channel=CHANNEL, message_ts=ts).get("permalink") or "")
    except Exception:
        return ""


def _clickup(method: str, path: str, body: dict | None = None) -> dict:
    resp = httpx.request(
        method, f"https://api.clickup.com/api/v2{path}",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _comment(task_id: str, text: str) -> None:
    try:
        _clickup("POST", f"/task/{task_id}/comment",
                 {"comment_text": text, "notify_all": False})
    except Exception as exc:
        print(f"[BUG REPORTS] comment on {task_id} failed: {exc}")


def _set_source(task_id: str) -> None:
    _set_field(task_id, FIELD_SOURCE, SOURCE_SLACK)


def _set_field(task_id: str, field_id: str, value) -> None:
    try:
        _clickup("POST", f"/task/{task_id}/field/{field_id}",
                 {"value": value})
    except Exception as exc:
        print(f"[BUG REPORTS] field {field_id} not set on {task_id}: {exc}")


def _link(task_id: str, other: str) -> None:
    if not other:
        return
    try:
        _clickup("POST", f"/task/{task_id}/link/{other}")
    except Exception as exc:
        print(f"[BUG REPORTS] could not link {task_id} to {other}: {exc}")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def backfill(dry_run: bool, limit: int = 20) -> list[dict]:
    """Every top-level human report in the channel not yet handled."""
    history = _slack().conversations_history(channel=CHANNEL, limit=100)
    reports = [m for m in history.get("messages") or []
               if not m.get("bot_id") and not m.get("subtype")
               and (m.get("text") or "").strip()]
    out = []
    for msg in reversed(reports[:limit]):
        ts = msg["ts"]
        if not dry_run:
            if _row(ts):
                continue
            _post("Looking into this. I'll check it against the code and "
                  "update this thread.", thread_ts=ts)
        out.append(analyse(ts, dry_run=dry_run))
    return out


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.backfill:
        results = backfill(args.dry_run, args.limit)
        print(f"\n{len(results)} report(s).")
