"""
eng_priority.py

One priority ladder for every engineer task the agents file, and the one
rule for when an engineer is pinged in Slack.

WHY
---
Sam, 2026-10-27, after auditing #eng-backend: 75 posts in two weeks, 17 of
them "URGENT" (any account at 4000+ ARR was urgent), the same task pinged up
to four times, and 29 of OnlyG's 65 Priority Queue tasks with no priority,
the worst security findings among them. His model: Slack is for urgent
items, ClickUp is the backlog, and the Priority Queue always shows current
priorities so an engineer knows what to do first when he wakes up.

THE LADDER (ClickUp priority)
-----------------------------
    URGENT (1)  production down or degraded, client data lost, a security
                hole that can be used today, a hard date, or the same
                problem from two or more accounts
    HIGH   (2)  a client bug on a very-high account (ARR 4000+) or an
                important client, Sam's infrastructure work, Sentry S2
    NORMAL (3)  a single-account bug, a one-off data audit beside a fix,
                Sentry S3
    LOW    (4)  feature handoffs and cleanup

No task is filed without one. Only URGENT pings; everything else reaches
the engineer in the morning digest (eng_digest.py).

PINGS (`ping`)
--------------
At most once per task, ever: a ticket joining a task already pinged is a
ClickUp comment. A burst folds into one thread: an urgent ping to the same
engineer within BURST_MINUTES of the last one replies under it, with no
second mention (the Oct 26 audit sent 18 posts in ten minutes). When the
store is down the ping still goes out, unthreaded: an urgent must arrive.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from vomeos import store

URGENT, HIGH, NORMAL, LOW = 1, 2, 3, 4
NAMES = {URGENT: "urgent", HIGH: "high", NORMAL: "normal", LOW: "low"}
_BY_NAME = {v: k for k, v in NAMES.items()}

PINGS_TABLE = "vomeos_eng_pings"

store.register_table(
    PINGS_TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {PINGS_TABLE} (
        task_id    VARCHAR PRIMARY KEY,
        engineer   VARCHAR NOT NULL,
        channel    VARCHAR,
        ts         VARCHAR,
        thread_ts  VARCHAR,
        sent_at    TIMESTAMP NOT NULL
    )
    """,
)


def burst_minutes() -> int:
    try:
        return int(os.environ.get("ENG_PING_BURST_MINUTES", "30"))
    except ValueError:
        return 30


def from_name(name, default: int = NORMAL) -> int:
    """"urgent" -> 1. Anything unknown is `default`."""
    return _BY_NAME.get(str(name or "").strip().lower(), default)


def name(priority) -> str:
    return NAMES.get(_int(priority), "none")


def of_task(task: dict) -> int | None:
    """The ClickUp priority of a task as the API returns it, or None."""
    raw = (task or {}).get("priority")
    if isinstance(raw, dict):
        raw = raw.get("id") or raw.get("orderindex")
    return _int(raw)


def _int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number in NAMES else None


def higher(new: int, current: int | None) -> bool:
    """Whether `new` outranks what the task already has (1 is highest)."""
    return current is None or new < current


def below(priority: int) -> int:
    """One step down, never below NORMAL unless it already was: the data
    audit beside an urgent fix is high, beside anything else normal."""
    return LOW if priority >= LOW else min(priority + 1, NORMAL)


def ping(owner: str, task_id: str, text: str, priority: int) -> str:
    """Tell the engineer in their channel, if and only if this is urgent.

    `text` carries no mention; the first ping of a burst gets one.
    Returns "pinged", "threaded", or why nothing was posted."""
    if priority != URGENT:
        return "not urgent"
    import sprint_thread

    _clickup_id, slack_id, _team, channel = sprint_thread.ENGINEERS[owner]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tracked = store.ensure_table(PINGS_TABLE)
    if tracked and task_id:
        claimed = store.query(
            f"INSERT INTO {PINGS_TABLE} (task_id, engineer, channel, "
            "sent_at) VALUES (:t, :e, :c, :at) ON CONFLICT DO NOTHING "
            "RETURNING task_id",
            {"t": task_id, "e": owner, "c": channel, "at": now}, write=True)
        if not claimed:
            return "already pinged"
    root = _burst_root(owner, now) if tracked else ""
    if root:
        ts = sprint_thread._post(f":rotating_light: Also urgent: {text}",
                                 thread_ts=root, channel=channel)
        outcome = "threaded"
    else:
        ts = sprint_thread._post(f":rotating_light: <@{slack_id}> {text}",
                                 channel=channel)
        outcome = "pinged"
    if tracked and task_id:
        store.query(
            f"UPDATE {PINGS_TABLE} SET ts = :ts, thread_ts = :root "
            "WHERE task_id = :t RETURNING task_id",
            {"ts": ts, "root": root, "t": task_id}, write=True)
    return outcome if ts else "post failed"


def _burst_root(owner: str, now: datetime) -> str:
    """The ts of this engineer's last top-level ping, if it is recent."""
    rows = store.query(
        f"SELECT ts FROM {PINGS_TABLE} WHERE engineer = :e "
        "AND COALESCE(thread_ts, '') = '' AND COALESCE(ts, '') <> '' "
        "AND sent_at >= :since ORDER BY sent_at DESC LIMIT 1",
        {"e": owner, "since": now - timedelta(minutes=burst_minutes())})
    return str(rows[0]["ts"]) if rows else ""
