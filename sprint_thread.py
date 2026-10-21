"""
sprint_thread.py

The daily support sprint, run as one Slack thread in #support-sprints.

WHAT SAM SEES
-------------
Batches (sprint_batch.py, since 2026-10-18): one message holding everything
that needs him, every reply in full in its thread. Item numbers are
permanent, so a command works in any batch thread, and in the older day
threads. An incident breaks the schedule with an immediate mention. The day
thread with a card per ticket remains behind SPRINT_PER_TICKET_CARDS.

BATCH COMMANDS (fast path, never the model)
-------------------------------------------
    send ready [except 4 7]   every reply shown READY in THIS batch, still
                              exactly as shown, and nothing newer from the
                              customer
    next                      a batch of what this one did not show
    catch me up               a fresh batch of everything
    clear the queue           triage every untriaged ticket, 25 at a time
    hold 4 till fri           back on top of the first batch after Friday 8am

TALKING TO IT
-------------
The thread is a conversation, not a command line. Sam writes the way he
would to a colleague running the sprint with him:

    what's going on with 4?           context first: what the customer said,
                                      what we told them, what exists
    2 is too long, drop the timeline  the draft is rewritten and shown back
    ok send it                        sends the item being discussed
    give 5 to Sanjay                  Priority Queue task, notice, Zoho status
    log 8 / build 8 / park 6 / close 7

Every free-text message goes to the sprint partner agent, with today's
items, the conversation so far, and the full thread of any ticket it names.
Exact commands ("send 1-4", "hold 6", "edit 2: <text>") skip the model.

WHAT THE MODEL CANNOT DO ON ITS OWN
-----------------------------------
The partner proposes actions; this module decides whether they run.

  * A send runs only when Sam's own message says send, approve or go ahead,
    for an item his message names, or for the single item the conversation
    was just about. "send it" with nothing in focus sends nothing.
  * An edit and a send of the same item in one message: the edit is stored
    and shown, the send waits for Sam to read it (lessons log, 2026-09-28:
    composed-from-instruction replies are shown before sending).
  * Every draft the partner writes runs every outbound guard before it is
    stored, and a held draft cannot be sent.
  * Routing, logging, holding and closing each need their own word in Sam's
    message too.

Only the reviewers in SPRINT_REVIEWER_IDS (Sam) can action anything. "send"
emails the customer from Zoho and is logged in #vome-agent-log.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import sprint_store
from vomeos import store

CHANNEL = os.environ.get("SLACK_CHANNEL_SUPPORT_SPRINTS", "C0CA1QB2XSQ")
AGENT_LOG = os.environ.get("SLACK_CHANNEL_VOME_AGENT_LOG", "C0AMGELBDB2")

ENG_BACKEND = os.environ.get("SLACK_CHANNEL_ENG_BACKEND", "C0AQ5U0GXEF")
ENG_FRONTEND = os.environ.get("SLACK_CHANNEL_ENG_FRONTEND", "C0AQ5TWQPTM")

COMMAND_KEY = "customer_success.sprint_command"

PRIORITY_QUEUE = "901113386257"
RAW_INTAKE = "901113386484"
SAM_ACCEPTED = "901115573620"

FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"
FIELD_SOURCE = "857e1262-cb5c-4c22-b8da-770a8fcfa82e"
SOURCE_ZOHO_TICKET = "9b678f29-3b49-4842-9305-ada436cfc0b3"

ENGINEERS = {
    # name -> (ClickUp id, Slack id, team, channel)
    "onlyg": (49257687, "U0APZ7JUHRD", "backend", ENG_BACKEND),
    "sanjay": (4434086, "U0AQJ8YJF6Y", "frontend", ENG_FRONTEND),
}
ENGINEER_ALIASES = {"backend": "onlyg", "frontend": "sanjay",
                    "onlygod": "onlyg"}

COMMANDS_TABLE = "vomeos_sprint_commands"
INCIDENTS_TABLE = "vomeos_sprint_incidents"

store.register_table(
    COMMANDS_TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {COMMANDS_TABLE} (
        message_ts  VARCHAR PRIMARY KEY,
        user_id     VARCHAR,
        text        TEXT,
        result      TEXT,
        created_at  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)
store.register_table(
    INCIDENTS_TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {INCIDENTS_TABLE} (
        incident_key VARCHAR NOT NULL,
        day          DATE NOT NULL,
        announced_at TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (incident_key, day)
    )
    """,
)


def _tables() -> bool:
    """Create this module's own tables on first use."""
    return (store.ensure_table(COMMANDS_TABLE)
            and store.ensure_table(INCIDENTS_TABLE))


def reviewers() -> tuple[str, ...]:
    raw = os.environ.get("SPRINT_REVIEWER_IDS", "U01B9FQNSBU")
    return tuple(r.strip() for r in raw.split(",") if r.strip())


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _slack():
    from slack_sdk import WebClient

    return WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))


def _post(text: str, thread_ts: str = "", channel: str = "",
          broadcast: bool = False) -> str:
    """Post and return the message ts, or "" on failure."""
    try:
        kwargs = {"channel": channel or CHANNEL, "text": text,
                  "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
            if broadcast:
                kwargs["reply_broadcast"] = True
        resp = _slack().chat_postMessage(**kwargs)
        return str(resp.get("ts") or "")
    except Exception as exc:
        print(f"[SPRINT] Slack post failed: {exc}")
        return ""


def day_label(day) -> str:
    """"Friday, Oct 17". No platform-specific strftime flags."""
    return f"{day:%A}, {day:%b} {day.day}"


def _clock(moment: datetime) -> str:
    """"1:00 PM"."""
    return f"{moment.hour % 12 or 12}:{moment:%M} {moment:%p}"


def ensure_thread(day=None) -> str:
    """The thread_ts for the day, creating the parent message if needed.

    Returns "" when the day cannot be remembered. Posting a header it cannot
    store means a new header on every post: production, 2026-10-17, four
    "Support sprint, Saturday" threads in 13 minutes before the tables
    existed. Callers post to the channel instead, which is noisy but honest.
    """
    day = day or sprint_store.today()
    if not sprint_store.ready():
        return ""
    row = sprint_store.get_day(day)
    if row.get("thread_ts"):
        return row["thread_ts"]
    text = (
        f":clipboard: *Support sprint, {day_label(day)}*\n"
        "Cards arrive here as tickets are triaged; the full list posts at "
        "10, 1, 4 and 7. Talk to me in this thread: ask about any ticket, "
        "push back on a draft, or tell me what to do (\"send 2\", \"give 5 "
        "to Sanjay\", \"park 6\"). Nothing goes to a customer until you say "
        "send."
    )
    ts = _post(text)
    if ts:
        sprint_store.set_day_thread(day, CHANNEL, ts)
    return ts


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def _zoho_link(item: dict) -> str:
    import zoho_actions

    return (f"<{zoho_actions.ticket_url(item.get('ticket_id'))}|"
            f"#{item.get('ticket_number')}>")


def _age(item: dict) -> str:
    arrived = item.get("arrived_at")
    if not isinstance(arrived, datetime):
        return ""
    hours = (sprint_store._now() - arrived).total_seconds() / 3600
    if hours < 1:
        return "new"
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def render_card(item: dict, release: dict | None = None) -> str:
    card = item.get("card") or {}
    rcpt = item.get("recipients") or {}
    n = item.get("number")
    flags = [f"`{card.get('category') or item.get('category') or '?'}`",
             str(card.get("confidence") or "?")]
    if item.get("important"):
        flags.append("*IMPORTANT*")
    lines = [
        f"*{n}.* {_zoho_link(item)} {item.get('account') or ''} · "
        + " · ".join(flags),
        f"*Waiting on:* {card.get('live_question') or item.get('summary')}",
        f"*Situation:* {card.get('situation') or ''}",
    ]
    if card.get("existing_work"):
        lines.append(f"*Existing:* {card['existing_work']}")
    code = card.get("code_check") or {}
    checked = code.get("verdict") not in (None, "", "not_checked") \
        if isinstance(code, dict) else False
    if checked:
        lines.append(
            f":mag: *Code check:* `{code.get('verdict')}` "
            f"({code.get('confidence') or '?'}) {code.get('finding') or ''}"
            + (f" `{code['where']}`" if code.get("where") else "")
        )
    if item.get("fix_pr"):
        lines.append(f":hammer_and_wrench: *Proposed fix:* {item['fix_pr']}")
    if release and (release.get("task_id")
                    or "could not be read" in (release.get("text") or "")):
        lines.append(f"*Release:* {release.get('text')}")
    if card.get("blocking_claims"):
        lines.append(f":warning: *Unbacked claim in thread:* "
                     f"{card['blocking_claims']}")
    if card.get("why_human"):
        lines.append(f":bust_in_silhouette: *Needs you:* {card['why_human']}")
    if card.get("recommended_action"):
        lines.append(f"*Recommended:* {card['recommended_action']}")

    draft = (item.get("draft") or "").strip()
    status = item.get("draft_status") or "none"
    if draft and status == "ok":
        cc = ", ".join(rcpt.get("cc") or [])
        lines.append(f"*Draft* to {rcpt.get('to') or '?'}"
                     + (f", cc {cc}" if cc else "") + ":")
        lines.append("```" + draft + "```")
        lines.append(f"`send {n}` · `edit {n}: ...` · `redraft {n}: ...` · "
                     f"`hold {n}` · `done {n}`")
    elif draft and status == "blocked":
        lines.append(f":no_entry: *Draft held by a guard:* "
                     f"{item.get('draft_why')}")
        lines.append("```" + draft + "```")
        lines.append(f"`edit {n}: ...` · `redraft {n}: ...` · `done {n}`")
    else:
        lines.append(f"_No draft ({status}). Decide: `route {n} to OnlyG`, "
                     f"`log {n}`, `build {n}`, `hold {n}` or `done {n}`._")
    return "\n".join(lines)


def post_card(item: dict, release: dict | None = None) -> str:
    """A card per ticket, only behind SPRINT_PER_TICKET_CARDS. With batches
    the item waits for the next batch instead of trickling into a thread."""
    import sprint_batch

    if not sprint_batch.cards_enabled():
        return ""
    thread = ensure_thread()
    if not thread:
        return ""
    ts = _post(render_card(item, release), thread_ts=thread)
    if ts:
        sprint_store.set_card_ts(item["id"], ts)
    return ts


def alert_sam(key: str, text: str) -> bool:
    """Interrupt Sam in #support-sprints, once a day per key.

    Sam, 2026-10-19: the channel is for urgent work only. Everything else
    is already with someone (the agent, an engineer) and waits for his
    sprint in Claude.ai, so a message here means "act now": an important
    client is blocked and an engineer has it, or an agent fix needs his
    merge. The "possible incident" posts it replaces paged on resolved
    tickets and single customers and never said what to do.
    """
    day = sprint_store.today()
    _tables()
    rows = store.query(
        f"INSERT INTO {INCIDENTS_TABLE} (incident_key, day) VALUES (:k, :d)"
        " ON CONFLICT DO NOTHING RETURNING incident_key",
        {"k": key, "d": day}, write=True,
    )
    if not rows and store.usable():
        return False
    mention = " ".join(f"<@{r}>" for r in reviewers())
    return bool(_post(f":rotating_light: {mention} {text}"))


# ---------------------------------------------------------------------------
# Scheduled update
# ---------------------------------------------------------------------------

def _release_lines() -> list[str]:
    try:
        import release_watcher as rw

        tasks = rw._list_on_dev_tasks()
    except Exception as exc:
        return [f"_Agent Fixes could not be read: {exc}_"]
    lines = []
    for task in tasks:
        if task.get("parent"):
            continue
        commits = rw._field(task, rw.FIELD_FIX_COMMIT).get("value") or ""
        if not commits:
            continue
        check = rw._release_check_value(task) or "waiting"
        clients = len(rw._children(task["id"], tasks))
        lines.append(f"  - <{task.get('url')}|{task.get('name')}> · "
                     f"{check} · {clients} client(s)")
    return lines


def render_update(items: list[dict], label: str) -> str:
    needs = [i for i in items if i.get("status") == sprint_store.OPEN]
    held = [i for i in items if i.get("status") == sprint_store.HELD]
    fixing = [i for i in items if i.get("status") == sprint_store.FIXING]
    done = [i for i in items if i.get("status") not in sprint_store.CARRIES]

    def line(i: dict) -> str:
        draft = {"ok": "draft ready", "blocked": "draft held",
                 "error": "draft failed"}.get(i.get("draft_status") or "",
                                              "decide")
        flag = " *IMPORTANT*" if i.get("important") else ""
        return (f"*{i.get('number')}.* {_zoho_link(i)} "
                f"{i.get('account') or ''}{flag} · "
                f"`{i.get('category') or '?'}` · {draft} · {_age(i)}")

    out = [f":spiral_calendar_pad: *{label}* · {len(needs)} need you · "
           f"{len(held)} held · {len(fixing)} agent fixing · "
           f"{len(done)} done today"]
    if needs:
        out.append("*Needs you*")
        out += [line(i) for i in needs]
    if held:
        out.append("*Held*")
        out += [line(i) for i in held]
    if fixing:
        import sprint_batch

        out.append("*Agent is fixing*")
        out += [f"*{i.get('number')}.* {_zoho_link(i)} "
                f"{i.get('account') or ''} · {sprint_batch.fix_stage(i)}"
                for i in fixing]
    release = _release_lines()
    if release:
        out.append("*Waiting on a release*")
        out += release
    if not needs and not held:
        out.append("Nothing waiting on you.")
    return "\n".join(out)


def run_sprint_update(backfill: int | None = None) -> dict:
    """The 10 / 1 / 4 / 7 update. Safe to run on demand."""
    if not sprint_store.ready():
        return {"status": "no database"}
    day = sprint_store.today()
    row = sprint_store.get_day(day)
    last = row.get("last_update_at")
    if isinstance(last, datetime) and (
        sprint_store._now() - last < timedelta(minutes=20)
    ):
        return {"status": "skipped", "reason": "updated in the last 20 min"}

    moved = sprint_store.carry_forward() if not row.get("updates") else 0
    thread = ensure_thread(day)

    # Tickets already open before shadow triage existed, or whose webhook
    # was missed. A few per update so the backlog clears without one update
    # costing fifty senior calls.
    backfilled = 0
    limit = int(os.environ.get("SPRINT_BACKFILL_PER_UPDATE", "8")
                if backfill is None else backfill)
    if limit:
        try:
            import shadow_triage
            import sprint

            for ticket in sprint.fetch_open_tickets(80):
                if backfilled >= limit:
                    break
                if sprint_store.by_ticket(str(ticket.get("id"))):
                    continue
                outcome = shadow_triage._handle(str(ticket["id"]), "backfill")
                if outcome.get("status") == "ok":
                    backfilled += 1
        except Exception as exc:
            print(f"[SPRINT] backfill stopped: {exc}")

    hour = _clock(datetime.now(sprint_store.ZONE))
    text = render_update(sprint_store.items_for_day(day), f"Update {hour}")
    ts = _post(text, thread_ts=thread)
    sprint_store.mark_update(day)
    return {"status": "ok" if ts else "post failed", "carried": moved,
            "backfilled": backfilled}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_RANGE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def _numbers(spec: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+(?:and\s+)?", spec.strip()):
        if not part:
            continue
        match = _RANGE.match(part)
        if match:
            a, b = int(match.group(1)), int(match.group(2))
            if 0 < b - a < 50:
                out += list(range(a, b + 1))
        elif part.isdigit():
            out.append(int(part))
    return list(dict.fromkeys(out))


def parse(text: str) -> list[dict]:
    """Commands in a reply. `edit` and `redraft` take the rest of the message.

    Anything that is not a command is ignored, so Sam can chat in the thread
    without triggering anything.
    """
    commands: list[dict] = []
    lines = (text or "").strip().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip().strip("`")
        index += 1
        match = re.match(r"^(edit|redraft)\s+(\d+)\s*:\s*(.*)$", line,
                         re.IGNORECASE | re.DOTALL)
        if match:
            rest = "\n".join([match.group(3)] + lines[index:]).strip()
            commands.append({"verb": match.group(1).lower(),
                             "numbers": [int(match.group(2))], "arg": rest})
            break
        match = re.match(r"^(log|build)\s+(\d+)\s+(?:to\s+)?(new|\w{6,12})"
                         r"\s*$", line, re.IGNORECASE)
        if match:
            commands.append({"verb": match.group(1).lower(),
                             "numbers": [int(match.group(2))],
                             "arg": match.group(3)})
            continue
        match = re.match(
            r"^hold\s+([\d,\s\-and]+?)\s+(?:till|until|to)\s+(\w+)\s*$",
            line, re.IGNORECASE)
        if match:
            commands.append({"verb": "hold",
                             "numbers": _numbers(match.group(1)),
                             "arg": match.group(2).lower()})
            continue
        match = re.match(r"^route\s+([\d,\s\-and]+?)\s+to\s+(\w+)\s*$", line,
                         re.IGNORECASE)
        if match:
            commands.append({"verb": "route",
                             "numbers": _numbers(match.group(1)),
                             "arg": match.group(2).lower()})
            continue
        match = re.match(
            r"^(send|hold|done|skip|log|build|fix|close)\s+([\d,\s\-and]+)$",
            line, re.IGNORECASE,
        )
        if match:
            verb = match.group(1).lower()
            commands.append({"verb": "done" if verb == "skip" else verb,
                             "numbers": _numbers(match.group(2)), "arg": ""})
    return commands


def handle_reply(event: dict) -> bool:
    """Web side: queue Sam's message in the sprint thread. Never raises.

    Every message from a reviewer is queued, not only commands, because the
    thread is a conversation. Anyone else's chatter is left alone.
    """
    if event.get("user") not in reviewers():
        return False
    if not (event.get("text") or "").strip():
        return False
    try:
        from vomeos.worker import enqueue_event

        enqueue_event(COMMAND_KEY, {
            "user": event.get("user") or "",
            "text": event.get("text") or "",
            "thread_ts": event.get("thread_ts") or "",
            "ts": event.get("ts") or "",
        }, queue="support")
        return True
    except Exception as exc:
        print(f"[SPRINT] could not queue message: {exc}")
        return False


def _day_for_thread(thread_ts: str):
    rows = store.query(
        f"SELECT day FROM {sprint_store.DAYS} WHERE thread_ts = :t",
        {"t": thread_ts},
    )
    return rows[0]["day"] if rows else None


def parse_strict(text: str) -> list[dict] | None:
    """Commands, only when EVERY line of the message is one. Else None.

    "send 1-4" or "hold 6\ndone 7" skip the model. Anything with a question
    or a sentence in it goes to the partner, so "send 3 and tell me about 5"
    is not half-handled by the fast path.
    """
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return None
    commands = parse(text)
    if not commands:
        return None
    if commands[-1]["verb"] in ("edit", "redraft"):
        return commands
    return commands if len(commands) == len(lines) else None


def handle_command(message: dict) -> dict:
    """Worker side: one message from Sam. Idempotent on the Slack ts."""
    thread = message.get("thread_ts") or ""
    if message.get("user") not in reviewers():
        return {"status": "refused"}
    _tables()
    claimed = store.query(
        f"INSERT INTO {COMMANDS_TABLE} (message_ts, user_id, text)"
        " VALUES (:ts, :u, :t) ON CONFLICT DO NOTHING RETURNING message_ts",
        {"ts": message.get("ts"), "u": message.get("user"),
         "t": message.get("text")}, write=True,
    )
    if not claimed and store.usable():
        return {"status": "duplicate"}
    batch = sprint_store.batch_for_thread(thread) \
        if sprint_store.batches_ready() else {}
    day = _day_for_thread(thread)
    if day is None and not batch:
        return {"status": "not a sprint thread"}
    day = day or sprint_store.today()

    text = message.get("text") or ""
    _record_turn(thread, message.get("ts") or "", "sam", text, None)

    batch_reply = _batch_command(text, thread, batch,
                                 message.get("ts") or "")
    lesson_reply = _lesson_command(text) if batch_reply is None else None
    commands = parse_strict(text) \
        if batch_reply is None and lesson_reply is None else None
    if batch_reply is not None:
        reply, focus, outcome = batch_reply, None, {
            "status": "ok", "results": [], "path": "batch"}
    elif lesson_reply is not None:
        reply, focus, outcome = lesson_reply, None, {
            "status": "ok", "results": [], "path": "lessons"}
    elif commands is not None:
        results = _run_commands(commands, day)
        reply = "\n".join(results) or "Nothing to do."
        focus = (commands[0]["numbers"] or [None])[0]
        outcome = {"status": "ok", "results": results, "path": "commands"}
    else:
        reply, focus, results = converse(text, day, thread)
        outcome = {"status": "ok", "results": results, "path": "partner"}

    ts = _post(reply, thread_ts=thread)
    _record_turn(thread, ts, "agent", reply, focus)
    store.execute(
        f"UPDATE {COMMANDS_TABLE} SET result = :r WHERE message_ts = :ts",
        {"r": reply[:4000], "ts": message.get("ts")},
    )
    return outcome


# ---------------------------------------------------------------------------
# Learning from Sam (vomeos/lessons.py)
# ---------------------------------------------------------------------------

DRAFTER = "customer_success.reply_drafter"
_LEARNABLE = {"customer_success.reply_drafter",
              "customer_success.triage_director",
              "customer_success.sprint_partner",
              "customer_success.incident_matcher", "*"}


def _learn(item: dict, before: str, after: str, sam_said: str,
           agent: str = DRAFTER) -> str:
    """Ask the lesson keeper whether this correction is a general rule.

    Returns the line to show Sam ("Learned ... say forget 7"), or "" when it
    was a one-off fix for this ticket. Never raises: a lesson missed costs
    one more correction; a crash here would cost the edit Sam just made.
    """
    if not after or before.strip() == after.strip():
        return ""
    try:
        from vomeos import lessons, run_agent

        existing = lessons.active_for(agent, limit=40)
        result = run_agent(
            "customer_success.lesson_keeper",
            context={
                "agent": agent,
                "category": item.get("category") or "",
                "before": before,
                "after": after,
                "sam_said": sam_said or "(nothing beyond the edit)",
                "existing_lessons": "\n".join(
                    f"- [L{r['id']}] {r['rule']}" for r in existing)
                or "(none yet)",
            },
            subject_type="sprint_item", subject_id=str(item.get("id")),
        )
        data = result.data if result.ok else None
        if not data or not data.get("general") or \
                data.get("confidence") == "low":
            return ""
        if data.get("duplicate_of"):
            return (f"\n:brain: Already a lesson "
                    f"({data['duplicate_of']}); applied again.")
        target = data.get("applies_to") or agent
        target = target if target in _LEARNABLE else agent
        lesson_id = lessons.add(
            target, data.get("rule") or "", "slack_edit",
            evidence=f"Before:\n{before}\n\nAfter:\n{after}\n\n"
                     f"Sam: {sam_said}",
            ticket=str(item.get("ticket_number") or ""),
            created_by="sprint_thread",
        )
        if not lesson_id:
            return ""
        return (f"\n:brain: Learned *L{lesson_id}* ({_short(target)}): "
                f"{data.get('rule')} Say `forget {lesson_id}` to undo.")
    except Exception as exc:
        print(f"[SPRINT] lesson keeper failed: {exc}")
        return ""


def _short(agent: str) -> str:
    return "every agent" if agent == "*" else agent.split(".")[-1].replace(
        "_", " ")


def _record_partner_lessons(raw, sam_text: str) -> list[str]:
    """General feedback Sam gave in conversation, as the partner heard it."""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    try:
        from vomeos import lessons
    except Exception:
        return out
    for entry in raw[:3]:
        if not isinstance(entry, dict):
            continue
        agent = str(entry.get("agent") or "")
        rule = str(entry.get("rule") or "").strip()
        if agent not in _LEARNABLE or not rule:
            continue
        lesson_id = lessons.add(agent, rule, "slack_feedback",
                                evidence=f"Sam: {sam_text}",
                                created_by="sprint_partner")
        if lesson_id:
            out.append(f":brain: Learned *L{lesson_id}* ({_short(agent)}): "
                       f"{rule} Say `forget {lesson_id}` to undo.")
    return out


_FORGET = re.compile(r"^\s*(forget|unlearn)\s+L?(\d+)\s*$", re.I)
_LIST = re.compile(r"^\s*(lessons|what have you learned\??)\s*$", re.I)


def _lesson_command(text: str) -> str | None:
    """`forget 7` and `lessons`. None when the message is neither."""
    from vomeos import lessons

    match = _FORGET.match(text or "")
    if match:
        lesson_id = int(match.group(2))
        row = lessons.get(lesson_id)
        if not row:
            return f"There is no lesson L{lesson_id}."
        lessons.retire(lesson_id, "Sam said forget")
        return f":wastebasket: Forgot L{lesson_id}: {row.get('rule')}"
    if _LIST.match(text or ""):
        rows = lessons.all_active()
        if not rows:
            return "No live lessons yet."
        lines = [f":brain: *{len(rows)} live lessons* (newest first):"]
        lines += [f"*L{r['id']}* ({_short(r['agent'])}) {r['rule']}"
                  for r in rows[:30]]
        lines.append("Say `forget N` to drop one.")
        return "\n".join(lines)
    return None


_SEND_READY = re.compile(
    r"^\s*send\s+(?:all\s+)?(?:the\s+)?ready(?:\s+ones)?"
    r"(?:\s*,?\s*(?:except|but\s+not|but)\s+(?P<ex>[\d,\s\-and]+))?"
    r"\s*[.!]?\s*$", re.I)
_CLOSE_QUIET = re.compile(
    r"^\s*close\s+(?:them\s+|all\s+)?quiet(?:ly)?"
    r"(?:\s*,?\s*(?:except|but\s+not|but)\s+(?P<ex>[\d,\s\-and]+))?"
    r"\s*[.!]?\s*$", re.I)
_NEXT = re.compile(r"^\s*(next|next batch|more)\s*[.!?]?\s*$", re.I)
_CATCH_UP = re.compile(
    r"^\s*(?:please\s+)?(catch me up|batch|new batch|fresh batch|refresh)"
    r"(?:\s+(?:now|please))?\s*[.!?]?\s*$", re.I)
# "clear the queue fresh" triages every open ticket again under today's
# rules, not only the untriaged ones.
_CLEAR = re.compile(
    r"^\s*clear(?:\s+the)?(?:\s+(?:queue|pipeline|backlog))?"
    r"(?P<fresh>\s+(?:fresh|again|from scratch|all))?"
    r"(?:\s+(?:now|please))?\s*[.!]?\s*$", re.I)


def _batch_command(text: str, thread: str, batch: dict,
                   message_ts: str) -> str | None:
    """`send ready`, `next`, `catch me up`, `clear the queue`. None when
    the message is none of them."""
    import sprint_batch

    match = _SEND_READY.match(text or "")
    if match:
        if not batch:
            return ("Say `send ready` in a batch's thread, so I send exactly "
                    "the replies that batch showed you.")
        return send_ready(batch, set(_numbers(match.group("ex") or "")))
    match = _CLOSE_QUIET.match(text or "")
    if match:
        if not batch:
            return ("Say `close quiet` in a batch's thread, so I close "
                    "exactly what that batch showed you.")
        return close_quiet(batch, set(_numbers(match.group("ex") or "")))
    if _NEXT.match(text or ""):
        shown = {int(k) for k in (batch.get("shown") or {})}
        result = sprint_batch.run_batch("next", exclude=shown,
                                        reply_thread="")
        return _batch_result(result)
    if _CATCH_UP.match(text or ""):
        return _batch_result(sprint_batch.run_batch("catch_up"))
    match = _CLEAR.match(text or "")
    if match:
        return sprint_batch.start_clear(message_ts, thread,
                                        fresh=bool(match.group("fresh")))
    return None


def _batch_result(result: dict) -> str:
    if result.get("status") != "ok":
        return f"No batch posted ({result.get('status')})."
    more = result.get("more") or 0
    return (f"Posted batch {result.get('batch')} in the channel: "
            f"{result.get('shown')} need you"
            + (f", {more} more after those." if more else "."))


def send_ready(batch: dict, except_numbers: set) -> str:
    """Send every reply this batch showed as ready, exactly as shown.

    A reply that changed after the batch was posted (an edit, a redraft, a
    new customer message) is not sent: Sam approved what he read, not what
    it became. _send still checks for a newer customer message.
    """
    ready = batch.get("ready") or {}
    if not ready:
        return "This batch had no replies ready to send."
    results = []
    for item_id, shown_hash in ready.items():
        item = sprint_store.get_item(int(item_id))
        if not item:
            continue
        n = item.get("number")
        if n in except_numbers:
            results.append(f"*{n}.* left out, as you asked.")
            continue
        import sprint_batch

        if sprint_batch.draft_hash(item.get("draft")) != shown_hash:
            results.append(f"*{n}.* changed since this batch, so not sent. "
                           f"Read it and say `send {n}`.")
            continue
        try:
            results.append(_send(item))
        except Exception as exc:
            results.append(f"*{n}.* failed: {exc}")
    return "\n".join(results) or "Nothing to send."


def _wake_at(word: str):
    """8am Montreal on the day Sam named, as naive UTC. None if unknown.

    No day: the next weekday morning.
    """
    names = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
             "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
             "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
             "sun": 6, "sunday": 6}
    today = datetime.now(sprint_store.ZONE)
    word = (word or "").strip().lower()
    if not word:
        days = 1
        while (today + timedelta(days=days)).weekday() >= 5:
            days += 1
    elif word == "tomorrow":
        days = 1
    elif word in names:
        days = (names[word] - today.weekday()) % 7 or 7
    else:
        return None
    wake = (today + timedelta(days=days)).replace(hour=8, minute=0,
                                                  second=0, microsecond=0)
    return wake.astimezone(timezone.utc).replace(tzinfo=None)


def _run_commands(commands: list[dict], day) -> list[str]:
    results = []
    for command in commands:
        for number in command["numbers"]:
            item = sprint_store.by_number(number)
            if not item:
                results.append(f"*{number}.* no such item.")
                continue
            try:
                results.append(_execute(command, item))
            except Exception as exc:
                results.append(f"*{number}.* failed: {exc}")
    return results


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

TURNS_TABLE = "vomeos_sprint_turns"

store.register_table(
    TURNS_TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TURNS_TABLE} (
        id         SERIAL PRIMARY KEY,
        thread_ts  VARCHAR NOT NULL,
        message_ts VARCHAR,
        role       VARCHAR NOT NULL,
        text       TEXT,
        focus      INTEGER,
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    (f"CREATE INDEX IF NOT EXISTS {TURNS_TABLE}_thread_idx "
     f"ON {TURNS_TABLE} (thread_ts, id)",),
)

# Words in SAM'S OWN message that each action needs. The partner proposes;
# these decide. A send is the one that can reach a customer, so it also
# needs its item named or in focus (see _allowed_sends).
_GATES = {
    "send": re.compile(
        r"\b(send|approve[ds]?|go ahead|ship it|good to go|fire it)\b", re.I),
    "hold": re.compile(r"\b(hold|park|later|wait|tomorrow|monday|"
                       r"leave it)\b", re.I),
    "done": re.compile(r"\b(done|close|closed|skip|drop|ignore|resolved|"
                       r"nothing (more|to do))\b", re.I),
    "route": re.compile(r"\b(onlyg|only g|sanjay|backend|frontend)\b", re.I),
    "log": re.compile(r"\b(log|feature|backlog|intake)\b", re.I),
    "build": re.compile(r"\bbuild\b", re.I),
    # Opens a fix PR on Sam's word (a design question, or a fix the
    # agent did not make on its own). Never merges.
    "fix": re.compile(
        r"\b(fix|make the (pr|change|update)|open (a|the) pr)\b", re.I),
    # Closes the Zoho ticket with no email. Its own word, not "done".
    "close": re.compile(r"\bclose\b", re.I),
}
_FREE = ("edit", "redraft", "retriage")
_TICKET = re.compile(r"#?\b(\d{4,6})\b")
_ITEM = re.compile(r"(?<![\d#:.])\b(\d{1,2})\b(?![\d:.])")


def _record_turn(thread: str, ts: str, role: str, text: str, focus) -> None:
    store.ensure_table(TURNS_TABLE)
    store.execute(
        f"INSERT INTO {TURNS_TABLE} (thread_ts, message_ts, role, text,"
        " focus) VALUES (:th, :ts, :r, :t, :f)",
        {"th": thread, "ts": ts, "r": role, "t": (text or "")[:6000],
         "f": focus},
    )


def _turns(thread: str, limit: int = 16) -> list[dict]:
    store.ensure_table(TURNS_TABLE)
    rows = store.query(
        f"SELECT role, text, focus, created_at FROM {TURNS_TABLE}"
        " WHERE thread_ts = :th ORDER BY id DESC LIMIT :n",
        {"th": thread, "n": int(limit)},
    )
    return list(reversed(rows))


def _last_focus(turns: list[dict]):
    for turn in reversed(turns):
        if turn.get("role") == "agent" and turn.get("focus"):
            return int(turn["focus"])
    return None


def referenced_numbers(text: str, items: list[dict]) -> list[int]:
    """Item numbers Sam's message names, directly or by Zoho ticket number."""
    by_ticket = {str(i.get("ticket_number")): i.get("number") for i in items}
    numbers = set(i.get("number") for i in items)
    found: list[int] = []
    for ticket in _TICKET.findall(text or ""):
        if ticket in by_ticket and by_ticket[ticket] not in found:
            found.append(by_ticket[ticket])
    for raw in _ITEM.findall(text or ""):
        n = int(raw)
        if n in numbers and n not in found:
            found.append(n)
    return found


def _allowed_sends(text: str, items: list[dict], turns: list[dict]):
    """Which items Sam's message can send. Empty when it cannot send any."""
    if not _GATES["send"].search(text or ""):
        return set()
    named = referenced_numbers(text, items)
    if named:
        return set(named)
    focus = _last_focus(turns[:-1])  # the reply before Sam's message
    return {focus} if focus else set()


def _items_block(items: list[dict]) -> str:
    if not items:
        return "(no items in today's thread yet)"
    blocks = []
    for i in items:
        card = i.get("card") or {}
        rcpt = i.get("recipients") or {}
        cc = ", ".join(rcpt.get("cc") or [])
        lines = [
            f"{i.get('number')}. #{i.get('ticket_number')} "
            f"{i.get('account') or ''} | {i.get('category') or '?'} | "
            f"{i.get('status')}",
            f"   Waiting on: {card.get('live_question') or i.get('summary')}",
            f"   Situation: {card.get('situation') or ''}",
            f"   Existing work: {card.get('existing_work') or 'none found'}",
        ]
        if card.get("blocking_claims"):
            lines.append(f"   Unbacked claim in thread: "
                         f"{card['blocking_claims']}")
        code = card.get("code_check") or {}
        if isinstance(code, dict) and code.get("verdict"):
            lines.append(f"   Code check: {code.get('verdict')} "
                         f"({code.get('confidence')}): {code.get('finding')}"
                         f" [{code.get('where') or ''}]")
        if i.get("fix_pr"):
            lines.append(f"   Proposed fix PR: {i['fix_pr']}")
        if card.get("why_human"):
            lines.append(f"   Needs a person because: {card['why_human']}")
        draft = (i.get("draft") or "").strip()
        if draft:
            lines.append(f"   Draft ({i.get('draft_status')}) to "
                         f"{rcpt.get('to') or '?'}"
                         + (f", cc {cc}" if cc else "") + f": {draft}")
            if i.get("draft_why"):
                lines.append(f"   Guard notes: {i['draft_why']}")
        else:
            lines.append("   Draft: none")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _conversation_block(turns: list[dict]) -> str:
    if len(turns) <= 1:
        return "(no earlier messages in this thread today)"
    lines = []
    for t in turns[:-1]:
        when = t.get("created_at")
        stamp = f"{when:%H:%M}" if isinstance(when, datetime) else ""
        who = "Sam" if t.get("role") == "sam" else "Agent"
        lines.append(f"[{stamp}] {who}: {(t.get('text') or '')[:1200]}")
    return "\n".join(lines)


def _ticket_text(item: dict | None, ticket_number: str = "") -> str:
    """The full customer thread for one ticket, for the partner to read."""
    try:
        from agent import _format_conversations, fetch_ticket_conversations

        ticket_id = (item or {}).get("ticket_id")
        if not ticket_id and ticket_number:
            import sprint

            row = sprint.resolve_ticket(ticket_number) or {}
            ticket_id = row.get("id")
        if not ticket_id:
            return f"#{ticket_number}: not found in Zoho."
        text = _format_conversations(fetch_ticket_conversations(ticket_id))
        number = (item or {}).get("ticket_number") or ticket_number
        return f"#{number} full thread:\n{(text or '')[:6000]}"
    except Exception as exc:
        return f"#{ticket_number}: thread could not be read ({exc})."


def _referenced_block(text: str, items: list[dict], focus) -> str:
    by_number = {i.get("number"): i for i in items}
    wanted = referenced_numbers(text, items) or ([focus] if focus else [])
    blocks = [_ticket_text(by_number[n]) for n in wanted[:3]
              if n in by_number]
    known = {str(i.get("ticket_number")) for i in items}
    for ticket in _TICKET.findall(text or ""):
        if ticket not in known and len(blocks) < 3:
            blocks.append(_ticket_text(None, ticket))
    return "\n\n".join(blocks) or "(none)"


def _check_draft(item: dict, text: str,
                 sam_wrote: bool = False) -> tuple[str, str]:
    """Every outbound guard on a draft the partner wrote, or on Sam's own
    edit when `sam_wrote`. (status, why)."""
    from vomeos.guards import run_guards

    import shadow_triage

    release = shadow_triage.release_state(item.get("ticket_number") or "",
                                          item.get("ticket_id") or "")
    ctx = {"agent": "customer_success.sprint_partner",
           "contact_name": (item.get("recipients") or {}).get("first_name")
           or "", "sam_wrote": sam_wrote}
    if release.get("live"):
        ctx.update({"verified_fix_task": release.get("task_id") or "",
                    "verified_fix_status": "on prod",
                    "resolution_reply": True})
    results = run_guards(
        ("non_empty", "client_message", "no_unverified_claims",
         "no_roadmap_without_task", "no_confirmation", "required_signature"),
        text, ctx,
    )
    failed = [g for g in results if not g.ok]
    if failed:
        return "blocked", "; ".join(f"{g.name}: {g.reason}" for g in failed)
    return "ok", ""


def converse(text: str, day, thread: str) -> tuple[str, object, list[str]]:
    """Answer Sam, and run whatever his message actually asked for.

    Returns (reply to post, focus item, action results).
    """
    from vomeos import run_agent

    items = sprint_store.working_set()
    turns = _turns(thread)
    focus_before = _last_focus(turns[:-1])
    knowledge = "No product knowledge search was run."
    try:
        from product_knowledge import describe_knowledge

        knowledge = describe_knowledge(text)
    except Exception:
        pass

    result = run_agent(
        "customer_success.sprint_partner",
        context={
            "now": datetime.now(sprint_store.ZONE).strftime(
                "%Y-%m-%d %H:%M ET"),
            "items_today": _items_block(items),
            "conversation": _conversation_block(turns),
            "referenced_tickets": _referenced_block(text, items,
                                                    focus_before),
            "product_knowledge": knowledge,
            "sam_message": text,
        },
        subject_type="sprint_thread",
        subject_id=thread,
    )
    if not result.ok or not result.data:
        return (f"I could not answer that one ({result.why()}). Try again, "
                "or use a direct command like `send 3`.", focus_before, [])

    data = result.data
    reply = str(data.get("reply") or "").strip() or "Done."
    focus = data.get("focus") if isinstance(data.get("focus"), int) else None
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict)]

    learned = _record_partner_lessons(data.get("lessons"), text)

    by_number = {i.get("number"): i for i in items}
    sendable = _allowed_sends(text, items, turns)
    edited = {a.get("number") for a in actions
              if a.get("verb") in ("edit", "redraft")}
    results: list[str] = []
    for action in actions:
        verb = str(action.get("verb") or "").lower()
        number = action.get("number")
        item = by_number.get(number)
        if not item:
            results.append(f"_Skipped {verb} {number}: no such item._")
            continue
        if verb == "send":
            if number not in sendable:
                results.append(
                    f"_Not sent: your message did not ask me to send "
                    f"{number}. Say `send {number}`._")
                continue
            if number in edited:
                results.append(f"_{number} was just rewritten, so it was not "
                               f"sent. Read it above and say `send "
                               f"{number}`._")
                continue
        elif verb in _GATES and not _GATES[verb].search(text):
            results.append(f"_Did not {verb} {number}: your message did not "
                           f"ask for it._")
            continue
        elif verb not in _GATES and verb not in _FREE:
            continue
        try:
            results.append(_run_action(verb, action, item, sam_text=text,
                                       learn=not learned))
        except Exception as exc:
            results.append(f"*{number}.* {verb} failed: {exc}")

    results += learned
    full = reply + ("\n\n" + "\n".join(results) if results else "")
    return full, focus, results


def _run_action(verb: str, action: dict, item: dict, sam_text: str = "",
                learn: bool = True) -> str:
    n = item.get("number")
    if verb == "edit":
        body = str(action.get("text") or "").strip()
        if not body:
            return f"*{n}.* edit had no text."
        before = item.get("draft") or ""
        status, why = _check_draft(item, body)
        sprint_store.set_draft(item["id"], body, status, why)
        note = _learn(item, before, body, sam_text) if learn else ""
        if status != "ok":
            return (f":no_entry: *{n}.* the new draft is held by a guard: "
                    f"{why}. Not sendable until fixed." + note)
        return (f"*{n}.* draft updated. Say `send {n}` when you're happy."
                + note)
    if verb == "retriage":
        import shadow_triage

        outcome = shadow_triage._handle(item["ticket_id"], "retriage",
                                        force=True)
        return f"*{n}.* re-triaged: {outcome.get('status')}."
    command = {"verb": verb, "numbers": [n],
               "arg": str(action.get("text") or action.get("who") or "")}
    if verb == "route":
        command["arg"] = str(action.get("who") or "").lower()
    return _execute(command, item)


def _execute(command: dict, item: dict) -> str:
    verb = command["verb"]
    n = item.get("number")
    if verb == "send":
        return _send(item)
    if verb == "hold":
        wake = _wake_at(command.get("arg") or "")
        if wake is None:
            return (f"*{n}.* not held: I did not understand "
                    f"\"{command.get('arg')}\". Say `hold {n} till fri`.")
        local = wake.replace(tzinfo=timezone.utc).astimezone(
            sprint_store.ZONE)
        when = f"{local:%A} {local:%b} {local.day}"
        sprint_store.hold(item["id"], wake, f"held by Sam until {when}")
        return f"*{n}.* held. It comes back in the first batch after {when}."
    if verb == "done":
        sprint_store.decide(item["id"], sprint_store.DONE, "done by Sam")
        return f"*{n}.* done."
    if verb == "edit":
        return _edit(item, command["arg"])
    if verb == "redraft":
        return _redraft(item, command["arg"])
    if verb == "route":
        return _route(item, command["arg"])
    if verb in ("log", "build"):
        return _feature(item, verb, command.get("arg") or "")
    if verb == "fix":
        return _fix(item)
    if verb == "close":
        return _close_quietly(item)
    return f"*{n}.* unknown command."


def _signed(item: dict, text: str) -> str:
    """Sam's words verbatim, plus only the greeting and signature."""
    body = text.strip()
    first = (item.get("recipients") or {}).get("first_name") or ""
    if first and not re.match(r"^(hi|hello|bonjour|dear)\b", body, re.I):
        body = f"Hi {first},\n\n{body}"
    if "support.vomevolunteer.com" not in body:
        body = f"{body}\n\nSam | Vome team\nsupport.vomevolunteer.com"
    return body


def _guard_notes(text: str, item: dict) -> list[str]:
    from vomeos.guards.outbound import validate_client_message

    first = (item.get("recipients") or {}).get("first_name") or ""
    result = validate_client_message(text, category="sprint edit",
                                     contact_name=first, sam_wrote=True)
    notes = list(result.get("reasons") or []) + list(
        result.get("warnings") or [])
    return notes


def _edit(item: dict, text: str) -> str:
    n = item.get("number")
    if not text.strip():
        return f"*{n}.* edit needs text after the colon."
    body = _signed(item, text)
    notes = _guard_notes(body, item)
    before = item.get("draft") or ""
    sprint_store.set_draft(item["id"], body, "ok",
                           "; ".join(notes) if notes else "")
    shown = f"*{n}.* draft replaced. Send with `send {n}`:\n```{body}```"
    if notes:
        shown += "\n:warning: Check before sending: " + "; ".join(notes)
    return shown + _learn(item, before, body, "")


def _redraft(item: dict, notes: str) -> str:
    import shadow_triage

    n = item.get("number")
    card = item.get("card") or {}
    release = shadow_triage.release_state(item.get("ticket_number") or "",
                                          item.get("ticket_id") or "")
    context = {"kb_matches": "", "thread": ""}
    try:
        import sprint

        built = sprint.build_context({"id": item["ticket_id"],
                                      "ticketNumber": item["ticket_number"]})
        context = built or context
    except Exception:
        pass
    draft = shadow_triage.draft_reply(
        item.get("ticket_number") or "", card, context,
        item.get("recipients") or {}, release,
        language=shadow_triage._language(context.get("thread", "")),
        sam_notes=notes,
    )
    sprint_store.set_draft(item["id"], draft["text"], draft["status"],
                           draft["why"], kind="redraft")
    note = _learn(item, item.get("draft") or "", draft["text"], notes)
    if draft["status"] == "ok":
        return (f"*{n}.* redrafted. Send with `send {n}`:\n"
                f"```{draft['text']}```" + note)
    return (f"*{n}.* redraft {draft['status']}: {draft['why']}\n"
            f"```{draft['text']}```")


def _send(item: dict) -> str:
    import zoho_actions

    n = item.get("number")
    owed = _reply_owed(item)
    if item.get("status") not in sprint_store.CARRIES and not owed:
        return f"*{n}.* already {item.get('status')}, nothing sent."
    if item.get("status") == sprint_store.FIXING and item.get("sent_at"):
        return (f"*{n}.* already acknowledged and the agent is fixing it, "
                "nothing sent. The resolution comes back here once the fix "
                "is live.")
    draft = (item.get("draft") or "").strip()
    if not draft or item.get("draft_status") != "ok":
        return (f"*{n}.* has no ready draft ({item.get('draft_status')}). "
                f"Use `edit {n}: ...` or `redraft {n}: ...` first.")
    shipped = _engineer_shipped(item)
    if shipped:
        return shipped

    # A newer customer message means this draft answers an old question.
    latest = zoho_actions.latest_inbound(item["ticket_id"])
    if str(latest.get("id") or "") != str(item.get("last_thread_id") or ""):
        return (f"*{n}.* the customer has written again since this draft. "
                "It is being re-triaged; nothing was sent.")

    rcpt = item.get("recipients") or {}
    to, cc = rcpt.get("to") or "", list(rcpt.get("cc") or [])
    if not to:
        # #9538 (2026-10-18): a ready draft whose latest message had no
        # readable sender. Never guess an address.
        return (f"*{n}.* has no recipient (no sender on the latest customer "
                "message), so nothing was sent. Reply from Zoho, or say "
                f"`retriage {n}`.")
    zoho_actions.send_reply(item["ticket_id"], to, cc, draft)
    if _still_fixing(item):
        # The acknowledgment went; the agent still owns the fix, so the item
        # stays in flight instead of finishing (#9582).
        sprint_store.keep_fixing(item["id"])
    else:
        sprint_store.decide(item["id"], sprint_store.SENT,
                            f"sent by Sam after {item.get('decision')}"
                            if owed else "sent by Sam")
    sprint_store.record_sent(item["id"], draft)
    copied = f" cc {', '.join(cc)}" if cc else ""
    _post(f"Sent #{item.get('ticket_number')} to {to}{copied} on Sam's "
          "approval in the sprint.", channel=AGENT_LOG)
    after = _after_send(item)
    return f"*{n}.* sent to {to}{copied}." + (f" {after}" if after else "")


def _reply_owed(item: dict) -> bool:
    """Logging or building a request finishes the item, but the customer
    has not been answered yet, and `_feature` says to draft that reply. A
    finished item is sendable only in that case: its decision is the
    request and nothing has gone out (#9557, 2026-10-21)."""
    decision = str(item.get("decision") or "")
    return (item.get("status") == sprint_store.DONE
            and not item.get("sent_at")
            and decision.startswith(("log:", "build:")))


def _engineer_shipped(item: dict) -> str:
    """Why an acknowledgment must not go: the engineer triage routed it to
    has already shipped, and the on-prod handler emailed the client "it is
    fixed". A "we are on it" after that reads as if nobody is reading. A
    resolution draft for the same ticket is the reply that is owed, so it
    passes. '' when the send may go ahead (ClickUp unreadable included)."""
    filed = item.get("eng_task") or {}
    if not filed.get("id"):
        return ""
    import sprint_batch
    import ticket_routing

    if str(item.get("ticket_id")) in sprint_batch._resolution_tickets():
        return ""
    state = ticket_routing.task_state(filed)
    if not state.get("shipped"):
        return ""
    sprint_store.decide(item["id"], sprint_store.DONE,
                        f"{filed.get('owner')} shipped it "
                        f"({state.get('status')}) before Sam replied")
    return (f"*{item.get('number')}.* nothing sent: {filed.get('owner')} "
            f"already shipped this ({state.get('status')}, "
            f"{filed.get('url')}), and the client is emailed when a fix "
            "reaches production. Finished.")


def _still_fixing(item: dict) -> bool:
    """An agent fix PR is open for this ticket and not yet live."""
    if item.get("status") != sprint_store.FIXING:
        return False
    import shadow_triage

    release = shadow_triage.release_state(item.get("ticket_number") or "",
                                          item.get("ticket_id") or "")
    return not release.get("live")


# What a sent reply means for the ticket. The reply is the decision; the
# status follows from it, so nobody has to tidy two systems by hand.
CLOSE_AFTER = {"user_education", "admin_action", "misdirected",
               "account_deletion"}
AWAIT_AFTER = {"vague_bug", "email_update"}


def _after_send(item: dict) -> str:
    """Zoho status and Agent Fixes closure after a sent reply.

    A resolution (the linked fix is Live) or an answer closes the ticket,
    silently. A question waits for the customer. An acknowledgment leaves
    the status alone: the work is still open. Failures are reported, never
    raised: the email already went, and that must not look like it failed.
    """
    import shadow_triage
    import zoho_actions

    category = item.get("category") or ""
    release = shadow_triage.release_state(item.get("ticket_number") or "",
                                          item.get("ticket_id") or "")
    # Triage routed it and the client has now been told: the engineer owns
    # it from here, and the on-prod handler closes the loop when it ships.
    # Zoho moves out of New/Processing only now that the client has been
    # told (ticket_routing): until then that status means "owed a reply".
    filed = item.get("eng_task") or {}
    if filed.get("url") and not release.get("live"):
        reason = re.sub(r"\W+", "_", str(filed.get("reason") or "auto"))
        sprint_store.decide(item["id"], sprint_store.ROUTED,
                            f"routed to {filed.get('owner')} "
                            f"({reason.strip('_') or 'auto'})")
        team = ENGINEERS.get(filed.get("owner"), ("", "", "backend", ""))[2]
        try:
            zoho_actions.set_in_progress(item["ticket_id"], team)
            moved = "Zoho set to In Progress"
        except Exception as exc:
            moved = f":warning: Zoho status NOT set to In Progress ({exc})"
        return (f"{moved} with {filed.get('owner')} ({filed['url']}). The "
                "client is emailed when it ships.")
    if item.get("fix_pr") and not release.get("live"):
        try:
            zoho_actions.set_agent_fixing(item["ticket_id"])
            return ("Zoho set to In Progress (the agent's fix is in flight, "
                    "waiting on your merge).")
        except Exception as exc:
            return f":warning: Zoho status NOT set to In Progress ({exc})."
    if release.get("live") or category in CLOSE_AFTER:
        status = "Closed"
    elif category in AWAIT_AFTER:
        status = "Awaiting Client Response"
    else:
        return "Zoho status left as it is (the work is still open)."

    notes = []
    try:
        zoho_actions.set_status(item["ticket_id"], status)
        notes.append(f"Zoho set to {status}"
                     + (" with no closure email." if status == "Closed"
                        else "."))
    except Exception as exc:
        notes.append(f":warning: Zoho status NOT set to {status} ({exc}).")

    if release.get("live"):
        notes.append(_close_agent_fix(item))
    return " ".join(n for n in notes if n)


def _clickup_status(task_id: str, status: str) -> None:
    import httpx

    resp = httpx.put(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json={"status": status}, timeout=30,
    )
    resp.raise_for_status()


def _close_agent_fix(item: dict) -> str:
    """Close this ticket's Agent Fixes child, and the parent once its last
    client has been told.

    `Closed`, never `on prod`: on prod fires the on-prod handler, which
    would write and send a second email to the same customer.
    """
    try:
        import release_watcher as rw

        tasks = rw._list_on_dev_tasks()
    except Exception as exc:
        return f":warning: Agent Fixes not closed ({exc})."
    marker = f"[#{item.get('ticket_number')}]"
    child = next((t for t in tasks if t.get("parent")
                  and marker in (t.get("name") or "")), None)
    if not child:
        return ""
    try:
        _clickup_status(child["id"], "Closed")
        left = [t for t in rw._children(child["parent"], tasks)
                if t.get("id") != child["id"]]
        if not left:
            _clickup_status(child["parent"], "Closed")
            return "Agent Fixes child and parent closed."
        return f"Agent Fixes child closed; {len(left)} client(s) to go."
    except Exception as exc:
        return f":warning: Agent Fixes close failed ({exc})."


def _clickup_create(list_id: str, body: dict) -> dict:
    import httpx

    resp = httpx.post(
        f"https://api.clickup.com/api/v2/list/{list_id}/task",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


SAM_FOOTER = "_Filed from the support sprint on Sam's instruction._"


def _task_description(item: dict, footer: str = SAM_FOOTER) -> str:
    card = item.get("card") or {}
    import zoho_actions

    lines = [
        f"**Zoho:** {zoho_actions.ticket_url(item.get('ticket_id'))}",
        f"**Account:** {item.get('account') or ''}",
        f"**Waiting on:** {card.get('live_question') or ''}",
        "",
        f"**Situation:** {card.get('situation') or ''}",
        f"**Existing work:** {card.get('existing_work') or 'none found'}",
        f"**Recommended:** {card.get('recommended_action') or ''}",
    ]
    # The engineer summary the playbook asks for (VomeOS Code Access page):
    # what the agent's code check found, so the engineer starts from a lead.
    code = card.get("code_check") or {}
    if isinstance(code, dict) and code.get("verdict") not in (
            None, "", "not_checked"):
        reason = card.get("escalation_reason") or ""
        lines += [
            "",
            "**Agent first pass (code check)**",
            f"Likely cause: {code.get('finding') or 'not settled'}",
            f"Where: {code.get('where') or 'not located'}",
            f"Potential fix: {code.get('fix') or 'none proposed'}",
            f"Confidence: {code.get('confidence') or '?'} (verdict "
            f"{code.get('verdict')}, fix risk {code.get('fix_risk') or '?'})",
            "Not verified: " + (f"why it needs an engineer: {reason}"
                                if reason else "anything beyond the code "
                                "read on the live branch"),
        ]
    lines += ["", footer]
    return "\n".join(lines)


def _route(item: dict, who: str) -> str:
    """Sam routes by hand (`route N to onlyg`, route_ticket). Same path and
    rules as triage's own routing (ticket_routing): the task, a private
    Zoho note, Slack only when urgent, and the Zoho status left alone,
    because New or Processing means the client is still owed Sam's reply
    (Rule 6, 2026-10-19). Sending that reply hands the ticket over."""
    import ticket_routing as tr
    import zoho_actions

    n = item.get("number")
    key = ENGINEER_ALIASES.get(who, who)
    if key not in ENGINEERS:
        return f"*{n}.* route to OnlyG or Sanjay."
    # Triage may have routed it already (ticket_routing): one bug, one task.
    filed = item.get("eng_task") or {}
    if filed.get("url"):
        return (f"*{n}.* already with {filed.get('owner')}: {filed['url']}. "
                "Nothing new was filed.")
    card = item.get("card") or {}
    out = tr.route(item, card, {}, tr.incident_of(item),
                   tr.arr_of(item.get("account") or ""), owner=key)
    if not out.get("routed"):
        return f"*{n}.* not routed: {out.get('why')}."
    task = out["task"]
    urgent = (f" Urgent ({out['urgent']}): {key} and you were pinged."
              if out.get("urgent") else "")
    if item.get("sent_at"):
        # Already acknowledged earlier: nothing is owed now, so the ticket
        # is the engineer's from here.
        try:
            zoho_actions.set_in_progress(item["ticket_id"],
                                         ENGINEERS[key][2])
        except Exception as exc:
            print(f"[SPRINT] Zoho status not set on {item['ticket_id']}: "
                  f"{exc}")
        reason = re.sub(r"\W+", "_", str(task.get("reason") or "sam_call"))
        sprint_store.decide(item["id"], sprint_store.ROUTED,
                            f"routed to {key} ({reason.strip('_')})")
        return (f"*{n}.* routed to {key}: {task['url']}. The client was "
                f"already told, so Zoho is now In Progress.{urgent}")
    return (f"*{n}.* routed to {key}: {task['url']} ({task['how']}). Zoho "
            "left as it is: the client is still owed your reply, and sending "
            f"it hands the ticket over.{urgent}")


def _close_quietly(item: dict) -> str:
    """Close the Zoho ticket with no closure email, and finish the item.
    For tickets that owe nothing (confirmed fixed, a thank-you, an
    out-of-office, a handled error report)."""
    import zoho_actions

    n = item.get("number")
    if item.get("status") not in sprint_store.CARRIES:
        return f"*{n}.* already {item.get('status')}."
    try:
        zoho_actions.set_status(item["ticket_id"], "Closed")
    except Exception as exc:
        return f"*{n}.* not closed: Zoho said {exc}"
    sprint_store.decide(item["id"], sprint_store.DONE,
                        "closed quietly by Sam")
    return f"*{n}.* closed in Zoho, no email."


def close_quiet(batch: dict, except_numbers: set) -> str:
    """Close what this batch showed under CLOSE QUIETLY, and only that."""
    ids = batch.get("quiet") or []
    if not ids:
        return "This batch had nothing to close quietly."
    results = []
    for item_id in ids:
        item = sprint_store.get_item(int(item_id))
        if not item:
            continue
        if item.get("number") in except_numbers:
            results.append(f"*{item.get('number')}.* left open, as you "
                           "asked.")
            continue
        results.append(_close_quietly(item))
    return "\n".join(results) or "Nothing to close."


def _fix(item: dict) -> str:
    """Sam said `fix N`: write the fix and open the PR, for a design
    question he decided to change or a bug the agent did not fix on its own
    call. The low-risk bar and patch safety still apply; nothing merges."""
    import ticket_fix
    import zoho_actions

    n = item.get("number")
    out = ticket_fix.propose_for_ticket(
        item["ticket_id"], str(item.get("ticket_number") or ""),
        item.get("account") or "", item.get("card") or {},
        zoho_actions.ticket_url(item.get("ticket_id")), sam_approved=True)
    if out.get("outcome") == "pr_opened":
        sprint_store.set_fix_pr(item["id"], out["pr_url"])
        then = ("OnlyG merges core to production"
                if "bitbucket" in out["pr_url"]
                else "you deploy web to production")
        return (f"*{n}.* fix PR opened: {out['pr_url']}. Merge it into the "
                f"dev branch, then {then}. Once it is proven live, the "
                "resolution comes back to you here, ready to send.")
    return (f"*{n}.* no PR: {out.get('reason') or out.get('outcome')}. "
            f"`route {n} to onlyg` or `route {n} to sanjay` hands it to an "
            "engineer instead.")


_TASK_ID = re.compile(r"^[a-z0-9]{6,12}$", re.I)


def _feature(item: dict, verb: str, mode: str = "", title: str = "",
             module: str = "") -> str:
    """`log N` / `build N`, with a duplicate check first (G10), and every
    decision posted to #vome-feature-requests with the client (G9).

    `build` is Sam building it himself, soon: Sam Accepted (Build Now),
    assigned to him, high priority. `log` is for later: Raw Intake,
    unassigned, until he triages it.

    mode "" checks for an existing request and stops if one may cover it;
    "new" logs it anyway; a ClickUp task id adds this client to that one
    (and with `build` moves that task to Sam Accepted).
    """
    import clickup_tasks
    from mcp_connector import feature_requests

    n = item.get("number")
    card = item.get("card") or {}
    # The card's question ("Will Vome add ...") is the fallback; the sprint
    # passes a short capability name so the task reads like a request.
    title = (" ".join((title or "").split())
             or card.get("live_question") or item.get("summary")
             or "request")
    problem = card.get("situation") or title
    org = (item.get("account") or "").split("(")[0].strip()
    mode = (mode or "").strip()
    if mode and mode != "new" and not _TASK_ID.match(mode):
        mode = ""  # the partner's free text is not a task id

    if mode and mode != "new":
        import zoho_actions

        try:
            existing = feature_requests.add_plus_one(
                task_id=mode, requested_by=org, logged_by="Sam (sprint)",
                note=title,
                ticket_url=zoho_actions.ticket_url(item.get("ticket_id")))
        except Exception as exc:
            return f"*{n}.* not added to {mode}: {exc}"
        url = existing.get("url") or f"https://app.clickup.com/t/{mode}"
        where = f"the existing request {existing.get('name') or mode}"
        if verb == "build":
            try:
                feature_requests.triage(
                    task_id=mode, decision="build_now", priority="high",
                    assign_to=clickup_tasks.CLICKUP_USER_SAM,
                    note=("Build now, from ticket "
                          f"#{item.get('ticket_number')}"),
                    triaged_by="Sam (sprint)")
                where += ", moved to Sam Accepted (Build Now)"
            except Exception as exc:
                where += (f" (not moved to Sam Accepted: {exc}; move it in "
                          "ClickUp)")
    else:
        if mode != "new":
            try:
                found = feature_requests.find_duplicates(title, problem)
            except Exception as exc:
                return (f"*{n}.* nothing logged: I could not check for an "
                        f"existing request ({exc}). Say `{verb} {n} new` to "
                        "log it anyway.")
            if found:
                lines = [f"*{n}.* nothing logged yet. These may already "
                         "cover it:"]
                lines += [
                    f"  - <{d.get('url')}|{d.get('name')}> · "
                    f"{d.get('list') or ''} · {d.get('status') or ''}"
                    + (" · shipped" if d.get("shipped") else "")
                    + f" · `{verb} {n} to {d.get('id')}`"
                    for d in found[:5]]
                lines.append(f"Or `{verb} {n} new` if none is the same "
                             "request.")
                return "\n".join(lines)
        list_id = SAM_ACCEPTED if verb == "build" else RAW_INTAKE
        task = _clickup_create(list_id, _feature_task(item, verb, title,
                                                      org, module))
        url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
        where = ("Sam Accepted (Build Now)" if verb == "build"
                 else "Raw Intake")
    sprint_store.decide(item["id"], sprint_store.DONE, f"{verb}: {url}")
    _post_feature_request(item, verb, where, url)
    return (f"*{n}.* {'building' if verb == 'build' else 'logged'}: {where}"
            f" {url}. Posted in #vome-feature-requests. The customer has not "
            "been told anything; draft a reply with `edit` if one is owed.")


def _feature_task(item: dict, verb: str, title: str, org: str,
                  module: str = "") -> dict:
    """The ClickUp body for a new request: the fields Sam sorts by (client,
    ARR, ticket), and on `build` his name and a high priority so Sam
    Accepted is his own working list."""
    import clickup_tasks
    import ticket_routing
    import zoho_actions

    fields = [
        {"id": FIELD_SOURCE, "value": SOURCE_ZOHO_TICKET},
        {"id": clickup_tasks.FIELD_TYPE, "value": clickup_tasks.TYPE_FEATURE},
        {"id": FIELD_ZOHO_TICKET_LINK,
         "value": zoho_actions.ticket_url(item.get("ticket_id"))},
    ]
    if org:
        fields.append({"id": clickup_tasks.FIELD_REQUESTING_CLIENTS,
                       "value": org})
    arr = ticket_routing.arr_of(item.get("account") or "")
    if arr:
        fields.append({"id": clickup_tasks.FIELD_COMBINED_ARR, "value": arr})
    module_option = (clickup_tasks._map_module_option(module)
                     if module else None)
    if module_option:
        fields.append({"id": clickup_tasks.FIELD_MODULE,
                       "value": module_option})
    name = f"[#{item.get('ticket_number')}] "
    name += f"{org} -- {title}" if org else title
    body = {"name": name[:150],
            "markdown_description": _task_description(item),
            "custom_fields": fields}
    if verb == "build":
        body["assignees"] = [clickup_tasks.CLICKUP_USER_SAM]
        body["priority"] = 2  # high
    return body


FEATURE_REQUESTS = os.environ.get("SLACK_CHANNEL_FEATURE_REQUESTS",
                                  "C0ALL4VPWK0")


def _post_feature_request(item: dict, verb: str, where: str,
                          url: str) -> None:
    """One message per decision in #vome-feature-requests, so Sam's list of
    requests is in one place (feedback, 2026-10-16)."""
    import zoho_actions

    card = item.get("card") or {}
    ticket = (f"<{zoho_actions.ticket_url(item.get('ticket_id'))}|"
              f"#{item.get('ticket_number')}>")
    _post("\n".join([
        f":bulb: *Feature request* from {item.get('account') or 'unknown'} "
        f"· {ticket}",
        f"*Asked for:* {card.get('live_question') or item.get('summary')}",
        f"*Situation:* {card.get('situation') or ''}",
        f"*Decision:* {'build now' if verb == 'build' else 'logged'}, "
        f"<{url}|{where}>",
        "*Client told:* nothing yet.",
    ]), channel=FEATURE_REQUESTS)
