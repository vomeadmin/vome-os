"""
eng_digest.py

Each engineer's morning list, in their channel: what to work on today, in
order, straight from their Priority Queue.

WHY
---
Sam, 2026-10-27: "Slack should be designed for urgent items ... ClickUp
should be the backlog of actual tasks", and the engineers should wake up
knowing what to do first. Since then only urgent work pings
(eng_priority). Everything else is filed at a priority and reaches the
engineer here, once a weekday, as one message with one mention.

WHAT IT SAYS
------------
    the top of the list   every urgent task, then the next by priority up
                          to ENG_DIGEST_TOP (5); in progress before queued
                          at the same priority (finish what is started),
                          then oldest first
    new since last time   tasks created since the last weekday
    on dev                how many wait for a production merge
    in progress > 3       a nudge: too much started reads as nothing moving
    no priority           the tasks Sam still has to place
    raised overnight      what eng_escalation's daily pass just raised, and
                          why (it runs first, inside the same job)

A task with no priority sorts after high and before normal, so a blank
security finding cannot sink to the bottom while it waits for Sam.

SWITCHES
--------
    ENG_DIGEST_ENABLED   default on
    ENG_DIGEST_DRY_RUN   "true" prints instead of posting
    ENG_DIGEST_TOP       how many to list, default 5
    ENG_DIGEST_HOUR / ENG_DIGEST_MINUTE  America/Montreal, default 07:05
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import eng_priority

ACTIVE = ("queued", "in progress", "escalated")
ON_DEV = "on dev"
WIP_LIMIT = 3
NAME_LENGTH = 80

# A blank priority sorts between high (2) and normal (3).
_BLANK = 2.5
_LABEL = {eng_priority.URGENT: ":red_circle: *Urgent*",
          eng_priority.HIGH: ":large_orange_circle: High",
          eng_priority.NORMAL: ":large_blue_circle: Normal",
          eng_priority.LOW: ":white_circle: Low"}


def enabled() -> bool:
    return os.environ.get("ENG_DIGEST_ENABLED", "true").strip().lower() \
        not in ("false", "0", "off", "no")


def _dry_run() -> bool:
    return os.environ.get("ENG_DIGEST_DRY_RUN", "false").strip().lower() \
        in ("1", "true", "yes")


def _top() -> int:
    try:
        return max(1, int(os.environ.get("ENG_DIGEST_TOP", "5")))
    except ValueError:
        return 5


def _status(task: dict) -> str:
    return str((task.get("status") or {}).get("status") or "").lower()


def _created(task: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(task.get("date_created")) / 1000,
                                      tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def order(tasks: list[dict]) -> list[dict]:
    """The engineer's working order for the active tasks."""
    def key(task: dict):
        priority = eng_priority.of_task(task)
        created = _created(task)
        return (_BLANK if priority is None else priority,
                0 if _status(task) == "in progress" else 1,
                created.timestamp() if created else float("inf"))
    return sorted((t for t in tasks if _status(t) in ACTIVE), key=key)


def pick(ordered: list[dict], top: int) -> list[dict]:
    """Every urgent task, then the rest up to `top`."""
    urgent = [t for t in ordered
              if eng_priority.of_task(t) == eng_priority.URGENT]
    return ordered[:max(top, len(urgent))]


def _since(now: datetime) -> datetime:
    """The last weekday's run: Friday's for a Monday."""
    return now - timedelta(days=3 if now.weekday() == 0 else 1)


def _age(task: dict, now: datetime) -> str:
    created = _created(task)
    if not created:
        return ""
    days = (now - created).days
    return "today" if days < 1 else f"{days}d old"


def _level(task: dict) -> str:
    return eng_priority.name(eng_priority.of_task(task))


def _link(task: dict) -> str:
    name = " ".join(str(task.get("name") or "Task").split())
    if len(name) > NAME_LENGTH:
        name = name[:NAME_LENGTH].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "..."
    name = name.replace("|", "/").replace("<", "").replace(">", "")
    url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    return f"<{url}|{name}>"


def render(owner: str, tasks: list[dict], now: datetime,
           raised: dict | None = None) -> str:
    """The message for one engineer, or "" when nothing is active.
    `raised` is eng_escalation.run's {task_id: {"to", "why"}}."""
    import sprint_thread

    _cid, slack_id, _team, _channel = sprint_thread.ENGINEERS[owner]
    ordered = order(tasks)
    if not ordered:
        return ""
    lines = [f":sunrise: <@{slack_id}> your Priority Queue for "
             f"{sprint_thread.day_label(now)}, in the order to work it:"]
    for n, task in enumerate(pick(ordered, _top()), 1):
        priority = eng_priority.of_task(task)
        label = _LABEL.get(priority, ":black_circle: No priority")
        lines.append(f"{n}. {label} · {_status(task)} · {_link(task)} · "
                     f"{_age(task, now)}")
    rest = len(ordered) - len(pick(ordered, _top()))
    if rest:
        lines.append(f"_{rest} more after these, in ClickUp._")

    mine = [(t, (raised or {})[str(t.get("id"))]) for t in tasks
            if str(t.get("id")) in (raised or {})]
    if mine:
        lines.append("*Raised overnight:* " + "; ".join(
            f"{_link(t)} to {eng_priority.name(r['to'])} ({r['why']})"
            for t, r in mine))

    since = _since(now)
    new = [t for t in tasks if (_created(t) or now) >= since
           and _status(t) in ACTIVE + (ON_DEV,)]
    if new:
        shown = [f"{_link(t)} ({_level(t)})" for t in new[:8]]
        more = f" and {len(new) - 8} more" if len(new) > 8 else ""
        lines.append(f"*New since {sprint_thread.day_label(since)}* "
                     f"({len(new)}): " + ", ".join(shown) + more)
    on_dev = sum(1 for t in tasks if _status(t) == ON_DEV)
    if on_dev:
        lines.append(f"*On dev, waiting for production:* {on_dev}")
    started = sum(1 for t in ordered if _status(t) == "in progress")
    if started > WIP_LIMIT:
        lines.append(f"_{started} tasks in progress. Finish some or move "
                     "them back to queued, so this list shows what is "
                     "really moving._")
    blank = [t for t in ordered if eng_priority.of_task(t) is None]
    if blank:
        lines.append(f"_No priority yet on {len(blank)} (Sam places "
                     "these): " + ", ".join(_link(t) for t in blank[:5])
                     + "_")
    return "\n".join(lines)


def run_daily_digest(now: datetime | None = None) -> dict:
    """Post each engineer's list. A failed ClickUp read posts nothing for
    that engineer: a partial list looks complete and misleads."""
    if not enabled():
        return {"status": "off"}
    import sprint_thread
    import weekly_engineering_report as report

    now = now or datetime.now(timezone.utc)
    # The daily raise-only pass first, so the list shows today's order.
    try:
        import eng_escalation

        raised = eng_escalation.run(now, dry_run=_dry_run())
    except Exception as exc:
        print(f"[ENG DIGEST] escalation pass failed: {exc}")
        raised = {}
    out = {}
    for owner, (clickup_id, _sid, _team, channel) in \
            sprint_thread.ENGINEERS.items():
        tasks = report._fetch_one_assignee(str(clickup_id))
        if tasks is None:
            out[owner] = "ClickUp unreadable, nothing posted"
            continue
        text = render(owner, tasks, now, raised)
        if not text:
            out[owner] = "nothing active"
        elif _dry_run():
            print(f"[ENG DIGEST] {owner} (dry run):\n{text}")
            out[owner] = "dry run"
        else:
            out[owner] = ("posted" if sprint_thread._post(text,
                                                          channel=channel)
                          else "post failed")
    print(f"[ENG DIGEST] {out}")
    return out
