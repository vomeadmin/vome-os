"""
sprint_store.py

The memory behind the daily sprint thread: one numbered item per ticket that
needs Sam, numbers that stay fixed all day, and unanswered items carried into
the next day.

WHY NUMBERS ARE STORED, NOT COMPUTED
------------------------------------
Sam answers "send 1-4" or "hold 6". If the numbers were recomputed from the
queue at each update, a ticket closing at 11:00 would shift every number
below it, and "send 4" at 13:05 would send the wrong reply. So a number is
assigned once, when the item enters the day, and is never reused that day.

An item still open at the end of the day is carried into the next day with a
new number and its original arrival time, so the age marker is honest.

ONE ITEM PER TICKET
-------------------
A ticket is one row. A new reply from the customer reopens it (status back to
open, card and draft replaced) rather than adding a second row, so the thread
never shows the same customer twice. `last_thread_id` is the idempotency key
for shadow triage: the same customer message is never triaged twice, even
when Zoho fires two webhooks for it or Celery redelivers.

Tables are `vomeos_` prefixed, created on first use, same as every OS table.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from vomeos import store

ZONE = ZoneInfo("America/Montreal")

DAYS = "vomeos_sprint_days"
ITEMS = "vomeos_sprint_items"

# Item statuses. `open` and `held` carry forward; the rest are finished.
OPEN = "open"
HELD = "held"
SENT = "sent"
ROUTED = "routed"
DONE = "done"
CARRIES = (OPEN, HELD)

store.register_table(
    DAYS,
    f"""
    CREATE TABLE IF NOT EXISTS {DAYS} (
        day            DATE PRIMARY KEY,
        channel        VARCHAR,
        thread_ts      VARCHAR,
        created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
        last_update_at TIMESTAMP,
        updates        INTEGER NOT NULL DEFAULT 0
    )
    """,
)
store.register_table(
    ITEMS,
    f"""
    CREATE TABLE IF NOT EXISTS {ITEMS} (
        id              SERIAL PRIMARY KEY,
        ticket_id       VARCHAR UNIQUE,
        ticket_number   VARCHAR,
        kind            VARCHAR NOT NULL DEFAULT 'ticket',
        day             DATE NOT NULL,
        number          INTEGER NOT NULL,
        status          VARCHAR NOT NULL DEFAULT 'open',
        category        VARCHAR,
        account         TEXT,
        subject         TEXT,
        summary         TEXT,
        important       BOOLEAN DEFAULT FALSE,
        card            JSONB,
        draft           TEXT,
        draft_status    VARCHAR,
        draft_why       TEXT,
        recipients      JSONB,
        last_thread_id  VARCHAR,
        card_ts         VARCHAR,
        incident_key    VARCHAR,
        decision        TEXT,
        decided_at      TIMESTAMP,
        arrived_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        UNIQUE (day, number)
    )
    """,
    (
        f"CREATE INDEX IF NOT EXISTS {ITEMS}_day_idx ON {ITEMS} (day, number)",
        f"CREATE INDEX IF NOT EXISTS {ITEMS}_status_idx "
        f"ON {ITEMS} (status, updated_at DESC)",
    ),
)


def ready() -> bool:
    return store.ensure_table(DAYS) and store.ensure_table(ITEMS)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today() -> date:
    """The sprint day, in Montreal, where Sam is."""
    return datetime.now(ZONE).date()


# ---------------------------------------------------------------------------
# Days
# ---------------------------------------------------------------------------

def get_day(day: date) -> dict:
    rows = store.query(f"SELECT * FROM {DAYS} WHERE day = :d", {"d": day})
    return rows[0] if rows else {}


def set_day_thread(day: date, channel: str, thread_ts: str) -> bool:
    return store.execute(
        f"INSERT INTO {DAYS} (day, channel, thread_ts) VALUES (:d, :c, :t)"
        " ON CONFLICT (day) DO UPDATE SET channel = EXCLUDED.channel,"
        " thread_ts = EXCLUDED.thread_ts",
        {"d": day, "c": channel, "t": thread_ts},
    )


def mark_update(day: date) -> None:
    store.execute(
        f"UPDATE {DAYS} SET last_update_at = :now, updates = updates + 1"
        " WHERE day = :d",
        {"d": day, "now": _now()},
    )


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _decode(row: dict) -> dict:
    for key in ("card", "recipients"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except ValueError:
                row[key] = {}
        elif value is None:
            row[key] = {}
    return row


def get_item(item_id: int) -> dict:
    rows = store.query(f"SELECT * FROM {ITEMS} WHERE id = :i", {"i": item_id})
    return _decode(rows[0]) if rows else {}


def by_ticket(ticket_id: str) -> dict:
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE ticket_id = :t", {"t": str(ticket_id)}
    )
    return _decode(rows[0]) if rows else {}


def by_number(day: date, number: int) -> dict:
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE day = :d AND number = :n",
        {"d": day, "n": int(number)},
    )
    return _decode(rows[0]) if rows else {}


def items_for_day(day: date) -> list[dict]:
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE day = :d ORDER BY number", {"d": day}
    )
    return [_decode(r) for r in rows]


def recent(hours: int = 24) -> list[dict]:
    """Items that arrived or changed in the last `hours`, newest first."""
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE kind = 'ticket' AND updated_at >= "
        f"NOW() - make_interval(hours => :h) ORDER BY updated_at DESC "
        f"LIMIT 40",
        {"h": int(hours)},
    )
    return [_decode(r) for r in rows]


def _next_number_sql() -> str:
    return (
        f"(SELECT COALESCE(MAX(number), 0) + 1 FROM {ITEMS} WHERE day = :day)"
    )


def upsert_ticket(
    *,
    ticket_id: str,
    ticket_number: str,
    category: str,
    account: str,
    subject: str,
    summary: str,
    important: bool,
    card: dict,
    draft: str,
    draft_status: str,
    draft_why: str,
    recipients: dict,
    last_thread_id: str,
    kind: str = "ticket",
    incident_key: str = "",
) -> dict:
    """Create the ticket's item, or refresh it after a new customer message.

    Refreshing reopens it. It keeps its number when it is already in today's
    thread; an item from an earlier day comes into today with a new number.
    """
    day = today()
    params = {
        "ticket_id": str(ticket_id),
        "ticket_number": str(ticket_number),
        "kind": kind,
        "day": day,
        "category": category,
        "account": account,
        "subject": subject,
        "summary": summary,
        "important": bool(important),
        "card": json.dumps(card or {}, ensure_ascii=False),
        "draft": draft,
        "draft_status": draft_status,
        "draft_why": draft_why,
        "recipients": json.dumps(recipients or {}, ensure_ascii=False),
        "last_thread_id": last_thread_id,
        "incident_key": incident_key or None,
        "now": _now(),
    }
    existing = by_ticket(ticket_id)
    if existing:
        same_day = existing.get("day") == day
        number_sql = "number" if same_day else _next_number_sql()
        rows = store.query(
            f"UPDATE {ITEMS} SET status = 'open', day = :day,"
            f" number = {number_sql}, category = :category,"
            " account = :account, subject = :subject, summary = :summary,"
            " important = :important, card = CAST(:card AS JSONB),"
            " draft = :draft, draft_status = :draft_status,"
            " draft_why = :draft_why,"
            " recipients = CAST(:recipients AS JSONB),"
            " last_thread_id = :last_thread_id,"
            " incident_key = COALESCE(:incident_key, incident_key),"
            " card_ts = CASE WHEN day = :day THEN card_ts END,"
            " decision = NULL, decided_at = NULL, updated_at = :now"
            " WHERE ticket_id = :ticket_id RETURNING *",
            params, write=True,
        )
        return _decode(rows[0]) if rows else {}

    for _ in range(3):
        rows = store.query(
            f"INSERT INTO {ITEMS} (ticket_id, ticket_number, kind, day,"
            " number, category, account, subject, summary, important, card,"
            " draft, draft_status, draft_why, recipients, last_thread_id,"
            " incident_key, arrived_at, updated_at) VALUES ("
            f" :ticket_id, :ticket_number, :kind, :day, {_next_number_sql()},"
            " :category, :account, :subject, :summary, :important,"
            " CAST(:card AS JSONB), :draft, :draft_status, :draft_why,"
            " CAST(:recipients AS JSONB), :last_thread_id, :incident_key,"
            " :now, :now)"
            " ON CONFLICT (ticket_id) DO NOTHING RETURNING *",
            params, write=True,
        )
        if rows:
            return _decode(rows[0])
        again = by_ticket(ticket_id)
        if again:
            return again
    return {}


def set_card_ts(item_id: int, ts: str) -> None:
    store.execute(
        f"UPDATE {ITEMS} SET card_ts = :ts WHERE id = :i",
        {"ts": ts, "i": item_id},
    )


def set_draft(item_id: int, draft: str, status: str, why: str = "") -> None:
    store.execute(
        f"UPDATE {ITEMS} SET draft = :d, draft_status = :s, draft_why = :w,"
        " updated_at = :now WHERE id = :i",
        {"d": draft, "s": status, "w": why, "i": item_id, "now": _now()},
    )


def decide(item_id: int, status: str, decision: str) -> None:
    store.execute(
        f"UPDATE {ITEMS} SET status = :s, decision = :d, decided_at = :now,"
        " updated_at = :now WHERE id = :i",
        {"s": status, "d": decision, "i": item_id, "now": _now()},
    )


def set_incident(item_id: int, key: str) -> None:
    store.execute(
        f"UPDATE {ITEMS} SET incident_key = :k WHERE id = :i",
        {"k": key, "i": item_id},
    )


def carry_forward() -> int:
    """Move every unfinished item from earlier days into today. Returns count.

    Oldest first, so the longest-waiting item gets the lowest new number.
    """
    day = today()
    rows = store.query(
        f"SELECT id FROM {ITEMS} WHERE day < :day AND status IN"
        " ('open', 'held') ORDER BY arrived_at",
        {"day": day},
    )
    moved = 0
    for row in rows:
        updated = store.query(
            f"UPDATE {ITEMS} SET day = :day, number = {_next_number_sql()},"
            " card_ts = NULL WHERE id = :id AND day < :day RETURNING id",
            {"day": day, "id": row["id"]},
            write=True,
        )
        moved += len(updated)
    return moved
