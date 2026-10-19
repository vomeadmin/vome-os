"""
clickup_search.py

Find the ClickUp task that already covers a symptom.

WHY THIS EXISTS
---------------
The Sprint Lessons Log, 2026-09-02: checking for an existing task before
creating one "paid off five times today". One ticket was already logged under
the client's own earlier ticket, another belonged on an existing editor task,
and one was the same person's fifth request for the same thing. The same day
also produced the opposite: a client told something was on the roadmap when
no task existed anywhere, and a task created retroactively to make the
statement true.

Doing that check by hand is the single most valuable manual step in a sprint
and there was no code for it. This is that code.

HOW IT MATCHES
--------------
Token overlap on the task name, not the client's wording. Two people
reporting one defect rarely phrase it the same way, and a search on their
phrasing finds nothing while the task sits plainly in the backlog. Matching
is deliberately deterministic: no model call, no embeddings, because this
runs on every ticket in a sprint and anything slow or metered gets skipped.

It is a recall tool, not a verdict. It surfaces candidates and reports each
one's real status so a reviewer can judge. The important output is often
"this task exists but is only queued", which is the difference between a fix
and an intention.

THE LISTS
---------
All five from the System Configuration doc, because a request arriving for
the fifth time may be sitting in Declined, and that is information about
priority rather than a new request.
"""

from __future__ import annotations

import os
import re
import time

import httpx

from zoho_links import extract_zoho_ticket_id

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

# From the Vome AI Intelligence Layer doc.
LISTS: dict[str, str] = {
    "Priority Queue": "901113386257",
    "Raw Intake": "901113386484",
    "Accepted Backlog": "901113389889",
    "Sleeping": "901113389897",
    "Declined": "901113389900",
}

# Statuses that mean the work actually shipped. Everything else is in flight.
SHIPPED = frozenset({"on prod", "closed", "done", "complete"})

_STOPWORDS = frozenset(
    """
    a an and are as at be by can cannot for from get has have how in into is
    it its me my not of on or our so that the their them then there these
    this to up us was we what when where which who why will with you your
    issue problem error bug help please thanks hi hello re fw fwd
    """.split()
)

# Task lists change slowly and a sprint asks this question 25 times in a row.
_CACHE_TTL = int(os.environ.get("CLICKUP_SEARCH_CACHE_TTL", "600"))
_cache: dict[str, tuple[float, list[dict]]] = {}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9-]{2,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def _fetch_list(list_id: str, name: str) -> list[dict]:
    """Open tasks in one list, cached."""
    now = time.monotonic()
    hit = _cache.get(list_id)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]

    tasks: list[dict] = []
    try:
        for page in range(4):  # ClickUp pages at 100
            response = httpx.get(
                f"{CLICKUP_BASE}/list/{list_id}/task",
                headers={"Authorization": CLICKUP_API_TOKEN},
                params={
                    "page": page,
                    "include_closed": "true",
                    "subtasks": "false",
                },
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            batch = body.get("tasks", []) or []
            for task in batch:
                tasks.append({
                    "id": task.get("id"),
                    "name": task.get("name", ""),
                    "status": (task.get("status") or {}).get("status", ""),
                    "list": name,
                    "url": task.get("url", ""),
                    "zoho_ticket_id": extract_zoho_ticket_id(task) or "",
                })
            if body.get("last_page") or len(batch) < 100:
                break
    except Exception as exc:
        print(f"[CU SEARCH] could not read list {name}: {exc}")
        raise

    _cache[list_id] = (now, tasks)
    return tasks


def search_tasks_by_text(
    query: str, limit: int = 5, threshold: float = 0.18
) -> list[dict]:
    """Candidate tasks covering `query`, best match first.

    Raises when ClickUp cannot be read, so a caller can report "we did not
    look" rather than the far more dangerous "nothing found".
    """
    wanted = _tokens(query)
    if not wanted:
        return []

    scored: list[tuple[float, dict]] = []
    for name, list_id in LISTS.items():
        for task in _fetch_list(list_id, name):
            have = _tokens(task["name"])
            if not have:
                continue
            overlap = len(wanted & have) / len(wanted | have)
            if overlap >= threshold:
                task = dict(task)
                task["score"] = round(overlap, 3)
                task["shipped"] = task["status"].lower() in SHIPPED
                scored.append((overlap, task))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [task for _, task in scored[:limit]]


def describe_matches(query: str, limit: int = 5) -> str:
    """The `existing_tasks` context block, as prose for an agent.

    Says plainly when the search did not run. A confident "no existing task
    found" that was never actually executed is the failure this is meant to
    prevent, so the unavailable case must never look like the empty case.
    """
    if not CLICKUP_API_TOKEN:
        return (
            "Existing-task search did NOT run (no ClickUp token). Treat "
            "existing work as UNKNOWN, not as none."
        )
    try:
        hits = search_tasks_by_text(query, limit=limit)
    except Exception as exc:
        return (
            f"Existing-task search FAILED ({exc}). Treat existing work as "
            "UNKNOWN, not as none."
        )
    if not hits:
        return "No existing task found for this symptom (search ran cleanly)."

    lines = []
    for task in hits:
        shipped = "SHIPPED" if task["shipped"] else "work in flight, not a fix"
        lines.append(
            f"Task {task['id']} '{task['name']}' in {task['list']} is at "
            f"status: {task['status']} ({shipped})"
        )
    return "\n".join(lines)


# Words in a CRM account name that never appear in how the team names a task
# ("Sarasota County Government" is filed as "Sarasota County -- ...").
_ACCOUNT_NOISE = frozenset(
    """
    government inc llc ltd corp corporation company foundation association
    organization organisation society group the of and
    """.split()
)


def _ticket_linked(task: dict, ticket_number: str, zoho_ticket_id: str) -> bool:
    if zoho_ticket_id and task.get("zoho_ticket_id") == zoho_ticket_id:
        return True
    return bool(ticket_number) and bool(
        re.search(rf"#{re.escape(ticket_number)}\b", task.get("name", ""))
    )


def linked_tasks(ticket_number: str, zoho_ticket_id: str) -> list[dict]:
    """Tasks already tied to this ticket in any list, by Zoho link or by
    "#<number>" in the name. Raises when a list cannot be read, so a caller
    about to file new work never mistakes "could not look" for "none"."""
    if not CLICKUP_API_TOKEN:
        raise RuntimeError("no ClickUp token")
    return [
        task
        for name, list_id in LISTS.items()
        for task in _fetch_list(list_id, name)
        if _ticket_linked(task, ticket_number, zoho_ticket_id)
    ]


def _account_tokens(account: str) -> set[str]:
    return {t for t in _tokens(account) if t not in _ACCOUNT_NOISE}


def describe_existing_work(
    query: str,
    ticket_number: str = "",
    zoho_ticket_id: str = "",
    account: str = "",
    limit: int = 5,
) -> str:
    """The `existing_tasks` block, searched the way a person searches.

    Name-token overlap alone misses the commonest case. Ticket #9500 ("Forms
    Issue") was being worked on task "[#9393] ... Form settings won't save",
    which shares no token with the subject, and the overlap search picked an
    unrelated 2024 feature request instead. A person looks for the ticket
    number first, then at everything open for that customer, and only then
    at the symptom. This does the same, in that order, and labels each group
    so the agent can weigh a direct link above a word match.
    """
    if not CLICKUP_API_TOKEN:
        return (
            "Existing-task search did NOT run (no ClickUp token). Treat "
            "existing work as UNKNOWN, not as none."
        )
    try:
        all_tasks = [
            task
            for name, list_id in LISTS.items()
            for task in _fetch_list(list_id, name)
        ]
        symptom = search_tasks_by_text(query, limit=limit)
    except Exception as exc:
        return (
            f"Existing-task search FAILED ({exc}). Treat existing work as "
            "UNKNOWN, not as none."
        )

    def line(task: dict) -> str:
        shipped = task["status"].lower() in SHIPPED
        state = "SHIPPED" if shipped else "work in flight, not a fix"
        return (
            f"- Task {task['id']} '{task['name']}' in {task['list']} is at "
            f"status: {task['status']} ({state})"
        )

    seen: set[str] = set()
    sections: list[str] = []

    linked = [t for t in all_tasks if _ticket_linked(t, ticket_number, zoho_ticket_id)]
    if linked:
        seen.update(t["id"] for t in linked)
        sections.append(
            "Linked to THIS ticket (Zoho link or ticket number in the name):\n"
            + "\n".join(line(t) for t in linked)
        )

    wanted = _account_tokens(account)
    if wanted:
        same_account = [
            t for t in all_tasks
            if t["id"] not in seen
            and wanted <= _tokens(t["name"])
            and t["status"].lower() not in SHIPPED
        ]
        if same_account:
            seen.update(t["id"] for t in same_account)
            sections.append(
                "Other OPEN tasks for this same customer (check whether one "
                "of these is already the work, e.g. a reopened earlier "
                "ticket):\n" + "\n".join(line(t) for t in same_account[:10])
            )

    symptom = [t for t in symptom if t["id"] not in seen]
    if symptom:
        sections.append(
            "Possible matches on the symptom wording only (weak evidence):\n"
            + "\n".join(line(t) for t in symptom)
        )

    if not sections:
        return "No existing task found for this ticket, customer or symptom (search ran cleanly)."
    return "\n\n".join(sections)
