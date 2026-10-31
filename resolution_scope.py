"""
resolution_scope.py

Before an automatic "it is fixed" email goes out, make sure everything the
client raised in the ticket is fixed, not only the one thing that shipped.

WHY THIS EXISTS
---------------
Sam, 2026-10-23: clients told us we notified them too early. A ticket raised
two related problems, the first fix reached production, the on-prod handler
(or the release watcher) emailed "fixed", and the second problem was still
broken. Every automatic path judged one task at a time: the one that just
shipped. The rule now: a client hears "fixed" once, when every problem in
the ticket is fixed.

TWO CHECKS, BOTH MUST PASS
--------------------------
1. Open work. Every other task tied to the ticket (Priority Queue by link or
   "#<number>", Agent Fixes parents and children) has shipped. An Agent Fix
   counts as shipped only when its parent's Release Check is Live, because
   the watcher never moves a child past `on dev`. No model call.
2. Scope. customer_success.resolution_scope_check reads the thread and every
   shipped fix for the ticket, and names any problem the client raised that
   none of them covers. This catches the second problem that never got a
   task of its own.

The last fix to ship is the one that sends: when it runs, everything before
it reads as shipped, so the email goes once, at the end.

Fails closed. If ClickUp, Zoho or the agent cannot be read, the email is
held: a held email is a draft in front of Sam, never a lost one.
"""

from __future__ import annotations

import re

AGENT = "customer_success.resolution_scope_check"
# The lists whose open tasks are problems still being fixed. Feature lists
# (Raw Intake, Accepted Backlog, Sleeping, Declined) never hold a fix email:
# the client was told a request is logged, not that it would be built.
FIX_LISTS = ("Priority Queue",)
# How much of each shipped fix the agent reads.
_FIX_CHARS = 1200
# Where a fix task says what it changed. Agent fix briefs open with the
# likely cause, which alone can run past _FIX_CHARS: on #9528 (2026-10-30)
# the agent saw the cause, never the fix, and held a fixed ticket. "Fix PR"
# and "Fix Commit" are links, not the change.
# Times the agent is asked before an empty or unreadable verdict holds.
_ASKS = 2
_FIX_SECTION = re.compile(r"\*\*(?:potential fix|fix)\b(?! pr\b| commit\b)",
                          re.I)


def _linked_queue_tasks(ticket_number: str, ticket_id: str) -> list[dict]:
    import clickup_search

    return [t for t in clickup_search.linked_tasks(ticket_number, ticket_id)
            if t.get("list") in FIX_LISTS]


def _linked_agent_fixes(ticket_number: str, ticket_id: str) -> list[dict]:
    """Open Agent Fixes tasks for this ticket, each with `shipped` set from
    its parent's Release Check."""
    import release_watcher as rw

    tasks: list[dict] = []
    for page in range(20):
        data = rw._clickup(
            f"/list/{rw.AGENT_FIXES_LIST}/task"
            f"?subtasks=true&include_closed=false&page={page}"
        )
        batch = data.get("tasks") or []
        tasks.extend(batch)
        if data.get("last_page", True) or not batch:
            break
    by_id = {t.get("id"): t for t in tasks}
    marker = re.compile(rf"#{re.escape(ticket_number)}\b") if ticket_number \
        else None
    found = []
    for task in tasks:
        link = str(rw._field(task, rw.FIELD_ZOHO_TICKET_LINK).get("value")
                   or "")
        named = bool(marker and marker.search(task.get("name") or ""))
        if not named and not (ticket_id and ticket_id in link):
            continue
        parent = by_id.get(task.get("parent") or task.get("id"), task)
        found.append({
            "id": task.get("id"),
            "parent": task.get("parent") or "",
            "name": task.get("name") or "",
            "status": ((task.get("status") or {}).get("status") or "").lower(),
            "shipped": rw._release_check_value(parent) == "live",
        })
    return found


def open_work(ticket_number: str, ticket_id: str,
              exclude: set[str]) -> tuple[list[dict], list[dict]]:
    """(open, shipped) tasks tied to the ticket, leaving out `exclude` (the
    fix that is shipping now, and its parent or children). Raises when
    ClickUp cannot be read."""
    import clickup_search

    open_, shipped = [], []
    for task in _linked_queue_tasks(ticket_number, ticket_id):
        if task["id"] in exclude:
            continue
        status = (task.get("status") or "").lower()
        if status == "declined":
            continue
        (shipped if status in clickup_search.SHIPPED else open_).append(task)
    for task in _linked_agent_fixes(ticket_number, ticket_id):
        if task["id"] in exclude or task["parent"] in exclude:
            continue
        (shipped if task["shipped"] else open_).append(task)
    return open_, shipped


def _fix_first(brief: str) -> str:
    """The brief with its fix section moved to the front, so the cut at
    _FIX_CHARS drops cause, never the change."""
    match = _FIX_SECTION.search(brief)
    if not match:
        return brief
    return f"{brief[match.start():]} {brief[:match.start()]}"


def _fix_text(task: dict) -> str:
    import clickup_search

    brief = ""
    try:
        brief = clickup_search._task_brief(task["id"])
    except Exception as exc:
        print(f"[SCOPE] could not read task {task.get('id')}: {exc}")
    body = _fix_first(brief.strip())[:_FIX_CHARS]
    return f"{task.get('name') or task.get('id')} (live)" + (
        f": {body}" if body else "")


def thread_text(ticket_id: str) -> str:
    """The client thread, formatted the way the on-prod handler reads it."""
    from agent import _format_conversations, fetch_ticket_conversations

    return _format_conversations(fetch_ticket_conversations(ticket_id))


def check(ticket_id: str, ticket_number: str, fixes: list[dict],
          thread: str | None = None, exclude: set[str] | None = None) -> dict:
    """May the automatic "it is fixed" email go out for this ticket?

    `fixes`: the task(s) shipping now, each {"id", "name"}. `exclude`: their
    ids plus any parent or child ids, so they are not counted as open work.
    `thread`: the formatted conversation, read from Zoho when not given.

    Returns {"complete": bool, "reason": str, "open": [str]}. `reason` is
    one line for Sam. Never raises; anything unreadable holds.
    """
    exclude = set(exclude or ()) | {f["id"] for f in fixes if f.get("id")}
    try:
        open_, shipped = open_work(ticket_number, ticket_id, exclude)
    except Exception as exc:
        print(f"[SCOPE] {ticket_id}: ClickUp unreadable ({exc}), holding")
        return {"complete": False, "open": [],
                "reason": f"could not check the ticket's other tasks ({exc})"}
    if open_:
        names = [t.get("name") or t.get("id") for t in open_]
        return {"complete": False, "open": names,
                "reason": "the client also raised work that has not shipped: "
                          + "; ".join(names)}

    try:
        if thread is None:
            thread = thread_text(ticket_id)
        from vomeos import run_agent

        context = {
            "thread": thread or "(no messages could be read)",
            "shipped_fixes": "\n".join(
                _fix_text(t) for t in list(fixes) + shipped),
        }
        # Asked again before holding: no verdict is not a "no", and a held
        # resolution is never asked again (#9571, 2026-10-30: one empty
        # model reply held a live fix's email for good).
        for _ in range(_ASKS):
            result = run_agent(AGENT, context=context,
                               subject_type="zoho_ticket",
                               subject_id=ticket_id)
            if result.ok:
                break
    except Exception as exc:
        print(f"[SCOPE] {ticket_id}: scope check failed ({exc}), holding")
        return {"complete": False, "open": [],
                "reason": f"could not check what the client raised ({exc})"}
    if not result.ok:
        return {"complete": False, "open": [],
                "reason": f"no scope verdict ({result.why()})"}
    issues = [str(i) for i in (result.get("open_issues") or []) if i]
    if result.get("all_fixed") is True and not issues:
        return {"complete": True, "open": [],
                "reason": str(result.get("reason") or "")}
    return {"complete": False, "open": issues,
            "reason": "not everything the client raised is fixed yet: "
                      + ("; ".join(issues)
                         or str(result.get("reason") or "unclear"))}
