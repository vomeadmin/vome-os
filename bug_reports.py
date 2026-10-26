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

Replies in the thread: from Sam, `fix`, `route backend|frontend|both`,
`close` and `forget N` act anywhere; any other reply to a report waiting on
an answer runs the analysis again with the thread.

EVERY OTHER REPLY (Sam, 2026-10-21)
-----------------------------------
Ron's "one more on this" on a merged fix (a second email sending the same
template, with its own English-only text) was dropped as chat. Now any reply
on a report that is not waiting goes to engineering.bug_thread_reader:

    follow_up  a problem the report did not cover -> its own report, keyed
               on the reply (icon on the reply, posts in the same thread,
               the reporter's task unless the reply links another). Sam's
               commands then answer the newest follow-up that is waiting.
    info       detail for the record -> a comment on the ClickUp task
    question   answered in the thread, from the code and the status
    feedback   a correction that holds beyond this report -> a live lesson
               for the analyst or the fix author (Sam tagged when someone
               else taught it, `forget N` drops it)
    chat       nothing posted

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
READER = "engineering.bug_thread_reader"
# Agents a thread comment can teach. Lessons from here never reach the
# customer-facing agents.
LEARNABLE = {"bug_report_analyst": "engineering.bug_report_analyst",
             "fix_author": "engineering.fix_author"}

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
# No `declined` column (Sam, 2026-10-22): a turned-down report is Closed and
# the Resolution field says why.
DECLINE = {"status": "Closed", "resolution": RESOLUTION_DECLINED}

# A report that keeps coming back for more information stops asking after
# this many runs and goes to Sam.
MAX_ROUNDS = 4

_TASK_LINK = re.compile(r"app\.clickup\.com/t/(?:\d+/)?([a-z0-9]+)")
_ROUTE = re.compile(r"^route\s+(backend|frontend|both)\b", re.I)
_FORGET = re.compile(r"^\s*(forget|unlearn)\s+L?(\d+)\s*$", re.I)
# Thanks and nods need no model run.
_CHAT = re.compile(r"^(thanks?( you)?|ty|ok(ay)?|great|nice|cool|perfect|"
                   r"awesome|got it|sounds good|lgtm|:[\w+-]+:)[\s.!]*$",
                   re.I)
FOLLOW_UP_NOTE = ("FOLLOW-UP raised in the thread of an earlier report. The "
                  "earlier report and everything said since is in `thread`; "
                  "it is already handled. Judge only this follow-up.\n\n")
# "core:form_app/s.py:120", as ticket_fix requires of a fix location.
_EXACT_WHERE = re.compile(r"^[\w-]+:[^\s:()]+(:\d+(-\d+)?)?$")
# A fix spanning repos lists its PRs this way, so ticket_fix.promote_merged
# (which reads "Fix PR:") leaves it to run_rollup.
MULTI_PR_LABEL = "Bug report PR:"
_LEFT_OUT = re.compile(r"Left out:\s*(.+)", re.I | re.S)
# How sentry_fix.open_pull_request reports a diff the applier refused: the
# one failure a second fix author run can repair.
PATCH_REFUSED = "patch does not apply"
# A refused diff, as kept on the Agent Workflows task and shown to the retry.
REJECTED_DIFF_CHARS = 20_000

# What a greenlit fix author reads: each edited file whole up to
# WHOLE_FILE_CHARS (past that, GREENLIT_WINDOW lines either side of each
# spot), then reference files, all within SOURCE_BUDGET per repo. Large on
# purpose: at 60,000 the cancellation policy fix (2026-10-20) got a 300-line
# window of MassUpdateScheduleSettings.js that missed its state and payload
# code. A greenlit build is rare, so whole files are worth the tokens.
WHOLE_FILE_CHARS = 300_000
GREENLIT_WINDOW = 300
SOURCE_BUDGET = 450_000
MAX_REFERENCES = 6
_MULTI_PR = re.compile(r"Bug report PR:\s*(\S+)")

# Sam's greenlight. Only his replies count, and only whole answers: "yes but
# use option 2" is a conversation, not a yes.
# A reply is a yes when every word is one of these and at least one is in
# YES_SIGNAL: "yes build it", "go ahead and ship it", "build the PR". Any
# other word ("but", "why", "don't", "only") makes it a conversation. "Yes
# build it" (2026-10-19) was read as a conversation by the first version,
# which matched whole phrases only.
YES_SIGNAL = {"yes", "y", "yep", "yeah", "yup", "go", "build", "ship",
              "execute", "fix", "approved", "approve", "lgtm", "do"}
YES_FILLER = {"it", "ahead", "please", "now", "the", "this", "that", "a",
              "pr", "pull", "request", "and", "open", "make", "ok", "okay",
              "sure", "thanks", "thank", "you", "go"}

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

# One icon on each report, so #bug-reports reads as a list of what is
# handled without opening threads (Sam, 2026-10-19). Set wherever a status
# is saved, so every path keeps it current.
# After an agent fix: MERGED once every PR is in the dev branch (the reporter
# is told in the thread, to test), LIVE once the release watcher proves it
# in production.
MERGED, LIVE = "merged", "live"

MARKS = {"checking": "eyes", "waiting": "hourglass_flowing_sand",
         "done": "white_check_mark", "merged": "test_tube",
         "live": "rocket", "error": "warning"}
STATUS_MARK = {
    CHECKING: "checking",
    NEEDS_INFO: "waiting", DECISION: "waiting",
    GREENLIGHT: "waiting", GREENLIT: "waiting",
    ROUTED: "done", FIXING: "done", CLOSED: "done",
    MERGED: "merged", LIVE: "live",
    ERROR: "error",
}

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
        parent_ts     VARCHAR,
        reporter_id   VARCHAR,
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
            _mark(ts, "checking")
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
        reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        url = _log_failure(ts, {}, reason, what="The bug report workflow "
                                                "crashed")
        _post(f"I couldn't finish checking this ({type(exc).__name__}). "
              f"<@{SAM_SLACK}> it needs a look." + _logged_line(url),
              thread_ts=ts)
        return {"status": "error", "error": str(exc)}


def _on_reply(ts: str, reply: dict) -> dict:
    row = _row(ts)
    if not row:
        return {"status": "skipped", "reason": "not a report we handled"}
    if row.get("last_reply_ts") == reply.get("ts"):
        return {"status": "skipped", "reason": "reply already handled"}
    _save(ts, last_reply_ts=reply.get("ts"))
    text = str(reply.get("text") or "").strip()
    sam = reply.get("user") == SAM_SLACK
    if sam and _FORGET.match(text):
        return _forget(ts, text)

    # Commands and answers go to whatever in the thread is waiting on one:
    # the report itself, or a follow-up raised in its thread.
    target = _active(ts, row)
    key, card = target["ts"], _card(target)
    status = target.get("status")
    asking = status in (GREENLIGHT, GREENLIT)

    if sam:
        word = text.lower().rstrip(".! ")
        route = _ROUTE.match(text)
        if route:
            return _route(key, target, card, route.group(1).lower(),
                          reason="Sam's call")
        if word in ("close", "no"):
            _save(key, status=CLOSED)
            _post("Closed. Nothing filed.", thread_ts=ts)
            return {"status": "ok", "action": "closed"}
        # `fix` works anywhere; the other yeses only answer a greenlight.
        if word == "fix" or (asking and _is_yes(text)):
            _save(key, status=GREENLIT)
            if "read" not in card:
                # Saved before the analysis recorded which files it opened
                # (deployed 2026-10-19 16:37), so the fix author would get no
                # reference files: Sam's yes on the exemption thread failed
                # that way. Look again first, then build.
                card = _refresh_card(key) or card
            return _build(key, {**target, "greenlit": True}, card,
                          greenlit=True)
        if asking:
            # A question or a change to the fix: think again with his note
            # in the thread, then ask again.
            return analyse(key, greenlit=status == GREENLIT)
    elif asking and _is_yes(text):
        return {"status": "skipped", "reason": "only Sam can greenlight"}

    if status in WAITING:
        return analyse(key)
    # Everything else used to be dropped as chat on a finished report, so
    # Ron's "one more on this" after a merged fix went nowhere (Sam,
    # 2026-10-21). Now it is read.
    return _digest(ts, row, reply)


def _active(ts: str, row: dict) -> dict:
    """The newest thing in the thread waiting on an answer (a follow-up
    first, then the report), or the report itself."""
    asking = WAITING + (GREENLIGHT, GREENLIT)
    for child in _follow_ups(ts):
        if child.get("status") in asking:
            return child
    return {**row, "ts": ts}


def _follow_ups(ts: str) -> list[dict]:
    """Follow-ups raised in a report's thread, newest first."""
    if not store.ensure_table(LEDGER):
        return []
    return [dict(r) for r in store.query(
        f"SELECT * FROM {LEDGER} WHERE parent_ts = :t"
        " ORDER BY created_at DESC", {"t": ts})]


def _parent_of(ts: str) -> str:
    """The report a follow-up was raised under, or "" for a report."""
    try:
        return str(_row(ts).get("parent_ts") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Reading every other reply (Sam, 2026-10-21)
# ---------------------------------------------------------------------------

def _digest(ts: str, row: dict, reply: dict) -> dict:
    """A reply nobody's command or question covers: a new problem, detail
    for the task, a question, a correction, or chat. The reader decides; a
    new problem is checked and handled like a report of its own, in the
    same thread."""
    from vomeos import run_agent

    text = str(reply.get("text") or "").strip()
    if _CHAT.match(text):
        return {"status": "skipped", "reason": "acknowledgement"}
    report = _read_report(ts)
    if not report:
        return {"status": "skipped", "reason": "message unreadable"}
    user = str(reply.get("user") or "")
    who = "Sam" if user == SAM_SLACK else _name(user)
    result = run_agent(READER, context={
        "report": report["text"],
        "reporter": report["reporter"],
        "thread": report["thread"] or "(no replies)",
        "status": _status_text(ts, row),
        "new_reply": f"{who}: {text}",
    }, subject_type="slack_bug_report_reply", subject_id=str(reply.get("ts")))
    if not result.ok:
        # Nobody asked for this read, so a failed one stays out of the
        # thread rather than becoming a question for Sam.
        print(f"[BUG REPORTS] reply read failed on {ts}: {result.why()}")
        return {"status": "skipped", "reason": "reply read failed"}
    data = result.data or {}
    kind = str(data.get("kind") or "chat")
    # A correction can also leave work undone ("route the leftover part
    # straight away"): the rule is learned and the work checked.
    learned = (_learn(data, who, text)
               if kind in ("feedback", "follow_up") else "")
    if kind == "follow_up":
        return _follow_up(ts, reply, str(data.get("follow_up") or ""),
                          learned)

    lines = []
    said = str(data.get("reply") or "").strip()
    if said and kind in ("question", "feedback", "info"):
        lines.append(said)
    note = str(data.get("task_note") or "").strip()
    task_id = row.get("task_id") or ""
    if note and task_id:
        _comment(task_id, f"Added in the #bug-reports thread by {who}:\n\n"
                          f"{note}\n\n{_permalink(str(reply.get('ts')))}")
        lines.append(f"Added to the ClickUp task: "
                     f"<https://app.clickup.com/t/{task_id}|task>")
    if learned:
        lines.append(learned)
    if lines:
        _post("\n".join(lines), thread_ts=ts)
    return {"status": "ok", "kind": kind,
            "action": "replied" if lines else "read, stayed quiet"}


def _follow_up(ts: str, reply: dict, restated: str,
               learned: str = "") -> dict:
    """A new problem raised in a report's thread becomes its own report,
    keyed on the reply: checked against the code, then fixed, greenlit,
    routed or asked about, with its icon on the reply."""
    key = str(reply.get("ts") or "")
    if not key or _row(key):
        return {"status": "skipped", "reason": "follow-up already handled"}
    _post("Looking into this follow-up. I'll check it against the code and "
          "update this thread." + (f"\n{learned}" if learned else ""),
          thread_ts=ts)
    _save(key, parent_ts=ts, reporter_id=str(reply.get("user") or ""),
          status=CHECKING, card=json.dumps({"follow_up": restated}))
    try:
        return {**analyse(key), "follow_up": key}
    except Exception as exc:
        print(f"[BUG REPORTS] follow-up {key} failed: "
              f"{type(exc).__name__}: {exc}")
        _save(key, status=ERROR)
        url = _log_failure(key, {}, f"{type(exc).__name__}: "
                                    f"{str(exc)[:300]}",
                           what="A #bug-reports follow-up crashed")
        _post(f"I couldn't finish checking this follow-up "
              f"({type(exc).__name__}). <@{SAM_SLACK}> it needs a look."
              + _logged_line(url), thread_ts=ts)
        return {"status": "error", "error": str(exc)}


def _learn(data: dict, who: str, said: str) -> str:
    """A correction that holds beyond this report becomes a live lesson for
    the agent it is about. Sam is tagged when someone else taught it, so he
    can drop it. Returns the line for the thread, or ""."""
    rule = str(data.get("lesson") or "").strip()
    agent = LEARNABLE.get(str(data.get("lesson_agent") or ""))
    if not rule or not agent:
        return ""
    try:
        from vomeos import lessons

        lesson_id = lessons.add(agent, rule, "bug_reports_thread",
                                evidence=f"{who}: {said}",
                                created_by="bug_thread_reader")
    except Exception as exc:
        print(f"[BUG REPORTS] lesson not saved: {exc}")
        return ""
    if not lesson_id:
        return ""
    sam = "" if who == "Sam" else f"<@{SAM_SLACK}> "
    short = agent.split(".")[-1].replace("_", " ")
    return (f"{sam}:brain: Learned *L{lesson_id}* ({short}): {rule} "
            f"Say `forget {lesson_id}` to undo.")


def _forget(ts: str, text: str) -> dict:
    from vomeos import lessons

    lesson_id = int(_FORGET.match(text).group(2))
    found = lessons.get(lesson_id)
    if found:
        lessons.retire(lesson_id, "Sam said forget in #bug-reports")
        _post(f":wastebasket: Forgot L{lesson_id}: {found.get('rule')}",
              thread_ts=ts)
    else:
        _post(f"There is no lesson L{lesson_id}.", thread_ts=ts)
    return {"status": "ok", "action": "forget"}


# What each status means, for the reader: it must never call a fix live or
# merged that is not.
STATUS_TEXT = {
    CHECKING: "being checked against the code now",
    NEEDS_INFO: "waiting on the reporter's answer",
    DECISION: "waiting on Sam's call",
    GREENLIGHT: "fix described, waiting on Sam's yes to build it",
    GREENLIT: "Sam said yes; the build did not finish",
    ROUTED: "sent to an engineer, not fixed yet",
    FIXING: "agent pull request open, not merged yet",
    MERGED: "fix merged into the dev branch, not in production yet",
    LIVE: "fix live in production",
    CLOSED: "closed",
    ERROR: "the check crashed",
}


def _status_text(ts: str, row: dict) -> str:
    def one(r: dict) -> str:
        card = _card(r)
        bits = [STATUS_TEXT.get(str(r.get("status")), str(r.get("status")))]
        if card.get("headline"):
            bits.append(f"headline: {card['headline']}")
        if r.get("team"):
            bits.append(f"team: {r['team']}")
        if r.get("pr_url"):
            bits.append(f"PRs: {r['pr_url']}")
        code = card.get("code_check") or {}
        if code.get("where"):
            bits.append(f"where: {code['where']}")
        if code.get("fix"):
            bits.append(f"fix: {code['fix']}")
        return "; ".join(bits)

    lines = [f"Report: {one(row)}"]
    lines += [f"Follow-up {c['ts']}: {one(c)}" for c in _follow_ups(ts)]
    return "\n".join(lines)


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
    card = _analyst_card(ts, report, run_agent)
    if card.get("failed") and not dry_run:
        url = _log_failure(ts, card, card["failed"],
                           what="The bug report analysis did not finish")
        card["question"] = card["question"] + _logged_line(url)
    if row.get("parent_ts"):
        # A rerun keeps the reader's restatement of the follow-up.
        card.setdefault("follow_up", _card(row).get("follow_up", ""))
    if not dry_run:
        _save(ts, reporter=report["reporter"], task_id=task_id,
              reporter_id=report["reporter_id"],
              verdict=str(card.get("verdict") or ""),
              team=str(card.get("team") or ""), card=json.dumps(card),
              rounds=rounds, status=GREENLIT if greenlit else CHECKING)
    row = {**row, "ts": ts, "task_id": task_id, "text": report["text"],
           "reporter": report["reporter"], "greenlit": greenlit,
           "reporter_id": report["reporter_id"], "rounds": rounds}
    return act(ts, row, card, dry_run=dry_run)


def _analyst_card(ts: str, report: dict, run_agent) -> dict:
    context = {
        "report": report["text"],
        "reporter": report["reporter"],
        "thread": report["thread"] or "(no replies)",
        "linked_task": _task_text(report["task_id"]),
        "existing_tasks": _existing(report["text"]),
    }
    result = run_agent(AGENT, context=context,
                       subject_type="slack_bug_report", subject_id=ts)
    if result.ok:
        card = dict(result.data or {})
        card["read"] = _files_read(getattr(result, "tool_calls", []))
        return card
    return {"verdict": DECISION, "team": "none",
            "headline": "The analysis did not finish",
            "analysis": f"The analyst returned nothing usable "
                        f"({result.why()}).",
            "question": "Can you take a look at this one?",
            "failed": result.why()}


def _refresh_card(ts: str) -> dict:
    """Run the analyst again and save its card, or {} if it could not."""
    from vomeos import run_agent

    report = _read_report(ts)
    if not report:
        return {}
    card = _analyst_card(ts, report, run_agent)
    if card.get("verdict") == DECISION and not card.get("code_check"):
        return {}  # the analysis did not finish: keep the card Sam approved
    _save(ts, card=json.dumps(card))
    return card


def preview_build(ts: str) -> dict:
    """What Sam's yes would build for this report, without opening a pull
    request or writing anything: the analysis, then the greenlit build as a
    dry run, diffs included. `py bug_reports.py --build <ts> --dry-run`."""
    from vomeos import run_agent

    report = _read_report(ts)
    if not report:
        return {"status": "skipped", "reason": "message unreadable"}
    card = _analyst_card(ts, report, run_agent)
    row = {"ts": ts, "task_id": report["task_id"], "greenlit": True,
           "reporter": report["reporter"]}
    return _build(ts, row, card, greenlit=True, dry_run=True)


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
            text = f":white_check_mark: {label}. Marked closed on ClickUp: " \
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
    # On its own the agent edits only the `where` file. A fix that needs more
    # cannot be built that way, so asking first saves a fix author run that
    # was always going to refuse (group members fix, 2026-10-25: the solo
    # attempt was shown the serializer but not the view it also needed).
    alone = _spot_set(card, greenlit=False)
    needed = _spot_set(card, greenlit=True)
    if needed - alone:
        return _ask_greenlight(ts, row, card, f"it changes {len(needed)} "
                               "files, more than I change without your yes",
                               dry_run)
    return _build(ts, row, card, greenlit=False, dry_run=dry_run)


def _spot_set(card: dict, greenlit: bool) -> set[tuple[str, str]]:
    """(repo alias, path) for every file the fix edits."""
    return {(alias, path) for alias, places in _spots(card, greenlit).items()
            for path, _line in places}


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
    """"yes", "yes build it", "go ahead and ship it". Anything with more in
    it ("yes but use option 2") is a conversation, not a greenlight."""
    plain = re.sub(r"<[^>]*>", " ", text.lower())  # mentions and links
    words = re.findall(r"[a-z']+", plain)
    return (bool(words) and any(w in YES_SIGNAL for w in words)
            and all(w in YES_SIGNAL or w in YES_FILLER for w in words))


def _ask_greenlight(ts: str, row: dict, card: dict, why: str,
                    dry_run: bool = False, logged: str = "") -> dict:
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
    ]
    if card.get("also_needs"):
        lines.append(f"*Also needs an engineer:* {card['also_needs']}. "
                     "Sent to backend once the pull request is open.")
    lines.append("Reply `yes` to build the pull request, "
                 "`route backend|frontend|both` to send it to an engineer, "
                 "or `close`.")
    if url:
        lines.append(f"Details on ClickUp: <{url}|task>")
    if logged:
        lines.append(_logged_line(logged).strip())
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
                 dry_run: bool = False, log: bool = False,
                 rejected: list | None = None) -> dict:
    """`log`: this is a failure, not a refusal by design, so it also goes
    to Agent Workflows for review (a hard limit is not logged).
    `rejected`: (repo alias, diff) the applier refused, kept on the log."""
    text = (f"<@{SAM_SLACK}> I can't build this: {reason.rstrip('. ')}. "
            f"Say `route {_team(card)}` to send it to an engineer, or "
            "`close`.")
    if dry_run:
        return _dry(ts, card, [f"thread: {text}"]
                    + (["log the failure in Agent Workflows"] if log else []))
    if log:
        text += _logged_line(_log_failure(ts, card, reason,
                                          rejected=rejected))
    _post(text, thread_ts=ts)
    _save(ts, status=GREENLIT)
    return {"status": "ok", "action": "route offered", "reason": reason}


def _log_failure(ts: str, card: dict, reason: str,
                 what: str = "The greenlit build failed",
                 rejected: list | None = None) -> str:
    """File a bug report workflow failure in Agent Workflows (Sam,
    2026-10-20: every failure is reviewed). Returns the task link or "".

    A refused diff goes on the task too. The group members failure
    (2026-10-25) logged only the applier's error, so nobody could see which
    hunk was wrong."""
    import workflow_log

    code = card.get("code_check") or {}
    repos = ", ".join(sorted(_spots(card, greenlit=True))) or "unknown"
    diffs = [f"\n**Refused diff ({alias}):**\n```diff\n"
             f"{diff[:REJECTED_DIFF_CHARS].rstrip()}\n```"
             for alias, diff in rejected or []]
    return workflow_log.log_issue(
        area="bug reports",
        title=f"{what}: {card.get('headline') or 'a #bug-reports report'}"[
            :150],
        summary=f"{what} on a #bug-reports report. Reason: "
                f"{reason.rstrip('. ')}.",
        details="\n".join([
            f"- Reason: {reason}",
            f"- Fix it was building: {code.get('fix') or '(none)'}",
            f"- Repos: {repos}",
            f"- Where: `{code.get('where') or ''}`",
            f"- Files: {', '.join(code.get('files') or []) or '(none)'}",
            f"- References: "
            f"{', '.join(code.get('references') or []) or '(none)'}",
            *diffs,
        ]),
        channel=CHANNEL, thread_ts=_parent_of(ts) or ts,
        next_step="Work out with Claude what the workflow needs, fix it, "
                  "then say yes again in the thread.",
    )


def _logged_line(url: str) -> str:
    return (f"\nLogged for review in Agent Workflows: <{url}|task>"
            if url else "")


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
                            "code to change", dry_run, log=greenlit)

    import sentry_fix

    headline = str(card.get("headline") or "")[:60]
    several = len(spots) > 1
    opened, failed, previews, rejected = [], [], [], []

    def open_pr(proposal, repo, alias):
        return sentry_fix.open_pull_request(
            {"issue_id": _issue_key(ts, alias if several else ""),
             "triage_summary": headline,
             "source_label": f"#bug-reports, reported by "
                             f"{row.get('reporter') or 'a teammate'}",
             "severity": "internal report"},
            proposal,
            {"repo": repo.slug, "host": repo.host, "owner": repo.owner,
             "root_cause": (card.get("code_check") or {}).get("finding")
             or ""},
        )

    for alias, places in spots.items():
        proposal, repo = _propose(ts, card, alias, places, several,
                                  greenlit=greenlit)
        if not proposal.get("diff") or not proposal.get("attempted"):
            failed.append((alias, proposal.get("reason") or "no diff"))
            continue
        if dry_run:
            failed += _left_out(alias, proposal)
            previews.append(f"PR into {alias} dev branch:\n"
                            f"{proposal['diff'][:3000]}")
            continue
        pr = open_pr(proposal, repo, alias)
        reason = str(pr.get("reason") or "")
        if greenlit and not pr.get("opened") and \
                reason.startswith(PATCH_REFUSED):
            # A diff that misquotes the file. Sam's yes is worth one more
            # senior run: the fix author sees the applier's exact complaint
            # and its own diff, and writes it again from the source.
            rejected.append((alias, proposal["diff"]))
            retry, _repo = _propose(
                ts, card, alias, places, several, greenlit=True,
                previous_attempt=_previous_attempt(reason, proposal["diff"]))
            if retry.get("diff") and retry.get("attempted"):
                proposal = retry
                pr = open_pr(proposal, repo, alias)
                if not pr.get("opened") and str(
                        pr.get("reason") or "").startswith(PATCH_REFUSED):
                    rejected.append((alias, proposal["diff"]))
            else:
                pr = {"reason": f"{reason}. A second attempt wrote no diff: "
                                f"{retry.get('reason') or 'no reason given'}"}
        failed += _left_out(alias, proposal)
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
            url = _log_failure(ts, card, reason,
                               what="The agent's own small fix failed",
                               rejected=rejected)
            return _ask_greenlight(ts, row, card, f"I tried the small "
                                   f"version and it did not work ({reason})",
                                   logged=url)
        return _offer_route(ts, card, reason, log=True, rejected=rejected)

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
    if card.get("also_needs"):
        lines.append(_route_extra(ts, row, card))
    if failed:
        rest = "; ".join(f"{a}: {r.rstrip('. ')}" for a, r in failed)
        lines.append(f"Not built: {rest}. Say `route {_team(card)}` to send "
                     "the rest to an engineer."
                     + _logged_line(_log_failure(
                         ts, card, rest, what="Part of the fix was not "
                                              "built")))
    _post("\n".join(lines), thread_ts=ts)
    return {"status": "ok", "verdict": "agent_fix", "pr_urls": urls,
            "not_built": [a for a, _r in failed]}


def _left_out(alias: str, proposal: dict) -> list:
    """A partial build: the fix author wrote what it could and named the
    rest (skill building-a-greenlit-fix)."""
    left = _LEFT_OUT.search(str(proposal.get("rationale") or ""))
    return [(alias, "left out " + left.group(1).strip())] if left else []


def _previous_attempt(reason: str, diff: str) -> str:
    """What the fix author is told when its diff did not apply: the
    applier's complaint, then the diff itself."""
    return (f"{reason}\n\nThe diff that was refused:\n\n"
            f"{diff[:REJECTED_DIFF_CHARS]}")


def _propose(ts: str, card: dict, alias: str, places: list,
             several: bool = False, greenlit: bool = False,
             previous_attempt: str = ""):
    """The fix author's diff for one repo, written against the branch it
    will merge into. Same ladder as ticket_fix._propose, minus the Zoho
    ticket, across every place in this repo.

    A greenlit fix gets whole files plus the reference files it copies
    from, and the Greenlight section that tells the fix author Sam approved
    it. The first greenlit build (exemption rules, 2026-10-19) failed on all
    three: a 60-line window that stopped before two of the three endpoints,
    no copy of the sibling module it was told to copy, and a refusal of
    "authorization logic" nobody had told it Sam had reviewed."""
    import sentry_code
    import sentry_fix
    import sentry_projects
    from vomeos.tools import repo_map

    code = card.get("code_check") or {}
    repo = repo_map().get(alias)
    if not repo:
        return {"reason": f"unknown repo {alias!r}"}, repo
    ref = sentry_projects.fix_target(repo.slug) or repo.branch
    if greenlit:
        source = _whole_source(repo, ref, places,
                               _references(card, alias))
    else:
        trace = "\n".join(f'File "{path}", line {line or 1}, in bug_report'
                          for path, line in places)
        source = sentry_code.gather(trace, repo.slug, ref, host=repo.host,
                                    owner=repo.owner).get("files", "")
    fix = str(code.get("fix") or "")
    if several:
        fix += f" (this pull request covers the {alias} part only)"
    extra = {}
    if greenlit:
        extra["greenlight"] = (
            "Sam read the problem, this fix and its risk in #bug-reports "
            f"and said yes. Risk as he saw it: {card.get('risk_plain') or ''}"
            " He reviews the pull request before it merges.")
    if previous_attempt:
        extra["previous_attempt"] = previous_attempt
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
    }, {"source": source, "repo": repo.slug, "branch": ref,
        "issue_id": _issue_key(ts, alias if several else ""), **extra})
    return proposal, repo


def _files_read(tool_calls: list) -> list[str]:
    """`repo:path:line` for every file the analyst opened, in order.

    The second greenlit build of the exemption fix (2026-10-19) failed
    because the analyst read step_status/views.py but did not list it in
    `references`, so the fix author never saw it. What the analyst looked at
    is passed on whether or not it remembered to name it."""
    out = []
    for call in tool_calls or []:
        if call.get("name") != "read_code_file":
            continue
        args = call.get("input") or {}
        path = str(args.get("path") or "").strip("/")
        # A directory listing is no use as a reference.
        if not path or "." not in path.rsplit("/", 1)[-1]:
            continue
        place = f"{args.get('repo')}:{path}:{args.get('start_line') or 1}"
        if place not in out:
            out.append(place)
    return out


def _references(card: dict, alias: str) -> list[tuple[str, int]]:
    """Files the fix copies from, in this repo: the ones the analyst named
    in `code_check.references`, then every file it opened while checking."""
    import ticket_fix

    named = (card.get("code_check") or {}).get("references") or []
    out: list[tuple[str, int]] = []
    for place in list(named) + list(card.get("read") or []):
        place = str(place or "").strip()
        if _EXACT_WHERE.match(place):
            ref_alias, path, line = ticket_fix.parse_where(place)
            if ref_alias == alias and path and \
                    path not in (p for p, _l in out):
                out.append((path, line))
    return out[:MAX_REFERENCES]


def _whole_source(repo, ref: str, places: list, references: list) -> str:
    """Numbered source for a greenlit fix: each file it edits whole (wide
    windows around each spot past WHOLE_FILE_CHARS), then each reference
    file marked read only. Never silent about a file it could not read."""
    import sentry_fix
    from vomeos.integrations import code_search

    reader = code_search.GitHub() if repo.host == "github" \
        else code_search.Bitbucket()
    kwargs = {"owner": repo.owner} if repo.host == "github" and repo.owner \
        else {}
    chunks, budget = [], SOURCE_BUDGET

    def numbered(path, lines_wanted, label, window):
        result = reader.read_file(repo.slug, path, ref=ref,
                                  max_bytes=sentry_fix.APPLY_MAX_BYTES,
                                  **kwargs)
        text, why = sentry_fix._file_text(result)
        if text is None:
            return f"--- {path} could not be read ({why}) ---"
        lines = text.splitlines()
        if len(text) <= WHOLE_FILE_CHARS:
            spans = [(1, len(lines))]
        else:
            spans = [(max(1, n - window), min(len(lines), n + window))
                     for n in sorted({n or 1 for n in lines_wanted})]
        width = len(str(len(lines)))
        parts = []
        for start, end in spans:
            body = "\n".join(f"   {str(i).rjust(width)} | {lines[i - 1]}"
                             for i in range(start, end + 1))
            parts.append(f"--- {path} lines {start}-{end}{label} ---\n{body}")
        return "\n\n".join(parts)

    edits: dict[str, list[int]] = {}
    for path, line in places:
        edits.setdefault(path, []).append(line)
    for path, lines_wanted in edits.items():
        chunk = numbered(path, lines_wanted, "", GREENLIT_WINDOW)
        chunks.append(chunk[:budget])
        budget -= len(chunks[-1])
    for path, line in references:
        if path in edits or budget <= 0:
            continue
        chunk = numbered(path, [line], ", REFERENCE, read only: copy its "
                         "pattern and imports, do not edit", 80)
        chunks.append(chunk[:budget])
        budget -= len(chunks[-1])
    return "\n\n".join(chunks)


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


def run_followups() -> dict:
    """Every 15 minutes: roll up multi-repo fixes, then tell each report's
    thread when its fix merges and when it goes live."""
    return {"rollup": run_rollup(), "threads": notify_progress()}


def notify_progress() -> dict:
    """Keep the reporter in their own thread after an agent fix (Sam,
    2026-10-19: Ron should hear when it is merged and ready to test).

    FIXING  every PR merged into the dev branch -> "merged into development,
            ready to test", MERGED (test tube)
            a PR closed unmerged -> back to Sam with `route` offered
    MERGED  its Agent Fixes task's Release Check is Live -> "live in
            production", LIVE (rocket)

    Never moves a task: promote_merged, run_rollup and the release watcher
    own that. This only reads their results and tells the thread.
    """
    import ticket_fix as tf

    if not store.ensure_table(LEDGER):
        return {"skipped": "no database"}
    told = []
    rows = store.query(
        f"SELECT * FROM {LEDGER} WHERE status IN (:f, :m)"
        " AND COALESCE(pr_url, '') <> ''",
        {"f": FIXING, "m": MERGED},
    )
    for row in rows:
        ts = row["ts"]
        try:
            if row["status"] == FIXING:
                urls = str(row["pr_url"]).split()
                states = [(url, *tf._pr_state(url)) for url in urls]
                closed = [u for u, state, _a, _s in states
                          if state == "closed"]
                if closed:
                    _offer_route(ts, _card(row), "the pull request was "
                                 f"closed without merging ({closed[0]})")
                    told.append(f"{ts} closed unmerged")
                elif all(state == "merged" for _u, state, _a, _s in states):
                    commits = ", ".join(f"`{a}@{sha}`" if sha else f"`{a}`"
                                        for _u, _st, a, sha in states)
                    who = _reporter_mention(ts)
                    _post(f"{who}Merged into `development` ({commits}). "
                          "Ready to test there; I'll post again here when "
                          "it's live in production.", thread_ts=ts)
                    _save(ts, status=MERGED)
                    told.append(f"{ts} merged")
            elif row["status"] == MERGED and _is_live(row.get("fix_task")):
                who = _reporter_mention(ts)
                _post(f"{who}Live in production now. :rocket:", thread_ts=ts)
                _save(ts, status=LIVE)
                told.append(f"{ts} live")
        except Exception as exc:
            told.append(f"{ts} failed: {exc}")
    return {"told": told}


def _is_live(fix_task: str) -> bool:
    """The release watcher proved every commit on the task is in production
    (Release Check = Live)."""
    if not fix_task:
        return False
    import release_watcher as rw

    try:
        task = _clickup("GET", f"/task/{fix_task}")
    except Exception as exc:
        print(f"[BUG REPORTS] could not read {fix_task}: {exc}")
        return False
    return rw._release_check_value(task) == "live"


def _reporter_mention(ts: str) -> str:
    """A mention of whoever posted the report (or raised the follow-up), or
    "" if nobody knows."""
    try:
        stored = str(_row(ts).get("reporter_id") or "")
        if stored:
            return f"<@{stored}> "
    except Exception:
        pass
    try:
        replies = _slack().conversations_replies(
            channel=CHANNEL, ts=ts, limit=1).get("messages") or []
        user = str((replies[0] if replies else {}).get("user") or "")
        return f"<@{user}> " if user else ""
    except Exception:
        return ""


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


def _route_extra(ts: str, row: dict, card: dict) -> str:
    """The part of a report no agent may do (a one-off data update beside
    the code fix) goes to the backend engineer as its own Priority Queue
    task, linked to the reporter's. Data work is backend work. Returns the
    thread line. The report itself stays on the fix's track."""
    engineers = _engineers("backend")
    extra = str(card.get("also_needs") or "").strip()
    task_id = _create_task(
        {**card, "headline": f"{card.get('headline') or 'Bug report'}: "
                             f"{extra}"[:180],
         "engineer_summary": extra},
        ts, [e[1] for e in engineers],
        "this part of a greenlit fix is work agents never do",
        row.get("text") or "")
    if row.get("task_id") and task_id:
        _link(row["task_id"], task_id)
    url = f"https://app.clickup.com/t/{task_id}" if task_id else ""
    names = _names(engineers)
    for _key, _cid, slack_id, label, channel in engineers:
        _post("\n".join(p for p in (
            f"<@{slack_id}> New {label} task from #bug-reports, beside an "
            f"agent fix: *{extra}*",
            f"ClickUp: {url}" if url else "",
            f"Report: {_permalink(ts)}",
        ) if p), channel=channel)
    channels = " and ".join(f"<#{e[4]}>" for e in engineers)
    return (f"Also logged on ClickUp for {names} and sent in {channels}: "
            f"{extra}" + (f" <{url}|task>" if url else ""))


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
    """The report, its thread, and the task it links. For a follow-up, the
    reply is the report and the earlier report heads its thread; its task
    is the one the reply links, else the earlier report's."""
    parent_ts = _parent_of(ts)
    row = _row(ts) if parent_ts else {}
    client = _slack()
    replies = client.conversations_replies(channel=CHANNEL,
                                           ts=parent_ts or ts,
                                           limit=100).get("messages") or []
    if not replies:
        return {}

    def said(msg: dict) -> str:
        who = "agent" if msg.get("bot_id") else _name(msg.get("user"))
        return f"{who}: {msg.get('text') or ''}"

    first = replies[0]
    if parent_ts:
        mine = next((m for m in replies if m.get("ts") == ts), None)
        if not mine:
            return {}
        thread = [f"Earlier report by {said(first)}"]
        thread += [said(m) for m in replies[1:] if m.get("ts") != ts]
        restated = str(_card(row).get("follow_up") or "")
        text = FOLLOW_UP_NOTE + (f"In short: {restated}\n\nAs written:\n"
                                 if restated else "") + str(
            mine.get("text") or "")
        reporter_id = str(mine.get("user") or "")
        match = (_TASK_LINK.search(str(mine.get("text") or ""))
                 or _TASK_LINK.search(str(first.get("text") or "")))
    else:
        thread = [said(m) for m in replies[1:]]
        text = str(first.get("text") or "")
        reporter_id = str(first.get("user") or "")
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
    if fields.get("status") in STATUS_MARK:
        _mark(ts, STATUS_MARK[fields["status"]])
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


def _mark(ts: str, state: str) -> None:
    """Show where the report stands on the report itself, swapping the
    bot's previous icon. Only the bot's own reactions are ever removed."""
    name = MARKS.get(state)
    if not name:
        return
    client = _slack()
    for other in MARKS.values():
        if other == name:
            continue
        try:
            client.reactions_remove(channel=CHANNEL, timestamp=ts, name=other)
        except Exception:
            pass  # it was not there
    try:
        client.reactions_add(channel=CHANNEL, timestamp=ts, name=name)
    except Exception as exc:
        if "already_reacted" not in str(exc):
            print(f"[BUG REPORTS] could not mark {ts} {name}: {exc}")


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
            # A follow-up is keyed on its reply; Slack threads only under
            # the report itself.
            kwargs["thread_ts"] = _parent_of(thread_ts) or thread_ts
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
    # "file_share" is a report posted with a screenshot.
    reports = [m for m in history.get("messages") or []
               if not m.get("bot_id")
               and m.get("subtype") in (None, "file_share")
               and (m.get("text") or "").strip()]
    out = []
    for msg in reversed(reports[:limit]):
        ts = msg["ts"]
        if not dry_run:
            if _row(ts):
                continue
            _post("Looking into this. I'll check it against the code and "
                  "update this thread.", thread_ts=ts)
            _mark(ts, "checking")
        out.append(analyse(ts, dry_run=dry_run))
    return out


def run_one(ts: str, dry_run: bool) -> dict:
    """One top-level report the webhook missed, by its Slack ts. Runs it
    like a new report unless the ledger already handled it."""
    if not dry_run:
        row = _row(ts)
        if row and row.get("status") != ERROR:
            return {"status": "skipped", "reason": "already handled",
                    "row_status": row.get("status")}
        _post("Looking into this. I'll check it against the code and "
              "update this thread.", thread_ts=ts)
        _mark(ts, "checking")
    return analyse(ts, dry_run=dry_run)


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--build", metavar="TS",
                        help="preview what Sam's yes would build (needs "
                             "--dry-run; it never opens a PR)")
    parser.add_argument("--ts", metavar="TS",
                        help="run one missed top-level report by its "
                             "Slack ts")
    args = parser.parse_args()
    if args.ts:
        print(json.dumps(run_one(args.ts, args.dry_run), indent=2,
                         default=str))
    elif args.build:
        if not args.dry_run:
            parser.error("--build only previews: add --dry-run")
        preview_build(args.build)
    elif args.backfill:
        results = backfill(args.dry_run, args.limit)
        print(f"\n{len(results)} report(s).")
