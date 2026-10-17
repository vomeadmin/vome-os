"""
shadow_triage.py

When a ticket arrives or a customer replies, do the sprint work for it at
once: read the whole thread and its screenshots, check for existing work and
a linked fix, look for an incident, draft the reply, and put a numbered card
in today's #support-sprints thread.

WHY "SHADOW"
------------
Nothing here acts on the ticket. It writes no Zoho status, sends no email and
creates no task. Its whole output is a card and a draft waiting for Sam, so
the work is ready when he sits down instead of starting when he does. The old
paths (auto-ack, the #vome-tickets brief) keep running beside it until Sam
retires them.

FLOW
----
    webhook (web)  ->  enqueue customer_success.shadow_triage  ->  worker:
      1. newest customer message; skip if this exact message was triaged
      2. context: thread, images, CRM tier, existing work, help centre,
         and the release state of any Agent Fixes task for this ticket
      3. triage director (senior, sees the images)  -> the card
      4. incident matcher (fast) against the last 24h and Sentry
      5. reply drafter (standard, every outbound guard)  -> the draft
      6. numbered item in sprint_store, card posted in the day's thread

Idempotency is the customer message id (`last_thread_id`): Zoho fires more
than one webhook per reply and Celery delivers at least once.

SWITCHES
--------
    SHADOW_TRIAGE_ENABLED     default on. "false" queues nothing.
    SHADOW_TRIAGE_DAILY_CAP   tickets triaged per day, default 60.
"""

from __future__ import annotations

import os
import re

import sprint_store

HANDLER_KEY = "customer_success.shadow_triage"
QUEUE = "support"

OPEN_STATUSES = ("New", "Processing")

RESOLUTION_NOTES = (
    "This is a resolution reply: the linked fix is live. Say only that the "
    "update is live, what they will notice, and invite them to try it. Do "
    "not apologise, do not refer to anything we sent before, and do not "
    "bring up any other topic from the thread."
)

# Categories that get a drafted reply. The rest wait for Sam's decision:
# a feature request may already exist, needs_human needs a person, an
# escalation is an engineer asking Sam, and awaiting_client owes nothing.
DRAFT_CATEGORIES = {
    "clear_bug", "vague_bug", "user_education", "admin_action",
    "misdirected", "email_update", "account_deletion",
}


def enabled() -> bool:
    value = os.environ.get("SHADOW_TRIAGE_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def daily_cap() -> int:
    return int(os.environ.get("SHADOW_TRIAGE_DAILY_CAP", "60"))


def enqueue(ticket_id: str, reason: str) -> bool:
    """Called from the Zoho webhooks. Cheap, never raises."""
    if not enabled() or not ticket_id or ticket_id == "unknown":
        return False
    try:
        from vomeos.worker import enqueue_event

        enqueue_event(
            HANDLER_KEY, {"ticket_id": str(ticket_id), "reason": reason},
            queue=QUEUE,
        )
        return True
    except Exception as exc:
        print(f"[SHADOW] could not queue {ticket_id}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Release state: is there an agent fix for this ticket, and is it live?
# ---------------------------------------------------------------------------

def release_state(ticket_number: str, ticket_id: str) -> dict:
    """The Agent Fixes parent for this ticket, and its Release Check.

    Returns {"text", "live", "task_id"}. "text" goes to the agents, so it
    says plainly what is and is not backed.
    """
    try:
        import release_watcher as rw

        tasks = rw._list_on_dev_tasks()
    except Exception as exc:
        return {"text": f"Agent Fixes could not be read ({exc}). Treat any "
                        "fix as UNVERIFIED.", "live": False, "task_id": ""}

    marker = f"[#{ticket_number}]"
    for child in tasks:
        link = str(rw._field(child, rw.FIELD_ZOHO_TICKET_LINK).get("value")
                   or "")
        if marker not in (child.get("name") or "") and ticket_id not in link:
            continue
        parent_id = child.get("parent") or child.get("id")
        parent = next((t for t in tasks if t.get("id") == parent_id), child)
        check = rw._release_check_value(parent) or "waiting"
        commits = rw._field(parent, rw.FIELD_FIX_COMMIT).get("value") or ""
        name = parent.get("name") or ""
        if check == "live":
            text = (f"LIVE. Agent fix {parent_id} ({name}) is deployed to "
                    f"production ({commits}). A resolution reply is backed.")
        else:
            text = (f"NOT LIVE. Agent fix {parent_id} ({name}) is merged to "
                    f"the dev branch only, Release Check {check}. Nothing "
                    "may be described to the customer as fixed.")
        return {"text": text, "live": check == "live", "task_id": parent_id}
    return {"text": "No agent fix is linked to this ticket.", "live": False,
            "task_id": ""}


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

def _recent_errors() -> str:
    """Actionable production errors from the last 48h, from the ledger."""
    try:
        from vomeos import store

        rows = store.query(
            "SELECT issue_id, title, culprit, first_seen_at, times_seen"
            " FROM vomeos_sentry_issues WHERE status <> 'gated'"
            " AND verdict = 'actionable'"
            " AND first_seen_at >= NOW() - INTERVAL '48 hours'"
            " ORDER BY first_seen_at DESC LIMIT 15"
        )
    except Exception:
        rows = []
    if not rows:
        return "No production errors in the last 48 hours."
    return "\n".join(
        f"Sentry {r['issue_id']}: {r['title']}\n   in {r['culprit']}, first "
        f"seen {str(r['first_seen_at'])[:16]} UTC, "
        f"{r.get('times_seen') or 0} events"
        for r in rows
    )


def _recent_tickets(exclude_ticket: str) -> str:
    rows = [r for r in sprint_store.recent(24)
            if str(r.get("ticket_number")) != str(exclude_ticket)]
    if not rows:
        return "No other tickets triaged in the last 24 hours."
    lines = []
    for r in rows[:25]:
        card = r.get("card") or {}
        lines.append(
            f"- #{r.get('ticket_number')} {r.get('account') or ''} "
            f"({str(r.get('arrived_at'))[:16]} UTC)\n"
            f"   Situation: {card.get('situation') or r.get('summary') or ''}"
            f"\n   Live question: {card.get('live_question') or ''}"
        )
    return "\n".join(lines)


def match_incident(ticket_number: str, account: str, card: dict,
                   when: str) -> dict:
    """Run the matcher. {} when nothing matches or it could not run."""
    from vomeos import run_agent

    new_ticket = (
        f"#{ticket_number} {account} ({when})\n"
        f"Situation: {card.get('situation') or ''}\n"
        f"Live question: {card.get('live_question') or ''}"
    )
    result = run_agent(
        "customer_success.incident_matcher",
        context={
            "new_ticket": new_ticket,
            "recent_tickets": _recent_tickets(ticket_number),
            "recent_errors": _recent_errors(),
        },
        subject_type="zoho_ticket",
        subject_id=str(ticket_number),
    )
    if not result.ok or not result.data:
        return {}
    data = result.data
    same = [str(n) for n in data.get("same_as") or [] if str(n).isdigit()]
    sentry = str(data.get("sentry_issue") or "").strip()
    # Only a high-confidence match interrupts Sam. The first shadow run on
    # #9500 (Oct 17) paired it with #9415 at medium confidence because both
    # once mentioned a form "not saving"; they are different bugs. A false
    # page costs more trust than a missed one costs time, since the next
    # scheduled update shows every card anyway.
    if (not same and not sentry) or data.get("confidence") != "high":
        return {}
    key = f"sentry:{sentry}" if sentry else (
        "tickets:" + min(same + [str(ticket_number)], key=int)
    )
    return {"key": key, "same_as": same, "sentry_issue": sentry,
            "symptom": data.get("symptom") or "",
            "reason": data.get("reason") or "",
            "confidence": data.get("confidence") or ""}


def _drop_known_different(incident: dict, release: dict) -> dict:
    """Remove tickets already tied to a different fix.

    The rehearsal paired #9415 (PDF highlighting) with #9436 (a note that
    would not delete): same customer, both forms, different bugs, and each
    already has its own Agent Fixes parent. Linked work is a fact; the
    matcher's similarity is a guess, so the fact wins.
    """
    if not incident or not release.get("task_id"):
        return incident
    keep = []
    for number in incident.get("same_as") or []:
        other = release_state(str(number), "")
        if other.get("task_id") and other["task_id"] != release["task_id"]:
            continue
        keep.append(number)
    if not keep and not incident.get("sentry_issue"):
        return {}
    return {**incident, "same_as": keep}


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def draft_reply(ticket_number: str, card: dict, context: dict,
                rcpt: dict, release: dict, language: str = "English",
                sam_notes: str = "") -> dict:
    """{"text", "status", "why"}. status: ok | blocked | error | none."""
    from vomeos import run_agent

    category = str(card.get("category") or "")
    guard_context = {"contact_name": rcpt.get("first_name") or ""}
    if release.get("live"):
        guard_context.update({
            "verified_fix_task": release.get("task_id") or "",
            "verified_fix_status": "on prod",
            "resolution_reply": True,
        })
    result = run_agent(
        "customer_success.reply_drafter",
        context={
            "ticket_number": str(ticket_number),
            "contact_first_name": rcpt.get("first_name") or "",
            "language": language,
            "category": category,
            "live_question": card.get("live_question") or "",
            "situation": card.get("situation") or "",
            "existing_work": card.get("existing_work") or "",
            "release_state": release.get("text") or "",
            "blocking_claims": card.get("blocking_claims") or "",
            "missing_information": card.get("missing_information") or "",
            "kb_matches": context.get("kb_matches") or "",
            "thread": context.get("thread") or "",
            "sam_notes": sam_notes or "",
        },
        subject_type="zoho_ticket",
        subject_id=str(ticket_number),
        guard_context=guard_context,
    )
    if result.status == "error":
        return {"text": "", "status": "error", "why": result.error}
    return {"text": result.text, "status": result.status,
            "why": "" if result.ok else result.why()}


def _language(thread: str) -> str:
    """French when the customer writes French. The cheap, reliable signal."""
    french = len(re.findall(
        r"\b(bonjour|merci|nous|vous|est|pas|les|des|une|pour|avec)\b",
        (thread or "").lower(),
    ))
    english = len(re.findall(
        r"\b(the|and|you|is|not|for|with|this|that|have)\b",
        (thread or "").lower(),
    ))
    return "French" if french > english else "English"


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------

def _triaged_today() -> int:
    from vomeos import store

    rows = store.query(
        f"SELECT COUNT(*) AS n FROM {sprint_store.ITEMS}"
        " WHERE updated_at >= date_trunc('day', NOW())"
    )
    return int(rows[0]["n"]) if rows else 0


def handle(message: dict) -> dict:
    """One ticket. Never raises: a lost card is an item Sam never sees."""
    ticket_id = str(message.get("ticket_id") or "")
    if not ticket_id or not enabled():
        return {"status": "skipped", "reason": "disabled or no ticket"}
    if not sprint_store.ready():
        return {"status": "skipped", "reason": "no database"}
    try:
        return _handle(ticket_id, message.get("reason") or "")
    except Exception as exc:
        print(f"[SHADOW] {ticket_id} failed: {type(exc).__name__}: {exc}")
        return {"status": "error", "error": str(exc)}


def _handle(ticket_id: str, reason: str, force: bool = False,
            release: dict | None = None) -> dict:
    """`release` is the release state when the caller already knows it.

    The release watcher passes the fix it just proved live, so a second
    ClickUp lookup cannot lose it: in the 2026-10-17 rehearsal a throttled
    lookup came back empty and #9500 got no resolution draft at all.
    """
    import zoho_actions
    from vomeos import run_agent

    rcpt = zoho_actions.recipients(ticket_id)
    thread_id = rcpt.get("thread_id") or ""
    existing = sprint_store.by_ticket(ticket_id)
    if (not force and existing and thread_id
            and existing.get("last_thread_id") == thread_id
            and existing.get("status") in sprint_store.CARRIES):
        return {"status": "skipped", "reason": "already triaged"}
    if not force and _triaged_today() >= daily_cap():
        return {"status": "skipped", "reason": "daily cap reached"}

    import sprint

    row = {"id": ticket_id, "ticketNumber": ""}
    context = sprint.build_context(row)
    if not context:
        return {"status": "skipped", "reason": "ticket unreadable"}
    status = context.pop("_status", "")
    if status and status not in OPEN_STATUSES and not force:
        return {"status": "skipped", "reason": f"ticket is {status}"}

    ticket_number = context.get("ticket_number") or ticket_id
    subject = context.pop("_subject", "")
    images = context.pop("_images", [])
    # The CRM sometimes returns ARR as text ("7000"); a comparison on that
    # crashed the whole triage when the card itself was empty.
    try:
        arr = float(context.pop("_arr", 0) or 0)
    except (TypeError, ValueError):
        arr = 0.0
    context.pop("_ticket_id", None)

    release = release or release_state(ticket_number, ticket_id)
    if release.get("task_id"):
        context["existing_tasks"] = (
            f"{context.get('existing_tasks', '')}\n\nRELEASE STATE: "
            f"{release['text']}"
        )

    result = run_agent(
        sprint.AGENT, context=context, subject_type="zoho_ticket",
        subject_id=ticket_id, images=images,
    )
    if not result.ok:
        card = {"category": "needs_human", "confidence": "low",
                "situation": f"Triage did not produce a card "
                             f"({result.why()}). Read the ticket in Zoho.",
                "why_human": "triage failed"}
    else:
        card = result.data or {}

    from error_reports import is_error_report

    when = sprint_store._now().strftime("%Y-%m-%d %H:%M UTC")
    incident = {}
    try:
        incident = match_incident(ticket_number, context.get("account", ""),
                                  card, when)
        incident = _drop_known_different(incident, release)
    except Exception as exc:
        print(f"[SHADOW] incident check failed on {ticket_number}: {exc}")

    category = str(card.get("category") or "")
    draft = {"text": "", "status": "none", "why": ""}
    # A fix proven live owes the customer a resolution whatever the card's
    # category (#9500 was needs_human because of an earlier wrong reply;
    # the resolution is exactly what she is waiting for). Sam still reviews
    # it, and release_resolutions never auto-sends a needs_human ticket.
    owed = category in DRAFT_CATEGORIES or (
        release.get("live") and category not in ("misdirected",
                                                 "account_deletion",
                                                 "email_update"))
    if owed and not is_error_report(subject):
        resolution_card = card
        notes = ""
        if release.get("live"):
            # The "unbacked claim" note exists to stop a draft repeating a
            # promise. Once the fix is live it only pulls the draft backwards
            # ("Apologies for that, the note was not removed before", the
            # #9436 rehearsal). A resolution is the fix, nothing else.
            resolution_card = {**card, "blocking_claims": ""}
            notes = RESOLUTION_NOTES
        draft = draft_reply(
            ticket_number, resolution_card, context, rcpt, release,
            language=_language(context.get("thread", "")),
            sam_notes=notes,
        )

    item = sprint_store.upsert_ticket(
        ticket_id=ticket_id,
        ticket_number=ticket_number,
        category=category,
        account=context.get("account", ""),
        subject=subject,
        summary=str(card.get("live_question") or subject)[:300],
        important=bool(card.get("important_client")) or arr > 1500,
        card=card,
        draft=draft["text"],
        draft_status=draft["status"],
        draft_why=draft["why"],
        recipients=rcpt,
        last_thread_id=thread_id,
        incident_key=incident.get("key", ""),
    )
    if not item:
        return {"status": "error", "reason": "could not record the item"}

    # A small, certain bug the code check found: propose the fix as a pull
    # request into the dev branch, and file it in Agent Fixes. Most cards
    # stop at the first rung (see ticket_fix.should_attempt).
    import ticket_fix
    import zoho_actions as _za

    fix = ticket_fix.propose_for_ticket(
        ticket_id, ticket_number, context.get("account", ""), card,
        _za.ticket_url(ticket_id),
    )
    if fix.get("pr_url"):
        sprint_store.set_fix_pr(item["id"], fix["pr_url"])
        item["fix_pr"] = fix["pr_url"]

    import sprint_thread

    sprint_thread.post_card(item, release=release)
    if incident:
        sprint_thread.announce_incident(item, incident)

    print(f"[SHADOW] #{ticket_number} -> item {item.get('number')} "
          f"{category} draft={draft['status']} "
          f"incident={incident.get('key', '')}")
    return {"status": "ok", "item": item.get("number"),
            "category": category, "draft": draft["status"],
            "incident": incident.get("key", "")}
