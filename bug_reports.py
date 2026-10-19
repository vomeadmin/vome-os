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
      3. agent_fix   -> PR into the dev branch, Agent Fixes task, thread update
                        (a PR that cannot be written falls back to engineer)
         engineer    -> the reporter's task (or a new Priority Queue task)
                        assigned to OnlyG / Sanjay, a post in their channel
         decision    -> Sam is asked in the thread
         needs_info  -> the reporter is asked in the thread
         not_a_bug, already_fixed, reporter_fix -> the finding, nothing filed

Replies in the thread: from Sam, `fix`, `route backend|frontend|both` and
`close` act; any other reply to a report waiting on an answer runs the
analysis again with the thread.

THE REPORTER'S OWN TASK
-----------------------
Ron files a ClickUp task for every report and links it (Sam chose to adopt
it rather than file a second one). It gets Source = Slack, the analysis as a
comment, and the engineer as assignee. A new task is created only when the
report links none.

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

# A report that keeps coming back for more information stops asking after
# this many runs and goes to Sam.
MAX_ROUNDS = 4

_TASK_LINK = re.compile(r"app\.clickup\.com/t/(?:\d+/)?([a-z0-9]+)")
_ROUTE = re.compile(r"^route\s+(backend|frontend|both)\b", re.I)

CHECKING, NEEDS_INFO, DECISION = "checking", "needs_info", "decision"
ROUTED, FIXING, CLOSED, ERROR = "routed", "fixing", "closed", "error"
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

    if reply.get("user") == SAM_SLACK:
        word = text.lower().rstrip(".! ")
        route = _ROUTE.match(text)
        if word == "fix":
            return _fix(ts, row, card, sam_approved=True)
        if route:
            return _route(ts, row, card, route.group(1).lower(),
                          reason="Sam's call")
        if word in ("close", "no"):
            _save(ts, status=CLOSED)
            _post("Closed. Nothing filed.", thread_ts=ts)
            return {"status": "ok", "action": "closed"}

    if row.get("status") in WAITING:
        return analyse(ts)
    return {"status": "skipped", "reason": "chat on a finished report"}


def analyse(ts: str, dry_run: bool = False) -> dict:
    """Run the analyst on the report and act on its verdict."""
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
              rounds=rounds, status=CHECKING)
    row = {**row, "ts": ts, "task_id": task_id,
           "reporter": report["reporter"],
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
    if blind and verdict in ("agent_fix", "engineer"):
        verdict = DECISION
        card = {**card, "question": "I couldn't read the code, so this is "
                                    "unverified. Route it as is "
                                    f"(`route {_team(card)}`) or `close`?"}
    if verdict == "agent_fix":
        return _fix(ts, row, card, dry_run=dry_run)
    if verdict == "engineer":
        return _route(ts, row, card, _team(card), dry_run=dry_run)

    analysis = str(card.get("analysis") or "")
    question = str(card.get("question") or "")
    if verdict == "needs_info":
        who = row.get("reporter_id") or ""
        text = (f"{f'<@{who}> ' if who else ''}{question}\n\n"
                f"_What I found so far:_ {analysis}")
        status = NEEDS_INFO
    elif verdict == "not_a_bug":
        text = f"*Not a bug.* {analysis}"
        status = CLOSED
    elif verdict == "already_fixed":
        text = f"*Already fixed.* {analysis}"
        status = CLOSED
    elif verdict == "reporter_fix":
        text = (f"*Only in your branch.* {analysis}\n"
                "Nothing to fix on development or develop.")
        status = CLOSED
    else:
        text = (f"<@{SAM_SLACK}> *Needs your call.* {analysis}\n"
                f"*Question:* {question}\n"
                "Reply here with your answer, or `fix`, "
                "`route backend|frontend|both`, `close`.")
        status = DECISION
    headline = str(card.get("headline") or "")
    if dry_run:
        return _dry(ts, card, [f"thread: {text}"])
    _post(f"*{headline}*\n{text}" if headline else text, thread_ts=ts)
    if row.get("task_id"):
        _comment(row["task_id"], _comment_text(card, ts))
        _set_source(row["task_id"])
    _save(ts, status=status)
    return {"status": "ok", "verdict": verdict}


# ---------------------------------------------------------------------------
# Agent fix
# ---------------------------------------------------------------------------

def _fix(ts: str, row: dict, card: dict, sam_approved: bool = False,
         dry_run: bool = False) -> dict:
    """A pull request into the dev branch, and its Agent Fixes task.
    Anything that stops the PR hands the report to an engineer instead."""
    import ticket_fix

    ok, why = ticket_fix.should_attempt(card, sam_approved=sam_approved)
    if ok and not sam_approved and not dry_run and \
            _opened_today() >= daily_cap():
        ok, why = False, "the daily pull request cap is reached"
    if not ok and sam_approved:
        # Sam's word lifts the verdict bar, never the risk bar.
        _post(f"I can't open a fix for this: {why}. Say `route backend` "
              "(or frontend, both) to send it to an engineer.", thread_ts=ts)
        return {"status": "ok", "action": "fix refused", "reason": why}
    if not ok:
        return _route(ts, row, card, _team(card),
                      reason=f"no agent fix: {why}", dry_run=dry_run)

    proposal, repo = _propose(ts, card)
    if not proposal.get("diff"):
        return _route(ts, row, card, _team(card),
                      reason="the fix author declined: "
                             f"{proposal.get('reason') or 'no diff'}",
                      dry_run=dry_run)
    if dry_run:
        return _dry(ts, card, [f"PR into {repo.alias} dev branch:\n"
                               f"{proposal['diff'][:3000]}"])

    import sentry_fix

    headline = str(card.get("headline") or "")[:60]
    pr = sentry_fix.open_pull_request(
        {"issue_id": _issue_key(ts), "triage_summary": headline,
         "source_label": f"#bug-reports, reported by "
                         f"{row.get('reporter') or 'a teammate'}",
         "severity": "internal report"},
        proposal,
        {"repo": repo.slug, "host": repo.host, "owner": repo.owner,
         "root_cause": (card.get("code_check") or {}).get("finding") or ""},
    )
    if not pr.get("opened"):
        return _route(ts, row, card, _team(card),
                      reason=f"the PR did not open: {pr.get('reason')}")

    fix_task = _file_fix_task(card, repo, pr["url"], ts)
    task_url = f"https://app.clickup.com/t/{fix_task}" if fix_task else ""
    if row.get("task_id"):
        _comment(row["task_id"],
                 f"Agent fix proposed: {pr['url']}\n"
                 f"Agent Fixes task: {task_url}\n\n"
                 + _comment_text(card, ts))
        _set_source(row["task_id"])
        _link(row["task_id"], fix_task)
    _save(ts, status=FIXING, pr_url=pr["url"], fix_task=fix_task)
    _post("\n".join([
        f"*Agent fix proposed.* {card.get('headline') or ''}",
        str(card.get("analysis") or ""),
        f"PR: <{pr['url']}|pull request into the dev branch>. Sam reviews "
        "and merges.",
        f"ClickUp: <{task_url}|Agent Fixes task>" if task_url else "",
    ]).strip(), thread_ts=ts)
    return {"status": "ok", "verdict": "agent_fix", "pr_url": pr["url"]}


def _propose(ts: str, card: dict):
    """The fix author's diff, written against the branch it will merge into.
    Same ladder as ticket_fix._propose, minus the Zoho ticket."""
    import sentry_code
    import sentry_fix
    import sentry_projects
    import ticket_fix
    from vomeos.tools import repo_map

    code = card.get("code_check") or {}
    alias, path, line = ticket_fix.parse_where(code.get("where"))
    repo = repo_map().get(alias)
    if not repo or not path:
        return {"reason": f"unknown location {code.get('where')!r}"}, repo
    ref = sentry_projects.fix_target(repo.slug) or repo.branch
    trace = f'File "{path}", line {line or 1}, in bug_report'
    gathered = sentry_code.gather(trace, repo.slug, ref, host=repo.host,
                                  owner=repo.owner)
    proposal = sentry_fix.propose({
        "root_cause": code.get("finding") or "",
        "proposed_fix": code.get("fix") or "",
        "risk": "low",
        "confidence": code.get("confidence") or "medium",
        "needs_migration": False,
        "files": [path],
    }, {"source": gathered.get("files", ""), "repo": repo.slug,
        "branch": ref, "issue_id": _issue_key(ts)})
    return proposal, repo


def _file_fix_task(card: dict, repo, pr_url: str, ts: str) -> str:
    """The Agent Fixes parent. Its "Fix PR:" line is what
    ticket_fix.promote_merged follows to `on dev` once the PR merges."""
    import ticket_fix as tf

    code = card.get("code_check") or {}
    description = "\n".join([
        "**Agent fix proposed from #bug-reports.** Not reviewed by a person. "
        "Merge into the dev branch only after review.",
        "",
        f"Fix PR: {pr_url}",
        f"**Repo:** {repo.alias} ({repo.slug}), into the dev branch",
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
        "release watcher takes it from there._",
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
        return str(task.get("id") or "")
    except Exception as exc:
        print(f"[BUG REPORTS] Agent Fixes task not filed for {ts}: {exc}")
        return ""


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


def _route(ts: str, row: dict, card: dict, team: str, reason: str = "",
           dry_run: bool = False) -> dict:
    """Assign the reporter's task (or a new one) and post in each
    engineer's channel. Sam chose auto-routing, 2026-10-19."""
    engineers = _engineers(team)
    names = " and ".join(e[0].replace("onlyg", "OnlyG")
                         .replace("sanjay", "Sanjay") for e in engineers)
    headline = str(card.get("headline") or "Bug report")
    summary = str(card.get("engineer_summary") or card.get("analysis") or "")
    if dry_run:
        task = row.get("task_id") or "a new Priority Queue task"
        because = f", because {reason}" if reason else ""
        return _dry(ts, card, [
            f"route to {names} ({team}){because}",
            f"ClickUp: assign {task}, Source = Slack, comment the analysis",
        ])

    task_id = row.get("task_id") or ""
    ids = [e[1] for e in engineers]
    if task_id:
        try:
            _clickup("PUT", f"/task/{task_id}",
                     {"assignees": {"add": ids, "rem": []}})
        except Exception as exc:
            print(f"[BUG REPORTS] could not assign {task_id}: {exc}")
        _set_source(task_id)
        _comment(task_id, _comment_text(card, ts, reason))
    else:
        task_id = _create_task(card, ts, ids, reason)
    task_url = f"https://app.clickup.com/t/{task_id}" if task_id else ""

    link = _permalink(ts)
    for _key, _clickup_id, slack_id, label, channel in engineers:
        _post("\n".join([
            f"<@{slack_id}> new {label} bug from #bug-reports "
            f"({row.get('reporter') or 'a teammate'}): *{headline}*",
            summary,
            " · ".join(p for p in (
                f"ClickUp: {task_url}" if task_url else "",
                f"Slack: {link}" if link else "") if p),
        ]), channel=channel)

    _save(ts, status=ROUTED, team=team, task_id=task_id)
    _post("\n".join([
        f"*Sent to {names}* ({team}). {headline}",
        str(card.get("analysis") or ""),
        f"_Why it went to an engineer:_ {reason}" if reason else "",
        f"ClickUp: <{task_url}|task>" if task_url else "",
    ]).strip(), thread_ts=ts)
    return {"status": "ok", "verdict": "engineer", "team": team,
            "task": task_id}


def _create_task(card: dict, ts: str, assignees: list, reason: str) -> str:
    import ticket_fix as tf

    try:
        task = _clickup("POST", f"/list/{PRIORITY_QUEUE}/task", {
            "name": str(card.get("headline") or "Bug report")[:180],
            "markdown_description": _comment_text(card, ts, reason),
            "assignees": assignees,
            "status": "queued",
            "custom_fields": [
                {"id": FIELD_SOURCE, "value": SOURCE_SLACK},
                {"id": tf.FIELD_TYPE, "value": tf.TYPE_BUG},
            ],
        })
        return str(task.get("id") or "")
    except Exception as exc:
        print(f"[BUG REPORTS] task not created for {ts}: {exc}")
        return ""


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

def _issue_key(ts: str) -> str:
    return "slack-" + ts.replace(".", "-")


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
    try:
        _clickup("POST", f"/task/{task_id}/field/{FIELD_SOURCE}",
                 {"value": SOURCE_SLACK})
    except Exception as exc:
        print(f"[BUG REPORTS] Source not set on {task_id}: {exc}")


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
