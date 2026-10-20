"""
mcp_connector/feature_requests.py

The only writes the connector makes, and all of them are to ClickUp.

THE FLOW
--------
1. A customer asks for something Vome does not do.
2. Whoever is drafting calls `log_feature_request`. It searches the
   backlog lists first. If anything plausible exists it creates NOTHING and
   returns the candidates, because the Lessons Log of 2026-09-02 found the
   same person's fifth request for one feature, and five tasks for one
   feature hide demand instead of showing it.
3. If it is a repeat, `add_request_to_existing` puts a +1 comment on the
   existing task and appends the organization to Requesting Clients.
4. If it is new, it lands in Raw Intake as Type: Feature with no priority.
5. Sam (MCP_TRIAGE_EMAILS) calls `triage_feature_request` to set a priority
   and move it: Sam Accepted (Build Now, he builds it himself), Priority
   Queue, Accepted Backlog, Sleeping or Declined.

Until (5), a request is a record that someone asked and nothing more. A
reply to the customer may say "logged with the product team" and must not
say "on the roadmap", which is what `vomeos/guards/claims.py` exists to stop.

SCOPE OF THE WRITES
-------------------
Comments and triage are refused on any task outside the backlog lists
in `clickup_search.LISTS`, so the connector cannot be pointed at an
arbitrary task elsewhere in the workspace.
"""

from __future__ import annotations

import re

import httpx

import clickup_search
import clickup_tasks
from mcp_connector import config
from status_constants import CU_WRITE_QUEUED_LOWER

BASE = clickup_tasks.CLICKUP_BASE

DECISIONS = {
    "build_now": clickup_search.LISTS["Sam Accepted (Build Now)"],
    "priority_queue": clickup_search.LISTS["Priority Queue"],
    "accepted_backlog": clickup_search.LISTS["Accepted Backlog"],
    "sleeping": clickup_search.LISTS["Sleeping"],
    "declined": clickup_search.LISTS["Declined"],
    "keep_in_intake": None,
}

# ClickUp priority integers. Title suffix follows the P1/P2/P3 convention
# create_clickup_task already uses, with P4 for low.
PRIORITIES = {"urgent": 1, "high": 2, "normal": 3, "low": 4}

_BACKLOG_LIST_IDS = frozenset(clickup_search.LISTS.values())


class RequestError(RuntimeError):
    """A write was refused or failed. The message is safe to show."""


def _headers() -> dict[str, str]:
    token = config.clickup_token()
    if not token:
        raise RequestError("CLICKUP_API_TOKEN is not set on the server")
    return {"Authorization": token, "Content-Type": "application/json"}


def _get_backlog_task(task_id: str) -> dict:
    task_id = (task_id or "").strip().lstrip("#")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", task_id):
        raise RequestError(f"{task_id!r} is not a ClickUp task id")
    response = httpx.get(
        f"{BASE}/task/{task_id}", headers=_headers(), timeout=20,
    )
    if response.status_code == 404:
        raise RequestError(f"task {task_id} does not exist")
    response.raise_for_status()
    task = response.json()
    list_id = str((task.get("list") or {}).get("id") or "")
    if list_id not in _BACKLOG_LIST_IDS:
        raise RequestError(
            f"task {task_id} is not in a product backlog list; the "
            "connector only writes to Raw Intake, Sam Accepted (Build "
            "Now), Priority Queue, Accepted Backlog, Sleeping and Declined"
        )
    return task


def _list_name(list_id: str) -> str:
    for name, known in clickup_search.LISTS.items():
        if known == list_id:
            return name
    return list_id


def find_duplicates(title: str, problem: str) -> list[dict]:
    """Candidates covering this request. Raises if ClickUp cannot be read."""
    hits = clickup_search.search_tasks_by_text(title, limit=5)
    if len(hits) < 3 and problem:
        seen = {h["id"] for h in hits}
        hits += [
            h for h in clickup_search.search_tasks_by_text(problem, limit=5)
            if h["id"] not in seen
        ]
    return hits[:5]


def create_request(
    *,
    title: str,
    problem: str,
    requested_by: str,
    logged_by: str,
    ticket_url: str = "",
    platform: str = "",
    module: str = "",
    workaround: str = "",
) -> dict:
    title = " ".join((title or "").split())[:120]
    if len(title) < 8:
        raise RequestError("give the request a descriptive title")
    if not (problem or "").strip():
        raise RequestError("describe what the customer is trying to do")

    org = " ".join((requested_by or "").split()) or "Unknown organization"
    lines = [f"**Requested by:** {org}"]
    if ticket_url:
        lines.append(f"**Zoho ticket:** {ticket_url}")
    lines += [
        f"**Logged by:** {logged_by} via the Vome OS connector",
        "",
        "---",
        "",
        "**What they are trying to do**",
        problem.strip(),
    ]
    if workaround.strip():
        lines += [
            "", "**Workaround given to the customer**", workaround.strip(),
        ]
    lines += [
        "",
        "_Not yet triaged. Nothing has been promised to the customer beyond "
        "\"logged with the product team\"._",
    ]

    fields = [
        {"id": clickup_tasks.FIELD_TYPE, "value": clickup_tasks.TYPE_FEATURE},
        {
            "id": clickup_tasks.FIELD_SOURCE,
            "value": (
                clickup_tasks.SOURCE_ZOHO_TICKET if ticket_url
                else clickup_tasks.SOURCE_SUPPORT_WIDGET
            ),
        },
        {"id": clickup_tasks.FIELD_REQUESTING_CLIENTS, "value": org},
    ]
    if ticket_url:
        fields.append(
            {"id": clickup_tasks.FIELD_ZOHO_TICKET_LINK, "value": ticket_url}
        )
    platform_option = clickup_tasks._map_platform_option(platform or "")
    if platform_option:
        fields.append(
            {"id": clickup_tasks.FIELD_PLATFORM, "value": platform_option}
        )
    if module:
        fields.append({
            "id": clickup_tasks.FIELD_MODULE,
            "value": clickup_tasks._map_module_option(module),
        })

    description = "\n".join(lines)
    list_id = config.feature_request_list()
    response = httpx.post(
        f"{BASE}/list/{list_id}/task",
        headers=_headers(),
        json={
            "name": f"{org} -- {title}",
            # markdown_content wins when ClickUp supports it; description is
            # the same text for anything that reads the plain field.
            "description": description,
            "markdown_content": description,
            "status": CU_WRITE_QUEUED_LOWER,
            "custom_fields": fields,
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise RequestError(
            f"ClickUp refused the task (HTTP {response.status_code}): "
            f"{response.text[:200]}"
        )
    task = response.json()
    # The search cache would otherwise hide this task from the next dedupe
    # check for up to ten minutes.
    clickup_search._cache.pop(list_id, None)
    return {
        "task_id": task.get("id"),
        "name": task.get("name"),
        "url": (
            task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
        ),
        "list": _list_name(list_id),
    }


def add_plus_one(
    *, task_id: str, requested_by: str, logged_by: str, note: str = "",
    ticket_url: str = "",
) -> dict:
    task = _get_backlog_task(task_id)
    org = " ".join((requested_by or "").split()) or "Unknown organization"
    comment = (
        f"+1 from {org}, logged by {logged_by} via the Vome OS connector."
    )
    if ticket_url:
        comment += f"\nZoho ticket: {ticket_url}"
    if note.strip():
        comment += f"\n\n{note.strip()}"

    response = httpx.post(
        f"{BASE}/task/{task['id']}/comment",
        headers=_headers(),
        json={"comment_text": comment, "notify_all": False},
        timeout=20,
    )
    if response.status_code >= 400:
        raise RequestError(
            f"ClickUp refused the comment (HTTP {response.status_code})"
        )

    # Append to Requesting Clients so demand is visible on the task itself.
    # Best effort: the comment is the record, this is the summary.
    clients_updated = False
    current = ""
    for field in task.get("custom_fields") or []:
        if field.get("id") == clickup_tasks.FIELD_REQUESTING_CLIENTS:
            current = str(field.get("value") or "")
    if org.lower() not in current.lower():
        value = f"{current}; {org}" if current else org
        try:
            httpx.post(
                f"{BASE}/task/{task['id']}/field/"
                f"{clickup_tasks.FIELD_REQUESTING_CLIENTS}",
                headers=_headers(),
                json={"value": value},
                timeout=20,
            ).raise_for_status()
            clients_updated = True
        except Exception as exc:
            print(f"[MCP REQUESTS] requesting-clients update failed: {exc}")

    return {
        "task_id": task["id"],
        "name": task.get("name"),
        "url": task.get("url"),
        "status": (task.get("status") or {}).get("status"),
        "requesting_clients_updated": clients_updated,
    }


def triage(
    *, task_id: str, decision: str, priority: str = "", assign_to: int | None,
    note: str = "", triaged_by: str,
) -> dict:
    if decision not in DECISIONS:
        raise RequestError(
            f"decision must be one of: {', '.join(DECISIONS)}"
        )
    if priority and priority not in PRIORITIES:
        raise RequestError(
            f"priority must be one of: {', '.join(PRIORITIES)}"
        )
    task = _get_backlog_task(task_id)
    changes: dict = {}
    if priority:
        level = PRIORITIES[priority]
        changes["priority"] = level
        base = re.sub(r"\s+--\s+P[1-4]$", "", task.get("name") or "")
        changes["name"] = f"{base} -- P{level}"
    if assign_to:
        changes["assignees"] = {"add": [assign_to], "rem": []}
    if changes:
        response = httpx.put(
            f"{BASE}/task/{task['id']}", headers=_headers(), json=changes,
            timeout=20,
        )
        if response.status_code >= 400:
            raise RequestError(
                f"ClickUp refused the update (HTTP {response.status_code})"
            )

    moved_to = None
    target = DECISIONS[decision]
    if target:
        if not clickup_tasks.move_clickup_task(task["id"], target):
            raise RequestError(
                "priority saved but the move failed; move it by hand"
            )
        moved_to = _list_name(target)

    if note.strip():
        httpx.post(
            f"{BASE}/task/{task['id']}/comment",
            headers=_headers(),
            json={
                "comment_text": f"Triage by {triaged_by}: {note.strip()}",
                "notify_all": False,
            },
            timeout=20,
        )

    for list_id in _BACKLOG_LIST_IDS:
        clickup_search._cache.pop(list_id, None)
    return {
        "task_id": task["id"],
        "url": task.get("url"),
        "name": changes.get("name", task.get("name")),
        "priority": priority or None,
        "moved_to": moved_to,
        "assigned": bool(assign_to),
    }
