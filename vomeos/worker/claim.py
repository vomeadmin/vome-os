"""
vomeos/worker/claim.py

The lock that makes an at-least-once broker safe.

WHY THIS EXISTS
---------------
Celery delivers at least once. A worker that dies mid-task, a broker
reconnect, a redelivery after a visibility timeout: all of them can hand the
same job to a second worker. For a job that reads a queue that is harmless.
For the stale sweep, which closes tickets on both Zoho and ClickUp and posts a
report, it is not.

So every scheduled job claims its period before doing anything. The claim is
an INSERT on a primary key: a duplicate raises a conflict, DO NOTHING swallows
it, and rowcount tells the caller whether it won. Exactly the pattern
`database.claim_sweeper_run` already uses in the support application, lifted
into the OS so every job gets it without each one reimplementing it.

The table also doubles as the job history: when a job started, when it
finished, and what it reported.

DEGRADED BEHAVIOUR
------------------
With no database configured, `claim()` returns True and the job runs
unlocked. That is deliberate and matches the existing convention: a developer
running locally with no Postgres should still be able to run a job. It does
mean the lock is only as good as the database being reachable, which is the
right trade for a single Postgres that the OS already depends on for its
trace.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from vomeos import store

TABLE = "vomeos_job_runs"

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        claim_key   VARCHAR PRIMARY KEY,
        job_key     VARCHAR NOT NULL,
        queue       VARCHAR,
        status      VARCHAR NOT NULL DEFAULT 'running',
        started_at  TIMESTAMP NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMP,
        duration_ms INTEGER,
        result      JSONB DEFAULT '{{}}'::jsonb,
        error       TEXT
    )
    """,
    (
        f"CREATE INDEX IF NOT EXISTS {TABLE}_job_idx "
        f"ON {TABLE} (job_key, started_at DESC)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_status_idx "
        f"ON {TABLE} (status, started_at DESC)",
    ),
)


def claim(claim_key: str, job_key: str, queue: str = "") -> bool:
    """Try to claim this period. True means this caller won and should run.

    An empty claim_key means the job opted out of claiming, so it always
    proceeds.
    """
    if not claim_key:
        return True
    if not store.ensure_table(TABLE):
        # No database. Run unlocked rather than silently skipping work.
        print(f"[VOMEOS] {job_key}: no database, running unclaimed")
        return True

    engine = store.get_engine()
    if engine is None:
        return True
    try:
        from sqlalchemy import text as sql_text

        with engine.begin() as conn:
            result = conn.execute(
                sql_text(
                    f"INSERT INTO {TABLE} "
                    "(claim_key, job_key, queue, status, started_at) "
                    "VALUES (:claim_key, :job_key, :queue, 'running', :now) "
                    "ON CONFLICT (claim_key) DO NOTHING"
                ),
                {
                    "claim_key": claim_key,
                    "job_key": job_key,
                    "queue": queue or None,
                    "now": datetime.now(timezone.utc),
                },
            )
            won = result.rowcount > 0
        if not won:
            print(f"[VOMEOS] {job_key}: {claim_key} already claimed, skipping")
        return won
    except Exception as exc:
        # Never let a lock failure block the work entirely. Matches the
        # existing convention in database.claim_sweeper_run.
        print(f"[VOMEOS] {job_key}: claim failed ({exc}); proceeding")
        return True


def finish(
    claim_key: str,
    *,
    status: str = "ok",
    duration_ms: int = 0,
    result: object = None,
    error: str = "",
) -> bool:
    """Close out a claimed run."""
    if not claim_key or not store.ensure_table(TABLE):
        return False
    payload: dict = {}
    if isinstance(result, dict):
        payload = result
    elif result is not None:
        payload = {"result": str(result)[:2000]}
    return store.execute(
        f"UPDATE {TABLE} SET status = :status, finished_at = :now,"
        " duration_ms = :duration, result = CAST(:result AS JSONB),"
        " error = :error"
        " WHERE claim_key = :claim_key",
        {
            "claim_key": claim_key,
            "status": status,
            "now": datetime.now(timezone.utc),
            "duration": duration_ms,
            "result": json.dumps(payload),
            "error": error or None,
        },
    )


def release(claim_key: str) -> bool:
    """Delete a claim so the job can be run again in the same period.

    For the "a deploy killed it, run it again" case, which currently needs a
    bespoke endpoint per job.
    """
    if not claim_key or not store.ensure_table(TABLE):
        return False
    return store.execute(
        f"DELETE FROM {TABLE} WHERE claim_key = :claim_key",
        {"claim_key": claim_key},
    )


def recent(job_key: str = "", limit: int = 30) -> list[dict]:
    """Recent job runs, newest first."""
    if not store.ensure_table(TABLE):
        return []
    clause = "WHERE job_key = :job_key " if job_key else ""
    return store.query(
        "SELECT claim_key, job_key, queue, status, started_at, finished_at,"
        f" duration_ms, error FROM {TABLE} {clause}"
        " ORDER BY started_at DESC LIMIT :limit",
        {"job_key": job_key, "limit": limit},
    )
