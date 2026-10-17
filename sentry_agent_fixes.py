"""
sentry_agent_fixes.py

File an actionable Sentry issue into the Agent Fixes list, once per root
cause.

WHY THIS EXISTS
---------------
By 2026-10-17 the Sentry pipeline had triaged 140 issues and analysed 61, and
none of it existed anywhere a person works. The verdicts lived in a Postgres
table and a Slack thread that was failing to post. The profile-creation
incident (Oct 15) was diagnosed correctly 12 seconds after the first error and
2h20m before the first ticket, and nobody saw it.

Agent Fixes is where the support agent's own fixes already live, with a
release watcher that proves them live. A Sentry diagnosis belongs there too:
it is agent-owned work until a person says otherwise, and it stays out of the
engineers' Priority Queue and their weekly report.

ONE TASK PER ROOT CAUSE, NOT PER ISSUE
--------------------------------------
Sentry groups by title and stack, so one bug can arrive as many issues. The
`content_mode` failure of Oct 15 to 16 came in as seven: "Error sending Vome
birthday email: null value in column ...", "IntegrityError: null value in
column ...", and so on. Seven tasks would be seven chances for two people to
fix the same thing. `group_key` strips the part that differs (the logging
prefix, numbers, ids) and keeps what identifies the fault (the message and its
quoted identifiers). Issues sharing a key share a task; later ones are added
as a comment.

Generic messages are the exception. "'NoneType' object has no attribute
'user'" means nothing without where it happened, so for those the culprit is
part of the key and different call sites stay different tasks.

WHAT IT DOES NOT DO
-------------------
It never assigns an engineer, never moves a task to `on prod` (the on-prod
handler would email a client), and never touches Zoho. When the analyst named
an existing task as the same fault, it comments there instead of creating one.

SWITCHES
--------
    SENTRY_AGENT_FIXES_ENABLED    default on. "false" files nothing.
    SENTRY_AGENT_FIXES_DAILY_CAP  new tasks per day, default 5. Comments on an
                                  existing task do not count.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx

import sentry_ledger
from vomeos import store

CLICKUP_BASE = "https://api.clickup.com/api/v2"
AGENT_FIXES_LIST = "901115573468"

FIELD_TYPE = "e0e439f5-397d-432d-addd-e90fbf50cd30"
TYPE_BUG = "d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df"
FIELD_SOURCE = "857e1262-cb5c-4c22-b8da-770a8fcfa82e"
SOURCE_INTERNAL = "ea82838e-f5ee-4cc6-b5d3-da4ef9052343"
FIELD_TAGS = "291fd0e7-42e3-4376-bacd-2b54a6c1d48c"
TAG_AGENT = "d4cf0a1c-7f21-41c5-8b8b-f00ebb459552"
FIELD_MERGE_OWNER = "12682083-5a03-4f27-a6b6-5c716aa5dd73"

CLICKUP_USER_ONLYG = 49257687
CLICKUP_USER_SANJAY = 4434086

PRIORITY = {"s1": 1, "s2": 2, "s3": 3}

# Messages that say what went wrong but not where. Their key includes the
# culprit, or every None dereference in the codebase would be one task.
_GENERIC = re.compile(
    r"nonetype|has no attribute|keyerror|indexerror|list index out of range"
    r"|object is not subscriptable|matching query does not exist"
    r"|not json serializable|unexpected keyword argument"
    r"|is not a function|cannot read propert|undefined is not"
    r"|expecting value: line|is not a valid uuid",
    re.IGNORECASE,
)


def enabled() -> bool:
    value = os.environ.get("SENTRY_AGENT_FIXES_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def daily_cap() -> int:
    return int(os.environ.get("SENTRY_AGENT_FIXES_DAILY_CAP", "5"))


def _token() -> str:
    return os.environ.get("CLICKUP_API_TOKEN", "")


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _detail(title: str) -> str:
    """The part of a title that identifies the fault.

    "Error sending Vome birthday email: null value in column ..." and
    "IntegrityError: null value in column ..." both reduce to the database
    message, which is the fault; the prefix is only who logged it.
    """
    title = (title or "").strip()
    head, sep, tail = title.partition(":")
    if sep and tail.strip() and len(tail.strip()) >= 15:
        return tail.strip()
    return title


def group_key(repo: str, title: str, culprit: str = "") -> str:
    """Stable key for "the same fault". Lowercase, ids and numbers removed."""
    detail = _detail(title).lower()
    detail = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", detail)
    detail = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", detail)
    detail = re.sub(r"\d+", "<n>", detail)
    detail = re.sub(r"\s+", " ", detail).strip(" .")
    # A trailing fragment cut off by Sentry's title length differs between
    # issues of the same fault; compare a fixed-length head instead.
    detail = detail[:90]
    parts = [repo or "?", detail]
    if _GENERIC.search(detail):
        parts.append((culprit or "").strip().lower())
    return " | ".join(parts)


def _group_task(key: str, issue_id: str) -> str:
    """The task already filed for this key, if any other issue has one."""
    rows = store.query(
        f"SELECT clickup_task_id FROM {sentry_ledger.TABLE}"
        " WHERE group_key = :key AND issue_id <> :issue_id"
        " AND clickup_task_id IS NOT NULL AND clickup_task_id <> ''"
        " ORDER BY filed_at NULLS LAST LIMIT 1",
        {"key": key, "issue_id": issue_id},
    )
    return rows[0]["clickup_task_id"] if rows else ""


def _filed_today() -> int:
    rows = store.query(
        f"SELECT COUNT(DISTINCT clickup_task_id) AS n FROM "
        f"{sentry_ledger.TABLE} WHERE filed_at >= :start",
        {"start": datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None)},
    )
    return int(rows[0]["n"]) if rows else 0


# ---------------------------------------------------------------------------
# ClickUp
# ---------------------------------------------------------------------------

def _clickup(path: str, body: dict) -> dict:
    resp = httpx.post(
        CLICKUP_BASE + path,
        headers={"Authorization": _token(),
                 "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _sentry_line(issue: dict) -> str:
    link = issue.get("permalink") or ""
    label = f"Sentry {issue.get('issue_id')}"
    return f"[{label}]({link})" if link else label


def _description(issue: dict, analysis: dict, pull_request: dict,
                 proposal: dict) -> str:
    pr_url = pull_request.get("url") or ""
    lines = [
        "**Filed by the Sentry pipeline.** Agent-owned until a person "
        "reassigns it. Not in the engineers' Priority Queue.",
        "",
        f"**Issue:** {issue.get('title')}",
        f"**Where:** `{issue.get('culprit') or '?'}` in "
        f"`{issue.get('repo')}@{issue.get('ref')}`",
        f"**Severity:** {issue.get('severity') or '?'} | "
        f"**Risk:** {analysis.get('risk') or '?'} | "
        f"**Confidence:** {analysis.get('confidence') or '?'} | "
        f"**Migration:** {'yes' if analysis.get('needs_migration') else 'no'}",
        f"**Sentry:** {_sentry_line(issue)}",
        "",
        "## Engineer summary",
        f"**Likely cause:** {analysis.get('root_cause') or '(none)'}",
        f"**Evidence:** {analysis.get('evidence') or '(none)'}",
        f"**Potential fix:** {analysis.get('proposed_fix') or '(none)'}",
    ]
    files = analysis.get("files") or []
    if files:
        lines.append("**Files:** " + ", ".join(f"`{f}`" for f in files[:6]))
    lines += ["", "## Fix"]
    if pr_url:
        lines.append(f"Draft pull request: {pr_url} (into `development`, "
                     "needs a person to review and merge).")
    else:
        lines.append(
            "No automatic fix: "
            + (proposal.get("reason") or pull_request.get("reason")
               or "not attempted")
        )
    lines += [
        "",
        "**Not verified:** read from the code and the trace only. Nothing "
        "was run. Data on the affected accounts was not checked.",
    ]
    return "\n".join(lines)


def _create(issue: dict, analysis: dict, pull_request: dict,
            proposal: dict) -> str:
    title = (issue.get("title") or "Sentry issue").strip()
    severity = (issue.get("severity") or "").lower()
    custom_fields = [
        {"id": FIELD_TYPE, "value": TYPE_BUG},
        {"id": FIELD_SOURCE, "value": SOURCE_INTERNAL},
        {"id": FIELD_TAGS, "value": [TAG_AGENT]},
    ]
    if pull_request.get("opened"):
        owner = (
            CLICKUP_USER_SANJAY if issue.get("stack") == "frontend"
            else CLICKUP_USER_ONLYG
        )
        custom_fields.append(
            {"id": FIELD_MERGE_OWNER, "value": {"add": [owner]}}
        )
    body = {
        "name": f"[Sentry] {title[:110]} -- {severity.upper() or 'S?'}",
        "markdown_description": _description(
            issue, analysis, pull_request, proposal
        ),
        "status": "in progress" if pull_request.get("opened") else "queued",
        "priority": PRIORITY.get(severity, 3),
        "custom_fields": custom_fields,
    }
    data = _clickup(f"/list/{AGENT_FIXES_LIST}/task", body)
    return str(data.get("id") or "")


def _comment(task_id: str, issue: dict, analysis: dict) -> None:
    text = (
        f"Another Sentry issue with the same cause: {issue.get('title')}\n"
        f"{issue.get('permalink') or issue.get('issue_id')}\n"
        f"Severity {issue.get('severity') or '?'}, "
        f"risk {analysis.get('risk') or '?'}."
    )
    _clickup(f"/task/{task_id}/comment",
             {"comment_text": text, "notify_all": False})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def file_issue(issue: dict, analysis: dict | None, pull_request: dict,
               proposal: dict) -> dict:
    """Create or extend the Agent Fixes task for one analysed issue.

    Returns {"action": "created" | "commented" | "skipped", "task_id",
    "reason"}. Never raises: the issue is already claimed, and a ClickUp
    hiccup must not lose its diagnosis, which is in the ledger regardless.
    """
    if not enabled():
        return {"action": "skipped", "reason": "disabled"}
    if not analysis:
        return {"action": "skipped", "reason": "no analysis"}
    if not _token():
        return {"action": "skipped", "reason": "CLICKUP_API_TOKEN not set"}

    issue_id = str(issue.get("issue_id") or "")
    key = group_key(issue.get("repo", ""), issue.get("title", ""),
                    issue.get("culprit", ""))
    sentry_ledger.update(issue_id, group_key=key)

    target = (analysis.get("duplicate_of") or "").strip()
    target = target if re.fullmatch(r"[0-9a-z]{6,12}", target) else ""
    target = target or _group_task(key, issue_id)

    try:
        if target:
            _comment(target, issue, analysis)
            sentry_ledger.update(issue_id, clickup_task_id=target)
            return {"action": "commented", "task_id": target}

        if _filed_today() >= daily_cap():
            return {"action": "skipped",
                    "reason": f"daily cap of {daily_cap()} reached"}

        task_id = _create(issue, analysis, pull_request, proposal)
        if task_id:
            sentry_ledger.update(
                issue_id, clickup_task_id=task_id,
                filed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        return {"action": "created", "task_id": task_id}
    except Exception as exc:
        print(f"[SENTRY] Agent Fixes filing failed for {issue_id}: {exc}")
        return {"action": "skipped", "reason": f"ClickUp failed: {exc}"}
