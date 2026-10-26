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
assigned once, when the ticket first becomes an item, and it is permanent:
it never changes and is never reused, across days.

Until 2026-10-18 numbers restarted every day and a carried item got a new
one, while its card stayed in yesterday's thread where commands could no
longer reach it (the first weekend proved it). Batches (sprint_batch.py)
made the day irrelevant, so the number stopped depending on it.

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
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from vomeos import store

ZONE = ZoneInfo("America/Montreal")

DAYS = "vomeos_sprint_days"
ITEMS = "vomeos_sprint_items"

# Item statuses. `open`, `held` and `fixing` carry forward; the rest are
# finished.
OPEN = "open"
HELD = "held"
SENT = "sent"
ROUTED = "routed"
DONE = "done"
# The agent opened a fix PR and owns the ticket until the fix is live. The
# item stays out of the queue (one line per sprint) and is not re-triaged;
# it comes back as a resolution when the release watcher proves it live, or
# as a decision if the PR is closed without merging. (#9582, 2026-10-19)
FIXING = "fixing"
CARRIES = (OPEN, HELD, FIXING)

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
        -- What the agent first wrote, kept when Sam edits or redrafts, so
        -- "sent unedited" can be measured per category (autonomy_report).
        original_draft  TEXT,
        edits           INTEGER NOT NULL DEFAULT 0,
        redrafts        INTEGER NOT NULL DEFAULT 0,
        sent_text       TEXT,
        sent_unedited   BOOLEAN,
        sent_at         TIMESTAMP,
        fix_pr          TEXT,
        -- The engineer's Priority Queue task when triage routed the ticket
        -- on its own (ticket_routing): id, url, owner, urgent, how, reason.
        eng_task        JSONB,
        draft_status    VARCHAR,
        draft_why       TEXT,
        recipients      JSONB,
        last_thread_id  VARCHAR,
        card_ts         VARCHAR,
        incident_key    VARCHAR,
        decision        TEXT,
        decided_at      TIMESTAMP,
        wake_at         TIMESTAMP,
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
    for key in ("card", "recipients", "eng_task"):
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


def by_number(number: int) -> dict:
    """The item with this permanent number."""
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE number = :n"
        " ORDER BY updated_at DESC LIMIT 1",
        {"n": int(number)},
    )
    return _decode(rows[0]) if rows else {}


def items_for_day(day: date) -> list[dict]:
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE day = :d ORDER BY number", {"d": day}
    )
    return [_decode(r) for r in rows]


def working_set(hours: int = 36) -> list[dict]:
    """Everything still open or held, plus anything decided recently, so
    "what happened with 5" still has an answer after 5 was sent."""
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE kind = 'ticket' AND (status IN"
        " ('open', 'held', 'fixing') OR updated_at >= NOW() - make_interval("
        "hours => :h)) ORDER BY number",
        {"h": int(hours)},
    )
    return [_decode(r) for r in rows]


def week_counts(days: int = 7) -> dict:
    """Sent and routed in the last `days`, routed split by engineer."""
    rows = store.query(
        f"SELECT status, decision FROM {ITEMS} WHERE decided_at >= NOW()"
        " - make_interval(days => :d) AND status IN ('sent', 'routed')",
        {"d": int(days)},
    )
    out = {"sent": 0, "routed": 0, "by_engineer": {}, "by_reason": {}}
    for row in rows:
        out[row["status"]] += 1
        if row["status"] != ROUTED:
            continue
        # "routed to onlyg (data_lookup)"; older rows have no reason.
        match = re.match(r"routed to (\w+)(?: \((\w+)\))?",
                         str(row.get("decision") or ""))
        if not match:
            continue
        who, why = match.group(1), match.group(2) or "not recorded"
        out["by_engineer"][who] = out["by_engineer"].get(who, 0) + 1
        out["by_reason"][why] = out["by_reason"].get(why, 0) + 1
    return out


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
    """The next permanent number: one above the highest ever used."""
    return f"(SELECT COALESCE(MAX(number), 0) + 1 FROM {ITEMS})"


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

    Refreshing reopens it and it keeps its number. Only a number that
    collides with one already used today (from before numbers were
    permanent) is replaced.
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
        rows = _refresh(params, "number")
        if not rows and existing.get("day") != day:
            rows = _refresh(params, _next_number_sql())
        return _decode(rows[0]) if rows else {}

    for _ in range(3):
        rows = store.query(
            f"INSERT INTO {ITEMS} (ticket_id, ticket_number, kind, day,"
            " number, category, account, subject, summary, important, card,"
            " draft, original_draft, draft_status, draft_why, recipients,"
            " last_thread_id,"
            " incident_key, arrived_at, updated_at) VALUES ("
            f" :ticket_id, :ticket_number, :kind, :day, {_next_number_sql()},"
            " :category, :account, :subject, :summary, :important,"
            " CAST(:card AS JSONB), :draft, :draft, :draft_status,"
            " :draft_why,"
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


def _refresh(params: dict, number_sql: str) -> list[dict]:
    return store.query(
        f"UPDATE {ITEMS} SET status = 'open', day = :day,"
        f" number = {number_sql}, category = :category,"
        " account = :account, subject = :subject, summary = :summary,"
        " important = :important, card = CAST(:card AS JSONB),"
        " draft = :draft, draft_status = :draft_status,"
        " original_draft = :draft, edits = 0, redrafts = 0,"
        " sent_text = NULL, sent_unedited = NULL, sent_at = NULL,"
        " draft_why = :draft_why,"
        " recipients = CAST(:recipients AS JSONB),"
        " last_thread_id = :last_thread_id,"
        " incident_key = COALESCE(:incident_key, incident_key),"
        " card_ts = CASE WHEN day = :day THEN card_ts END,"
        " decision = NULL, decided_at = NULL, wake_at = NULL,"
        " updated_at = :now WHERE ticket_id = :ticket_id RETURNING *",
        params, write=True,
    )


def set_card_ts(item_id: int, ts: str) -> None:
    store.execute(
        f"UPDATE {ITEMS} SET card_ts = :ts WHERE id = :i",
        {"ts": ts, "i": item_id},
    )


EDITS = "vomeos_draft_edits"

store.register_table(
    EDITS,
    f"""
    CREATE TABLE IF NOT EXISTS {EDITS} (
        id             SERIAL PRIMARY KEY,
        item_id        INTEGER,
        ticket_number  VARCHAR,
        category       VARCHAR,
        kind           VARCHAR,
        before_text    TEXT,
        after_text     TEXT,
        created_at     TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)


def set_draft(item_id: int, draft: str, status: str, why: str = "",
              kind: str = "edit") -> None:
    """Replace the draft. `kind` is edit (Sam's words or the partner's
    rewrite on his feedback) or redraft (the drafter again, with his notes).

    The change is logged with what it replaced: every edit Sam makes is a
    correction to the drafter, and the log is where answer-key cases come
    from (autonomy_report --export).
    """
    before = get_item(item_id)
    column = "redrafts" if kind == "redraft" else "edits"
    store.execute(
        f"UPDATE {ITEMS} SET draft = :d, draft_status = :s, draft_why = :w,"
        f" {column} = {column} + 1, updated_at = :now WHERE id = :i",
        {"d": draft, "s": status, "w": why, "i": item_id, "now": _now()},
    )
    if store.ensure_table(EDITS):
        store.execute(
            f"INSERT INTO {EDITS} (item_id, ticket_number, category, kind,"
            " before_text, after_text) VALUES (:i, :t, :c, :k, :b, :a)",
            {"i": item_id, "t": before.get("ticket_number"),
             "c": before.get("category"), "k": kind,
             "b": before.get("draft") or "", "a": draft},
        )


def set_eng_task(item_id: int, task: dict) -> None:
    """Triage put the ticket with an engineer. The item stays open: the
    client has not been told yet, and that is still Sam's to send."""
    store.execute(
        f"UPDATE {ITEMS} SET eng_task = CAST(:t AS JSONB), updated_at = :now"
        " WHERE id = :i",
        {"t": json.dumps(task), "i": item_id, "now": _now()},
    )


def by_ticket_numbers(numbers: list[str]) -> list[dict]:
    if not numbers:
        return []
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE ticket_number = ANY(:n)",
        {"n": [str(n) for n in numbers]},
    )
    return [_decode(r) for r in rows]


def by_incident(key: str) -> list[dict]:
    if not key:
        return []
    rows = store.query(
        f"SELECT * FROM {ITEMS} WHERE incident_key = :k", {"k": key})
    return [_decode(r) for r in rows]


def set_fix_pr(item_id: int, url: str) -> None:
    """The agent opened a fix PR: the item becomes `fixing`.

    `decided_at` marks when the agent took it, so the sprint can say how
    long the fix has been in flight. An item already finished keeps its
    status.
    """
    store.execute(
        f"UPDATE {ITEMS} SET fix_pr = :u, status = CASE WHEN status IN"
        " ('open', 'held') THEN 'fixing' ELSE status END,"
        " decision = CASE WHEN status IN ('open', 'held')"
        " THEN 'agent fix PR open' ELSE decision END,"
        " decided_at = CASE WHEN status IN ('open', 'held') THEN :now"
        " ELSE decided_at END, updated_at = :now WHERE id = :i",
        {"u": url, "i": item_id, "now": _now()},
    )


def keep_fixing(item_id: int) -> None:
    """After the acknowledgment is sent: still the agent's, still in flight.

    Unlike `decide`, leaves `decided_at` (when the agent took it) alone.
    """
    store.execute(
        f"UPDATE {ITEMS} SET status = 'fixing', updated_at = :now"
        " WHERE id = :i",
        {"i": item_id, "now": _now()},
    )


# Decision text on an item whose agent fix PR was closed without merging.
FIX_ABANDONED = "agent fix PR closed without merging"


def fix_progress(pr_url: str, note: str) -> int:
    """Record where an in-flight fix stands ("merged into dev as web@abc").

    Only the decision text changes, so `decided_at` still says when the
    agent took the ticket. Returns how many items were updated.
    """
    rows = store.query(
        f"UPDATE {ITEMS} SET decision = :d WHERE fix_pr = :u"
        " AND status = 'fixing' RETURNING id",
        {"d": note, "u": pr_url}, write=True,
    )
    return len(rows)


def fix_abandoned(pr_url: str) -> list[dict]:
    """The PR was closed unmerged: hand the item back to Sam to decide.

    `fix_pr` is kept so the card can say what happened, and so the queue
    sync does not mistake the ticket's In Progress status for "handled".
    """
    rows = store.query(
        f"UPDATE {ITEMS} SET status = 'open', decision = :d,"
        " decided_at = :now, updated_at = :now WHERE fix_pr = :u"
        " AND status = 'fixing' RETURNING *",
        {"d": FIX_ABANDONED, "u": pr_url, "now": _now()}, write=True,
    )
    return [_decode(r) for r in rows]


def record_sent(item_id: int, text: str, compared: str | None = None) -> None:
    """What actually went to the customer, and whether it was the agent's.

    `compared`: the text to measure against the agent's draft, when the
    sent text differs only by the automatic "Vome team" signature."""
    store.execute(
        f"UPDATE {ITEMS} SET sent_text = :t, sent_at = :now,"
        " sent_unedited = (COALESCE(original_draft, '') = :c)"
        " WHERE id = :i",
        {"t": text, "c": text if compared is None else compared,
         "i": item_id, "now": _now()},
    )


def decide(item_id: int, status: str, decision: str) -> None:
    store.execute(
        f"UPDATE {ITEMS} SET status = :s, decision = :d, decided_at = :now,"
        " updated_at = :now WHERE id = :i",
        {"s": status, "d": decision, "i": item_id, "now": _now()},
    )


def set_received_at(item_id: int, value: str) -> None:
    """When the customer last wrote, for an item stored before it was."""
    store.execute(
        f"UPDATE {ITEMS} SET recipients = COALESCE(recipients,"
        " '{}'::jsonb) || jsonb_build_object('received_at',"
        " CAST(:v AS TEXT)) WHERE id = :i",
        {"v": value, "i": item_id},
    )


def hold(item_id: int, wake_at: datetime, decision: str) -> None:
    """Out of the batches until `wake_at` (naive UTC), then back on top."""
    store.execute(
        f"UPDATE {ITEMS} SET status = 'held', decision = :d, wake_at = :w,"
        " decided_at = :now, updated_at = :now WHERE id = :i",
        {"d": decision, "w": wake_at, "i": item_id, "now": _now()},
    )


def set_incident(item_id: int, key: str) -> None:
    store.execute(
        f"UPDATE {ITEMS} SET incident_key = :k WHERE id = :i",
        {"k": key, "i": item_id},
    )


def carry_forward() -> int:
    """Move every unfinished item from earlier days into today. Returns count.

    Numbers are permanent, so an item keeps its own. Only one that collides
    with a number already used today (from before numbers were permanent)
    gets a new one.
    """
    day = today()
    rows = store.query(
        f"SELECT id FROM {ITEMS} WHERE day < :day AND status IN"
        " ('open', 'held', 'fixing') ORDER BY arrived_at",
        {"day": day},
    )
    moved = 0
    for row in rows:
        for number_sql in ("number", _next_number_sql()):
            updated = store.query(
                f"UPDATE {ITEMS} SET day = :day, number = {number_sql},"
                " card_ts = NULL WHERE id = :id AND day < :day RETURNING id",
                {"day": day, "id": row["id"]},
                write=True,
            )
            if updated:
                moved += 1
                break
    return moved


# ---------------------------------------------------------------------------
# Batches (sprint_batch.py)
# ---------------------------------------------------------------------------

BATCHES = "vomeos_sprint_batches"

store.register_table(
    BATCHES,
    f"""
    CREATE TABLE IF NOT EXISTS {BATCHES} (
        id             SERIAL PRIMARY KEY,
        channel        VARCHAR,
        ts             VARCHAR,
        kind           VARCHAR,
        -- item id -> hash of the draft as shown. `send ready` sends only
        -- what Sam was shown, unchanged.
        shown          JSONB,
        ready          JSONB,
        -- item ids shown under CLOSE QUIETLY: `close quiet` closes only these
        quiet          JSONB,
        posted_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        superseded_at  TIMESTAMP
    )
    """,
    (f"CREATE INDEX IF NOT EXISTS {BATCHES}_ts_idx ON {BATCHES} (ts)",),
)


def batches_ready() -> bool:
    return store.ensure_table(BATCHES)


def _decode_batch(row: dict) -> dict:
    for key in ("shown", "ready", "quiet"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except ValueError:
                row[key] = {}
        elif value is None:
            row[key] = {}
    return row


def start_batch(kind: str) -> int:
    """Reserve the batch number before posting, so the message can say it."""
    rows = store.query(
        f"INSERT INTO {BATCHES} (kind, posted_at) VALUES (:k, :now)"
        " RETURNING id",
        {"k": kind, "now": _now()}, write=True,
    )
    return int(rows[0]["id"]) if rows else 0


def finish_batch(batch_id: int, channel: str, ts: str, shown: dict,
                 ready: dict, quiet: list | None = None) -> None:
    store.execute(
        f"UPDATE {BATCHES} SET channel = :c, ts = :ts,"
        " shown = CAST(:s AS JSONB), ready = CAST(:r AS JSONB),"
        " quiet = CAST(:q AS JSONB), posted_at = :now WHERE id = :i",
        {"c": channel, "ts": ts, "s": json.dumps(shown),
         "r": json.dumps(ready), "q": json.dumps(quiet or []),
         "now": _now(), "i": batch_id},
    )


def latest_batch() -> dict:
    rows = store.query(
        f"SELECT * FROM {BATCHES} WHERE ts IS NOT NULL"
        " ORDER BY id DESC LIMIT 1"
    )
    return _decode_batch(rows[0]) if rows else {}


def batch_for_thread(ts: str) -> dict:
    if not ts:
        return {}
    rows = store.query(f"SELECT * FROM {BATCHES} WHERE ts = :ts",
                       {"ts": ts})
    return _decode_batch(rows[0]) if rows else {}


def mark_superseded(batch_id: int) -> None:
    store.execute(
        f"UPDATE {BATCHES} SET superseded_at = :now WHERE id = :i",
        {"now": _now(), "i": batch_id},
    )
