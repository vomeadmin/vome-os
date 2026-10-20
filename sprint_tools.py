"""
sprint_tools.py

The support sprint, worked from Claude.ai through the Vome OS connector.

WHY
---
Sam, 2026-10-18: he runs sprints in Claude.ai, where they have worked for
him, and wants every bug put through the agents first ("see if they can
uncover the issue, or if the recommendation is still to assign it to the
engineer"). Everything that needs nobody keeps running on the worker: the
Zoho webhook and the 30-minute sweep triage every ticket (card, code check,
draft, and a fix PR when the code check finds a small bug), the release
watcher proves fixes live and drafts resolutions, and shipped_untold drafts
replies for work that shipped with nobody telling the client. These
functions put that work in front of Sam in Claude.ai and let him act on it
through the same guarded paths the sprint thread uses: one send path, one
route path, the same close-the-loop and the same learning.

THE TOOLS (registered in mcp_connector/server.py)
-------------------------------------------------
Read:   sprint_queue, ticket_first_pass, fix_status, sprint_followups
Sam:    propose_fix, send_ticket_reply, route_ticket, close_ticket_quietly,
        log_ticket_request

Anything that writes is refused for anyone but Sam (config.can_triage).
A reply goes out only through send_ticket_reply, which re-checks the draft
against every outbound guard, refuses when the customer has written since
the draft, and needs Sam's own words passed in `sam_said`. Claude.ai also
asks him to approve each call (destructive annotation).
"""

from __future__ import annotations

import re

import sprint_store
from vomeos import store

PROPOSE_FIX_KEY = "customer_success.propose_fix"


def _refuse(message: str):
    from mcp_connector.feature_requests import RequestError

    raise RequestError(message)


def _sam_only(caller: str, what: str) -> None:
    from mcp_connector import config

    if not config.can_triage(caller):
        _refuse(f"{caller or 'this account'} cannot {what}. Only Sam can.")


# ---------------------------------------------------------------------------
# Finding a ticket and describing it
# ---------------------------------------------------------------------------

def find(ticket: str) -> tuple[str, dict]:
    """(Zoho ticket id, sprint item or {}) for "9500", "#9500" or an id."""
    value = str(ticket or "").strip().lstrip("#")
    if not value:
        _refuse("give a ticket number, e.g. 9500")
    if not sprint_store.ready():
        _refuse("the sprint database is not reachable")
    rows = store.query(
        f"SELECT * FROM {sprint_store.ITEMS} WHERE ticket_number = :v"
        " OR ticket_id = :v ORDER BY updated_at DESC LIMIT 1", {"v": value})
    if rows:
        item = sprint_store._decode(rows[0])
        return str(item["ticket_id"]), item
    ticket_id = value if len(value) > 8 else ""
    if not ticket_id:
        import sprint

        row = sprint.resolve_ticket(value) or {}
        ticket_id = str(row.get("id") or "")
    if not ticket_id:
        _refuse(f"no Zoho ticket #{value}")
    return ticket_id, sprint_store.by_ticket(ticket_id)


def _agent_fix(item: dict, card: dict) -> str:
    """The fix's stage when a PR exists; for an agent-fix card without one,
    what the agent's attempt came to, so a decline is never invisible."""
    import sprint_batch

    if item.get("fix_pr"):
        return sprint_batch.fix_stage(item)
    if card.get("lane") != "agent_fix":
        return ""
    try:
        ledger = _ledger(str(item.get("ticket_id") or ""))
    except Exception:
        return ""
    if not ledger:
        return "the agent has not tried a fix yet"
    return (f"the agent tried and did not open a PR ({ledger.get('outcome')}"
            f", {ledger.get('attempted_at')}): "
            f"{str(ledger.get('reason') or '')[:300]}")


def _engineer(item: dict) -> dict:
    """Who has it when triage routed it, read live from ClickUp, so the
    sprint reports where the work stands without triaging it again."""
    filed = item.get("eng_task") or {}
    if not filed.get("url"):
        return {}
    import ticket_routing

    state = ticket_routing.task_state(filed)
    return {"owner": filed.get("owner"), "task": filed.get("url"),
            "status": state.get("status") or "unknown",
            "shipped": bool(state.get("shipped")),
            "urgent": bool(filed.get("urgent")),
            "filed": filed.get("how") or ""}


def view(item: dict) -> dict:
    """One item as the model needs it: the agent's first pass, the draft in
    full, and where it stands."""
    import sprint_batch
    import zoho_actions

    card = item.get("card") or {}
    code = card.get("code_check") if isinstance(
        card.get("code_check"), dict) else {}
    updated = item.get("updated_at")
    recipients = item.get("recipients") or {}
    client = (item.get("account") or "").split("(")[0].strip()
    contact = " ".join(p for p in (
        recipients.get("first_name") or "",
        f"<{recipients['to']}>" if recipients.get("to") else "",
    ) if p)
    # One line Sam reads first on every card, so he always knows which
    # client and which person an item is before judging how hard to check it.
    header = " | ".join(p for p in (
        f"Item {item.get('number')}",
        f"#{item.get('ticket_number')}",
        item.get("account") or "",
        contact,
        f"waiting {sprint_batch.age(item)}",
        "IMPORTANT" if item.get("important") else "",
    ) if p)
    return {
        "item": item.get("number"),
        "ticket": f"#{item.get('ticket_number')}",
        "header": header,
        "client": client,
        "contact": contact,
        "zoho_url": zoho_actions.ticket_url(item.get("ticket_id")),
        "account": item.get("account"),
        "important": bool(item.get("important")),
        "status": item.get("status"),
        "section": sprint_batch.section(item) or "finished",
        "category": item.get("category"),
        "lane": card.get("lane") or "",
        "confidence": card.get("confidence") or "",
        "waiting": sprint_batch.age(item),
        "triaged_at_utc": updated.isoformat() if hasattr(
            updated, "isoformat") else str(updated or ""),
        "live_question": card.get("live_question") or item.get("summary"),
        "situation": card.get("situation") or "",
        "existing_work": card.get("existing_work") or "",
        "blocking_claims": card.get("blocking_claims") or "",
        "missing_information": card.get("missing_information") or "",
        "code_check": code,
        "escalation_reason": card.get("escalation_reason") or "",
        "why_human": card.get("why_human") or "",
        "recommended_action": card.get("recommended_action") or "",
        "draft": item.get("draft") or "",
        "draft_status": item.get("draft_status") or "none",
        "draft_held_because": item.get("draft_why") or "",
        "recipients": {k: v for k, v in (item.get("recipients") or {}).items()
                       if k in ("to", "cc", "first_name")},
        "fix_pr": item.get("fix_pr") or "",
        # Who owns the ticket now, in words: where the PR stands, or why
        # the agent did not open one.
        "agent_fix": _agent_fix(item, card),
        # The engineer task triage filed or joined, with its live status.
        "engineer": _engineer(item),
        "decision": item.get("decision") or "",
    }


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

SECTIONS = ("ready", "fix", "decide", "quiet", "owed_nothing", "held",
            "in_flight")


def sprint_queue(include_finished: bool = False) -> dict:
    """Every open sprint item, grouped the way Sam works them, important
    then longest waiting, plus how many open Zoho tickets have no card."""
    import sprint_batch

    if not sprint_store.ready():
        _refuse("the sprint database is not reachable")
    items = sprint_store.working_set()
    # Routed by triage and already shipped: the on-prod handler emailed the
    # client, so the acknowledgment waiting here is stale and finishes.
    import sprint_thread

    items = [i for i in items
             if not ((i.get("eng_task") or {}).get("id")
                     and i.get("status") in sprint_store.CARRIES
                     and sprint_thread._engineer_shipped(i))]
    groups: dict[str, list] = {name: [] for name in SECTIONS}
    finished = []
    for item in sorted(items, key=sprint_batch._priority):
        name = sprint_batch.section(item)
        if name in groups:
            groups[name].append(view(item))
        elif include_finished:
            finished.append(view(item))
    untriaged: int | str
    try:
        tickets = sprint_batch._queue()
        known = {str(i.get("ticket_id")) for i in items}
        rows = store.query(
            f"SELECT ticket_id FROM {sprint_store.ITEMS} WHERE ticket_id ="
            " ANY(:ids)", {"ids": [str(t.get("id")) for t in tickets]})
        known |= {str(r["ticket_id"]) for r in rows}
        untriaged = len([t for t in tickets if str(t.get("id")) not in known])
    except Exception as exc:
        untriaged = f"unknown: the Zoho queue could not be read ({exc})"
    out = {
        "counts": {name: len(groups[name]) for name in SECTIONS},
        "untriaged_open_tickets": untriaged,
        "ready_to_send": groups["ready"],
        "fix_first": groups["fix"],
        "needs_decision": groups["decide"],
        "close_quietly": groups["quiet"],
        "nothing_owed": groups["owed_nothing"],
        "held": groups["held"],
        "agent_fixing": groups["in_flight"],
        "how_to_use_this": (
            "ready_to_send drafts are complete; show each one in full and "
            "send only on Sam's explicit word. An item with agent_fix set "
            "is one the agent is fixing: say so plainly (the agent is "
            "fixing this, here is the acknowledgment to send). "
            "agent_fixing lists tickets already acknowledged that the agent "
            "still owns: one line each with agent_fix, nothing to do unless "
            "it says Sam's merge. An item with engineer set is already with "
            "that engineer (triage filed the ClickUp task on its own): do "
            "not route it again, and its reply is an acknowledgment that "
            "the team is on it, with no timeline. When the engineer ships, "
            "the client is emailed and the ticket closes automatically. "
            "needs_decision carries the agent's first pass (code_check, "
            "recommended_action, escalation_reason). Never re-triage a "
            "listed item to learn its status: engineer and agent_fix are "
            "read live. For a ticket not listed, call ticket_first_pass."),
    }
    if include_finished:
        out["finished_recently"] = finished
    return out


def _fresh(ticket_id: str, item: dict) -> bool:
    """The card answers the customer's newest message."""
    import zoho_actions

    if not item:
        return False
    try:
        latest = zoho_actions.latest_inbound(ticket_id)
    except Exception:
        return True  # cannot tell; do not re-triage on a Zoho hiccup
    return str(latest.get("id") or "") == str(item.get("last_thread_id")
                                              or "")


def ticket_first_pass(ticket: str, refresh: bool = False) -> dict:
    """The agents' first pass on one ticket. When the card is missing, out
    of date, or `refresh` is asked for, the triage director runs again on
    the worker (code check, draft, and a fix PR if the bug qualifies) and
    this says when to look again."""
    ticket_id, item = find(ticket)
    if item and not refresh and _fresh(ticket_id, item):
        return {"state": "ready", **view(item),
                "fix": fix_status(ticket).get("summary")}
    from vomeos.worker import enqueue_event

    import shadow_triage

    enqueue_event(shadow_triage.HANDLER_KEY,
                  {"ticket_id": ticket_id, "reason": "claude_ai",
                   "refresh": True}, queue="support")
    return {
        "state": "triaging",
        "ticket": f"#{(item or {}).get('ticket_number') or ticket}",
        "next_step": ("The triage director is reading the thread, the "
                      "screenshots and the code (one to three minutes). "
                      "Call ticket_first_pass again for the card. If the "
                      "code check finds a small, low-risk bug, a fix PR is "
                      "opened on its own and fix_status will show it."),
        "previous_card": view(item) if item else None,
    }


def _ledger(ticket_id: str) -> dict:
    rows = store.query(
        "SELECT outcome, reason, pr_url, parent_task, child_task,"
        " attempted_at FROM vomeos_ticket_fixes WHERE ticket_id = :t",
        {"t": ticket_id})
    return rows[0] if rows else {}


def _resolution(ticket_id: str) -> dict:
    rows = store.query(
        "SELECT parent_id, outcome, item_number, detail FROM"
        " vomeos_release_resolutions WHERE ticket_id = :t"
        " ORDER BY created_at DESC LIMIT 1", {"t": ticket_id})
    return rows[0] if rows else {}


def _production_owner(commits: str) -> str:
    """Who takes a merged fix to production (Sam, 2026-10-18). A fix in
    both repos needs both."""
    owners = []
    if "core@" in commits or "database@" in commits:
        owners.append("OnlyG merges core to production")
    if "web@" in commits:
        owners.append("Sam deploys web to production")
    return " and ".join(owners) or "the repo owner promotes it to production"


def fix_status(ticket: str) -> dict:
    """Where an agent fix for this ticket stands, and whose move it is."""
    import shadow_triage

    ticket_id, item = find(ticket)
    number = str((item or {}).get("ticket_number") or ticket).lstrip("#")
    ledger = _ledger(ticket_id)
    release = shadow_triage.release_state(number, ticket_id)
    resolution = _resolution(ticket_id)
    pr = ledger.get("pr_url") or (item or {}).get("fix_pr") or ""
    if release.get("live"):
        if (item or {}).get("status") == sprint_store.SENT:
            summary = "Fixed, live, and the client has been told."
        elif resolution.get("outcome") in ("drafted", "held"):
            summary = ("Live. The resolution reply is drafted: show it and "
                       "send it with send_ticket_reply on Sam's word.")
        else:
            summary = ("Live. The resolution reply is being drafted by the "
                       "release watcher (every 15 minutes).")
    elif release.get("task_id"):
        summary = ("Merged to the dev branch, not live yet. Next: "
                   f"{_production_owner(release.get('text') or '')}. The "
                   "release watcher proves it live.")
    elif ledger.get("outcome") == "pr_opened":
        summary = (f"Agent fix PR open: {pr}. Next: Sam reviews and merges "
                   "it into the dev branch.")
    elif ledger:
        summary = (f"The agent tried and did not open a PR "
                   f"({ledger.get('outcome')}: {ledger.get('reason')}). "
                   "Next: Sam. Run propose_fix again, or route it to an "
                   "engineer with the code check.")
    else:
        code = ((item or {}).get("card") or {}).get("code_check") or {}
        summary = ("No agent fix. Code check: "
                   f"{code.get('verdict') or 'none'}"
                   + (f" ({code.get('confidence')}, risk "
                      f"{code.get('fix_risk')})" if code else "")
                   + ". propose_fix asks the agent to try (low risk only).")
    return {"ticket": f"#{number}", "summary": summary, "pr_url": pr,
            "fix_attempt": {k: str(v) for k, v in ledger.items()},
            "release_state": release.get("text"),
            "resolution": {k: str(v) for k, v in resolution.items()}}


def sprint_followups() -> dict:
    """What happened since the last sprint that needs a follow-up: replies
    owed for work that shipped, PRs waiting on Sam's merge, fixes waiting
    on production, and release checks that failed."""
    if not sprint_store.ready():
        _refuse("the sprint database is not reachable")
    owed = store.query(
        f"SELECT i.* FROM {sprint_store.ITEMS} i JOIN"
        " vomeos_release_resolutions r ON r.ticket_id = i.ticket_id"
        " WHERE r.outcome IN ('drafted', 'held', 'no_draft')"
        " AND i.status IN ('open', 'held')")
    prs = store.query(
        "SELECT tf.ticket_number, tf.pr_url, tf.attempted_at FROM"
        " vomeos_ticket_fixes tf WHERE tf.outcome = 'pr_opened' AND NOT"
        " EXISTS (SELECT 1 FROM vomeos_release_fixes rf"
        " WHERE rf.task_id = tf.parent_task) ORDER BY tf.attempted_at")
    fixes = store.query(
        "SELECT task_id, name, fix_commit, state, last_checked_at FROM"
        " vomeos_release_fixes WHERE state <> 'live' ORDER BY task_id")
    waiting = [dict(f) for f in fixes if f.get("state") == "waiting"]
    problems = [dict(f) for f in fixes if f.get("state") in ("failed",
                                                             "unknown")]
    for f in waiting:
        f["next"] = _production_owner(str(f.get("fix_commit") or ""))
    return {
        "replies_owed_for_shipped_work": [
            view(sprint_store._decode(dict(r))) for r in owed],
        "agent_prs_waiting_on_sams_merge": [
            {k: str(v) for k, v in r.items()} for r in prs],
        "merged_waiting_on_production": [
            {k: str(v) for k, v in f.items()} for f in waiting],
        "release_checks_failed": [
            {k: str(v) for k, v in f.items()} for f in problems],
        "how_to_use_this": (
            "Show replies_owed in full and send on Sam's word. PRs: Sam "
            "merges into the dev branch. Production: Sam deploys web, OnlyG "
            "merges core. The release watcher drafts each resolution once "
            "a fix is proven live; nothing ever moves a task to on prod."),
    }


# ---------------------------------------------------------------------------
# Sam's actions
# ---------------------------------------------------------------------------

def propose_fix(ticket: str, caller: str) -> dict:
    """Sam asks the agent to write the fix and open a PR (like `fix N`).
    Runs on the worker; fix_status shows the result."""
    _sam_only(caller, "ask for an agent fix")
    ticket_id, item = find(ticket)
    if not item:
        _refuse("this ticket has no agent first pass yet; call "
                "ticket_first_pass first")
    from vomeos.worker import enqueue_event

    enqueue_event(PROPOSE_FIX_KEY, {"ticket_id": ticket_id,
                                    "requested_by": caller},
                  queue="support")
    return {"state": "writing the fix",
            "next_step": ("The fix author is writing a diff and the patch "
                          "safety check reviews it (a few minutes). Call "
                          "fix_status for the PR, or why there is none. "
                          "Only low-risk fixes are ever opened, and nothing "
                          "is merged by an agent.")}


def handle_propose_fix(message: dict) -> dict:
    """Worker side of propose_fix. Idempotent on a per-ticket lease."""
    import sprint_batch
    import ticket_fix
    import zoho_actions

    ticket_id = str(message.get("ticket_id") or "")
    if not ticket_id or not sprint_batch._lease(f"fix:{ticket_id}", 30):
        return {"status": "duplicate"}
    item = sprint_store.by_ticket(ticket_id)
    if not item:
        return {"status": "no item"}
    out = ticket_fix.propose_for_ticket(
        ticket_id, str(item.get("ticket_number") or ""),
        item.get("account") or "", item.get("card") or {},
        zoho_actions.ticket_url(ticket_id), sam_approved=True)
    if out.get("outcome") == "pr_opened":
        sprint_store.set_fix_pr(item["id"], out["pr_url"])
    return {"status": "ok", **out}


_SEND_WORD = re.compile(r"\b(send|approved?|go ahead|ship it)\b", re.I)


def send_ticket_reply(ticket: str, sam_said: str, caller: str,
                      text: str = "") -> dict:
    """Send the reply for a ticket: the agent's draft, or Sam's edited
    `text`. One send path with the sprint thread: every outbound guard,
    recipients from the latest message plus secondary contacts, refused if
    the customer wrote again, then Zoho status and Agent Fixes follow."""
    _sam_only(caller, "send replies")
    if not _SEND_WORD.search(sam_said or ""):
        _refuse("pass Sam's own words in sam_said; they must ask to send "
                "(send, approve, go ahead)")
    import sprint_thread

    ticket_id, item = find(ticket)
    if not item:
        _refuse("no draft for this ticket; call ticket_first_pass first")
    learned = ""
    if text.strip():
        body = sprint_thread._signed(item, text)
        status, why = sprint_thread._check_draft(item, body)
        before = item.get("draft") or ""
        sprint_store.set_draft(item["id"], body, status, why)
        learned = sprint_thread._learn(item, before, body, sam_said)
        if status != "ok":
            _refuse(f"the edited reply is held by a guard ({why}); nothing "
                    "was sent")
        item = sprint_store.get_item(item["id"])
    result = sprint_thread._send(item)
    return {"result": result, "lesson": learned.strip(),
            "sent": " sent to " in result}


def route_ticket(ticket: str, engineer: str, caller: str) -> dict:
    """Hand a ticket to an engineer: Priority Queue task with the agent's
    first pass as the engineer summary and a private Zoho note. The Zoho
    status is left alone until Sam's reply goes out (Rule 6); Slack only
    when the ticket is urgent."""
    _sam_only(caller, "route tickets")
    import sprint_thread

    _, item = find(ticket)
    if not item:
        _refuse("no agent first pass for this ticket yet; call "
                "ticket_first_pass first so the engineer gets its findings")
    return {"result": sprint_thread._route(item, engineer.lower())}


def close_ticket_quietly(ticket: str, caller: str) -> dict:
    """Close in Zoho with no closure email: nothing is owed."""
    _sam_only(caller, "close tickets")
    import sprint_thread

    _, item = find(ticket)
    if not item:
        _refuse("no sprint item for this ticket")
    return {"result": sprint_thread._close_quietly(item)}


AGENTS = {
    "drafter": "customer_success.reply_drafter",
    "triage": "customer_success.triage_director",
    "partner": "customer_success.sprint_partner",
    "incidents": "customer_success.incident_matcher",
    "all": "*",
}


def teach_agents(rule: str, agent: str, sam_said: str, caller: str,
                 ticket: str = "") -> dict:
    """Record Sam's general correction as a live lesson: added to that
    agent's prompt on its next run (CLAUDE.md, "Every correction Sam makes
    teaches the agents"). A one-off fix for a single ticket is not a
    lesson; edit that reply instead."""
    _sam_only(caller, "teach the agents")
    from vomeos import lessons

    target = AGENTS.get((agent or "").strip().lower())
    if not target:
        _refuse(f"agent must be one of {', '.join(AGENTS)}")
    rule = " ".join((rule or "").split())
    if len(rule) < 15:
        _refuse("write the rule as one clear imperative sentence")
    lesson_id = lessons.add(target, rule, "claude_ai",
                            evidence=f"Sam: {sam_said}",
                            ticket=str(ticket or "").lstrip("#"),
                            created_by="claude_ai_sprint")
    if not lesson_id:
        _refuse("the lesson could not be stored")
    return {"lesson": f"L{lesson_id}", "agent": target, "rule": rule,
            "note": ("Live from the agent's next run. A Claude Code session "
                     "promotes proven lessons into the charter and answer "
                     "key. Say forget L<n> in a sprint thread, or ask a "
                     "Claude Code session, to drop one.")}


def log_ticket_request(ticket: str, caller: str, build: bool = False,
                       existing_task_id: str = "",
                       confirm_new: bool = False) -> dict:
    """Log the ticket's feature request: duplicate check first, then Raw
    Intake (or Sam Accepted when `build`), or +1 an existing task; posted
    to #vome-feature-requests with the client."""
    _sam_only(caller, "log requests from the sprint")
    import sprint_thread

    _, item = find(ticket)
    if not item:
        _refuse("no sprint item for this ticket")
    mode = existing_task_id.strip() or ("new" if confirm_new else "")
    return {"result": sprint_thread._feature(
        item, "build" if build else "log", mode)}


# ---------------------------------------------------------------------------
# Walking the Zoho queue
# ---------------------------------------------------------------------------

WALK_SECTIONS = ("urgent", "route", "with_engineer", "agent_fixing",
                 "your_call", "answer", "close_quietly", "old_card",
                 "not_triaged")


def _fix_attempt(item: dict) -> dict:
    """What the agent's fix attempt came to, as ticket_routing reads it."""
    if item.get("fix_pr"):
        return {"pr_url": item["fix_pr"]}
    try:
        ledger = _ledger(str(item.get("ticket_id") or ""))
    except Exception:
        ledger = {}
    return {"outcome": ledger.get("outcome") or "",
            "reason": ledger.get("reason") or "",
            "pr_url": ledger.get("pr_url") or ""}


def _walk_one(item: dict) -> tuple[str, dict]:
    """(section, line) for one triaged ticket. Reads, never writes."""
    import ticket_routing as tr

    card = item.get("card") or {}
    code = card.get("code_check") if isinstance(
        card.get("code_check"), dict) else {}
    line = {"ticket": f"#{item.get('ticket_number')}",
            "account": item.get("account"),
            "waiting_on": card.get("live_question") or item.get("summary"),
            "lane": card.get("lane") or "",
            "code_check": code.get("verdict") or "",
            "draft": item.get("draft_status") or "none",
            "client_told": bool(item.get("sent_at"))}
    if (item.get("eng_task") or {}).get("url"):
        line["engineer"] = _engineer(item)
        return "with_engineer", line
    if item.get("fix_pr"):
        line["agent_fix"] = _agent_fix(item, card)
        return "agent_fixing", line
    # Triaged before lanes existed (Oct 17 and 18): nothing here can say
    # who should have it, so it is never routed on this card.
    if not card.get("lane"):
        line["triaged_at"] = str(item.get("updated_at") or "")[:10]
        return "old_card", line
    fix = _fix_attempt(item)
    go, reason = tr.should_route(card, fix)
    if go:
        urgent = tr.urgency(tr.arr_of(item.get("account") or ""),
                            item.get("account") or "", tr.incident_of(item))
        line.update({"route_to": tr.engineer_for(card), "reason": reason,
                     "urgent": urgent})
        return ("urgent" if urgent else "route"), line
    lane = card.get("lane") or ""
    if (lane in ("feature", "sam_ops")
            or code.get("verdict") == "design_question"
            or card.get("escalation_reason") == "product_decision"
            or (lane == "agent_fix" and not go)):
        line["why_yours"] = (card.get("why_human") or reason
                             or card.get("recommended_action") or "")
        return "your_call", line
    if lane == "housekeeping" or item.get("category") == "awaiting_client":
        return "close_quietly", line
    return "answer", line


def walk_queue() -> dict:
    """Every Zoho ticket at New or Processing (each one owes the client a
    reply), and what the system does with it now: routes it to an engineer,
    leaves it with the agent's fix, or puts it in front of Sam. Read-only,
    and no agent runs: it uses the cards triage already wrote."""
    import sprint_batch

    if not sprint_store.ready():
        _refuse("the sprint database is not reachable")
    try:
        tickets = sprint_batch._queue()
    except Exception as exc:
        _refuse(f"the Zoho queue could not be read ({exc}); that is not "
                "the same as an empty queue")
    groups: dict[str, list] = {name: [] for name in WALK_SECTIONS}
    for ticket in tickets:
        item = sprint_store.by_ticket(str(ticket.get("id")))
        if not item or not item.get("card"):
            groups["not_triaged"].append({
                "ticket": f"#{ticket.get('ticketNumber')}",
                "subject": ticket.get("subject") or "",
                "zoho_status": ticket.get("status")})
            continue
        section, line = _walk_one(item)
        line["zoho_status"] = ticket.get("status")
        groups[section].append(line)
    return {
        "open_in_zoho": len(tickets),
        "counts": {name: len(groups[name]) for name in WALK_SECTIONS},
        **groups,
        "how_to_use_this": (
            "Every ticket here is New or Processing in Zoho, so every one is "
            "owed a reply from Sam (except close_quietly). urgent and route "
            "are what route_queue would file now (urgent ones also ping the "
            "engineer's channel and Sam). with_engineer and agent_fixing are "
            "already owned: their reply is the acknowledgment, and sending it "
            "is what moves Zoho to In Progress. your_call needs Sam (feature "
            "requests to log, design questions, data or billing). "
            "old_card tickets were triaged before lanes existed: offer to "
            "re-run them with ticket_first_pass(refresh=true), about $0.40 "
            "each, only on Sam's yes. not_triaged tickets have no card "
            "yet: the 30-minute sweep picks "
            "them up, or use ticket_first_pass on one. If a line is wrong, "
            "that is a lesson: ask Sam for the rule and use teach_agents."),
    }


def route_queue(caller: str, tickets: str = "") -> dict:
    """Do what walk_queue shows under urgent and route (Sam only): file or
    join the engineer task for each, with no client email and no Zoho
    status change. `tickets` limits it to those numbers ("9500 9501")."""
    _sam_only(caller, "route the queue")
    import ticket_routing as tr
    from error_reports import is_error_report

    import shadow_triage

    wanted = {n.lstrip("#") for n in (tickets or "").replace(",", " ").split()}
    walked = walk_queue()
    results = []
    for line in walked["urgent"] + walked["route"]:
        number = line["ticket"].lstrip("#")
        if wanted and number not in wanted:
            continue
        _, item = find(number)
        if is_error_report(item.get("subject") or ""):
            results.append({"ticket": line["ticket"],
                            "result": "error report: stays the agent's"})
            continue
        release = shadow_triage.release_state(number,
                                              str(item.get("ticket_id")))
        if release.get("task_id"):
            results.append({"ticket": line["ticket"],
                            "result": "already linked to an agent fix"})
            continue
        out = tr.route(item, item.get("card") or {}, _fix_attempt(item),
                       tr.incident_of(item),
                       tr.arr_of(item.get("account") or ""))
        results.append({"ticket": line["ticket"],
                        "result": (f"{out['task']['how']} for "
                                   f"{out['task']['owner']}: "
                                   f"{out['task']['url']}"
                                   + (f" (URGENT: {out['urgent']})"
                                      if out.get("urgent") else ""))
                        if out.get("routed") else
                        f"not routed: {out.get('why')}"})
    return {"routed": results,
            "note": ("Nothing was sent to any client and Zoho statuses did "
                     "not change. Each still needs Sam's acknowledgment from "
                     "the sprint.")}
