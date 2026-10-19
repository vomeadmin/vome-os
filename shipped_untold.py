"""
shipped_untold.py

A fix or a feature that shipped, for a client nobody has told yet: prepare
the reply and put it in front of Sam in the next batch.

WHY
---
Sam, 2026-10-17: "Anything that we have not yet sent to the customer but has
already been fixed should have a prepared reply: it has been updated, and
they can check that out." The release watcher already does this for Agent
Fixes (release_resolutions). Two other places ship work for clients and can
leave them untold:

1. Priority Queue tasks an engineer moved to `on prod`. The on-prod handler
   emails the client at that moment, but a failed handler, a webhook outage
   or a task with its link added late leaves the client hearing nothing.
   (Sprint Lessons Log, Oct 9: six tasks closed in a week came back from
   clients who were never told.)
2. Sam Accepted (Build Now) feature requests that closed: what the client
   asked for exists now (gap G11).

WHAT IT DOES, ONCE PER TASK
---------------------------
For tasks updated in the last LOOKBACK_DAYS: read the Zoho ticket linked to
it; if we have written to the client since the task shipped, it is told and
nothing happens. Otherwise the ticket is triaged again with the shipped
state in front of the agents, so the draft is a resolution, through every
outbound guard. A shipped FIX whose draft passed every guard is sent and
the ticket closes, as the on-prod handler would have done (Sam, 2026-10-19:
fixed emails go out on their own; RELEASE_AUTO_SEND switches it off). A
shipped FEATURE, and any draft a guard held, waits under READY TO SEND for
Sam.

`on prod` only, never just "closed": a closed task is not proof of a fix
(the Oct 9 lesson). Each task is claimed in vomeos_release_resolutions (the
same ledger the Agent Fixes path uses), so a task is drafted once, retried
on an error up to three times, and the batch labels it a resolution.

SWITCH
------
    SHIPPED_UNTOLD_ENABLED   default on. "false" does nothing.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

PRIORITY_QUEUE = "901113386257"
SAM_ACCEPTED = "901115573620"
LOOKBACK_DAYS = int(os.environ.get("SHIPPED_UNTOLD_DAYS", "14"))
PER_RUN = int(os.environ.get("SHIPPED_UNTOLD_PER_RUN", "5"))

_ZOHO_URL = re.compile(r"https?://\S*(?:zoho|support\.vomevolunteer)\S*")


def enabled() -> bool:
    value = os.environ.get("SHIPPED_UNTOLD_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def _ms(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _from_ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc
                                      ).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _tasks(list_id: str, query: str) -> list[dict]:
    import release_watcher as rw

    since = _ms(datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(days=LOOKBACK_DAYS))
    out: list[dict] = []
    for page in range(10):
        data = rw._clickup(f"/list/{list_id}/task?{query}"
                           f"&date_updated_gt={since}&page={page}")
        batch = data.get("tasks") or []
        out.extend(batch)
        if data.get("last_page", True) or not batch:
            break
    return out


def shipped_tasks() -> list[dict]:
    """[{task, kind}] for work that shipped in the lookback window."""
    found = []
    for task in _tasks(PRIORITY_QUEUE,
                       "statuses[]=on%20prod&include_closed=true"
                       "&subtasks=true"):
        found.append({"task": task, "kind": "fix"})
    for task in _tasks(SAM_ACCEPTED, "include_closed=true&subtasks=true"):
        status = (task.get("status") or {})
        if status.get("type") in ("closed", "done"):
            found.append({"task": task, "kind": "feature"})
    return found


def ticket_id_for(task: dict) -> str:
    """The Zoho ticket: the link field, else the first Zoho URL in the
    description (Sam Accepted tasks are filed with it there)."""
    from zoho_links import extract_zoho_ticket_id, parse_zoho_ticket_id

    found = extract_zoho_ticket_id(task)
    if found:
        return str(found)
    text = f"{task.get('markdown_description') or ''}\n" \
           f"{task.get('description') or ''}\n{task.get('text_content') or ''}"
    match = _ZOHO_URL.search(text)
    return str(parse_zoho_ticket_id(match.group(0)) or "") if match else ""


def shipped_at(task: dict) -> datetime | None:
    return _from_ms(task.get("date_done") or task.get("date_closed")
                    or task.get("date_updated"))


def already_told(ticket_id: str, since: datetime | None) -> bool:
    """True when we have written to the client since the work shipped."""
    import zoho_actions
    from sprint_batch import _parse_time

    latest = _parse_time(
        (zoho_actions.latest_outbound(ticket_id) or {}).get("createdTime"))
    if latest is None:
        return False
    # The on-prod handler sends seconds after the status change, and ClickUp
    # stamps the change a moment later; allow for both clocks.
    return since is None or latest >= since - timedelta(minutes=10)


def _release(task: dict, kind: str) -> dict:
    name = task.get("name") or task.get("id")
    if kind == "feature":
        text = (f"SHIPPED. The feature this client asked for is now live in "
                f"the product (task {task.get('id')}: {name}). A reply "
                "telling them it is available is backed.")
    else:
        text = (f"LIVE. Engineering marked task {task.get('id')} ({name}) on "
                "prod: the fix is deployed to production. A resolution "
                "reply is backed.")
    return {"text": text, "live": True, "task_id": task.get("id") or ""}


def run_shipped_untold() -> dict:
    """The job. Never raises; an unreadable list is reported, not empty."""
    if not enabled():
        return {"status": "disabled"}
    import release_resolutions as rr
    import shadow_triage
    import sprint_store

    if not sprint_store.ready():
        return {"status": "no database"}
    try:
        shipped = shipped_tasks()
    except Exception as exc:
        print(f"[UNTOLD] ClickUp could not be read: {exc}")
        return {"status": "unreachable", "error": str(exc)}

    drafted, told, skipped = [], 0, 0
    for entry in shipped:
        if len(drafted) >= PER_RUN:
            break
        task, kind = entry["task"], entry["kind"]
        ticket_id = ticket_id_for(task)
        if not ticket_id:
            skipped += 1
            continue
        try:
            if already_told(ticket_id, shipped_at(task)):
                told += 1
                continue
        except Exception as exc:
            print(f"[UNTOLD] {ticket_id}: Zoho unreadable ({exc})")
            continue
        found = re.search(r"#(\d{3,6})", task.get("name") or "")
        number = found.group(1) if found else ""
        if not rr._claim(task["id"], kind, ticket_id, number):
            continue
        try:
            outcome = shadow_triage._handle(
                ticket_id, f"shipped_{kind}", force=True,
                release=_release(task, kind), ignore_cap=True)
        except Exception as exc:
            outcome = {"status": "error", "error": str(exc)}
        item = sprint_store.by_ticket(ticket_id)
        if outcome.get("status") != "ok" or not item:
            rr._finish(task["id"], None, "error",
                       str(outcome.get("reason") or outcome.get("error")))
            continue
        result = {"ok": "drafted", "blocked": "held"}.get(
            item.get("draft_status") or "", "no_draft")
        detail = item.get("draft_why") or ""
        if (result == "drafted" and kind == "fix"
                and rr.auto_send_enabled()
                and item.get("category") not in rr.NEVER_AUTO):
            import sprint_thread

            detail = sprint_thread._send(item)
            result = "sent" if "sent to" in detail else "drafted"
        rr._finish(task["id"], item.get("number"), result, detail)
        drafted.append(item.get("number"))
    print(f"[UNTOLD] {len(shipped)} shipped, {told} already told, "
          f"{len(drafted)} drafted, {skipped} without a ticket")
    return {"status": "ok", "shipped": len(shipped), "told": told,
            "drafted": drafted, "no_ticket": skipped}
