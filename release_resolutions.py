"""
release_resolutions.py

When a fix is proven live, write the resolution email for every client
waiting on it and put it in front of Sam, ready to send. Optionally, send it.

WHY THIS EXISTS
---------------
The first live release (2026-10-17, #9415 / #9436 / #9500) posted "Fix live,
1 client to notify" and stopped. Sam then had to find each ticket, ask for a
draft, read it, and send it. The watcher had already proved the one fact a
resolution reply needs (the fix is live), so the draft should be waiting for
him, not a to-do.

WHAT HAPPENS, PER CLIENT TICKET OF A LIVE FIX
---------------------------------------------
1. The ticket is triaged again with the release state LIVE in front of the
   triage director and the drafter, so the draft is a resolution, written
   from the customer's thread, through every outbound guard.
2. It lands as a numbered card in the day's #support-sprints thread, with
   its recipients. Sam says "send 3" (or talks it through first).
3. If RELEASE_AUTO_SEND is on, a draft that passed every guard on a ticket
   that does not need a person is sent at once, and the ticket and tasks
   close. Off by default: Sam turns it on when the autonomy report gives him
   the confidence.

Each client ticket is prepared once per fix (`vomeos_release_resolutions`),
so a restart or the next 15-minute pass never drafts or sends twice. A
ticket whose fix went live before this existed is picked up on the first
pass, which is how the first three were handled.
"""

from __future__ import annotations

import os

from vomeos import store

TABLE = "vomeos_release_resolutions"

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        child_id       VARCHAR PRIMARY KEY,
        parent_id      VARCHAR,
        ticket_id      VARCHAR,
        ticket_number  VARCHAR,
        item_number    INTEGER,
        outcome        VARCHAR,
        detail         TEXT,
        created_at     TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)

# Never sent automatically, whatever the switch says: the card itself says a
# person is needed (an earlier wrong reply, an angry important client).
NEVER_AUTO = {"needs_human", "escalated", "feature_request"}


def auto_send_enabled() -> bool:
    value = os.environ.get("RELEASE_AUTO_SEND", "").strip().lower()
    return value in ("true", "1", "on", "yes")


def _claim(child_id: str, parent_id: str, ticket_id: str,
           ticket_number: str) -> bool:
    """True the first time this client ticket is prepared for this fix."""
    if not store.ensure_table(TABLE):
        return False
    rows = store.query(
        f"INSERT INTO {TABLE} (child_id, parent_id, ticket_id, ticket_number,"
        " outcome) VALUES (:c, :p, :t, :n, 'claimed')"
        " ON CONFLICT (child_id) DO NOTHING RETURNING child_id",
        {"c": child_id, "p": parent_id, "t": ticket_id, "n": ticket_number},
        write=True,
    )
    return bool(rows)


def _finish(child_id: str, item_number, outcome: str, detail: str) -> None:
    store.execute(
        f"UPDATE {TABLE} SET item_number = :i, outcome = :o, detail = :d"
        " WHERE child_id = :c",
        {"i": item_number, "o": outcome, "d": detail[:1000], "c": child_id},
    )


def _live_release(parent: dict) -> dict:
    """The release state the watcher just proved, in the agents' words."""
    import release_watcher as rw

    commits = rw._field(parent, rw.FIELD_FIX_COMMIT).get("value") or ""
    return {
        "text": (f"LIVE. Agent fix {parent.get('id')} ({parent.get('name')})"
                 f" is deployed to production ({commits}). A resolution "
                 "reply is backed."),
        "live": True,
        "task_id": parent.get("id") or "",
    }


def prepare(parent: dict, children: list[dict]) -> list[dict]:
    """Draft (and maybe send) the resolution for each new client ticket.

    Returns one entry per client ticket prepared this time:
    {"ticket", "item", "outcome", "detail"}. outcome is one of drafted,
    held (a guard stopped the draft), no_draft, sent, error, no_link.
    Never raises.
    """
    import shadow_triage
    import sprint_store
    from zoho_links import extract_zoho_ticket_id

    entries = []
    for child in children:
        number = ""
        try:
            import release_watcher as rw

            number = rw._ticket_number(child.get("name") or "")
            ticket_id = extract_zoho_ticket_id(child) or ""
            if not ticket_id:
                entries.append({"ticket": number, "item": None,
                                "outcome": "no_link",
                                "detail": "no Zoho link on the task"})
                continue
            if not _claim(child["id"], parent.get("id") or "", ticket_id,
                          number):
                continue

            outcome = shadow_triage._handle(
                ticket_id, "release_live", force=True,
                release=_live_release(parent),
            )
            item = sprint_store.by_ticket(ticket_id)
            if outcome.get("status") != "ok" or not item:
                detail = outcome.get("reason") or outcome.get("error") or ""
                _finish(child["id"], None, "error", detail)
                entries.append({"ticket": number, "item": None,
                                "outcome": "error", "detail": detail})
                continue

            status = item.get("draft_status")
            if status == "ok":
                result, detail = "drafted", ""
                if (auto_send_enabled()
                        and item.get("category") not in NEVER_AUTO):
                    import sprint_thread

                    detail = sprint_thread._send(item)
                    result = "sent" if "sent to" in detail else "drafted"
            elif status == "blocked":
                result, detail = "held", item.get("draft_why") or ""
            else:
                result, detail = "no_draft", status or ""
            _finish(child["id"], item.get("number"), result, detail)
            entries.append({"ticket": number, "item": item.get("number"),
                            "outcome": result, "detail": detail,
                            "to": (item.get("recipients") or {}).get("to")})
        except Exception as exc:
            print(f"[RELEASE] resolution for {child.get('id')} failed: {exc}")
            entries.append({"ticket": number, "item": None,
                            "outcome": "error", "detail": str(exc)})
    return entries


def lines(entries: list[dict]) -> list[str]:
    """How each client stands, for the Fix live post."""
    out = []
    for e in entries:
        ticket = f"#{e.get('ticket')}" if e.get("ticket") else "a ticket"
        item = e.get("item")
        if e["outcome"] == "drafted":
            out.append(f"  - {ticket}: resolution draft ready as *item "
                       f"{item}* above. Say `send {item}`.")
        elif e["outcome"] == "sent":
            out.append(f"  - {ticket}: resolution sent automatically "
                       f"(item {item}). {e.get('detail')}")
        elif e["outcome"] == "held":
            out.append(f"  - {ticket}: draft held by a guard (item {item}): "
                       f"{e.get('detail')}. Edit it in the thread.")
        elif e["outcome"] == "no_draft":
            out.append(f"  - {ticket}: no draft (item {item}). Tell me what "
                       "to say in the thread.")
        elif e["outcome"] == "no_link":
            out.append(f"  - {ticket}: the task has no Zoho link, so I "
                       "cannot draft it.")
        else:
            out.append(f"  - {ticket}: could not draft "
                       f"({e.get('detail') or 'error'}).")
    return out
