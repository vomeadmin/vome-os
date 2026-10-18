"""
vomeos/lessons.py

What Sam has taught the agents, applied on their very next run.

WHY THIS EXISTS
---------------
Every correction Sam makes is the most valuable signal this system gets: an
edit to a draft, "too long", "never mention the earlier reply", a ticket he
routes somewhere other than where the card said. Before this, a correction
lived in one conversation and was gone by the next ticket, unless a person
later copied it into a charter. The same mistake came back, and Sam made the
same correction again (the "apologise for the earlier reply" pattern was
corrected three times in one day, 2026-10-16 to 17).

A lesson is one rule, the agent it applies to, and the evidence for it (Sam's
own words, or the before and after of his edit). Active lessons are appended
to that agent's system prompt by the composer, so a lesson learned at 10:05
shapes the 10:06 draft.

THE ORDER OF AUTHORITY
----------------------
Lessons refine a charter; they never override the handbook or a charter's
"never bent" rules (no sending without Sam, no engineer names, no fix claim
without a live release). The composer says so in the prompt, and the guards
run on the output regardless of what any lesson says.

THE LIFE OF A LESSON
--------------------
    active    in the prompt from the next run
    promoted  written into the charter, a skill or an answer key; out of the
              live block because the charter now carries it
    retired   Sam said "forget N", or it was wrong

The live block stays short (newest first, capped) because promotion moves
proven lessons into the versioned charter, which is reviewed like code.

SOURCES
-------
    slack_edit      Sam edited a draft in the sprint thread
    slack_feedback  Sam said something general in the thread
    claude_code     corrected in a Claude Code session (recorded with the CLI)
    claude_ai       corrected in a Claude.ai sprint (connector teach_agents,
                    or an edit sent with send_ticket_reply)
    rehearsal       found by a person testing the agents
    manual          added by hand

CLI
---
    py -m vomeos.lessons list [--agent A]
    py -m vomeos.lessons add --agent A --rule "..." --source claude_code
                             [--evidence "..."] [--ticket 9500]
    py -m vomeos.lessons retire ID
    py -m vomeos.lessons promote ID
"""

from __future__ import annotations

import os

from vomeos import store

TABLE = "vomeos_lessons"
ALL_AGENTS = "*"
LIVE_LIMIT = int(os.environ.get("VOMEOS_LESSONS_LIVE_LIMIT", "25"))

ACTIVE = "active"
PROMOTED = "promoted"
RETIRED = "retired"

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id           SERIAL PRIMARY KEY,
        agent        VARCHAR NOT NULL,
        rule         TEXT NOT NULL,
        source       VARCHAR NOT NULL,
        evidence     TEXT,
        ticket       VARCHAR,
        status       VARCHAR NOT NULL DEFAULT 'active',
        created_by   VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
        changed_at   TIMESTAMP,
        note         TEXT
    )
    """,
    (f"CREATE INDEX IF NOT EXISTS {TABLE}_agent_idx "
     f"ON {TABLE} (agent, status, id DESC)",),
)


def _ready() -> bool:
    return store.ensure_table(TABLE)


def add(agent: str, rule: str, source: str, evidence: str = "",
        ticket: str = "", created_by: str = "") -> int | None:
    """Record a lesson, active at once. Returns its id, or None.

    The same rule for the same agent is not stored twice; the existing id
    comes back, so a repeated correction does not crowd the live block.
    """
    rule = (rule or "").strip()
    if not rule or not _ready():
        return None
    existing = store.query(
        f"SELECT id FROM {TABLE} WHERE agent = :a AND status = 'active'"
        " AND lower(rule) = lower(:r) LIMIT 1",
        {"a": agent, "r": rule},
    )
    if existing:
        return int(existing[0]["id"])
    rows = store.query(
        f"INSERT INTO {TABLE} (agent, rule, source, evidence, ticket,"
        " created_by) VALUES (:a, :r, :s, :e, :t, :b) RETURNING id",
        {"a": agent, "r": rule[:600], "s": source,
         "e": (evidence or "")[:4000], "t": ticket or None,
         "b": created_by or None},
        write=True,
    )
    return int(rows[0]["id"]) if rows else None


def active_for(agent: str, limit: int = LIVE_LIMIT) -> list[dict]:
    """Active lessons for one agent, plus those for every agent. Newest first.

    Empty when the database is unavailable: a missing lesson costs a repeat
    correction, a failed prompt costs the whole run.
    """
    if not _ready():
        return []
    return store.query(
        f"SELECT id, agent, rule, source, created_at FROM {TABLE}"
        " WHERE status = 'active' AND agent IN (:a, :all)"
        " ORDER BY id DESC LIMIT :n",
        {"a": agent, "all": ALL_AGENTS, "n": int(limit)},
    )


def all_active() -> list[dict]:
    if not _ready():
        return []
    return store.query(
        f"SELECT id, agent, rule, source, evidence, ticket, created_at"
        f" FROM {TABLE} WHERE status = 'active' ORDER BY id DESC"
    )


def get(lesson_id: int) -> dict:
    if not _ready():
        return {}
    rows = store.query(f"SELECT * FROM {TABLE} WHERE id = :i",
                       {"i": int(lesson_id)})
    return rows[0] if rows else {}


def _set_status(lesson_id: int, status: str, note: str = "") -> bool:
    if not _ready():
        return False
    rows = store.query(
        f"UPDATE {TABLE} SET status = :s, changed_at = NOW(), note = :n"
        " WHERE id = :i RETURNING id",
        {"s": status, "n": note or None, "i": int(lesson_id)},
        write=True,
    )
    return bool(rows)


def retire(lesson_id: int, note: str = "") -> bool:
    return _set_status(lesson_id, RETIRED, note)


def promote(lesson_id: int, note: str = "") -> bool:
    """The charter, a skill or an answer key now carries it."""
    return _set_status(lesson_id, PROMOTED, note)


def render_block(agent: str) -> str:
    """The prompt section for one agent, or "" when it has none."""
    rows = active_for(agent)
    if not rows:
        return ""
    lines = [
        "## Lessons from Sam (live)",
        "",
        "Corrections Sam has made to work like yours, newest first. Apply "
        "them. They refine your instructions above; they never override the "
        "handbook or any rule marked as never bent.",
        "",
    ]
    for r in rows:
        when = str(r.get("created_at") or "")[:10]
        lines.append(f"- [L{r['id']}] {r['rule']} ({r['source']}, {when})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vomeos.lessons")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--agent", default="")
    ad = sub.add_parser("add")
    ad.add_argument("--agent", required=True)
    ad.add_argument("--rule", required=True)
    ad.add_argument("--source", default="claude_code")
    ad.add_argument("--evidence", default="")
    ad.add_argument("--ticket", default="")
    for name in ("retire", "promote"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("--note", default="")
    args = parser.parse_args(argv)

    # From a laptop, `railway run --service Postgres py -m vomeos.lessons ...`
    # provides DATABASE_PUBLIC_URL; the internal URL in .env does not resolve
    # outside Railway. Prefer the public one when it is there.
    public = os.environ.get("DATABASE_PUBLIC_URL", "")
    if public:
        from vomeos import config

        config.DATABASE_URL = public
        store.reset()

    if not _ready():
        print("No database: lessons need VOMEOS_DATABASE_URL / DATABASE_URL.")
        return 1
    if args.cmd == "list":
        rows = (active_for(args.agent, limit=200) if args.agent
                else all_active())
        for r in rows:
            print(f"L{r['id']:<4} {r['agent']:<38} {r['rule']}")
        print(f"{len(rows)} active")
    elif args.cmd == "add":
        lesson_id = add(args.agent, args.rule, args.source, args.evidence,
                        args.ticket, created_by="cli")
        print(f"L{lesson_id} active for {args.agent}")
    elif args.cmd == "retire":
        print("retired" if retire(args.id, args.note) else "not found")
    elif args.cmd == "promote":
        print("promoted" if promote(args.id, args.note) else "not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
