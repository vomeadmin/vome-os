"""
sprint_thread.py

The daily support sprint, run as one Slack thread in #support-sprints.

WHAT SAM SEES
-------------
One thread per business day. A card arrives in it as each ticket is triaged
(shadow_triage), and at 10:00, 13:00, 16:00 and 19:00 the agent posts the
complete current list: what needs Sam, what is held, what waits on a release.
Item numbers stay fixed all day. An incident breaks the schedule with an
immediate mention.

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
from datetime import datetime, timedelta

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
    """The thread_ts for the day, creating the parent message if needed."""
    day = day or sprint_store.today()
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
    if release and release.get("task_id"):
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
    thread = ensure_thread()
    if not thread:
        return ""
    ts = _post(render_card(item, release), thread_ts=thread)
    if ts:
        sprint_store.set_card_ts(item["id"], ts)
    return ts


def announce_incident(item: dict, incident: dict) -> bool:
    """Immediate mention, once per incident per day."""
    day = sprint_store.today()
    _tables()
    rows = store.query(
        f"INSERT INTO {INCIDENTS_TABLE} (incident_key, day) VALUES (:k, :d)"
        " ON CONFLICT DO NOTHING RETURNING incident_key",
        {"k": incident.get("key"), "d": day}, write=True,
    )
    if not rows and store.usable():
        return False
    thread = ensure_thread()
    mention = " ".join(f"<@{r}>" for r in reviewers())
    tickets = ", ".join(f"#{n}" for n in incident.get("same_as") or [])
    sentry = incident.get("sentry_issue")
    lines = [
        f":rotating_light: {mention} *Possible incident:* "
        f"{incident.get('symptom') or 'same symptom on several tickets'}",
        f"Newest: item {item.get('number')} {_zoho_link(item)} "
        f"{item.get('account') or ''}",
    ]
    if tickets:
        lines.append(f"Same symptom: {tickets}")
    if sentry:
        lines.append(f"Matches Sentry issue "
                     f"<https://sentry.io/issues/{sentry}/|{sentry}>")
    lines.append(f"_{incident.get('reason') or ''} "
                 f"(confidence {incident.get('confidence') or '?'})_")
    lines.append("Group these before replying: one fix, one reply each.")
    return bool(_post("\n".join(lines), thread_ts=thread, broadcast=True))


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
           f"{len(held)} held · {len(done)} done today"]
    if needs:
        out.append("*Needs you*")
        out += [line(i) for i in needs]
    if held:
        out.append("*Held*")
        out += [line(i) for i in held]
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
        match = re.match(r"^route\s+([\d,\s\-and]+?)\s+to\s+(\w+)\s*$", line,
                         re.IGNORECASE)
        if match:
            commands.append({"verb": "route",
                             "numbers": _numbers(match.group(1)),
                             "arg": match.group(2).lower()})
            continue
        match = re.match(
            r"^(send|hold|done|skip|log|build)\s+([\d,\s\-and]+)$",
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
    day = _day_for_thread(thread)
    if day is None:
        return {"status": "not a sprint thread"}

    text = message.get("text") or ""
    _record_turn(thread, message.get("ts") or "", "sam", text, None)

    commands = parse_strict(text)
    if commands is not None:
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


def _run_commands(commands: list[dict], day) -> list[str]:
    results = []
    for command in commands:
        for number in command["numbers"]:
            item = sprint_store.by_number(day, number)
            if not item:
                results.append(f"*{number}.* no such item in this thread.")
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


def _check_draft(item: dict, text: str) -> tuple[str, str]:
    """Every outbound guard on a draft the partner wrote. (status, why)."""
    from vomeos.guards import run_guards

    import shadow_triage

    release = shadow_triage.release_state(item.get("ticket_number") or "",
                                          item.get("ticket_id") or "")
    ctx = {"agent": "customer_success.sprint_partner",
           "contact_name": (item.get("recipients") or {}).get("first_name")
           or ""}
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

    items = sprint_store.items_for_day(day)
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
            results.append(f"_Skipped {verb} {number}: no such item today._")
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
            results.append(_run_action(verb, action, item))
        except Exception as exc:
            results.append(f"*{number}.* {verb} failed: {exc}")

    full = reply + ("\n\n" + "\n".join(results) if results else "")
    return full, focus, results


def _run_action(verb: str, action: dict, item: dict) -> str:
    n = item.get("number")
    if verb == "edit":
        body = str(action.get("text") or "").strip()
        if not body:
            return f"*{n}.* edit had no text."
        status, why = _check_draft(item, body)
        sprint_store.set_draft(item["id"], body, status, why)
        if status != "ok":
            return (f":no_entry: *{n}.* the new draft is held by a guard: "
                    f"{why}. Not sendable until fixed.")
        return f"*{n}.* draft updated. Say `send {n}` when you're happy."
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
        sprint_store.decide(item["id"], sprint_store.HELD, "held by Sam")
        return f"*{n}.* held. It carries to tomorrow."
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
        return _feature(item, verb)
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
                                     contact_name=first)
    notes = list(result.get("reasons") or []) + list(
        result.get("warnings") or [])
    return notes


def _edit(item: dict, text: str) -> str:
    n = item.get("number")
    if not text.strip():
        return f"*{n}.* edit needs text after the colon."
    body = _signed(item, text)
    notes = _guard_notes(body, item)
    sprint_store.set_draft(item["id"], body, "ok",
                           "; ".join(notes) if notes else "")
    shown = f"*{n}.* draft replaced. Send with `send {n}`:\n```{body}```"
    if notes:
        shown += "\n:warning: Check before sending: " + "; ".join(notes)
    return shown


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
    if draft["status"] == "ok":
        return (f"*{n}.* redrafted. Send with `send {n}`:\n"
                f"```{draft['text']}```")
    return (f"*{n}.* redraft {draft['status']}: {draft['why']}\n"
            f"```{draft['text']}```")


def _send(item: dict) -> str:
    import zoho_actions

    n = item.get("number")
    if item.get("status") not in sprint_store.CARRIES:
        return f"*{n}.* already {item.get('status')}, nothing sent."
    draft = (item.get("draft") or "").strip()
    if not draft or item.get("draft_status") != "ok":
        return (f"*{n}.* has no ready draft ({item.get('draft_status')}). "
                f"Use `edit {n}: ...` or `redraft {n}: ...` first.")

    # A newer customer message means this draft answers an old question.
    latest = zoho_actions.latest_inbound(item["ticket_id"])
    if str(latest.get("id") or "") != str(item.get("last_thread_id") or ""):
        return (f"*{n}.* the customer has written again since this draft. "
                "It is being re-triaged; nothing was sent.")

    rcpt = item.get("recipients") or {}
    to, cc = rcpt.get("to") or "", list(rcpt.get("cc") or [])
    zoho_actions.send_reply(item["ticket_id"], to, cc, draft)
    sprint_store.decide(item["id"], sprint_store.SENT, "sent by Sam")
    sprint_store.record_sent(item["id"], draft)
    copied = f" cc {', '.join(cc)}" if cc else ""
    _post(f"Sent #{item.get('ticket_number')} to {to}{copied} on Sam's "
          "approval in the sprint thread.", channel=AGENT_LOG)
    after = _after_send(item)
    return f"*{n}.* sent to {to}{copied}." + (f" {after}" if after else "")


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


def _task_description(item: dict) -> str:
    card = item.get("card") or {}
    import zoho_actions

    return "\n".join([
        f"**Zoho:** {zoho_actions.ticket_url(item.get('ticket_id'))}",
        f"**Account:** {item.get('account') or ''}",
        f"**Waiting on:** {card.get('live_question') or ''}",
        "",
        f"**Situation:** {card.get('situation') or ''}",
        f"**Existing work:** {card.get('existing_work') or 'none found'}",
        f"**Recommended:** {card.get('recommended_action') or ''}",
        "",
        "_Filed from the sprint thread on Sam's instruction._",
    ])


def _route(item: dict, who: str) -> str:
    import zoho_actions

    n = item.get("number")
    key = ENGINEER_ALIASES.get(who, who)
    if key not in ENGINEERS:
        return f"*{n}.* route to OnlyG or Sanjay."
    clickup_id, slack_id, team, channel = ENGINEERS[key]
    card = item.get("card") or {}
    summary = (card.get("live_question") or item.get("summary") or "")[:90]
    task = _clickup_create(PRIORITY_QUEUE, {
        "name": f"[#{item.get('ticket_number')}] "
                f"{(item.get('account') or '').split('(')[0].strip()} -- "
                f"{summary}",
        "markdown_description": _task_description(item),
        "assignees": [clickup_id],
        "status": "queued",
        "custom_fields": [
            {"id": FIELD_ZOHO_TICKET_LINK,
             "value": zoho_actions.ticket_url(item.get("ticket_id"))},
            {"id": FIELD_SOURCE, "value": SOURCE_ZOHO_TICKET},
        ],
    })
    url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    _post(f"<@{slack_id}> new {team} ticket: {summary}\n"
          f"ClickUp: {url} · Zoho: "
          f"{zoho_actions.ticket_url(item.get('ticket_id'))}",
          channel=channel)
    try:
        zoho_actions.set_in_progress(item["ticket_id"], team)
    except Exception as exc:
        print(f"[SPRINT] Zoho status not set on {item['ticket_id']}: {exc}")
    try:
        from zoho_desk_api import add_ticket_comment

        add_ticket_comment(item["ticket_id"], f"ClickUp task: {url}")
    except Exception:
        pass
    sprint_store.decide(item["id"], sprint_store.ROUTED, f"routed to {key}")
    return (f"*{n}.* routed to {key}: {url}. Zoho set In Progress. No reply "
            "was sent; say `send` on a draft if one is owed.")


def _feature(item: dict, verb: str) -> str:
    n = item.get("number")
    list_id = SAM_ACCEPTED if verb == "build" else RAW_INTAKE
    card = item.get("card") or {}
    title = card.get("live_question") or item.get("summary") or "request"
    task = _clickup_create(list_id, {
        "name": f"[#{item.get('ticket_number')}] {title[:110]}",
        "markdown_description": _task_description(item),
        "custom_fields": [{"id": FIELD_SOURCE, "value": SOURCE_ZOHO_TICKET}],
    })
    url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    sprint_store.decide(item["id"], sprint_store.DONE, f"{verb}: {url}")
    where = "Sam Accepted (Build Now)" if verb == "build" else "Raw Intake"
    return (f"*{n}.* logged in {where}: {url}. The customer has not been "
            "told anything; draft a reply with `edit` if one is owed.")
