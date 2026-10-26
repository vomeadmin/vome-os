"""
vomeos/integrations/clickup_read.py

Read one ClickUp task and its comments, for an agent that is checking what
an engineer already found.

WHY THIS EXISTS
---------------
The triage card's existing-work line is a search summary: a task name and a
status. What settles a ticket is usually inside the task: "not a bug, the
admin had the setting off", "fixed in the 10/14 release", "needs the user's
email". Sam reads that in Claude Code before he disagrees with a card; the
agent could not (2026-10-23). The reconciler needs the same thing to tell a
task closed as fixed from one closed as "cannot reproduce".

Read only, by construction: two GETs, no other verb in this file.

ENVIRONMENT
-----------
    CLICKUP_API_TOKEN   the same token the support app already uses
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx

BASE = "https://api.clickup.com/api/v2"
MAX_DESCRIPTION = 4000
MAX_COMMENTS = 15
MAX_COMMENT = 1200

_TASK_ID = re.compile(r"(?:/t/)?([0-9a-z]{7,12})\b", re.IGNORECASE)


def _date(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc
                                      ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _comment_text(comment: dict) -> str:
    plain = comment.get("comment_text")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    return "".join((b.get("text") or "") for b in comment.get("comment") or []
                   if isinstance(b, dict)).strip()


def task_id(ref: str) -> str:
    """`868abc123` from an id or any ClickUp task URL."""
    ref = (ref or "").strip().rstrip("/")
    if "/t/" in ref:
        ref = ref.split("/t/", 1)[1].split("/")[-1].split("?")[0]
    match = _TASK_ID.fullmatch(ref) or _TASK_ID.search(ref)
    return match.group(1) if match else ""


def fetch(ref: str) -> tuple[dict, list[dict]]:
    """(task, comments oldest first). Raises on a failed read."""
    token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not token:
        raise RuntimeError("no CLICKUP_API_TOKEN")
    tid = task_id(ref)
    if not tid:
        raise ValueError(f"not a ClickUp task id or URL: {ref!r}")
    headers = {"Authorization": token}
    task = httpx.get(f"{BASE}/task/{tid}", headers=headers,
                     params={"include_markdown_description": "true"},
                     timeout=30)
    task.raise_for_status()
    comments = httpx.get(f"{BASE}/task/{tid}/comment", headers=headers,
                         timeout=30)
    comments.raise_for_status()
    # ClickUp returns comments newest first.
    rows = list(reversed(comments.json().get("comments") or []))
    return task.json(), rows


def task_text(ref: str) -> str:
    """The task as plain text for an agent. Never raises."""
    try:
        task, comments = fetch(ref)
    except Exception as exc:
        return (f"Could not read task {ref!r}: {type(exc).__name__}: "
                f"{str(exc)[:160]}. This is NOT 'no task'.")
    status = (task.get("status") or {}).get("status") or "?"
    who = ", ".join(a.get("username") or a.get("email") or "?"
                    for a in task.get("assignees") or []) or "nobody"
    lines = [
        f"Task {task.get('id')} \"{task.get('name') or ''}\"",
        f"Status: {status}. Assigned: {who}. List: "
        f"{(task.get('list') or {}).get('name') or '?'}.",
        f"Created {_date(task.get('date_created'))}, updated "
        f"{_date(task.get('date_updated'))}"
        + (f", closed {_date(task.get('date_closed'))}"
           if task.get("date_closed") else "") + ".",
    ]
    description = (task.get("markdown_description") or task.get("description")
                   or "").strip()
    lines.append("Description:\n" + (description[:MAX_DESCRIPTION]
                                     or "(empty)"))
    if comments:
        lines.append(f"Comments ({len(comments)}, newest last):")
        for comment in comments[-MAX_COMMENTS:]:
            text = _comment_text(comment)
            if not text:
                continue
            author = (comment.get("user") or {}).get("username") or "someone"
            lines.append(f"[{_date(comment.get('date'))} {author}] "
                         f"{text[:MAX_COMMENT]}")
    else:
        lines.append("No comments.")
    return "\n".join(lines)
