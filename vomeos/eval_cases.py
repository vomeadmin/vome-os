"""
vomeos/eval_cases.py

Answer-key cases recorded from real tickets, where a person decided the
right answer after the agent got it wrong.

WHY THIS EXISTS
---------------
The triage answer key was written by an agent and never reviewed (gap G15:
14/17, under the bar, the same with or without new skills). Meanwhile Sam
was re-triaging tickets in Claude Code and disagreeing with the cards, and
those disagreements, the best signal in the system, were not kept anywhere
the evals could score (2026-10-23). Each one is now a case: the ticket's
real context and the category, lane and verdict Sam settled on. The deep
pass is judged on exactly the tickets where the first pass failed him.

They carry customer threads, so they live here and not in the
`evals/cases.jsonl` files in git. `vomeos.evaluate` reads both.

Recording one from a ticket needs the support app (Zoho, the sprint
store), so that CLI lives on the application side: `triage_cases.py`.
"""

from __future__ import annotations

import json

from vomeos import store

TABLE = "vomeos_eval_cases"

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id          SERIAL PRIMARY KEY,
        agent       VARCHAR NOT NULL,
        case_id     VARCHAR NOT NULL,
        ticket      VARCHAR,
        body        JSONB NOT NULL,
        source      VARCHAR,
        created_at  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)


def for_agent(agent: str) -> list[dict]:
    """Recorded cases for one agent. Empty when there is no database."""
    if not store.usable() or not store.ensure_table(TABLE):
        return []
    rows = store.query(
        f"SELECT body FROM {TABLE} WHERE agent = :a ORDER BY id", {"a": agent})
    out = []
    for row in rows:
        body = row.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                continue
        if isinstance(body, dict):
            out.append(body)
    return out


def add(agent: str, case: dict, ticket: str = "",
        source: str = "claude_code") -> int | None:
    if not store.ensure_table(TABLE):
        return None
    rows = store.query(
        f"INSERT INTO {TABLE} (agent, case_id, ticket, body, source)"
        " VALUES (:a, :c, :t, CAST(:b AS JSONB), :s) RETURNING id",
        {"a": agent, "c": case.get("id") or "", "t": ticket,
         "b": json.dumps(case, ensure_ascii=False), "s": source},
        write=True,
    )
    return int(rows[0]["id"]) if rows else None
