"""
sprint_batch.py

The support sprint as batches: one message in #support-sprints holding
everything that needs Sam, with every reply in full in its thread.

WHY
---
The first weekend of the sprint thread (2026-10-17/18) posted a card per
ticket as each was triaged, so Sam watched cards trickle in one by one and
scrolled back through them to act. Carried items kept their drafts in
yesterday's thread. Sam: "the message should only come to me with the full
thread of ALL tickets, or at least a batch of 25... I just want to focus on
the things I need to touch."

WHAT A BATCH IS
---------------
A top-level message: what needs Sam, in three sections (ready to send, fix
first, needs your decision), most important then longest waiting, at most
SPRINT_BATCH_SIZE (25) items. Everything that does not need him is one
line of counts. Its thread holds every item in full: the actual reply
exactly as the client will get it, with recipients (Sam, 2026-10-17: "I
want the actual message"), or the situation and the decision asked of him.

The previous batch is edited down to a pointer at the new one, so the
latest message in the channel is always the complete picture. Commands work
in any batch thread, because item numbers are permanent (sprint_store).

WHEN
----
Weekdays 9, 13 and 17 ET; Saturday and Sunday 11, only if something needs
him. A weekday batch with nothing new says so in one line in the previous
batch's thread instead of posting again. `next` / `catch me up` post one on
demand, `clear the queue` triages everything untriaged in rounds of 25.

Triage itself is silent (no card per ticket): the Zoho webhook, a sweep of
untriaged open tickets every 30 minutes, and clear mode. Incidents are the
only interruption.

SWITCH
------
    SPRINT_PER_TICKET_CARDS   "true" restores the old per-ticket cards and
                              release posts. Off by default.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

import sprint_store
from vomeos import store

BATCH_SIZE = int(os.environ.get("SPRINT_BATCH_SIZE", "25"))
SWEEP_CAP = int(os.environ.get("SPRINT_SWEEP_CAP", "10"))
PART_CHARS = 3500

CLEAR_KEY = "customer_success.queue_clear"
LEASES = "vomeos_sprint_leases"

store.register_table(
    LEASES,
    f"""
    CREATE TABLE IF NOT EXISTS {LEASES} (
        name   VARCHAR PRIMARY KEY,
        until  TIMESTAMP NOT NULL
    )
    """,
)

READY, FIX, DECIDE, HELD = "ready", "fix", "decide", "held"
# Nothing is owed (we asked, they have not answered): one line, `done N`.
OWED_NOTHING = "owed_nothing"
# Nothing owed and nothing to do (triage lane housekeeping): `close quiet`.
QUIET = "quiet"

ESCALATION_LABELS = {
    "data_lookup": "needs a record we cannot read",
    "mobile_unreadable": "lives in the mobile app",
    "schema_or_migration": "needs a model or migration change",
    "auth_or_billing": "touches login, permissions or payments",
    "out_of_repo": "chats or integrations service",
    "low_confidence": "the code check could not settle it",
    "product_decision": "a product call",
}
NO_RECIPIENT = "no recipient: the latest customer message has no sender"

CATEGORY_LABELS = {
    "clear_bug": "bug", "vague_bug": "needs info",
    "user_education": "how-to", "admin_action": "setting change",
    "misdirected": "misdirected", "email_update": "email change",
    "account_deletion": "account deletion",
    "feature_request": "feature request", "needs_human": "needs you",
    "escalated": "escalated", "awaiting_client": "awaiting client",
}


def cards_enabled() -> bool:
    value = os.environ.get("SPRINT_PER_TICKET_CARDS", "").strip().lower()
    return value in ("true", "1", "on", "yes")


def draft_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return sprint_store._now()


def _parse_time(raw) -> datetime | None:
    """Zoho's "2026-10-17T14:02:11.000Z" (or a datetime) as naive UTC."""
    if isinstance(raw, datetime):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def waiting_since(item: dict) -> datetime | None:
    """When the customer last wrote. Falls back to when we first saw it."""
    received = _parse_time((item.get("recipients") or {}).get("received_at"))
    arrived = item.get("arrived_at")
    return received or (arrived if isinstance(arrived, datetime) else None)


def age(item: dict, now: datetime | None = None) -> str:
    since = waiting_since(item)
    if since is None:
        return ""
    hours = ((now or _now()) - since).total_seconds() / 3600
    if hours < 1:
        return "new"
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def _start_of_today() -> datetime:
    local = datetime.now(sprint_store.ZONE).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _local(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc).astimezone(sprint_store.ZONE)


def _clock(moment: datetime) -> str:
    local = _local(moment)
    return f"{local:%a} {local.hour % 12 or 12}:{local:%M} {local:%p}"


# ---------------------------------------------------------------------------
# What needs Sam
# ---------------------------------------------------------------------------

def is_design_question(item: dict) -> bool:
    code = (item.get("card") or {}).get("code_check")
    return isinstance(code, dict) and code.get("verdict") == "design_question"


def held_is_due(item: dict, now: datetime | None = None) -> bool:
    """A held item comes back when its wake time passes. A hold from
    before wake times existed comes back the next day."""
    wake = item.get("wake_at")
    if isinstance(wake, datetime):
        return wake <= (now or _now())
    decided = item.get("decided_at")
    return isinstance(decided, datetime) and decided < _start_of_today()


def section(item: dict, now: datetime | None = None) -> str:
    status = item.get("status")
    if status not in sprint_store.CARRIES:
        return ""
    if status == sprint_store.HELD and not held_is_due(item, now):
        return HELD
    # Works as built, but maybe not as it should: Sam decides whether to
    # change it, log it, or answer it, even when a how-to draft exists.
    if is_design_question(item):
        return DECIDE
    draft = (item.get("draft") or "").strip()
    if draft and item.get("draft_status") == "ok":
        # A reply with nowhere to go is not ready (#9538, 2026-10-18).
        if not (item.get("recipients") or {}).get("to"):
            return FIX
        return READY
    if draft and item.get("draft_status") == "blocked":
        return FIX
    if (item.get("card") or {}).get("lane") == "housekeeping":
        return QUIET
    if item.get("category") == "awaiting_client":
        return OWED_NOTHING
    return DECIDE


def _fix_reason(item: dict) -> str:
    if item.get("draft_status") == "ok":
        return NO_RECIPIENT
    return str(item.get("draft_why") or "")


def _priority(item: dict):
    since = waiting_since(item) or datetime.max
    return (not item.get("important"), since)


def select(items: list[dict], exclude: set | None = None,
           limit: int = BATCH_SIZE, now: datetime | None = None) -> dict:
    """The items this batch shows, by section, plus what it leaves out."""
    exclude = exclude or set()
    actionable = [i for i in items
                  if section(i, now) in (READY, FIX, DECIDE)
                  and i.get("id") not in exclude]
    actionable.sort(key=_priority)
    chosen = actionable[:limit]
    held = [i for i in items if section(i, now) == HELD]
    owed = sorted((i for i in items if section(i, now) == OWED_NOTHING),
                  key=_priority)
    return {
        READY: [i for i in chosen if section(i, now) == READY],
        FIX: [i for i in chosen if section(i, now) == FIX],
        DECIDE: [i for i in chosen if section(i, now) == DECIDE],
        "more": len(actionable) - len(chosen),
        "held": held,
        OWED_NOTHING: owed,
        QUIET: sorted((i for i in items if section(i, now) == QUIET),
                      key=_priority),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _zoho(item: dict) -> str:
    import zoho_actions

    return (f"<{zoho_actions.ticket_url(item.get('ticket_id'))}|"
            f"#{item.get('ticket_number')}>")


def _account(item: dict) -> str:
    return str(item.get("account") or "").split("(")[0].strip()


def _label(item: dict, resolutions: set) -> str:
    if str(item.get("ticket_id")) in resolutions:
        return "resolution (fix is live)"
    if is_design_question(item):
        return "design question: change it?"
    category = item.get("category") or ""
    return CATEGORY_LABELS.get(category, category or "?")


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _star(item: dict) -> str:
    return ":star: " if item.get("important") else ""


def _after_merge(pr_url: str) -> str:
    """Who takes it to production once Sam merges it into the dev branch."""
    return ("OnlyG merges core to production" if "bitbucket" in (pr_url or "")
            else "you deploy web to production")


def _index_line(item: dict, resolutions: set, now: datetime) -> str:
    waited = age(item, now)
    line = (f"*{item.get('number')}* {_star(item)}{_zoho(item)} "
            f"{_account(item)} · {_label(item, resolutions)}"
            + (f" · waiting {waited}" if waited else "")
            + (" · fix PR open" if item.get("fix_pr") else ""))
    if section(item, now) == FIX:
        why = _short(_fix_reason(item), 120)
        line += f"\n      held: {why}"
    elif is_design_question(item):
        code = (item.get("card") or {}).get("code_check") or {}
        line += "\n      " + _short(code.get("finding"), 160)
    elif section(item, now) == DECIDE:
        card = item.get("card") or {}
        line += ("\n      " + _short(card.get("live_question")
                                     or item.get("summary"), 160))
    return line


def _recipients(item: dict) -> str:
    rcpt = item.get("recipients") or {}
    cc = ", ".join(rcpt.get("cc") or [])
    return f"To: {rcpt.get('to') or '?'}" + (f" · cc: {cc}" if cc else "")


def detail(item: dict, resolutions: set, now: datetime) -> str:
    """One item in full, for the batch thread."""
    n = item.get("number")
    card = item.get("card") or {}
    head = (f"*{n}* {_star(item)}{_zoho(item)} {_account(item)} · "
            f"{_label(item, resolutions)}"
            + (f" · waiting {age(item, now)}" if age(item, now) else ""))
    kind = section(item, now)
    lines = [head]
    if item.get("fix_pr"):
        # The agent opened a fix for this ticket. The client hears about it
        # when the release watcher proves it live, as a resolution here.
        lines.append(f":hammer_and_wrench: Fix PR opened, waiting on your "
                     f"merge into the dev branch (then "
                     f"{_after_merge(item['fix_pr'])}): {item['fix_pr']}")
    if kind in (READY, FIX):
        lines.append(_recipients(item))
        if kind == FIX:
            lines.append(f":no_entry: Held: {_fix_reason(item)}")
        lines.append("```" + (item.get("draft") or "").strip() + "```")
        if kind == READY:
            lines.append(f"`send {n}` · `edit {n}: ...` · `redraft {n}: ...` "
                         f"· `hold {n}`")
        else:
            lines.append(f"`edit {n}: ...` · `redraft {n}: ...` · "
                         f"`done {n}`")
        return "\n".join(lines)

    asked = card.get("live_question") or item.get("summary") or ""
    lines.append(f"*Waiting on:* {asked}")
    if card.get("situation"):
        lines.append(f"*Situation:* {card['situation']}")
    if card.get("existing_work"):
        lines.append(f"*Existing:* {card['existing_work']}")
    code = card.get("code_check") or {}
    if isinstance(code, dict) and code.get("verdict") not in (
            None, "", "not_checked"):
        lines.append(f"*Code check:* {code.get('verdict')} "
                     f"({code.get('confidence') or '?'}) "
                     f"{code.get('finding') or ''}")
    if card.get("why_human"):
        lines.append(f"*Why you:* {card['why_human']}")
    if card.get("lane") == "engineer" and card.get("escalation_reason"):
        reason = card["escalation_reason"]
        lines.append(f"*Needs an engineer:* "
                     f"{ESCALATION_LABELS.get(reason, reason)}")
    if card.get("recommended_action"):
        lines.append(f"*Recommended:* {card['recommended_action']}")
    if is_design_question(item):
        if (code or {}).get("fix"):
            lines.append(f"*If you change it:* {code['fix']}")
        draft = (item.get("draft") or "").strip()
        if draft:
            lines.append("*If you answer it as how it works instead:* "
                         + _recipients(item))
            lines.append("```" + draft + "```")
        lines.append(f"`fix {n}` opens the PR · `log {n}` logs it as a "
                     f"request · " + (f"`send {n}` sends the answer · "
                                      if draft else "")
                     + f"`edit {n}: <reply>` · `hold {n}`")
        return "\n".join(lines)
    lines.append(f"`route {n} to onlyg` · `route {n} to sanjay` · "
                 f"`log {n}` · `build {n}` · `edit {n}: <reply>` · "
                 f"`hold {n}` · `done {n}`")
    return "\n".join(lines)


def week_line(week: dict) -> str:
    """"this week 12 sent, 2 to engineers (onlyg 2; why: data lookup 1,
    mobile 1)". What still escalates, and why, in one line (G27)."""
    routed = ", ".join(f"{k} {v}" for k, v in
                       sorted(week.get("by_engineer", {}).items()))
    why = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in
                    sorted(week.get("by_reason", {}).items()))
    detail = "; ".join(p for p in (routed, f"why: {why}" if why else "")
                       if p)
    return (f"this week {week.get('sent', 0)} sent, "
            f"{week.get('routed', 0)} to engineers"
            + (f" ({detail})" if detail else ""))


def _parts(blocks: list[str]) -> list[str]:
    """Pack blocks into as few messages as Slack displays comfortably."""
    parts: list[str] = []
    current = ""
    for block in blocks:
        joined = f"{current}\n\n{block}" if current else block
        if current and len(joined) > PART_CHARS:
            parts.append(current)
            current = block
        else:
            current = joined
    if current:
        parts.append(current)
    return parts


def render(batch_number: int, picked: dict, context: dict,
           now: datetime) -> tuple[str, list[str]]:
    """(top-level message, thread messages)."""
    resolutions = context.get("resolutions") or set()
    ready, fix, decide = picked[READY], picked[FIX], picked[DECIDE]
    needs = len(ready) + len(fix) + len(decide)
    title = context.get("title") or "Support batch"
    top = [f":inbox_tray: *{title} {batch_number}* · {_clock(now)} · "
           f"*{needs} need you*"]
    since = context.get("since") or ""
    if since:
        top.append(since)
    if ready:
        word = "all" if len(ready) > 2 else "both" if len(ready) == 2 else ""
        top.append(f"\n*READY TO SEND ({len(ready)})* · `send ready` sends "
                   f"{word + ' ' if word else ''}"
                   f"{'of them' if word else 'it'} · each reply is in full "
                   "in the thread")
        top += [_index_line(i, resolutions, now) for i in ready]
    if fix:
        top.append(f"\n*FIX FIRST ({len(fix)})*")
        top += [_index_line(i, resolutions, now) for i in fix]
    if decide:
        top.append(f"\n*NEEDS YOUR DECISION ({len(decide)})*")
        top += [_index_line(i, resolutions, now) for i in decide]
    if not needs:
        top.append("\nNothing needs you right now.")
    if picked.get("more"):
        top.append(f"\n_{picked['more']} more need you after these. "
                   "Say `next` for them._")

    owed = picked.get(OWED_NOTHING) or []
    if owed:
        numbers = " ".join(str(i.get("number")) for i in owed)
        top.append(f"\n*NOTHING OWED ({len(owed)})* · we asked, they have not "
                   f"answered · `done {numbers}` clears them")
        top += [f"*{i.get('number')}* {_zoho(i)} {_account(i)}"
                + (f" · waiting {age(i, now)}" if age(i, now) else "")
                for i in owed]

    closing = picked.get(QUIET) or []
    if closing:
        top.append(f"\n*CLOSE QUIETLY ({len(closing)})* · nothing owed, no "
                   "reply · `close quiet` closes them in Zoho with no email")
        top += [f"*{i.get('number')}* {_zoho(i)} {_account(i)} · "
                + _short((i.get("card") or {}).get("live_question")
                         or (i.get("card") or {}).get("situation")
                         or i.get("summary"), 110)
                for i in closing]

    quiet = []
    held = picked.get("held") or []
    if held:
        wakes = [i.get("wake_at") for i in held
                 if isinstance(i.get("wake_at"), datetime)]
        quiet.append(f"{len(held)} held" + (
            f" (next back {_clock(min(wakes))})" if wakes else ""))
    if context.get("prs_open"):
        quiet.append(f"{context['prs_open']} agent fix PR(s) waiting on a "
                     "merge")
    if context.get("release_waiting"):
        quiet.append(f"{context['release_waiting']} fix(es) waiting on a "
                     "release")
    week = context.get("week") or {}
    if week:
        quiet.append(week_line(week))
    if context.get("untriaged") is not None:
        count = context["untriaged"]
        quiet.append(f"{count} open ticket(s) not triaged yet"
                     + (" (`clear the queue`)" if count else ""))
    elif context.get("queue_error"):
        quiet.append(f"Zoho queue could not be read ({context['queue_error']})"
                     ", so I cannot say what is untriaged")
    for problem in context.get("release_problems") or []:
        quiet.append(f":warning: release check {problem}")
    if quiet:
        top.append("\n_Not needing you:_ " + " · ".join(quiet))
    top.append("\nReply in this thread: `send ready` · `send 4 7` · "
               "`edit 3: ...` · `hold 1 till mon` · `route 1 to onlyg` · "
               "`next` · or just ask me about any of them.")

    blocks: list[str] = []
    for name, items in (("READY TO SEND", ready), ("FIX FIRST", fix),
                        ("NEEDS YOUR DECISION", decide)):
        for index, item in enumerate(items):
            body = detail(item, resolutions, now)
            blocks.append(f"*{name}*\n\n{body}" if index == 0 else body)
    return "\n".join(top), _parts(blocks)


# ---------------------------------------------------------------------------
# Context the batch reads (each part fails soft)
# ---------------------------------------------------------------------------

def _resolution_tickets() -> set:
    rows = store.query(
        "SELECT ticket_id FROM vomeos_release_resolutions"
        " WHERE outcome IN ('drafted', 'held', 'no_draft')"
    )
    return {str(r["ticket_id"]) for r in rows}


def _release_context(since: datetime | None) -> dict:
    rows = store.query(
        "SELECT name, state, live_at FROM vomeos_release_fixes"
    )
    shipped = [r for r in rows if since and isinstance(r.get("live_at"),
                                                       datetime)
               and r["live_at"] > since]
    problems = [f"{r['state']}: {_short(r.get('name'), 80)}" for r in rows
                if r.get("state") in ("failed", "unknown")]
    waiting = [r for r in rows if r.get("state") == "waiting"]
    return {"shipped": shipped, "problems": problems,
            "waiting": len(waiting)}


def _prs_open() -> int:
    """Agent fix PRs opened for tickets and not merged yet (their parent
    has no Fix Commit, so the release watcher has not started on it)."""
    rows = store.query(
        "SELECT COUNT(*) AS n FROM vomeos_ticket_fixes tf"
        " WHERE tf.outcome = 'pr_opened' AND NOT EXISTS (SELECT 1 FROM"
        " vomeos_release_fixes rf WHERE rf.task_id = tf.parent_task)"
    )
    return int(rows[0]["n"]) if rows else 0


def _queue_state(items: list[dict]) -> tuple[int | None, str]:
    """Read the Zoho queue once: finish items handled outside the sprint,
    and count open tickets with no item. (None, why) when Zoho is
    unreadable, and then nothing is finished."""
    try:
        tickets = _queue()
    except Exception as exc:
        return None, _short(str(exc), 80)
    open_ids = {str(t.get("id")) for t in tickets}
    sync_handled_elsewhere(items, open_ids)
    known = {str(i.get("ticket_id")) for i in sprint_store.working_set()}
    missing = [i for i in open_ids if i not in known]
    if missing:
        rows = store.query(
            f"SELECT ticket_id FROM {sprint_store.ITEMS}"
            " WHERE ticket_id = ANY(:ids)", {"ids": missing})
        known |= {str(r["ticket_id"]) for r in rows}
    return len([i for i in open_ids if i not in known]), ""


def fill_received(items: list[dict], limit: int = 40) -> int:
    """Look up when the customer last wrote, once, for items stored before
    that was recorded. Without it a months-old ticket triaged today shows
    as "new" (the first batch preview, #7688)."""
    import zoho_actions

    filled = 0
    for item in items:
        rcpt = item.get("recipients") or {}
        if rcpt.get("received_at") or filled >= limit:
            continue
        if item.get("status") not in sprint_store.CARRIES:
            continue
        try:
            latest = zoho_actions.latest_inbound(item["ticket_id"])
        except Exception as exc:
            print(f"[BATCH] no received time for {item.get('id')}: {exc}")
            continue
        value = str(latest.get("createdTime") or "")
        if not value:
            continue
        sprint_store.set_received_at(item["id"], value)
        item["recipients"] = {**rcpt, "received_at": value}
        filled += 1
    return filled


def _since_line(items: list[dict], prev: dict, release: dict) -> str:
    when = prev.get("posted_at")
    if not isinstance(when, datetime):
        return ""
    new = [i for i in items if isinstance(i.get("arrived_at"), datetime)
           and i["arrived_at"] > when]
    sent = [i for i in items if isinstance(i.get("sent_at"), datetime)
            and i["sent_at"] > when]
    parts = [f"{len(new)} new", f"{len(sent)} sent"]
    if release.get("shipped"):
        parts.append(f"{len(release['shipped'])} fix(es) went live")
    return f"Since batch {prev.get('id')} ({_clock(when)}): " + \
        ", ".join(parts)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _supersede(prev: dict, ts: str, number: int) -> None:
    """Edit the previous batch down to a pointer, so only one live batch
    shows in the channel. Its thread keeps working for commands."""
    if not prev.get("ts") or prev.get("superseded_at"):
        return
    try:
        import sprint_thread

        client = sprint_thread._slack()
        link = client.chat_getPermalink(channel=prev.get("channel")
                                        or sprint_thread.CHANNEL,
                                        message_ts=ts).get("permalink", "")
        target = f"<{link}|batch {number}>" if link else f"batch {number}"
        client.chat_update(
            channel=prev.get("channel") or sprint_thread.CHANNEL,
            ts=prev["ts"],
            text=(f":inbox_tray: Batch {prev.get('id')}, superseded by "
                  f"{target}. Commands in this thread still work."),
        )
        sprint_store.mark_superseded(prev["id"])
    except Exception as exc:
        print(f"[BATCH] could not supersede batch {prev.get('id')}: {exc}")


def run_batch(kind: str = "scheduled", exclude: set | None = None,
              reply_thread: str = "") -> dict:
    """Post one batch. Safe to run on demand.

    kind: scheduled (weekday), weekend (only if something needs Sam), next
    (skips what the batch Sam replied in already showed), catch_up, clear.
    """
    import sprint_thread

    if not (sprint_store.ready() and sprint_store.batches_ready()):
        return {"status": "no database"}
    sprint_store.carry_forward()
    now = _now()
    items = sprint_store.working_set()
    untriaged, queue_error = _queue_state(items)
    fill_received(items)
    picked = select(items, exclude=exclude, now=now)
    chosen = picked[READY] + picked[FIX] + picked[DECIDE]
    shown = {str(i["id"]): draft_hash(i.get("draft")) for i in chosen}
    prev = sprint_store.latest_batch()

    if kind == "weekend" and not chosen:
        return {"status": "skipped", "reason": "nothing needs Sam"}
    if kind == "scheduled" and prev and prev.get("shown") == shown and \
            chosen:
        oldest = min(chosen, key=lambda i: waiting_since(i) or now)
        sprint_thread._post(
            f"No change since this batch: {len(chosen)} still need you, "
            f"the oldest waiting {age(oldest, now) or 'a while'}.",
            thread_ts=prev["ts"])
        return {"status": "unchanged", "batch": prev.get("id")}

    release = _release_context(prev.get("posted_at") if prev else None)
    context = {
        "resolutions": _resolution_tickets(),
        "since": _since_line(items, prev, release) if prev else "",
        "release_waiting": release["waiting"],
        "prs_open": _prs_open(),
        "release_problems": release["problems"],
        "week": sprint_store.week_counts(),
        "untriaged": untriaged,
        "queue_error": queue_error,
        "title": "Clearing the queue, batch" if kind == "clear"
        else "Support batch",
    }
    number = sprint_store.start_batch(kind)
    top, parts = render(number, picked, context, now)
    ts = sprint_thread._post(top)
    if not ts:
        return {"status": "post failed"}
    for part in parts:
        sprint_thread._post(part, thread_ts=ts)
    ready = {str(i["id"]): draft_hash(i.get("draft")) for i in picked[READY]}
    quiet_ids = [i["id"] for i in picked[QUIET]]
    sprint_store.finish_batch(number, sprint_thread.CHANNEL, ts, shown,
                              ready, quiet_ids)
    if prev:
        _supersede(prev, ts, number)
    if reply_thread and reply_thread != ts:
        sprint_thread._post(f"Posted batch {number} below in the channel.",
                            thread_ts=reply_thread)
    return {"status": "ok", "batch": number, "shown": len(chosen),
            "ready": len(ready), "more": picked["more"]}


def run_scheduled_batch() -> dict:
    return run_batch("scheduled")


def run_weekend_batch() -> dict:
    return run_batch("weekend")


# ---------------------------------------------------------------------------
# Silent triage: the sweep and clear mode
# ---------------------------------------------------------------------------

def _lease(name: str, minutes: int) -> bool:
    """True when this process may do `name` now. Expires on its own."""
    if not store.ensure_table(LEASES):
        return False
    now = _now()
    rows = store.query(
        f"INSERT INTO {LEASES} (name, until) VALUES (:n, :u)"
        f" ON CONFLICT (name) DO UPDATE SET until = EXCLUDED.until"
        f" WHERE {LEASES}.until < :now RETURNING name",
        {"n": name, "u": now + timedelta(minutes=minutes), "now": now},
        write=True,
    )
    return bool(rows)


CLEAR_PAGES = 10


def _queue(pages: int | None = None) -> list[dict]:
    """Open (New / Processing) tickets. Raises when Zoho cannot be read:
    an unreadable queue is not an empty one."""
    import sprint

    return sprint.fetch_open_tickets(1000, max_pages=pages)


def _needs_triage(ticket: dict, fresh_since: datetime | None) -> bool:
    item = sprint_store.by_ticket(str(ticket.get("id")))
    if not item:
        return True
    if fresh_since is None:
        return False
    updated = item.get("updated_at")
    return (item.get("status") in sprint_store.CARRIES
            and isinstance(updated, datetime) and updated < fresh_since)


def triage_untriaged(limit: int, ignore_cap: bool = False,
                     fresh_since: datetime | None = None,
                     pages: int | None = None) -> dict:
    """Triage open tickets silently, oldest activity first.

    Normally only tickets with no item. With `fresh_since` ("clear the queue
    fresh"), also every open item last triaged before then, so the whole
    queue is judged by today's rules. A per-ticket lease keeps the sweep,
    clear mode and a second worker from triaging the same ticket at once.
    """
    import shadow_triage

    if not sprint_store.ready():
        return {"status": "no database"}
    try:
        tickets = _queue(pages)
    except Exception as exc:
        return {"status": "queue unreadable", "error": str(exc)}
    pending = [t for t in tickets if _needs_triage(t, fresh_since)]
    triaged = failed = 0
    for ticket in pending:
        if triaged >= limit:
            break
        ticket_id = str(ticket.get("id"))
        if not _needs_triage(ticket, fresh_since) or \
                not _lease(f"triage:{ticket_id}", 20):
            continue
        try:
            outcome = shadow_triage._handle(
                ticket_id, "clear_fresh" if fresh_since else "sweep",
                ignore_cap=ignore_cap, refresh=fresh_since is not None)
        except Exception as exc:
            print(f"[BATCH] triage of {ticket_id} failed: {exc}")
            outcome = {"status": "error"}
        if outcome.get("status") == "ok":
            triaged += 1
        else:
            failed += 1
    return {"status": "ok", "open": len(tickets), "triaged": triaged,
            "failed": failed,
            "untriaged": max(len(pending) - triaged - failed, 0)}


def run_queue_sweep() -> dict:
    """Every 30 minutes: tickets a webhook missed, a few at a time."""
    return triage_untriaged(SWEEP_CAP)


def sync_handled_elsewhere(items: list[dict], open_ids: set) -> int:
    """Finish items whose ticket was handled outside the sprint.

    Someone closed it in Zoho, or moved it to In Progress or Awaiting
    Client Response: it no longer needs Sam here. The queue read only covers
    recent tickets, so a ticket missing from it is asked about directly
    before anything is finished. A resolution waiting to be sent, or a
    ticket with an agent fix PR, stays: those tickets usually sit In
    Progress while the fix is built, and the client is still owed a reply.
    """
    import zoho_actions

    keep = _resolution_tickets()
    finished = 0
    for item in items:
        ticket_id = str(item.get("ticket_id") or "")
        if (item.get("status") not in sprint_store.CARRIES
                or ticket_id in open_ids or ticket_id in keep
                or item.get("fix_pr")):
            continue
        try:
            status = zoho_actions.ticket_status(ticket_id)
        except Exception as exc:
            print(f"[BATCH] status of {ticket_id} unreadable: {exc}")
            continue
        if not status or status in ("New", "Processing"):
            continue
        sprint_store.decide(item["id"], sprint_store.DONE,
                            f"handled outside the sprint (Zoho: {status})")
        item["status"] = sprint_store.DONE
        finished += 1
    return finished


def _fresh_start(thread: str, now: datetime) -> datetime:
    """When this thread's unfinished fresh clear began, else `now`.

    Saying `clear the queue fresh` again after a run stopped picks up where
    it left off: tickets refreshed since the first request are not triaged
    a second time (2026-10-18, a Zoho read failed after 25 of 46). The
    request time comes from the conversation record; a run is finished once
    a clear batch has posted after it.
    """
    import sprint_thread

    rows = store.query(
        f"SELECT text, created_at FROM {sprint_thread.TURNS_TABLE}"
        " WHERE thread_ts = :t AND role = 'sam' AND created_at >= :since"
        " ORDER BY created_at",
        {"t": thread, "since": now - timedelta(hours=6)})
    done = store.query(f"SELECT MAX(posted_at) AS at FROM "
                       f"{sprint_store.BATCHES} WHERE kind = 'clear'")
    done_at = done[0].get("at") if done else None
    for row in rows:
        match = sprint_thread._CLEAR.match(row.get("text") or "")
        at = row.get("created_at")
        if not (match and match.group("fresh") and isinstance(at, datetime)):
            continue
        if isinstance(done_at, datetime) and at <= done_at:
            continue
        return at
    return now


def start_clear(message_ts: str, thread: str, fresh: bool = False) -> str:
    """Sam said `clear the queue` (or `clear the queue fresh`)."""
    try:
        tickets = _queue(CLEAR_PAGES)
    except Exception as exc:
        return (f"I could not read the Zoho queue ({exc}), so I cannot "
                "clear it. Try again in a few minutes.")
    now = _now()
    since = _fresh_start(thread, now) if fresh else now
    resumed = fresh and since < now - timedelta(minutes=1)
    todo = [t for t in tickets if _needs_triage(t, since if fresh else None)]
    try:
        from vomeos.worker import enqueue_event

        enqueue_event(CLEAR_KEY, {
            "ts": message_ts, "thread": thread, "round": 1,
            "fresh_since": since.isoformat() if fresh else ""},
            queue="support")
    except Exception as exc:
        return f"Could not start clearing the queue ({exc})."
    if not todo:
        return ("Every open ticket is already triaged. Posting a fresh "
                "batch of what needs you now.")
    rounds = -(-len(todo) // BATCH_SIZE)
    if resumed:
        return (f"Picking up the fresh run from {_clock(since)}: "
                f"{len(todo)} ticket(s) left, the ones already done are "
                f"kept (about {rounds} round(s)). The batch posts when the "
                "last round is done.")
    if fresh:
        return (f"Triaging all {len(todo)} open ticket(s) again under "
                f"today's rules, {BATCH_SIZE} per round (about {rounds} "
                f"round(s), roughly a minute per ticket). I post progress "
                "here and the batch when the last round is done. Nothing is "
                "sent until you say send.")
    return (f"Clearing the queue: {len(todo)} untriaged ticket(s). I triage "
            f"them {BATCH_SIZE} at a time, oldest first, and post the batch "
            f"after the first round (about {rounds} round(s), roughly a "
            "minute per ticket). Nothing is sent until you say send.")


def handle_queue_clear(message: dict) -> dict:
    """One round of clear mode. Idempotent on Sam's message ts and round.

    Plain clear: the first round posts the batch Sam works from, later
    rounds only say the next tickets are ready (a new batch every few
    minutes would bury the one he is in). Fresh: every round reports
    progress, and the batch posts once, when the last round is done, so it
    is judged entirely by today's rules.
    """
    import sprint_thread

    key = f"clear:{message.get('ts')}:{message.get('round')}"
    if not _lease(key, 24 * 60):
        return {"status": "duplicate"}
    fresh_since = _parse_time(message.get("fresh_since")) \
        if message.get("fresh_since") else None
    thread = message.get("batch_ts") or message.get("thread") or ""
    for pause in (0, 30, 90):
        # A Zoho hiccup is retried here rather than ending the run.
        if pause:
            time.sleep(pause)
        result = triage_untriaged(BATCH_SIZE, ignore_cap=True,
                                  fresh_since=fresh_since, pages=CLEAR_PAGES)
        if result.get("status") != "queue unreadable":
            break
    if result.get("status") != "ok":
        again = ("`clear the queue fresh`" if fresh_since is not None
                 else "`clear the queue`")
        sprint_thread._post(
            f"Clearing paused: Zoho could not be read after three tries "
            f"({_short(result.get('error') or result.get('status'), 120)}). "
            f"Say {again} again in this thread to pick up where it "
            "stopped; nothing already triaged is redone.", thread_ts=thread)
        return result
    round_number = int(message.get("round") or 1)
    left = result.get("untriaged") or 0
    more = bool(left and result.get("triaged") and round_number < 10)
    posted: dict = {}
    if fresh_since is not None:
        if more:
            sprint_thread._post(
                f"Round {round_number}: {result.get('triaged')} triaged, "
                f"{left} to go.", thread_ts=thread)
        else:
            posted = run_batch("clear", reply_thread=thread)
    elif round_number == 1:
        posted = run_batch("clear", reply_thread=thread)
        message = {**message, "batch_ts": _batch_ts(posted)}
    else:
        sprint_thread._post(
            f"Round {round_number}: {result.get('triaged')} more triaged"
            + (f", {left} still to go" if left else ", the queue is clear")
            + ". Say `next` for the batch.", thread_ts=thread)
    if more:
        from vomeos.worker import enqueue_event

        enqueue_event(CLEAR_KEY, {**message, "round": round_number + 1},
                      queue="support")
    return {**result, "batch": posted.get("batch")}


def _batch_ts(posted: dict) -> str:
    latest = sprint_store.latest_batch()
    if posted.get("batch") and latest.get("id") == posted.get("batch"):
        return str(latest.get("ts") or "")
    return ""
