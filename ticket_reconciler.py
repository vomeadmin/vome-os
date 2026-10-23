"""
ticket_reconciler.py

Every night, every Zoho ticket sitting at In Progress is matched to its
ClickUp work and given one state Sam can quote in a sprint: our team is on
it, it is fixed and waiting for release, it shipped, or it fell through.

WHY
---
Sam, 2026-10-23: "I randomly see many tasks In Progress and assigned on
Zoho, but I don't know what the status of that task is on ClickUp." That
day 71 tickets were In Progress, 35 with no customer message in over 30
days, the oldest from April 30. Nothing looked at them: the sprint, the
30-minute sweep and walk_queue read only New and Processing (and only the
300 most recently active tickets), the stale sweep reads only Awaiting
Client Response, and the backlog sweep is a one-off command. Spot checks
found #9188 Closed in ClickUp but In Progress in Zoho, and #7218 queued and
unassigned since May.

STATES
------
    fix_live          an Agent Fixes task for it is Live (release watcher
                      drafts the resolution)
    fix_on_dev        an agent fix is merged, waiting for production
    shipped           its engineer task is `on prod`: the client is owed
                      a resolution
    task_closed       its engineer task is closed or declined without
                      `on prod`: what happened needs reading
    engineer_active   an open engineer task, updated in the last 14 days
    engineer_stalled  an open engineer task nobody touched in 14+ days
    unassigned        an open task with nobody on it
    no_task           In Progress with no task and no fix at all

WHAT IT DOES (and does not)
---------------------------
  * Records every state in vomeos_ticket_reconcile, for `ticket_states`
    in Claude.ai and `py ticket_reconciler.py report`.
  * engineer_stalled: one ClickUp comment on the task (which notifies its
    assignee), at most once per 14 days per task. RECONCILE_NUDGE=false
    turns it off.
  * shipped, task_closed, no_task: a fresh shadow triage (with the deep
    pass), oldest first, up to RECONCILE_RETRIAGE_CAP a night (15). It
    reads the engineer's task notes and lands a card and a draft in the
    sprint, so Sam sends "it's fixed" or the right answer in one word. It
    never files an engineer task on its own (`route N` does, on Sam's
    word); the fix ladder may still open a PR. A ticket is not re-triaged
    again for 7 days unless its task changed.
  * Never writes to Zoho, never emails anyone, never closes anything.

SWITCHES
--------
    RECONCILE_ENABLED          default on
    RECONCILE_STATUSES         Zoho statuses to reconcile ("In Progress")
    RECONCILE_STALL_DAYS       14
    RECONCILE_NUDGE            default on
    RECONCILE_RETRIAGE_CAP     15 per run
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from vomeos import store

TABLE = "vomeos_ticket_reconcile"
CLICKUP = "https://api.clickup.com/api/v2"
PRIORITY_QUEUE = "901113386257"
AGENT_FIXES = "901115573468"
PAGE_SIZE = 100

OPEN_TASK = {"queued", "in progress", "on dev", "to do", "open",
             "needs client info", "awaiting client response", "escalated",
             "user education", "review", "in review"}
ON_PROD = {"on prod"}
CLOSED_TASK = {"closed", "done", "complete", "declined", "finished"}

_TICKET_IN_NAME = re.compile(r"#(\d{3,6})\b")

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        ticket_id        VARCHAR PRIMARY KEY,
        ticket_number    VARCHAR,
        subject          VARCHAR,
        zoho_status      VARCHAR,
        zoho_assignee    VARCHAR,
        last_customer_at VARCHAR,
        state            VARCHAR,
        task_id          VARCHAR,
        task_name        VARCHAR,
        task_status      VARCHAR,
        task_assignees   VARCHAR,
        task_updated     VARCHAR,
        fix_task_id      VARCHAR,
        fix_check        VARCHAR,
        checked_at       TIMESTAMP,
        nudged_at        TIMESTAMP,
        retriaged_at     TIMESTAMP,
        retriage_task_status VARCHAR,
        retriage_result  VARCHAR
    )
    """,
)


def enabled() -> bool:
    value = os.environ.get("RECONCILE_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def _statuses() -> list[str]:
    raw = os.environ.get("RECONCILE_STATUSES", "In Progress")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _stall_days() -> int:
    return int(os.environ.get("RECONCILE_STALL_DAYS", "14"))


def _nudge_on() -> bool:
    value = os.environ.get("RECONCILE_NUDGE", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def _retriage_cap() -> int:
    return int(os.environ.get("RECONCILE_RETRIAGE_CAP", "15"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms_date(value) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc
                                      ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Zoho: every ticket at the reconciled statuses
# ---------------------------------------------------------------------------

def zoho_tickets(status: str) -> list[dict]:
    """Every ticket at `status`, all pages. Raises when Zoho cannot be read.

    Zoho's `status` parameter rejects custom statuses and the plain
    `filters.Status` form is ignored (sprint.fetch_open_tickets), but the
    `filters.criteria` form is honoured: on 2026-10-23 it returned exactly
    the 71 In Progress tickets. Rows are still checked on the way back.
    """
    from agent import _unwrap_mcp_result, _zoho_desk_call, get_last_mcp_error

    rows: list[dict] = []
    for page in range(20):
        result = None
        for pause in (0, 2, 6):
            if pause:
                time.sleep(pause)
            result = _zoho_desk_call("ZohoDesk_getTickets", {"query_params": {
                "filters": {"criteria": {"fieldConditions": [
                    {"field": "status", "con": "is", "data": [status]}]}},
                "limit": PAGE_SIZE, "from": page * PAGE_SIZE,
                "sortBy": "createdTime",
            }})
            if result:
                break
        if not result:
            raise RuntimeError(get_last_mcp_error() or "Zoho returned nothing")
        payload = _unwrap_mcp_result(result)
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            data = data.get("data")
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected Zoho shape: {type(data).__name__}")
        # If Zoho ever ignores the filter, the pages are the oldest tickets
        # in the system and the In Progress set would look tiny. Stop
        # rather than reconcile a wrong list.
        if any(r.get("status") != status for r in data):
            raise RuntimeError(f"Zoho ignored the {status!r} filter")
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            break
    return rows


# ---------------------------------------------------------------------------
# ClickUp: the work linked to each ticket
# ---------------------------------------------------------------------------

def _headers() -> dict:
    token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not token:
        raise RuntimeError("no CLICKUP_API_TOKEN")
    return {"Authorization": token}


def _list_tasks(list_id: str, updated_since_ms: int) -> list[dict]:
    """Open and closed tasks in a list updated since a date, all pages."""
    tasks: list[dict] = []
    for page in range(40):
        resp = httpx.get(
            f"{CLICKUP}/list/{list_id}/task", headers=_headers(),
            params={"page": page, "include_closed": "true",
                    "subtasks": "true",
                    "date_updated_gt": str(updated_since_ms)},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        batch = body.get("tasks") or []
        tasks.extend(batch)
        if body.get("last_page") or len(batch) < 100:
            break
    return tasks


def _index(tasks: list[dict]) -> tuple[dict, dict]:
    """(by Zoho ticket id, by ticket number) -> tasks."""
    from zoho_links import extract_zoho_ticket_id

    by_id: dict[str, list] = {}
    by_number: dict[str, list] = {}
    for task in tasks:
        tid = extract_zoho_ticket_id(task) or ""
        if tid:
            by_id.setdefault(tid, []).append(task)
        for number in _TICKET_IN_NAME.findall(task.get("name") or ""):
            by_number.setdefault(number, []).append(task)
    return by_id, by_number


def _status(task: dict) -> str:
    return ((task.get("status") or {}).get("status") or "").lower()


def _pick(tasks: list[dict]) -> dict:
    """The task that speaks for the ticket: an open one first (work in
    flight), then the most recently updated."""
    if not tasks:
        return {}
    unique = {t.get("id"): t for t in tasks}.values()
    return sorted(unique, key=lambda t: (
        _status(t) in OPEN_TASK, int(t.get("date_updated") or 0)),
        reverse=True)[0]


def _release_check(task: dict) -> str:
    try:
        import release_watcher as rw

        return rw._release_check_value(task) or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# The state of one ticket
# ---------------------------------------------------------------------------

def classify(task: dict, fix: dict, now: datetime | None = None) -> str:
    now = now or _now()
    if fix:
        if _release_check(fix) == "live" or _status(fix) in CLOSED_TASK:
            return "fix_live"
        return "fix_on_dev"
    if not task:
        return "no_task"
    status = _status(task)
    if status in ON_PROD:
        return "shipped"
    # A named open status wins over its type: ClickUp gives "awaiting
    # client response" a done type, and #8691 read as closed (2026-10-23).
    if status in CLOSED_TASK or (status not in OPEN_TASK and (
            task.get("status") or {}).get("type") in ("closed", "done")):
        return "task_closed"
    if not task.get("assignees"):
        return "unassigned"
    updated = int(task.get("date_updated") or 0) / 1000
    if updated and now.timestamp() - updated > _stall_days() * 86400:
        return "engineer_stalled"
    return "engineer_active"


def assess(rows: list[dict], pq: list[dict], fixes: list[dict]) -> list[dict]:
    """One record per ticket, joining Zoho rows to their ClickUp work."""
    pq_id, pq_num = _index(pq)
    fx_id, fx_num = _index(fixes)
    out = []
    for row in rows:
        tid = str(row.get("id") or "")
        number = str(row.get("ticketNumber") or "")
        task = _pick(pq_id.get(tid, []) + pq_num.get(number, []))
        fix_tasks = fx_id.get(tid, []) + fx_num.get(number, [])
        # The parent carries the Release Check; a child names the ticket.
        fix = {}
        if fix_tasks:
            child = _pick(fix_tasks)
            parent_id = child.get("parent")
            fix = next((t for t in fixes if t.get("id") == parent_id), child)
        out.append({
            "ticket_id": tid,
            "ticket_number": number,
            "subject": (row.get("subject") or "")[:200],
            "zoho_status": row.get("status") or "",
            "zoho_assignee": str(row.get("assigneeId") or ""),
            "last_customer_at": str(row.get("customerResponseTime")
                                    or row.get("createdTime") or "")[:10],
            "state": classify(task, fix),
            "task_id": str(task.get("id") or ""),
            "task_name": (task.get("name") or "")[:200],
            "task_status": _status(task),
            "task_assignees": ", ".join(
                a.get("username") or a.get("email") or ""
                for a in task.get("assignees") or []),
            "task_updated": _ms_date(task.get("date_updated")),
            "fix_task_id": str(fix.get("id") or ""),
            "fix_check": _release_check(fix) if fix else "",
        })
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _previous() -> dict[str, dict]:
    rows = store.query(f"SELECT * FROM {TABLE}")
    return {r["ticket_id"]: r for r in rows}


def save(records: list[dict]) -> None:
    now = _now()
    for r in records:
        store.execute(
            f"INSERT INTO {TABLE} (ticket_id, ticket_number, subject,"
            " zoho_status, zoho_assignee, last_customer_at, state, task_id,"
            " task_name, task_status, task_assignees, task_updated,"
            " fix_task_id, fix_check, checked_at)"
            " VALUES (:ticket_id, :ticket_number, :subject, :zoho_status,"
            " :zoho_assignee, :last_customer_at, :state, :task_id,"
            " :task_name, :task_status, :task_assignees, :task_updated,"
            " :fix_task_id, :fix_check, :now)"
            " ON CONFLICT (ticket_id) DO UPDATE SET"
            " ticket_number = EXCLUDED.ticket_number,"
            " subject = EXCLUDED.subject,"
            " zoho_status = EXCLUDED.zoho_status,"
            " zoho_assignee = EXCLUDED.zoho_assignee,"
            " last_customer_at = EXCLUDED.last_customer_at,"
            " state = EXCLUDED.state, task_id = EXCLUDED.task_id,"
            " task_name = EXCLUDED.task_name,"
            " task_status = EXCLUDED.task_status,"
            " task_assignees = EXCLUDED.task_assignees,"
            " task_updated = EXCLUDED.task_updated,"
            " fix_task_id = EXCLUDED.fix_task_id,"
            " fix_check = EXCLUDED.fix_check,"
            " checked_at = EXCLUDED.checked_at",
            {**r, "now": now},
        )


def _drop_gone(current: set[str]) -> None:
    """Tickets no longer at a reconciled status leave the table."""
    for tid in set(_previous()) - current:
        store.execute(f"DELETE FROM {TABLE} WHERE ticket_id = :t",
                      {"t": tid})


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _days_ago(value) -> float:
    if not isinstance(value, datetime):
        return 1e9
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (_now() - value).total_seconds() / 86400


def nudge(record: dict, previous: dict) -> bool:
    """One comment on a stalled task, at most every STALL_DAYS."""
    if record["state"] != "engineer_stalled" or not record["task_id"]:
        return False
    if _days_ago(previous.get("nudged_at")) < _stall_days():
        return False
    text = (
        f"Zoho #{record['ticket_number']} is still In Progress and this task "
        f"has not changed since {record['task_updated']}. The client last "
        f"wrote on {record['last_customer_at']}. Where does it stand? If it "
        "shipped, move it to on prod and the client is told automatically; "
        "if it needs something from them, needs client info.\n\n"
        "_Vome OS reconciler_"
    )
    try:
        resp = httpx.post(f"{CLICKUP}/task/{record['task_id']}/comment",
                          headers=_headers(),
                          json={"comment_text": text, "notify_all": True},
                          timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[RECONCILE] nudge failed on {record['task_id']}: {exc}")
        return False
    store.execute(f"UPDATE {TABLE} SET nudged_at = :n WHERE ticket_id = :t",
                  {"n": _now(), "t": record["ticket_id"]})
    return True


RETRIAGE_STATES = ("shipped", "task_closed", "no_task")
RETRIAGE_DAYS = 7
RETRIAGE_SECONDS = int(os.environ.get("RECONCILE_RETRIAGE_SECONDS", "2400"))


def wants_retriage(record: dict, previous: dict) -> bool:
    if record["state"] not in RETRIAGE_STATES:
        return False
    if _days_ago(previous.get("retriaged_at")) >= RETRIAGE_DAYS:
        return True
    # Sooner when the task moved since the last look.
    return (previous.get("retriage_task_status") or "") != \
        record["task_status"]


def retriage(record: dict) -> str:
    """A fresh first pass (and deep pass) on the ticket. The result lands
    in the sprint as an item with a draft; nothing is sent."""
    import shadow_triage

    try:
        out = shadow_triage._handle(record["ticket_id"], "reconcile",
                                    force=True, auto_route=False)
    except Exception as exc:
        out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    summary = (f"{out.get('status')} item {out.get('item') or ''} "
               f"{out.get('category') or out.get('reason') or ''}").strip()
    store.execute(
        f"UPDATE {TABLE} SET retriaged_at = :n, retriage_task_status = :s,"
        " retriage_result = :r WHERE ticket_id = :t",
        {"n": _now(), "s": record["task_status"], "r": summary[:200],
         "t": record["ticket_id"]})
    return summary


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def run(retriage_cap: int | None = None, act: bool = True) -> dict:
    if not enabled():
        return {"status": "disabled"}
    if not store.ensure_table(TABLE):
        return {"status": "no database"}
    rows: list[dict] = []
    try:
        for status in _statuses():
            rows.extend(zoho_tickets(status))
    except Exception as exc:
        # An unreadable queue is not an empty one: change nothing.
        return {"status": "zoho unreadable", "error": str(exc)}
    oldest = min((r.get("createdTime") or "" for r in rows), default="")
    since = datetime.now(timezone.utc) - timedelta(days=400)
    if oldest:
        try:
            since = datetime.fromisoformat(oldest.replace("Z", "+00:00")) \
                - timedelta(days=30)
        except ValueError:
            pass
    try:
        since_ms = int(since.timestamp() * 1000)
        pq = _list_tasks(PRIORITY_QUEUE, since_ms)
        fixes = _list_tasks(AGENT_FIXES, since_ms)
    except Exception as exc:
        return {"status": "clickup unreadable", "error": str(exc)}

    previous = _previous()
    records = assess(rows, pq, fixes)
    save(records)
    _drop_gone({r["ticket_id"] for r in records})

    nudged = 0
    retriaged: list[str] = []
    if act:
        for record in records:
            if _nudge_on() and nudge(record, previous.get(
                    record["ticket_id"], {})):
                nudged += 1
        cap = _retriage_cap() if retriage_cap is None else retriage_cap
        due = [r for r in records
               if wants_retriage(r, previous.get(r["ticket_id"], {}))]
        # Shipped first (good news owed), then closed, then orphans; oldest
        # customer message first within each.
        order = {s: i for i, s in enumerate(RETRIAGE_STATES)}
        due.sort(key=lambda r: (order[r["state"]], r["last_customer_at"]))
        started = time.monotonic()
        for record in due[:cap]:
            # Each re-triage can run a deep pass; stop starting new ones in
            # time to finish inside the worker's 3300s soft limit.
            if time.monotonic() - started > RETRIAGE_SECONDS:
                break
            retriaged.append(f"#{record['ticket_number']}: "
                             f"{retriage(record)}")
    counts: dict[str, int] = {}
    for r in records:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    out = {"status": "ok", "tickets": len(records), "states": counts,
           "nudged": nudged, "retriaged": retriaged,
           "retriage_waiting": max(0, len(
               [r for r in records if r["state"] in RETRIAGE_STATES])
               - len(retriaged))}
    print(f"[RECONCILE] {json.dumps(out, default=str)[:1500]}")
    return out


# ---------------------------------------------------------------------------
# Reading it back (Claude.ai `ticket_states`, the CLI)
# ---------------------------------------------------------------------------

SAY = {
    "fix_live": "Fixed and live. The resolution reply is drafted by the "
                "release watcher; send it.",
    "fix_on_dev": "Fixed by an agent PR, waiting for the next production "
                  "release. 'Our fix is in testing and goes out shortly.'",
    "shipped": "The engineer marked it on prod. The client is owed a "
               "resolution; a card and draft are queued in the sprint.",
    "task_closed": "The engineer task was closed without on prod. Read "
                   "the re-triage card before telling the client anything.",
    "engineer_active": "Our team is on it.",
    "engineer_stalled": "Our team has it, but nobody touched the task in "
                        "14+ days. The engineer was nudged on the task.",
    "unassigned": "A task exists with nobody on it. Assign or route it.",
    "no_task": "No task and no fix: fell through. A re-triage card is "
               "queued in the sprint.",
}


def states(state: str = "") -> dict:
    if not store.ensure_table(TABLE):
        return {"error": "no database"}
    params = {}
    where = ""
    if state:
        where = " WHERE state = :s"
        params["s"] = state
    rows = store.query(
        f"SELECT ticket_number, subject, state, task_id, task_status,"
        f" task_assignees, task_updated, last_customer_at, fix_check,"
        f" retriage_result, checked_at FROM {TABLE}{where}"
        " ORDER BY last_customer_at", params)
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["state"], []).append({
            "ticket": f"#{r['ticket_number']}",
            "subject": r["subject"],
            "task": (f"https://app.clickup.com/t/{r['task_id']}"
                     if r["task_id"] else ""),
            "task_status": r["task_status"],
            "assigned": r["task_assignees"],
            "task_updated": r["task_updated"],
            "client_last_wrote": r["last_customer_at"],
            "retriage": r["retriage_result"] or "",
        })
    checked = max((r["checked_at"] for r in rows if r.get("checked_at")),
                  default=None)
    return {
        "checked_at": str(checked or "never"),
        "counts": {k: len(v) for k, v in grouped.items()},
        "what_to_tell_the_client": {k: SAY.get(k, "") for k in grouped},
        **grouped,
    }


def main(argv: list[str]) -> int:
    if argv and argv[0] == "run":
        dry = "--dry-run" in argv
        print(json.dumps(run(act=not dry), indent=2, default=str))
        return 0
    if argv and argv[0] == "report":
        print(json.dumps(states(argv[1] if len(argv) > 1 else ""),
                         indent=2, default=str))
        return 0
    print("usage: py ticket_reconciler.py run [--dry-run] | report [state]")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
