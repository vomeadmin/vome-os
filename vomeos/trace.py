"""
vomeos/trace.py

One row per agent run, in the OS's own table.

WHY THIS EXISTS
---------------
Without this there is no token accounting, no cost per workflow, and no record
that a given agent ever made a given decision. Three classifiers were running
in production in the support app making fuzzy judgment calls, and none of them
had ever been scored against a human, because nothing had recorded what they
decided.

`vomeos_agent_runs` is the same table for every agent in every division, so
"which agents cost the most", "which agents fail guards most often" and "what
did we decide about this ticket, and which agent decided it" are one query
each rather than a grep across handlers.

Writing a trace must never break a run. Every function here swallows its own
errors: a database outage degrades observability, it does not stop work.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from vomeos import store

TABLE = "vomeos_agent_runs"

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        run_id          VARCHAR PRIMARY KEY,
        agent           VARCHAR NOT NULL,
        division        VARCHAR NOT NULL,
        tier            VARCHAR NOT NULL,
        model           VARCHAR NOT NULL,
        status          VARCHAR NOT NULL,
        subject_type    VARCHAR,
        subject_id      VARCHAR,
        input_tokens    INTEGER DEFAULT 0,
        output_tokens   INTEGER DEFAULT 0,
        duration_ms     INTEGER DEFAULT 0,
        attempts        INTEGER DEFAULT 1,
        guards          JSONB DEFAULT '[]'::jsonb,
        output          JSONB DEFAULT '{{}}'::jsonb,
        error           TEXT,
        created_at      TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    (
        f"CREATE INDEX IF NOT EXISTS {TABLE}_agent_idx "
        f"ON {TABLE} (agent, created_at DESC)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_subject_idx "
        f"ON {TABLE} (subject_type, subject_id)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_status_idx "
        f"ON {TABLE} (status, created_at DESC)",
    ),
)


def new_run_id() -> str:
    return uuid.uuid4().hex


def record(
    *,
    run_id: str,
    agent: str,
    division: str,
    tier: str,
    model: str,
    status: str,
    subject_type: str = "",
    subject_id: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
    attempts: int = 1,
    guards: list | None = None,
    output: dict | None = None,
    error: str = "",
) -> bool:
    """Persist one run. False when it could not be written."""
    if not store.ensure_table(TABLE):
        return False
    return store.execute(
        f"INSERT INTO {TABLE} ("
        " run_id, agent, division, tier, model, status,"
        " subject_type, subject_id, input_tokens, output_tokens,"
        " duration_ms, attempts, guards, output, error, created_at"
        ") VALUES ("
        " :run_id, :agent, :division, :tier, :model, :status,"
        " :subject_type, :subject_id, :input_tokens, :output_tokens,"
        " :duration_ms, :attempts, CAST(:guards AS JSONB),"
        " CAST(:output AS JSONB), :error, :created_at"
        ") ON CONFLICT (run_id) DO NOTHING",
        {
            "run_id": run_id,
            "agent": agent,
            "division": division,
            "tier": tier,
            "model": model,
            "status": status,
            "subject_type": subject_type or None,
            "subject_id": subject_id or None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "attempts": attempts,
            "guards": json.dumps(guards or []),
            "output": json.dumps(output or {}),
            "error": error or None,
            "created_at": datetime.now(timezone.utc),
        },
    )


def recent(agent: str = "", limit: int = 50) -> list[dict]:
    """Recent runs, newest first. For the CLI and the weekly report."""
    if not store.ensure_table(TABLE):
        return []
    clause = "WHERE agent = :agent " if agent else ""
    return store.query(
        "SELECT run_id, agent, division, tier, model, status,"
        " subject_type, subject_id, input_tokens, output_tokens,"
        " duration_ms, attempts, error, created_at"
        f" FROM {TABLE} {clause}"
        " ORDER BY created_at DESC LIMIT :limit",
        {"agent": agent, "limit": limit},
    )


def summary(days: int = 7) -> list[dict]:
    """Per-agent totals for the last N days: runs, failures, tokens.

    The scoreboard the weekly report reads. Guard blocks are counted apart
    from errors because they mean opposite things: an error is the system
    failing, a block is the system working.
    """
    if not store.ensure_table(TABLE):
        return []
    return store.query(
        "SELECT agent, division, tier,"
        " COUNT(*) AS runs,"
        " SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_runs,"
        " SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END)"
        "   AS blocked_runs,"
        " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_runs,"
        " SUM(input_tokens) AS input_tokens,"
        " SUM(output_tokens) AS output_tokens,"
        " ROUND(AVG(duration_ms)) AS avg_ms"
        f" FROM {TABLE}"
        " WHERE created_at >= NOW() - CAST(:window AS INTERVAL)"
        " GROUP BY agent, division, tier"
        " ORDER BY runs DESC",
        {"window": f"{int(days)} days"},
    )
