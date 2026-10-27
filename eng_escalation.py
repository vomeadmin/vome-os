"""
eng_escalation.py

The daily pass that keeps the engineers' Priority Queues honest over time.
It only ever RAISES a priority, one level a day at most, and never lowers
one: lowering is Sam's call.

WHY
---
Sam, 2026-10-27: priorities are set right when a task is filed
(eng_priority), but a client who has waited a week is more urgent than the
day they wrote. UMMS (39k CAD) reported the same sequence pop-up four times
between July and October before it was urgent. A weekly re-sort would let
that wait up to six more days and flip priorities under the engineers, so
this runs every weekday just before their morning list (eng_digest).

THE RULES (client tickets: a Zoho link on the task, or "[#1234]" in its name)
-------------------------------------------------------------------------
    came_back        the task was on prod or closed and is open again: the
                     client came back after we said it was fixed. +1 level
    very_high_wait   high, a very-high account (Combined ARR 4000+), waiting
                     7 days or more since it was filed -> urgent
    normal_wait      normal, waiting 10 business days or more -> high
    very_high_floor  normal on a very-high account -> high (the filing
                     ladder applied again, e.g. after the account grew)

Low (features, cleanup) and blank priorities are never raised; the digest
lists the blanks for Sam. Only queued, in progress and escalated tasks are
looked at: on dev already has its fix. Each rule raises a task once, ever
(vomeos_eng_escalations), so a task Sam lowers stays where he put it.

Every raise leaves a ClickUp comment with the reason. The digest shows what
was raised, and Friday's week-close report reviews the week (`week_review`).

SWITCHES
--------
    ENG_ESCALATION_ENABLED          default on
    ENG_RAISE_VERY_HIGH_DAYS        default 7
    ENG_RAISE_NORMAL_BUSINESS_DAYS  default 10
    ENG_STALE_IN_PROGRESS_DAYS      default 14 (week review only)
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

import eng_priority
from vomeos import store

ESCALATIONS_TABLE = "vomeos_eng_escalations"
ACTIVE = ("queued", "in progress", "escalated")
SAID_FIXED = ("on prod", "closed", "complete", "done")
VERY_HIGH_ARR = 4000.0
_TICKET = re.compile(r"^\s*\[#\d+")

store.register_table(
    ESCALATIONS_TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {ESCALATIONS_TABLE} (
        task_id        VARCHAR NOT NULL,
        reason         VARCHAR NOT NULL,
        from_priority  INTEGER,
        to_priority    INTEGER,
        engineer       VARCHAR,
        task_name      TEXT,
        raised_at      TIMESTAMP NOT NULL,
        PRIMARY KEY (task_id, reason)
    )
    """,
)


def enabled() -> bool:
    return os.environ.get("ENG_ESCALATION_ENABLED", "true").strip().lower() \
        not in ("false", "0", "off", "no")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _status(task: dict) -> str:
    return str((task.get("status") or {}).get("status") or "").lower()


def _field(task: dict, field_id: str):
    for field in task.get("custom_fields") or []:
        if field.get("id") == field_id:
            return field.get("value")
    return None


def arr(task: dict) -> float:
    import ticket_routing

    try:
        return float(_field(task, ticket_routing.FIELD_COMBINED_ARR) or 0)
    except (TypeError, ValueError):
        return 0.0


def is_ticket(task: dict) -> bool:
    import sprint_thread

    return bool(_field(task, sprint_thread.FIELD_ZOHO_TICKET_LINK)
                or _TICKET.match(str(task.get("name") or "")))


def _created(task: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(task.get("date_created")) / 1000,
                                      tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def business_days(start: datetime, end: datetime) -> int:
    """Weekdays from `start` to `end`, not counting the start day."""
    days, day = 0, start.date()
    while day < end.date():
        day += timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def decide(task: dict, history: list[str], now: datetime,
           done: set[str]) -> tuple[str, int, str] | None:
    """(reason, new priority, why in words) for one task, or None.

    `history` is every status the task has been in; `done` the rules
    already applied to it."""
    current = eng_priority.of_task(task)
    if current is None or current >= eng_priority.LOW:
        return None
    if _status(task) not in ACTIVE or not is_ticket(task):
        return None
    up = current - 1
    if up < eng_priority.URGENT:
        return None
    created = _created(task) or now
    days = (now - created).days
    money = arr(task)
    very_high = money >= VERY_HIGH_ARR
    rules = [
        ("came_back", any(s in SAID_FIXED for s in history),
         "the client came back after we said it was fixed"),
        ("very_high_wait",
         current == eng_priority.HIGH and very_high
         and days >= _env_int("ENG_RAISE_VERY_HIGH_DAYS", 7),
         f"a very-high account ({int(money):,} CAD) has waited {days} days"),
        ("normal_wait",
         current == eng_priority.NORMAL and business_days(created, now)
         >= _env_int("ENG_RAISE_NORMAL_BUSINESS_DAYS", 10),
         f"the client has waited {business_days(created, now)} business "
         "days"),
        ("very_high_floor", current == eng_priority.NORMAL and very_high,
         f"a very-high account ({int(money):,} CAD) starts at high"),
    ]
    for reason, applies, why in rules:
        if applies and reason not in done:
            return reason, up, why
    return None


def run(now: datetime | None = None, dry_run: bool = False) -> dict:
    """Raise what the rules say, for both engineers. Returns
    {task_id: {"to": priority, "why": text, "name": ..., "url": ...}}."""
    if not enabled():
        return {}
    import sprint_thread
    import weekly_engineering_report as report

    now = now or datetime.now(timezone.utc)
    tasks: dict[str, tuple[str, dict]] = {}
    for owner, (clickup_id, *_rest) in sprint_thread.ENGINEERS.items():
        for task in report._fetch_one_assignee(str(clickup_id)) or []:
            tasks.setdefault(str(task.get("id")), (owner, task))
    candidates = {tid: pair for tid, pair in tasks.items()
                  if _status(pair[1]) in ACTIVE and is_ticket(pair[1])}
    histories = _histories(list(candidates))
    applied = _applied(list(candidates))
    raised = {}
    for tid, (owner, task) in candidates.items():
        verdict = decide(task, histories.get(tid, []), now,
                         applied.get(tid, set()))
        if not verdict:
            continue
        reason, to, why = verdict
        raised[tid] = {"to": to, "why": why, "reason": reason,
                       "owner": owner, "name": task.get("name") or "",
                       "url": task.get("url") or ""}
        if dry_run:
            continue
        if not _raise(tid, to, why):
            raised.pop(tid)
            continue
        _record(tid, reason, eng_priority.of_task(task), to, owner,
                task.get("name") or "", now)
    print(f"[ENG ESCALATION] raised {len(raised)}"
          + (" (dry run)" if dry_run else ""))
    return raised


def _histories(task_ids: list[str]) -> dict[str, list[str]]:
    """Every status each task has been in, from ClickUp's bulk time in
    status (100 a call). A task it cannot read has no history, so
    came_back simply does not fire for it."""
    out: dict[str, list[str]] = {}
    for start in range(0, len(task_ids), 100):
        chunk = task_ids[start:start + 100]
        try:
            resp = httpx.get(
                "https://api.clickup.com/api/v2/task/bulk_time_in_status/"
                "task_ids",
                params=[("task_ids", t) for t in chunk],
                headers={"Authorization":
                         os.environ.get("CLICKUP_API_TOKEN", "")},
                timeout=30)
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:
            print(f"[ENG ESCALATION] status history unreadable: {exc}")
            continue
        for tid, info in data.items():
            out[str(tid)] = [
                str(s.get("status") or "").lower()
                for s in (info or {}).get("status_history") or []]
    return out


def _applied(task_ids: list[str]) -> dict[str, set[str]]:
    if not task_ids or not store.ensure_table(ESCALATIONS_TABLE):
        return {}
    rows = store.query(
        f"SELECT task_id, reason FROM {ESCALATIONS_TABLE} "
        "WHERE task_id = ANY(:ids)", {"ids": task_ids})
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(str(row["task_id"]), set()).add(str(row["reason"]))
    return out


def _raise(task_id: str, to: int, why: str) -> bool:
    headers = {"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
               "Content-Type": "application/json"}
    try:
        httpx.put(f"https://api.clickup.com/api/v2/task/{task_id}",
                  headers=headers, json={"priority": to},
                  timeout=30).raise_for_status()
    except Exception as exc:
        print(f"[ENG ESCALATION] {task_id} not raised: {exc}")
        return False
    try:
        httpx.post(
            f"https://api.clickup.com/api/v2/task/{task_id}/comment",
            headers=headers, timeout=30,
            json={"comment_text":
                  f"Priority raised to {eng_priority.name(to)} "
                  f"automatically: {why}. The daily pass only raises; "
                  "lowering it is Sam's call.",
                  "notify_all": False}).raise_for_status()
    except Exception as exc:
        print(f"[ENG ESCALATION] {task_id} raised, comment failed: {exc}")
    return True


def _record(task_id: str, reason: str, before, to: int, owner: str,
            name: str, now: datetime) -> None:
    if not store.ensure_table(ESCALATIONS_TABLE):
        return
    store.query(
        f"INSERT INTO {ESCALATIONS_TABLE} (task_id, reason, from_priority, "
        "to_priority, engineer, task_name, raised_at) VALUES (:t, :r, :f, "
        ":to, :e, :n, :at) ON CONFLICT DO NOTHING RETURNING task_id",
        {"t": task_id, "r": reason, "f": before, "to": to, "e": owner,
         "n": name[:300], "at": now.replace(tzinfo=None)}, write=True)


def week_review(tasks: list[dict], now: datetime | None = None) -> str:
    """Friday's review for Sam: what the pass raised this week, what still
    has no priority, and work in progress that has not moved."""
    now = now or datetime.now(timezone.utc)
    lines = ["*Priorities this week*"]
    rows = []
    if store.ensure_table(ESCALATIONS_TABLE):
        rows = store.query(
            f"SELECT task_id, to_priority, engineer, task_name, reason "
            f"FROM {ESCALATIONS_TABLE} WHERE raised_at >= :since "
            "ORDER BY raised_at",
            {"since": (now - timedelta(days=7)).replace(tzinfo=None)})
    if rows:
        lines.append(f"Raised by the daily pass ({len(rows)}):")
        lines += [f"• <https://app.clickup.com/t/{r['task_id']}|"
                  f"{_short(r['task_name'])}> to "
                  f"{eng_priority.name(r['to_priority'])} "
                  f"({r['reason'].replace('_', ' ')}, {r['engineer']})"
                  for r in rows[:15]]
    else:
        lines.append("Nothing was raised by the daily pass.")
    active = [t for t in tasks if _status(t) in ACTIVE]
    blank = [t for t in active if eng_priority.of_task(t) is None]
    if blank:
        lines.append(f"No priority yet ({len(blank)}): " + ", ".join(
            _link(t) for t in blank[:8]))
    stale_days = _env_int("ENG_STALE_IN_PROGRESS_DAYS", 14)
    cutoff = now - timedelta(days=stale_days)
    stale = [t for t in active if _status(t) == "in progress"
             and (_updated(t) or now) < cutoff]
    if stale:
        lines.append(f"In progress with no update in {stale_days}+ days "
                     f"({len(stale)}), for Sam to lower, close or push: "
                     + ", ".join(_link(t) for t in stale[:8]))
    return "\n".join(lines)


def _updated(task: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(task.get("date_updated")) / 1000,
                                      tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _short(name: str, length: int = 70) -> str:
    name = " ".join(str(name or "Task").split())
    if len(name) > length:
        name = name[:length].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "..."
    return name.replace("|", "/").replace("<", "").replace(">", "")


def _link(task: dict) -> str:
    url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    return f"<{url}|{_short(task.get('name'))}>"
