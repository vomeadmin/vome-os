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
Read:   sprint_board (start here), sprint_queue, ticket_first_pass,
        fix_status, sprint_followups
Sam:    propose_fix, send_ticket_reply, send_replies, ping_team,
        route_ticket, close_ticket_quietly, log_ticket_request

Anything that writes is refused for anyone but Sam (config.can_triage).
A reply goes out only through send_ticket_reply, which re-checks the draft
against every outbound guard, refuses when the customer has written since
the draft, and needs Sam's own words passed in `sam_said`. Claude.ai also
asks him to approve each call (destructive annotation).
"""

from __future__ import annotations

import os
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


# A ClickUp task id as cards name it ("already logged on 868mgyqdm") or as a
# link.
_TASK_ID = re.compile(r"(?:app\.clickup\.com/t/|\b)(86[a-z0-9]{7})\b")
_MAX_TASKS = 3


def _tasks(item: dict, card: dict) -> tuple[list[dict], str]:
    """The ClickUp tasks behind this ticket and the list each sits in now,
    so Sam can open the work and see whose list it is on without searching
    (Sam, 2026-10-24). The engineer task and the agent fix parent are read
    live; tasks linked to the ticket come from the cached list search.
    Returns (tasks, problem); a problem means a task may exist unseen."""
    import clickup_search
    import ticket_routing

    found: dict[str, dict] = {}
    problem = ""

    def live(task_id: str) -> None:
        if not task_id or task_id in found:
            return
        state = ticket_routing.task_state({"id": task_id})
        if state.get("status") == "unreadable":
            nonlocal problem
            problem = f"ClickUp task {task_id} could not be read"
            return
        found[task_id] = {
            "id": task_id, "name": state.get("name") or "",
            "list": state.get("list") or "", "status": state.get("status"),
            "assignee": state.get("assignee") or "",
            "url": state.get("url") or f"https://app.clickup.com/t/{task_id}"}

    live((item.get("eng_task") or {}).get("id") or "")
    if item.get("fix_pr"):
        try:
            live(str(_ledger(str(item.get("ticket_id") or ""))
                     .get("parent_task") or ""))
        except Exception:
            pass
    try:
        for task in clickup_search.linked_tasks(
                str(item.get("ticket_number") or ""),
                str(item.get("ticket_id") or "")):
            found.setdefault(task["id"], {
                k: task.get(k) or "" for k in
                ("id", "name", "list", "status", "assignee", "url")})
    except Exception as exc:
        problem = f"ClickUp could not be searched ({exc})"
    for task_id in _TASK_ID.findall(str(card.get("existing_work") or "")):
        live(task_id)
    return list(found.values())[:_MAX_TASKS], problem


def _links(item: dict, zoho_url: str, tasks: list[dict],
           problem: str) -> str:
    """'[Zoho #9557](..) | [ClickUp](..) in Sam Accepted (Build Now),
    queued, assigned Sam': where the ticket and its work live, one click
    each."""
    parts = [f"[Zoho #{item.get('ticket_number')}]({zoho_url})"]
    for task in tasks:
        where = ", ".join(p for p in (
            task.get("list") or "unknown list", task.get("status") or "",
            f"assigned {task['assignee']}" if task.get("assignee")
            else "unassigned") if p)
        parts.append(f"[ClickUp]({task['url']}) in {where}")
    if problem:
        parts.append(f"{problem}, so a task may exist that is not shown")
    elif not tasks:
        parts.append("no ClickUp task")
    return " | ".join(parts)


def _deep_view(card: dict) -> dict:
    deep = card.get("deep") or {}
    if not deep:
        return {}
    return {k: v for k, v in {
        "why_it_ran": deep.get("reason") or "",
        "status": deep.get("status") or "",
        "verdict": deep.get("verdict") or "",
        "changes": deep.get("changes") or [],
        "checked": deep.get("checked") or "",
        "first_pass": card.get("first_pass") or {},
        "not_run_because": deep.get("why") or "",
    }.items() if v}


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
    zoho_url = zoho_actions.ticket_url(item.get("ticket_id"))
    tasks, problem = _tasks(item, card)
    return {
        "item": item.get("number"),
        "ticket": f"#{item.get('ticket_number')}",
        "header": header,
        # The second line on every card: Zoho and ClickUp one click away,
        # and the list the work sits in now.
        "links": _links(item, zoho_url, tasks, problem),
        "client": client,
        "contact": contact,
        "zoho_url": zoho_url,
        "clickup_tasks": tasks,
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
        # The senior second look (deep_triage.py): whether it ran, and what
        # it changed in the first card and why.
        "deep_pass": _deep_view(card),
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
    # Answered or closed in Zoho by hand: finished before anything is listed
    # (lesson L27). An unreadable queue finishes nothing.
    try:
        tickets = sprint_batch._queue()
    except Exception as exc:
        tickets, queue_error = None, str(exc)
    else:
        sprint_batch.sync_handled_elsewhere(
            items, {str(t.get("id")) for t in tickets})
    # Routed by triage and already shipped, with the client told since:
    # the acknowledgment waiting here is stale and finishes.
    import sprint_thread

    items = [i for i in items
             if not ((i.get("eng_task") or {}).get("id")
                     and i.get("status") in sprint_store.CARRIES
                     and sprint_thread._engineer_shipped(i)
                     and i.get("status") == sprint_store.DONE)]
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
        if tickets is None:
            raise RuntimeError(queue_error)
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
            "Every card opens with two lines given exactly: header, then "
            "links (the Zoho ticket and each ClickUp task as clickable "
            "links, with the list each task sits in, its status and "
            "assignee). ready_to_send drafts are complete; show each one "
            "in full and send only on Sam's explicit word. An item with "
            "agent_fix set "
            "is one the agent is fixing: say so plainly (the agent is "
            "fixing this, here is the acknowledgment to send). "
            "agent_fixing lists tickets already acknowledged that the agent "
            "still owns: one line each with agent_fix, nothing to do unless "
            "it says Sam's merge. An item with engineer set is already with "
            "that engineer (triage filed the ClickUp task on its own): do "
            "not route it again, and its reply is an acknowledgment that "
            "the team is on it, with no timeline. When the engineer marks "
            "it on prod, the client is emailed and the ticket closes "
            "automatically. A listed item whose engineer.shipped is true "
            "had its task closed with nobody telling the client: its "
            "acknowledgment draft is stale, so offer "
            "ticket_first_pass(refresh=true) for a reply with the outcome. "
            "Tickets answered or closed by hand in Zoho are finished before "
            "this list is built and never appear. needs_decision carries "
            "the agent's first pass (code_check, "
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


def ticket_states(state: str = "") -> dict:
    """Every Zoho ticket at In Progress and what its ClickUp work says, as
    of the nightly reconcile (ticket_reconciler.py): our team is on it,
    fixed and waiting for release, shipped, closed, stalled or fell
    through, with the line to tell the client. Read only."""
    import ticket_reconciler

    out = ticket_reconciler.states(state.strip())
    if out.get("error"):
        _refuse(out["error"])
    return out


def triage_cost(days: int = 14) -> dict:
    """What triage cost over the last `days` (first pass and deep pass),
    how often the deep pass ran and corrected the first card, and the
    monthly and yearly forecast at that rate. Read only."""
    import deep_triage

    out = deep_triage.report(int(days or 14))
    if out.get("error"):
        _refuse(out["error"])
    return out


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
                   + ". propose_fix asks the agent to try (low or medium "
                     "risk).")
    return {"ticket": f"#{number}", "summary": summary, "pr_url": pr,
            "fix_attempt": {k: str(v) for k, v in ledger.items()},
            "release_state": release.get("text"),
            "resolution": {k: str(v) for k, v in resolution.items()}}


def sprint_followups() -> dict:
    """What happened since the last sprint that needs a follow-up: replies
    owed for work that shipped, PRs waiting on Sam's merge, fixes waiting
    on production, and release checks that failed."""
    import sprint_batch

    if not sprint_store.ready():
        _refuse("the sprint database is not reachable")
    owed = [sprint_store._decode(dict(r)) for r in store.query(
        f"SELECT i.* FROM {sprint_store.ITEMS} i JOIN"
        " vomeos_release_resolutions r ON r.ticket_id = i.ticket_id"
        " WHERE r.outcome IN ('drafted', 'held', 'no_draft')"
        " AND i.status IN ('open', 'held')")]
    # Sam may have answered or closed it in Zoho since it was drafted.
    sprint_batch.sync_handled_elsewhere(owed, set())
    owed = [i for i in owed if i.get("status") in sprint_store.CARRIES]
    return {
        "replies_owed_for_shipped_work": [view(i) for i in owed],
        **_pipeline(),
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
                          "Only low and medium risk fixes are opened (an "
                          "engineer reviews medium), and nothing "
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
        status, why = sprint_thread._check_draft(item, body, sam_wrote=True)
        before = item.get("draft") or ""
        sprint_store.set_draft(item["id"], body, status, why)
        learned = sprint_thread._learn(item, before, body, sam_said)
        if status != "ok":
            _refuse(f"the edited reply is held by a guard ({why}); nothing "
                    "was sent")
        item = sprint_store.get_item(item["id"])
    result = sprint_thread._send(item, sam_wrote=bool(text.strip()))
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
                       confirm_new: bool = False, title: str = "",
                       module: str = "") -> dict:
    """Log the ticket's feature request: duplicate check first, then Raw
    Intake for later, or Sam Accepted (assigned to Sam, high priority) when
    `build`, or +1 an existing task (moved to Sam Accepted when `build`);
    posted to #vome-feature-requests with the client."""
    _sam_only(caller, "log requests from the sprint")
    import sprint_thread

    _, item = find(ticket)
    if not item:
        _refuse("no sprint item for this ticket")
    mode = existing_task_id.strip() or ("new" if confirm_new else "")
    return {"result": sprint_thread._feature(
        item, "build" if build else "log", mode, title=title,
        module=module)}


# ---------------------------------------------------------------------------
# Walking the Zoho queue
# ---------------------------------------------------------------------------

WALK_SECTIONS = ("urgent", "route", "with_engineer", "agent_fixing",
                 "your_call", "answer", "close_quietly", "old_card",
                 "not_triaged", "answered_in_zoho")


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
            or (lane in ("agent_fix", "engineer") and not go)):
        line["why_yours"] = (card.get("why_human") or reason
                             or card.get("recommended_action") or "")
        return "your_call", line
    if lane == "housekeeping" or item.get("category") == "awaiting_client":
        return "close_quietly", line
    return "answer", line


def _still_answered(ticket_id: str) -> bool:
    """The customer has not written since we answered. Triage reopens the
    item on a new message, but not before its daily cap allows."""
    import sprint_batch

    try:
        return sprint_batch.answered_in_zoho(ticket_id)
    except Exception:
        return False


def walk_queue() -> dict:
    """Every Zoho ticket at New or Processing (each one owes the client a
    reply), and what the system does with it now: routes it to an engineer,
    leaves it with the agent's fix, or puts it in front of Sam. No agent
    runs: it uses the cards triage already wrote. The one write: an item
    answered in Zoho after the customer's last message is finished."""
    import sprint_batch

    if not sprint_store.ready():
        _refuse("the sprint database is not reachable")
    try:
        tickets = sprint_batch._queue()
    except Exception as exc:
        _refuse(f"the Zoho queue could not be read ({exc}); that is not "
                "the same as an empty queue")
    items = {str(t.get("id")): sprint_store.by_ticket(str(t.get("id")))
             for t in tickets}
    sprint_batch.sync_handled_elsewhere(
        [i for i in items.values() if i], set(items))
    groups: dict[str, list] = {name: [] for name in WALK_SECTIONS}
    for ticket in tickets:
        item = items[str(ticket.get("id"))]
        if not item or not item.get("card"):
            groups["not_triaged"].append({
                "ticket": f"#{ticket.get('ticketNumber')}",
                "subject": ticket.get("subject") or "",
                "zoho_status": ticket.get("status")})
            continue
        if (item.get("decision") == sprint_batch.ANSWERED
                and _still_answered(str(ticket.get("id")))):
            groups["answered_in_zoho"].append({
                "ticket": f"#{item.get('ticket_number')}",
                "account": item.get("account"),
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
            "re-run them with ticket_first_pass(refresh=true), $0.40 to "
            "about $3 each (an unsure card gets the deep pass), only on "
            "Sam's yes. not_triaged tickets have no card "
            "yet: the 30-minute sweep picks "
            "them up, or use ticket_first_pass on one. answered_in_zoho "
            "tickets were answered by hand after the customer's last "
            "message: nothing is owed, and the status is moved out of New "
            "or Processing on its own (Awaiting Client Response, or In "
            "Progress when routed). One still listed here is a move that "
            "failed: set it by hand, or offer to close it. If a line is "
            "wrong, that is a lesson: ask Sam for the rule and use "
            "teach_agents. In Progress tickets are not listed here: "
            "ticket_states shows each one against its ClickUp work."),
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


# ---------------------------------------------------------------------------
# The board: who is waiting on whom
# ---------------------------------------------------------------------------
#
# Sam, 2026-10-29, after a 64-card sprint: much of it needed no email. In
# Progress tickets the reconciler re-triaged came back as "thanks for your
# patience" drafts to clients who last wrote 37 to 105 days ago, while work
# that shipped with nobody telling the client sat in ClickUp. The sprint is
# the tickets a client is waiting on, sent with one word; good news nobody
# delivered is its own list; and work our team should be chased on is
# internal, never an email. The agents are right most of the time, so the
# board names only the few drafts Sam should read before they go.

STALE_DAYS = int(os.environ.get("SPRINT_STALE_DAYS", "21"))
DONE_STATES = ("fix_live", "shipped")
# Reconcile states where the next move is ours, not the client's.
INTERNAL_STATES = ("engineer_stalled", "unassigned", "task_closed",
                   "no_task")
BOARD_LANES = ("send", "tell_them_done", "check", "decide", "internal",
               "close_quietly")
COUNTED = ("agent_fixing", "held", "waiting_on_client")
MAX_SEND = 40

# A draft that asks the admin to do something before we can move.
_ASKS = re.compile(
    r"\?|\b(could|can|would) you\b|\bplease (send|share|provide|confirm|"
    r"let us know (which|who|what|whether|if))\b|\bscreenshot\b", re.I)
# A draft that tells the client the problem is gone.
_FIXED = re.compile(
    r"\b(is|are|been|now) (fixed|resolved|live)\b|\bhas been updated\b|"
    r"\bupdate (is|has been|was) (deployed|released|live)\b|"
    r"\b(corrig|r[ée]solu|d[ée]ploy[ée])", re.I)

NEXT_STEP = {
    "engineer_stalled": "ping_team: nobody touched the task in 14+ days",
    "engineer_active": "nothing: our team is on it (ping_team for news)",
    "unassigned": "route_ticket: the task has nobody on it",
    "task_closed": ("read the task: closed without on prod. Close the "
                    "ticket quietly, or reopen the task"),
    "no_task": "route_ticket, or close it quietly if nothing is left",
    "fix_on_dev": "nothing: the agent fix waits on production",
}


def _numbers(text: str) -> list[str]:
    seen: list[str] = []
    for part in (text or "").replace(",", " ").split():
        value = part.strip().lstrip("#")
        if value and value not in seen:
            seen.append(value)
    return seen


def _days_since(moment) -> int | None:
    import sprint_batch

    when = sprint_batch._parse_time(moment)
    if when is None:
        return None
    return int((sprint_batch._now() - when).total_seconds() // 86400)


def _reconcile_rows() -> dict[str, dict]:
    """Ticket id -> its nightly reconcile record, {} when the table is
    missing (the board then works from the sprint items alone)."""
    import ticket_reconciler

    try:
        rows = store.query(f"SELECT * FROM {ticket_reconciler.TABLE}")
    except Exception as exc:
        print(f"[BOARD] reconcile table unreadable: {exc}")
        return {}
    return {str(r["ticket_id"]): dict(r) for r in rows}


def _resolutions() -> dict[str, dict]:
    """Ticket id -> the newest resolution still waiting on Sam."""
    rows = store.query(
        "SELECT ticket_id, outcome, detail, created_at FROM"
        " vomeos_release_resolutions WHERE outcome IN"
        " ('drafted', 'held', 'no_draft') ORDER BY created_at")
    return {str(r["ticket_id"]): dict(r) for r in rows}


def _assess(item: dict, rec: dict, resolutions: dict,
            engineer: dict | None = None) -> tuple[bool, list[str]]:
    """(shipped, flags) for one item. shipped: the work behind it is live
    or on prod, so the reply owed is a resolution. flags: why Sam should
    read the draft before it goes; a draft with none is sent on "send
    all". The board and send_replies both judge by this, so what the board
    calls clean is exactly what a bulk send lets through."""
    import sprint_batch

    card = item.get("card") or {}
    resolution = resolutions.get(str(item.get("ticket_id"))) or {}
    if engineer is None:
        engineer = _engineer(item)
    shipped = bool(resolution or rec.get("state") in DONE_STATES
                   or (engineer or {}).get("shipped"))
    body = (item.get("draft") or "").split("Sam | Vome team")[0]
    flags = []
    if shipped:
        if resolution.get("outcome") == "held":
            flags.append("the release watcher held this resolution: "
                         + str(resolution.get("detail") or "")[:200])
        if body.strip() and not _FIXED.search(body):
            flags.append("the work shipped but this draft is not a "
                         "resolution: ticket_first_pass(refresh=true) "
                         "writes one")
    else:
        if _FIXED.search(body):
            flags.append("says it is fixed or live: send only if every "
                         "problem in the ticket shipped")
        if card.get("blocking_claims"):
            flags.append("an earlier reply told them: "
                         + str(card["blocking_claims"])[:200])
        days = _days_since(sprint_batch.waiting_since(item))
        if days is not None and days > STALE_DAYS:
            flags.append(f"the client has waited {days} days")
    if _ASKS.search(body):
        flags.append("asks the client for something: send only if our "
                     "team cannot look it up")
    if card.get("confidence") == "low":
        flags.append("the first pass is low confidence")
    return shipped, flags


def _client_waiting(ticket_id: str) -> bool:
    """The customer's newest message has no answer after it. Unreadable
    Zoho counts as waiting, so a ticket is never dropped on a hiccup."""
    import sprint_batch

    try:
        return not sprint_batch.answered_in_zoho(ticket_id)
    except Exception:
        return True


def _card(v: dict, flags: list[str], kind: str) -> dict:
    """What Sam reads for one reply: who, what, the draft in full."""
    keep = ("item", "ticket", "header", "links", "live_question",
            "situation", "recipients", "draft", "engineer", "agent_fix")
    out = {k: v.get(k) for k in keep if v.get(k)}
    out["kind"] = kind
    if flags:
        out["flags"] = flags
    return out


def _internal(v: dict, item: dict, rec: dict) -> dict:
    import sprint_batch

    engineer = v.get("engineer") or {}
    state = rec.get("state") or (
        "fix_on_dev" if item.get("fix_pr") else
        "engineer_active" if engineer.get("task") else "no_task")
    return {"ticket": v.get("ticket"), "header": v.get("header"),
            "links": v.get("links"),
            "client_last_wrote_days_ago": _days_since(
                sprint_batch.waiting_since(item)),
            "state": state,
            "task_updated": rec.get("task_updated") or "",
            "assigned": rec.get("task_assignees")
            or engineer.get("owner") or "",
            "last_pinged": str(rec.get("nudged_at") or "")[:10],
            "next": NEXT_STEP.get(state, "")}


def _place(item: dict, open_ids: set, rec: dict,
           resolutions: dict) -> tuple[str, dict]:
    """(lane, line) for one sprint item."""
    import sprint_batch

    section = sprint_batch.section(item)
    if section == sprint_batch.IN_FLIGHT:
        return "agent_fixing", {}
    if section == sprint_batch.HELD:
        return "held", {}
    if section == sprint_batch.OWED_NOTHING:
        return "waiting_on_client", {}
    v = view(item)
    if section == sprint_batch.QUIET:
        return "close_quietly", {
            "ticket": v["ticket"], "header": v["header"],
            "why": v.get("live_question") or v.get("situation") or ""}
    tid = str(item.get("ticket_id"))
    shipped, flags = _assess(item, rec, resolutions, v.get("engineer"))
    has_draft = (item.get("draft_status") == "ok"
                 and (item.get("recipients") or {}).get("to"))
    if shipped:
        if section == sprint_batch.FIX:
            flags = [sprint_batch._fix_reason(item) or "the draft is held"] \
                + flags
        elif not has_draft:
            flags = flags + ["no resolution draft yet: "
                             "ticket_first_pass(refresh=true) writes one"]
        return ("check" if flags else "tell_them_done"), _card(
            v, flags, "done")
    # Answered before (Zoho moved past New or Processing) and either we
    # wrote last or the client went quiet long ago: nothing to email.
    if tid not in open_ids:
        days = _days_since(sprint_batch.waiting_since(item))
        if (not _client_waiting(tid)
                or (days is not None and days > STALE_DAYS)):
            return "internal", _internal(v, item, rec)
    if section == sprint_batch.DECIDE:
        line = _card(v, flags, "reply")
        line["why_yours"] = (v.get("why_human")
                             or v.get("recommended_action") or "")
        line["code_check"] = v.get("code_check") or {}
        return "decide", line
    if section == sprint_batch.FIX:
        flags = [sprint_batch._fix_reason(item) or "the draft is held"] \
            + flags
    return ("check" if flags else "send"), _card(v, flags, "reply")


def _untold(rec: dict) -> bool:
    """A shipped In Progress ticket we have not written to since its task
    last changed."""
    import sprint_batch

    try:
        return not sprint_batch.answered_in_zoho(
            rec["ticket_id"],
            sprint_batch._parse_time(rec.get("task_updated")))
    except Exception:
        return False


def _pipeline() -> dict:
    """PRs waiting on Sam's merge, fixes waiting on production, release
    checks that failed."""
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
        "agent_prs_waiting_on_sams_merge": [
            {k: str(v) for k, v in r.items()} for r in prs],
        "merged_waiting_on_production": [
            {k: str(v) for k, v in f.items()} for f in waiting],
        "release_checks_failed": [
            {k: str(v) for k, v in f.items()} for f in problems],
    }


def sprint_board() -> dict:
    """The sprint as Sam works it: replies a client is waiting on (clean
    ones go on "send all"), shipped work nobody told the client about, the
    few drafts to read first, decisions, and work to chase internally with
    no email. Reads only, except finishing items answered or closed by
    hand in Zoho (as sprint_queue does)."""
    import sprint_batch

    if not sprint_store.ready():
        _refuse("the sprint database is not reachable")
    try:
        tickets = sprint_batch._queue()
    except Exception as exc:
        _refuse(f"the Zoho queue could not be read ({exc}); that is not "
                "the same as an empty queue")
    open_ids = {str(t.get("id")) for t in tickets}
    items = sprint_store.working_set()
    sprint_batch.sync_handled_elsewhere(items, open_ids)
    items = [i for i in items if i.get("status") in sprint_store.CARRIES]
    reconcile = _reconcile_rows()
    resolutions = _resolutions()
    lanes: dict[str, list] = {name: [] for name in BOARD_LANES}
    counted = {name: 0 for name in COUNTED}
    seen = set()
    for item in sorted(items, key=sprint_batch._priority):
        tid = str(item.get("ticket_id"))
        seen.add(tid)
        lane, line = _place(item, open_ids, reconcile.get(tid) or {},
                            resolutions)
        if lane in counted:
            counted[lane] += 1
        else:
            lanes[lane].append(line)
    # In Progress tickets with no sprint item: shipped and never told, or
    # stuck on our side. One line each, from the nightly reconcile.
    for tid, rec in reconcile.items():
        if tid in seen:
            continue
        line = {"ticket": f"#{rec.get('ticket_number')}",
                "subject": rec.get("subject") or "",
                "client_last_wrote": rec.get("last_customer_at") or "",
                "task": (f"https://app.clickup.com/t/{rec['task_id']}"
                         if rec.get("task_id") else ""),
                "task_status": rec.get("task_status") or "",
                "assigned": rec.get("task_assignees") or "",
                "state": rec.get("state")}
        if rec.get("state") in DONE_STATES and _untold(rec):
            lanes["check"].append({
                **line, "kind": "done",
                "flags": ["shipped and the client was never told; no "
                          "draft yet: ticket_first_pass(refresh=true) "
                          "writes the resolution"]})
        elif rec.get("state") in INTERNAL_STATES:
            lanes["internal"].append({
                **line, "last_pinged": str(rec.get("nudged_at") or "")[:10],
                "next": NEXT_STEP.get(rec.get("state"), "")})
    known = {str(r["ticket_id"]) for r in store.query(
        f"SELECT ticket_id FROM {sprint_store.ITEMS} WHERE ticket_id ="
        " ANY(:ids)", {"ids": list(open_ids)})}
    untriaged = [t for t in tickets if str(t.get("id")) not in known]
    return {
        "counts": {**{k: len(v) for k, v in lanes.items()}, **counted,
                   "untriaged": len(untriaged)},
        **lanes,
        "not_listed": counted,
        "untriaged": [{"ticket": f"#{t.get('ticketNumber')}",
                       "subject": t.get("subject") or ""}
                      for t in untriaged],
        "pipeline": _pipeline(),
        "how_to_use_this": (
            "Open with counts in one line and the pipeline in one line "
            "each. send and tell_them_done are clean: show each as a "
            "compact card (header exactly, links, one line on the "
            "situation, then the draft in full with To and Cc) and send "
            "them all on Sam's 'send all' with send_replies, minus any he "
            "names. check is the few to read first: show each flag, the "
            "draft, and a corrected version when the flag is about the "
            "wording; they go only when Sam names them (send_replies "
            "`checked`, or send_ticket_reply with his edit). decide needs "
            "his call (feature, design question, data or billing). "
            "internal never gets an email: one line each with `next`; on "
            "Sam's word, ping_team comments on the task and notifies its "
            "assignee. close_quietly: one line each, close_ticket_quietly "
            "on his word. not_listed are counts only (the agent is "
            "fixing, held until a date, waiting on the client). A flag "
            "Sam overrules more than once is a lesson: ask him for the "
            "rule and use teach_agents."),
    }


def send_replies(tickets: str, sam_said: str, caller: str,
                 checked: str = "") -> dict:
    """Send several drafts as they stand (Sam only): the board's "send
    all". Each goes through the one guarded send path. A draft the board
    flags is refused unless Sam named it in `checked`, so "send all" can
    never sweep in one he was meant to read."""
    _sam_only(caller, "send replies")
    if not _SEND_WORD.search(sam_said or ""):
        _refuse("pass Sam's own words in sam_said; they must ask to send "
                "(send, approve, go ahead)")
    wanted = _numbers(tickets)
    if not wanted:
        _refuse("name the tickets to send, e.g. '9678 9723'")
    if len(wanted) > MAX_SEND:
        _refuse(f"at most {MAX_SEND} at a time")
    import sprint_thread

    cleared = set(_numbers(checked))
    reconcile = _reconcile_rows()
    resolutions = _resolutions()
    results = []
    for number in wanted:
        try:
            ticket_id, item = find(number)
        except Exception as exc:
            results.append({"ticket": f"#{number}", "sent": False,
                            "result": f"not sent: {exc}"})
            continue
        if not item:
            results.append({"ticket": f"#{number}", "sent": False,
                            "result": "not sent: no draft for this ticket"})
            continue
        _, flags = _assess(item, reconcile.get(ticket_id) or {},
                           resolutions)
        if flags and number not in cleared:
            results.append({"ticket": f"#{number}", "sent": False,
                            "result": "not sent, Sam has not cleared its "
                                      "flags: " + "; ".join(flags)})
            continue
        try:
            result = sprint_thread._send(item)
        except Exception as exc:
            result = f"not sent: {type(exc).__name__}: {exc}"
        results.append({"ticket": f"#{number}",
                        "sent": " sent to " in result, "result": result})
    sent = sum(1 for r in results if r["sent"])
    return {"sent": sent, "not_sent": len(results) - sent,
            "results": results}


def ping_team(tickets: str, caller: str, note: str = "") -> dict:
    """Ask the engineer where a ticket stands (Sam only): a comment on its
    ClickUp task, which notifies the assignee. Never emails the client."""
    _sam_only(caller, "ping the team")
    wanted = _numbers(tickets)
    if not wanted:
        _refuse("name the tickets to ping about, e.g. '9188 9426'")
    import httpx

    import ticket_reconciler

    reconcile = _reconcile_rows()
    by_number = {str(r.get("ticket_number")): r for r in reconcile.values()}
    results = []
    for number in wanted:
        rec = by_number.get(number) or {}
        task_id = rec.get("task_id") or ""
        account = ""
        if not task_id:
            try:
                _, item = find(number)
            except Exception:
                item = {}
            task_id = ((item or {}).get("eng_task") or {}).get("id") or ""
            account = ((item or {}).get("account") or "").split("(")[0]
        if not task_id:
            results.append({"ticket": f"#{number}", "pinged": False,
                            "result": "no engineer task: route_ticket it, "
                                      "or close it quietly"})
            continue
        last = rec.get("last_customer_at") or "unknown"
        who = f" ({account.strip()})" if account.strip() else ""
        text = (f"Zoho #{number}{who}: the client last wrote on {last} "
                "and is still waiting "
                "on us. Where does this stand? If it shipped, move it to on "
                "prod and the client is told automatically. If you need "
                "something from the client, set needs client info."
                + (f"\n\nSam: {note.strip()}" if note.strip() else "")
                + "\n\n_Sam, via the support sprint_")
        try:
            httpx.post(f"{ticket_reconciler.CLICKUP}/task/{task_id}/comment",
                       headers=ticket_reconciler._headers(),
                       json={"comment_text": text, "notify_all": True},
                       timeout=20).raise_for_status()
        except Exception as exc:
            results.append({"ticket": f"#{number}", "pinged": False,
                            "result": f"ClickUp refused the comment: {exc}"})
            continue
        if rec:
            store.execute(
                f"UPDATE {ticket_reconciler.TABLE} SET nudged_at = NOW()"
                " WHERE ticket_id = :t", {"t": rec["ticket_id"]})
        results.append({"ticket": f"#{number}", "pinged": True,
                        "result": f"commented on https://app.clickup.com/t/"
                                  f"{task_id}"})
    return {"results": results,
            "note": "Nothing was sent to any client."}
