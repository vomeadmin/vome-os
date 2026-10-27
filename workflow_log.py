"""
workflow_log.py

"Log this on ClickUp", from any Slack thread the Vome Agent is in. The
thread becomes a task in Sam and Ron's Agent Workflows list (Operations
space), for Vome OS itself: bugs in the agent workflows, new workflow ideas,
improvements and open questions.

WHY THIS EXISTS
---------------
Sam, 2026-10-19: when he is away from his desk and something goes wrong in
a workflow, or he and Ron come up with one worth building, he wants to say
so in the thread and come back to it later with Claude, instead of trying to
remember it. ClickUp holds the record; Slack carries the one-line reply.

TRIGGER
-------
A message from Sam or Ron that asks to log it ("log this", "add it", "put
that", "save this") AND says where ("ClickUp", "agent workflows", "for us",
"for later"), or that @-mentions the bot. Checked in main.py before any
channel's own handling, so "log this for us on ClickUp" in a #bug-reports or
sprint thread is never read as that channel's command (the sprint's `log N`
has a number and no ClickUp, so it does not match).

    "Log this for us on ClickUp"     "@Vome Agent log this"
    "log this as an idea on clickup" "add it to agent workflows"
    "agent error: it re-asked go 3"  "log this as an agent error"

"Agent error" (Sam, 2026-10-27) is how Sam flags a workflow that went wrong
when he is away from his desk: it is always a bug, his words go on the task
as his note, and the whole thread goes on as comments, so the task carries
everything when it is worked later.

FLOW
----
    web: enqueue engineering.workflow_log
    worker:
      1. claim the trigger message (an @-mention arrives twice: as the
         mention and as a channel message)
      2. read the thread (or just the message, at the top level)
      3. engineering.workflow_logger writes the title, type (bug, idea,
         improvement, question), area, summary, details and next step
      4. a task in Agent Workflows: "[Bug] title", to do, Sam assigned,
         tagged with its type, the thread linked
      5. ":white_check_mark: Logged on ClickUp in Agent Workflows" + link

Logging the same thread again adds a comment to its task instead of making a
second one.

SWITCHES
--------
    WORKFLOW_LOG_ENABLED          default on. "false" ignores the phrase.
    CLICKUP_AGENT_WORKFLOWS_LIST  default 901115585535.
"""

from __future__ import annotations

import os
import re

import httpx

from vomeos import store

LIST = os.environ.get("CLICKUP_AGENT_WORKFLOWS_LIST", "901115585535")
SPACE = "90110779340"  # Operations
HANDLER_KEY = "engineering.workflow_log"
QUEUE = "engineering"
AGENT = "engineering.workflow_logger"

SAM_SLACK = "U01B9FQNSBU"
RON_SLACK = "U01B6D235N1"
# Who may log (Sam, 2026-10-19: him and Ron). Tasks are Sam's either way.
LOGGERS = {SAM_SLACK: "Sam", RON_SLACK: "Ron"}
SAM_CLICKUP = 3691763

TYPES = ("bug", "idea", "improvement", "question")
TAG_SLACK = "from slack"
LOGGED_MARK = "clipboard"  # on the thread's first message once logged
TAG_AUTO = "auto logged"   # filed by an agent after a failure, not a person
TAG_ERROR = "agent error"  # Sam or Ron flagged a workflow that went wrong
DONE = "complete"          # the list's closed status

_ASK = re.compile(r"\b(log|add|put|save)\s+(this|it|that)\b", re.I)
_WHERE = re.compile(r"click\s*up|agent\s+workflows?|\bfor\s+(us|later)\b",
                    re.I)
_MENTION = re.compile(r"<@([A-Z0-9]+)(\|[^>]*)?>")
# "agent error": with a log ask anywhere, or on its own at the start.
_ERROR = re.compile(r"\bagent\s+(?:error|failure|bug)s?\b", re.I)
_ERROR_FIRST = re.compile(r"^\W*agent\s+(?:error|failure|bug)s?\b", re.I)
THREAD_CHUNK = 9000      # characters per ClickUp comment
THREAD_MAX = 27000       # the most of a thread copied onto a task

LEDGER = "vomeos_workflow_log"
store.register_table(
    LEDGER,
    f"""
    CREATE TABLE IF NOT EXISTS {LEDGER} (
        trigger_ts  VARCHAR PRIMARY KEY,
        channel     VARCHAR NOT NULL,
        thread_ts   VARCHAR NOT NULL,
        logged_by   VARCHAR,
        task_id     VARCHAR,
        task_name   TEXT,
        created_at  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)


def enabled() -> bool:
    value = os.environ.get("WORKFLOW_LOG_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def wants(event: dict, mentioned: bool = False) -> bool:
    """Whether this Slack message asks to log the thread. Cheap."""
    if not enabled() or event.get("user") not in LOGGERS:
        return False
    text = str(event.get("text") or "")
    bot = os.environ.get("SLACK_BOT_USER_ID", "")
    mentioned = mentioned or bool(bot and f"<@{bot}" in text)
    plain = _MENTION.sub("", text)
    if _ERROR_FIRST.search(plain):
        return True
    if not _ASK.search(plain):
        return False
    return mentioned or bool(_WHERE.search(plain) or _ERROR.search(plain))


# ---------------------------------------------------------------------------
# Web side
# ---------------------------------------------------------------------------

def handle_message(event: dict) -> None:
    """Queue the log for the worker. Never raises."""
    try:
        from vomeos.worker import enqueue_event

        enqueue_event(HANDLER_KEY, {
            "channel": event.get("channel") or "",
            "ts": event.get("ts") or "",
            "thread_ts": event.get("thread_ts") or "",
            "user": event.get("user") or "",
            "text": event.get("text") or "",
        }, queue=QUEUE)
    except Exception as exc:
        print(f"[WORKFLOW LOG] could not queue {event.get('ts')}: {exc}")


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------

def handle(message: dict) -> dict:
    """One "log this". Never raises: a lost log is a thing Sam forgets."""
    channel = str(message.get("channel") or "")
    ts = str(message.get("ts") or "")
    if not channel or not ts or not enabled():
        return {"status": "skipped", "reason": "disabled or no message"}
    thread_ts = str(message.get("thread_ts") or "") or ts
    try:
        if not _claim(ts, channel, thread_ts, message.get("user") or ""):
            return {"status": "skipped", "reason": "already logged"}
        return _log(channel, ts, thread_ts, message)
    except Exception as exc:
        print(f"[WORKFLOW LOG] {ts} failed: {type(exc).__name__}: {exc}")
        _post(channel, thread_ts, f"I couldn't log this on ClickUp "
              f"({type(exc).__name__}). Try again in a minute.")
        return {"status": "error", "error": str(exc)}


def _log(channel: str, ts: str, thread_ts: str, message: dict) -> dict:
    from vomeos import run_agent

    who = LOGGERS.get(message.get("user"), "someone")
    request = _MENTION.sub("", str(message.get("text") or "")).strip()
    existing = _existing_task(channel, thread_ts, ts)
    thread = _read_thread(channel, thread_ts, ts)
    where = _channel_name(channel)
    result = run_agent(AGENT, context={
        "thread": thread or request,
        "channel": where,
        "logged_by": who,
        "request": request,
    }, subject_type="slack_thread", subject_id=f"{channel}:{thread_ts}")
    card = result.data if result.ok and result.data else _fallback(request)
    link = _permalink(channel, thread_ts)

    if existing:
        task_id, name = existing
        _comment(task_id, "\n".join([
            f"Logged again from Slack by {who}: \"{request}\"", "",
            str(card.get("summary") or ""), "",
            str(card.get("details") or ""),
        ]).strip())
        _attach_thread(task_id, thread)
        _record(ts, task_id, name)
        url = f"https://app.clickup.com/t/{task_id}"
        _mark_logged(channel, thread_ts)
        _post(channel, thread_ts, ":white_check_mark: Added to the existing "
              f"task in Agent Workflows: <{url}|{name}>")
        return {"status": "ok", "task": task_id, "action": "commented"}

    kind = str(card.get("type") or "question").lower()
    kind = kind if kind in TYPES else "question"
    error = bool(_ERROR.search(request))
    if error:
        kind = "bug"
    name = f"[{kind.capitalize()}] {card.get('title') or 'From Slack'}"[:180]
    description = "\n".join([
        f"**{who}'s note:** {request}", "",
        f"**Summary:** {card.get('summary') or ''}", "",
        "**Details**", str(card.get("details") or ""), "",
        f"**Next step:** {card.get('next_step') or ''}", "",
        "---",
        f"Area: {card.get('workflow') or 'other'} · Logged by {who} from "
        f"{where}" + (f" · [Slack thread]({link})" if link else ""),
    ])
    task = _clickup("POST", f"/list/{LIST}/task", {
        "name": name,
        "markdown_description": description,
        "status": "to do",
        "assignees": [SAM_CLICKUP],
    })
    task_id = str(task.get("id") or "")
    for tag in (kind, TAG_SLACK) + ((TAG_ERROR,) if error else ()):
        _tag(task_id, tag)
    _attach_thread(task_id, thread)
    _record(ts, task_id, name)
    url = task.get("url") or f"https://app.clickup.com/t/{task_id}"
    _mark_logged(channel, thread_ts)
    _post(channel, thread_ts, ":white_check_mark: Logged on ClickUp in Agent "
          f"Workflows: <{url}|{name}>")
    return {"status": "ok", "task": task_id, "type": kind}


def _attach_thread(task_id: str, thread: str) -> None:
    """The thread as it was when logged, on the task as comments: the
    summary is the agent's reading, this is what was actually said. The
    newest part is kept when it is too long."""
    if not task_id or not thread:
        return
    text = thread[-THREAD_MAX:]
    chunks = [text[i:i + THREAD_CHUNK]
              for i in range(0, len(text), THREAD_CHUNK)]
    for i, chunk in enumerate(chunks, 1):
        part = f" ({i}/{len(chunks)})" if len(chunks) > 1 else ""
        _comment(task_id, f"Slack thread when logged{part}:\n\n{chunk}")


def _fallback(request: str) -> dict:
    """When the logger returns nothing usable, still log it: a rough task
    beats a lost one."""
    return {"title": request[:80] or "From Slack", "type": "question",
            "workflow": "other",
            "summary": "Logged from Slack. The summary could not be written; "
                       "the thread link has everything.",
            "details": "", "next_step": "Read the Slack thread."}


# ---------------------------------------------------------------------------
# Logged by the agents themselves
# ---------------------------------------------------------------------------

def log_issue(area: str, title: str, summary: str, details: str,
              channel: str = "", thread_ts: str = "", next_step: str = "",
              kind: str = "bug") -> str:
    """A workflow went wrong: put it in Agent Workflows without anyone
    asking, and return the task's link ("" if it could not be logged).

    Sam, 2026-10-20: every time an agent fails to build something it must be
    logged, so he and Claude can review what the workflow needs. One task per
    Slack thread: a second failure in the same thread is a comment on the
    first. Never raises; a failed log must not hide the original failure.
    """
    if not enabled():
        return ""
    try:
        existing = (_existing_task(channel, thread_ts, "")
                    if channel and thread_ts else None)
        link = _permalink(channel, thread_ts) if channel and thread_ts else ""
        if existing:
            task_id, name = existing
            _reopen(task_id)
            _comment(task_id, "\n".join([
                f"Happened again ({area}): {summary}", "", details]).strip())
            if channel and thread_ts:
                _mark_logged(channel, thread_ts)
            return f"https://app.clickup.com/t/{task_id}"
        kind = kind if kind in TYPES else "bug"
        name = f"[{kind.capitalize()}] {title}"[:180]
        task = _clickup("POST", f"/list/{LIST}/task", {
            "name": name,
            "markdown_description": "\n".join([
                f"**Summary:** {summary}", "",
                "**Details**", details, "",
                f"**Next step:** {next_step or 'Review with Claude.'}", "",
                "---",
                f"Area: {area} · Logged automatically by the {area} workflow"
                + (f" · [Slack thread]({link})" if link else ""),
            ]),
            "status": "to do",
            "assignees": [SAM_CLICKUP],
        })
        task_id = str(task.get("id") or "")
        for tag in (kind, TAG_AUTO):
            _tag(task_id, tag)
        if store.ensure_table(LEDGER):
            store.execute(
                f"INSERT INTO {LEDGER} (trigger_ts, channel, thread_ts,"
                " logged_by, task_id, task_name) VALUES (:t, :c, :th,"
                " 'agent', :id, :n) ON CONFLICT (trigger_ts) DO NOTHING",
                {"t": f"auto:{channel}:{thread_ts}:{task_id}", "c": channel,
                 "th": thread_ts, "id": task_id, "n": name},
            )
        if channel and thread_ts:
            _mark_logged(channel, thread_ts)
        return task.get("url") or f"https://app.clickup.com/t/{task_id}"
    except Exception as exc:
        print(f"[WORKFLOW LOG] could not log {area} issue: {exc}")
        return ""


def close_thread(channel: str, thread_ts: str, note: str) -> list[str]:
    """Close what the agents logged from this thread, with `note` as a
    comment, and return the task ids closed.

    Sam, 2026-10-26: his `close` on a report means it no longer needs the
    agent (he had fixed security audit 1/31 by hand), but the failure the
    agent logged for it stayed open in Agent Workflows. Tasks Sam or Ron
    logged themselves stay open. Never raises.
    """
    if not (enabled() and channel and thread_ts):
        return []
    try:
        if not store.ensure_table(LEDGER):
            return []
        rows = store.query(
            f"SELECT DISTINCT task_id FROM {LEDGER} WHERE channel = :c AND"
            " thread_ts = :th AND logged_by = 'agent' AND task_id IS NOT NULL",
            {"c": channel, "th": thread_ts},
        )
    except Exception as exc:
        print(f"[WORKFLOW LOG] could not look up {thread_ts}: {exc}")
        return []
    closed = []
    for row in rows:
        task_id = str(row["task_id"])
        try:
            _clickup("PUT", f"/task/{task_id}", {"status": DONE})
        except Exception as exc:
            print(f"[WORKFLOW LOG] could not close {task_id}: {exc}")
            continue
        _comment(task_id, note)
        closed.append(task_id)
    return closed


def _reopen(task_id: str) -> None:
    """A task closed with its thread comes back when the thread fails
    again (Sam's `fix` after a `close`), so the new failure is seen."""
    try:
        task = _clickup("GET", f"/task/{task_id}")
        if (task.get("status") or {}).get("type") == "closed":
            _clickup("PUT", f"/task/{task_id}", {"status": "to do"})
    except Exception as exc:
        print(f"[WORKFLOW LOG] could not reopen {task_id}: {exc}")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _claim(ts: str, channel: str, thread_ts: str, user: str) -> bool:
    """True for the first worker to see this trigger message."""
    if not store.ensure_table(LEDGER):
        return True  # no database: logging twice beats not logging
    rows = store.query(
        f"INSERT INTO {LEDGER} (trigger_ts, channel, thread_ts, logged_by)"
        " VALUES (:t, :c, :th, :u) ON CONFLICT (trigger_ts) DO NOTHING"
        " RETURNING trigger_ts",
        {"t": ts, "c": channel, "th": thread_ts,
         "u": LOGGERS.get(user, user)},
        write=True,
    )
    return bool(rows)


def _existing_task(channel: str, thread_ts: str, ts: str):
    """(task id, name) already logged from this thread, if any."""
    rows = store.query(
        f"SELECT task_id, task_name FROM {LEDGER} WHERE channel = :c AND"
        " thread_ts = :th AND trigger_ts <> :t AND task_id IS NOT NULL"
        " ORDER BY created_at LIMIT 1",
        {"c": channel, "th": thread_ts, "t": ts},
    )
    return (rows[0]["task_id"], rows[0]["task_name"]) if rows else None


def _record(ts: str, task_id: str, name: str) -> None:
    store.execute(
        f"UPDATE {LEDGER} SET task_id = :id, task_name = :n"
        " WHERE trigger_ts = :t",
        {"id": task_id, "n": name, "t": ts},
    )


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _slack():
    from slack_sdk import WebClient

    return WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))


def _read_thread(channel: str, thread_ts: str, ts: str) -> str:
    """Every message in the thread, oldest first, the trigger last."""
    try:
        messages = _slack().conversations_replies(
            channel=channel, ts=thread_ts, limit=100).get("messages") or []
    except Exception as exc:
        print(f"[WORKFLOW LOG] thread {thread_ts} unreadable: {exc}")
        return ""
    names: dict[str, str] = {}
    lines = []
    for msg in messages:
        if msg.get("bot_id"):
            who = "agent"
        else:
            user = str(msg.get("user") or "")
            if user not in names:
                names[user] = _name(user)
            who = names[user]
        lines.append(f"{who}: {msg.get('text') or ''}")
    return "\n\n".join(lines)[-30000:]


def _name(user_id: str) -> str:
    if user_id in LOGGERS:
        return LOGGERS[user_id]
    try:
        profile = _slack().users_info(user=user_id)["user"]
        return profile.get("real_name") or profile.get("name") or user_id
    except Exception:
        return user_id or "someone"


def _channel_name(channel: str) -> str:
    try:
        info = _slack().conversations_info(channel=channel)["channel"]
        if info.get("is_im"):
            return "a DM"
        return f"#{info.get('name') or channel}"
    except Exception:
        return channel


def _permalink(channel: str, ts: str) -> str:
    try:
        return str(_slack().chat_getPermalink(
            channel=channel, message_ts=ts).get("permalink") or "")
    except Exception:
        return ""


def _mark_logged(channel: str, thread_ts: str) -> None:
    """A clipboard on the thread's first message: this thread is in Agent
    Workflows, visible without opening it (Sam, 2026-10-19)."""
    try:
        _slack().reactions_add(channel=channel, timestamp=thread_ts,
                               name=LOGGED_MARK)
    except Exception as exc:
        if "already_reacted" not in str(exc):
            print(f"[WORKFLOW LOG] could not mark {thread_ts}: {exc}")


def _post(channel: str, thread_ts: str, text: str) -> None:
    try:
        _slack().chat_postMessage(channel=channel, thread_ts=thread_ts,
                                  text=text, unfurl_links=False,
                                  unfurl_media=False)
    except Exception as exc:
        print(f"[WORKFLOW LOG] Slack post failed: {exc}")


# ---------------------------------------------------------------------------
# ClickUp
# ---------------------------------------------------------------------------

def _clickup(method: str, path: str, body: dict | None = None) -> dict:
    resp = httpx.request(
        method, f"https://api.clickup.com/api/v2{path}",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


_TAGS_READY: set[str] = set()


def _tag(task_id: str, tag: str) -> None:
    """Tag the task, creating the space tag the first time it is used."""
    if not task_id:
        return
    try:
        if tag not in _TAGS_READY:
            try:
                _clickup("POST", f"/space/{SPACE}/tag", {"tag": {"name": tag}})
            except Exception:
                pass  # already exists
            _TAGS_READY.add(tag)
        _clickup("POST", f"/task/{task_id}/tag/{tag.replace(' ', '%20')}")
    except Exception as exc:
        print(f"[WORKFLOW LOG] tag {tag!r} not set on {task_id}: {exc}")


def _comment(task_id: str, text: str) -> None:
    try:
        _clickup("POST", f"/task/{task_id}/comment",
                 {"comment_text": text, "notify_all": False})
    except Exception as exc:
        print(f"[WORKFLOW LOG] comment on {task_id} failed: {exc}")
