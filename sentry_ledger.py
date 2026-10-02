"""
sentry_ledger.py

One row per Sentry issue, ever. The answer to "have we already dealt with
this".

WHY A LEDGER AND NOT A CACHE
----------------------------
The requirement is that the team is told about an issue once, not once per
occurrence, and not once per worker that happened to pick up a redelivery.
That is an identity problem, and identity needs durable storage keyed on the
thing itself.

Sentry's issue id is that identity. It is stable for the life of a group, it
is what a permalink points at, and it is what a human means when they say
"that bug". So it is the primary key here.

THE CLAIM IS THE WHOLE MECHANISM
--------------------------------
`INSERT ... ON CONFLICT (issue_id) DO NOTHING`, and `rowcount` tells the
caller whether it won. Exactly the pattern `vomeos/worker/claim.py` uses for
scheduled jobs, keyed on an entity instead of a period, and for the same
reason: Celery delivers at least once, so two workers can be handed the same
issue and only one of them may act on it.

Everything the pipeline does that a human would notice (a Slack post, a
ClickUp task, a pull request) happens only on the branch where the claim was
won.

WHY GATED ISSUES ARE RECORDED TOO
---------------------------------
An issue the gate dropped never reaches the queue, but it does get a row, with
`status = 'gated'` and the rule that dropped it. Two reasons:

1. Tuning needs evidence. "Which issues did the culprit rule eat last week"
   has to be answerable, or nobody will ever trust the rule enough to widen
   it, or dare to remove it.
2. A gate that is quietly eating everything looks exactly like a quiet week.
   The daily report reads these rows, so over-eager rules are visible instead
   of invisible.

That is one small upsert in the web request, which is cheap, and it keeps the
model and the queue completely out of the noise path.

DEGRADED BEHAVIOUR
------------------
With no database, `claim()` returns True and the pipeline runs unclaimed, the
same convention as `vomeos/worker/claim.py` and `database.claim_sweeper_run`.
Losing the ledger costs deduplication, which is bad, but it is better than a
webhook that 500s. The health check reports it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from vomeos import store
from vomeos.integrations.sentry import IssueSignal

TABLE = "vomeos_sentry_issues"

# Status values, in the order an issue moves through them. One direction only.
STATUS_GATED = "gated"        # the gate dropped it, nothing else happened
STATUS_SEEN = "seen"          # passed the gate, awaiting triage
STATUS_TRIAGED = "triaged"    # a verdict exists
STATUS_ANALYSED = "analysed"  # root cause exists
STATUS_REPORTED = "reported"  # a human was told
STATUS_PR_OPEN = "pr_open"    # a pull request is waiting for review
STATUS_DISMISSED = "dismissed"

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        issue_id            VARCHAR PRIMARY KEY,
        project             VARCHAR,
        title               TEXT,
        culprit             TEXT,
        level               VARCHAR,
        environment         VARCHAR,
        platform            VARCHAR,
        exception_type      VARCHAR,
        permalink           TEXT,
        reason              VARCHAR,
        status              VARCHAR NOT NULL DEFAULT '{STATUS_SEEN}',
        gate_rule           VARCHAR,
        gate_detail         TEXT,
        first_seen_at       TIMESTAMP NOT NULL DEFAULT NOW(),
        last_seen_at        TIMESTAMP NOT NULL DEFAULT NOW(),
        hits                INTEGER NOT NULL DEFAULT 1,
        times_seen          INTEGER DEFAULT 0,
        users_affected      INTEGER DEFAULT 0,
        verdict             VARCHAR,
        severity            VARCHAR,
        -- The triage agent's one-sentence summary, and whether it judged the
        -- fix to be a setting rather than a line of code. Stored so the daily
        -- report can show the verdict next to the issue that produced it,
        -- which is the only way to tell whether the agent is any good.
        triage_summary      TEXT,
        infrastructure      BOOLEAN,
        -- Repo and stack come from sentry_projects.py, not from a model.
        -- The mapping is a fixed fact, so it is written on the first sighting
        -- and the agents are told it rather than asked for it.
        repo                VARCHAR,
        stack               VARCHAR,
        clickup_task_id     VARCHAR,
        slack_channel       VARCHAR,
        slack_ts            VARCHAR,
        pr_url              TEXT,
        analysed_at         TIMESTAMP,
        analysed_sha        VARCHAR,
        analysed_times_seen INTEGER,
        rearm_count         INTEGER NOT NULL DEFAULT 0,
        rearm_reason        VARCHAR,
        suppressed_until    TIMESTAMP,
        run_ids             JSONB DEFAULT '[]'::jsonb
    )
    """,
    (
        f"CREATE INDEX IF NOT EXISTS {TABLE}_status_idx "
        f"ON {TABLE} (status, last_seen_at DESC)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_gate_idx "
        f"ON {TABLE} (gate_rule, last_seen_at DESC)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_project_idx "
        f"ON {TABLE} (project, last_seen_at DESC)",
    ),
)


@dataclass(frozen=True)
class Claim:
    """The outcome of presenting an issue to the ledger."""

    # True only the first time this issue id has ever been seen. The branch
    # that is allowed to spend money and interrupt people.
    first_seen: bool
    issue_id: str
    status: str = ""
    # False when the ledger could not be reached. `first_seen` is then True
    # (fail open, run unclaimed) and the caller should know the difference.
    recorded: bool = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ready() -> bool:
    return store.ensure_table(TABLE)


def record(
    signal: IssueSignal,
    *,
    status: str = STATUS_SEEN,
    gate_rule: str = "",
    gate_detail: str = "",
    repo: str = "",
    stack: str = "",
) -> Claim:
    """Present an issue to the ledger. One round trip.

    Inserts on first sight and reports `first_seen=True`. On every later
    sighting it bumps the counters and reports `first_seen=False`, which is
    the signal to do nothing.
    """
    if not _ready():
        print(
            f"[SENTRY] no ledger available, {signal.issue_id} runs unclaimed"
        )
        return Claim(True, signal.issue_id, status, recorded=False)

    engine = store.get_engine()
    if engine is None:
        return Claim(True, signal.issue_id, status, recorded=False)

    params = {
        "issue_id": signal.issue_id,
        "project": signal.project or None,
        "title": signal.title or None,
        "culprit": signal.culprit or None,
        "level": signal.level or None,
        "environment": signal.environment or None,
        "platform": signal.platform or None,
        "exception_type": signal.exception_type or None,
        "permalink": signal.permalink or None,
        "reason": signal.reason or None,
        "status": status,
        "gate_rule": gate_rule or None,
        "gate_detail": gate_detail or None,
        "repo": repo or None,
        "stack": stack or None,
        "times_seen": int(signal.times_seen or 0),
        "users_affected": int(signal.users_affected or 0),
        "now": _now(),
    }

    try:
        from sqlalchemy import text as sql_text

        with engine.begin() as conn:
            result = conn.execute(
                sql_text(
                    f"INSERT INTO {TABLE} ("
                    " issue_id, project, title, culprit, level, environment,"
                    " platform, exception_type, permalink, reason, status,"
                    " gate_rule, gate_detail, repo, stack, times_seen,"
                    " users_affected, first_seen_at, last_seen_at, hits"
                    ") VALUES ("
                    " :issue_id, :project, :title, :culprit, :level,"
                    " :environment, :platform, :exception_type, :permalink,"
                    " :reason, :status, :gate_rule, :gate_detail, :repo,"
                    " :stack, :times_seen, :users_affected, :now, :now, 1"
                    ") ON CONFLICT (issue_id) DO NOTHING"
                ),
                params,
            )
            won = result.rowcount > 0

            if not won:
                # Already known. Bump what changes and leave the rest, so a
                # later sighting never overwrites a verdict or a thread id.
                # GREATEST because a per-event payload reports 0 occurrences
                # and must not walk the count backwards.
                conn.execute(
                    sql_text(
                        f"UPDATE {TABLE} SET"
                        " last_seen_at = :now,"
                        " hits = hits + 1,"
                        " times_seen = GREATEST("
                        "   COALESCE(times_seen, 0), :times_seen),"
                        " users_affected = GREATEST("
                        "   COALESCE(users_affected, 0), :users_affected),"
                        " environment = COALESCE("
                        "   NULLIF(environment, ''), :environment)"
                        " WHERE issue_id = :issue_id"
                    ),
                    params,
                )
    except Exception as exc:
        # Never let a ledger failure drop a webhook. Same convention as
        # vomeos.worker.claim.
        print(f"[SENTRY] ledger write failed for {signal.issue_id}: {exc}")
        return Claim(True, signal.issue_id, status, recorded=False)

    return Claim(won, signal.issue_id, status)


def get(issue_id: str) -> dict | None:
    """One issue's row, or None."""
    if not _ready():
        return None
    rows = store.query(
        f"SELECT * FROM {TABLE} WHERE issue_id = :issue_id",
        {"issue_id": issue_id},
    )
    return rows[0] if rows else None


def rearm(issue_id: str, reason: str, *, volume_multiple: int = 10) -> bool:
    """Try to re-open an already-analysed issue for a second look.

    True means this caller won the right to analyse it again. The conditions
    live in the WHERE clause on purpose: deciding in Python and then writing
    would let two workers both decide yes.

    The rules, from SENTRY_TRIAGE.md:
      1. it regressed (the caller asserts this, reason='regression')
      2. volume grew by an order of magnitude since we analysed it
      4. a human asked (reason='manual')

    Rule 3 (the blamed file changed since `analysed_sha`) needs the analyst's
    repo and sha, which arrive in phase 3. The column exists and is written;
    the check is not wired yet.
    """
    if not _ready():
        return True

    engine = store.get_engine()
    if engine is None:
        return True

    if reason == "manual":
        condition = "TRUE"
    elif reason == "regression":
        condition = "status IN ('reported', 'pr_open', 'dismissed')"
    elif reason == "volume":
        condition = (
            "analysed_times_seen IS NOT NULL"
            " AND analysed_times_seen > 0"
            " AND times_seen >= analysed_times_seen * :multiple"
        )
    else:
        return False

    try:
        from sqlalchemy import text as sql_text

        with engine.begin() as conn:
            result = conn.execute(
                sql_text(
                    f"UPDATE {TABLE} SET"
                    " status = :status,"
                    " rearm_count = rearm_count + 1,"
                    " rearm_reason = :reason"
                    " WHERE issue_id = :issue_id"
                    "   AND status <> :status"
                    "   AND (suppressed_until IS NULL"
                    "        OR suppressed_until < NOW())"
                    f"  AND ({condition})"
                ),
                {
                    "issue_id": issue_id,
                    "status": STATUS_SEEN,
                    "reason": reason,
                    "multiple": volume_multiple,
                },
            )
            return result.rowcount > 0
    except Exception as exc:
        print(f"[SENTRY] rearm failed for {issue_id}: {exc}")
        return False


def update(issue_id: str, **fields) -> bool:
    """Write named columns on one row. Used by the later phases.

    Column names are checked against a fixed set rather than interpolated
    from the caller, because this is one import away from a model-supplied
    string reaching an SQL statement.
    """
    writable = {
        "status", "verdict", "severity", "triage_summary", "infrastructure",
        "repo", "clickup_task_id", "slack_channel", "slack_ts", "pr_url",
        "analysed_at", "analysed_sha", "analysed_times_seen",
        "suppressed_until", "run_ids",
    }
    unknown = set(fields) - writable
    if unknown:
        raise ValueError(f"not writable on {TABLE}: {sorted(unknown)}")
    if not fields or not _ready():
        return False

    if "run_ids" in fields and not isinstance(fields["run_ids"], str):
        fields["run_ids"] = json.dumps(fields["run_ids"])

    assignments = ", ".join(f"{name} = :{name}" for name in fields)
    params = dict(fields)
    params["issue_id"] = issue_id
    return store.execute(
        f"UPDATE {TABLE} SET {assignments} WHERE issue_id = :issue_id",
        params,
    )


# ---------------------------------------------------------------------------
# Reporting. What shadow mode exists to produce.
# ---------------------------------------------------------------------------

def summary(days: int = 1) -> dict:
    """Counts for the daily report.

    This is the output of phase 1 and the number every threshold in
    SENTRY_TRIAGE.md is currently a guess about: how many issues a day
    actually survive the gate.
    """
    if not _ready():
        return {"available": False}

    window = f"last_seen_at > NOW() - INTERVAL '{int(days)} days'"

    totals = store.query(
        f"SELECT status, COUNT(*) AS n, SUM(hits) AS hits"
        f" FROM {TABLE} WHERE {window} GROUP BY status"
    )
    by_rule = store.query(
        f"SELECT gate_rule, COUNT(*) AS n, SUM(hits) AS hits"
        f" FROM {TABLE} WHERE {window} AND gate_rule IS NOT NULL"
        " GROUP BY gate_rule ORDER BY n DESC"
    )
    by_project = store.query(
        f"SELECT project, COUNT(*) AS n FROM {TABLE}"
        f" WHERE {window} AND status <> '{STATUS_GATED}'"
        " GROUP BY project ORDER BY n DESC"
    )
    by_repo = store.query(
        f"SELECT COALESCE(repo, '(unmapped)') AS repo, COUNT(*) AS n"
        f" FROM {TABLE} WHERE {window} AND status <> '{STATUS_GATED}'"
        " GROUP BY 1 ORDER BY n DESC"
    )
    # A Sentry project nobody has mapped. Reported by name rather than
    # swallowed, because a new project silently ignored forever is the exact
    # failure this pipeline exists to prevent.
    unmapped = store.query(
        f"SELECT project, COUNT(*) AS n, SUM(hits) AS hits FROM {TABLE}"
        f" WHERE {window} AND gate_rule = 'project_unmapped'"
        " GROUP BY project ORDER BY n DESC"
    )
    new_issues = store.query(
        f"SELECT COUNT(*) AS n FROM {TABLE}"
        f" WHERE first_seen_at > NOW() - INTERVAL '{int(days)} days'"
        f"   AND status <> '{STATUS_GATED}'"
    )
    by_verdict = store.query(
        f"SELECT COALESCE(verdict, '(untriaged)') AS verdict,"
        f" COALESCE(severity, '-') AS severity, COUNT(*) AS n"
        f" FROM {TABLE} WHERE {window} AND status <> '{STATUS_GATED}'"
        " GROUP BY 1, 2 ORDER BY n DESC"
    )
    # The actionable ones, with the agent's own summary, so a human reading
    # the report is grading the verdict and the issue side by side. That
    # comparison is the entire point of the evaluation period.
    actionable = store.query(
        "SELECT issue_id, project, repo, severity, infrastructure,"
        f" triage_summary, title, permalink FROM {TABLE}"
        f" WHERE {window} AND verdict = 'actionable'"
        " ORDER BY CASE severity WHEN 's1' THEN 1 WHEN 's2' THEN 2"
        " ELSE 3 END, last_seen_at DESC LIMIT 25"
    )
    environments = store.query(
        f"SELECT COALESCE(NULLIF(environment, ''), '(unknown)') AS environment,"
        f" COUNT(*) AS n FROM {TABLE} WHERE {window}"
        " GROUP BY 1 ORDER BY n DESC"
    )

    return {
        "available": True,
        "days": days,
        "by_status": {r["status"]: r["n"] for r in totals},
        "hits_by_status": {r["status"]: int(r["hits"] or 0) for r in totals},
        "gate_drops_by_rule": {
            r["gate_rule"]: {"issues": r["n"], "hits": int(r["hits"] or 0)}
            for r in by_rule
        },
        "surviving_by_project": {
            r["project"] or "(none)": r["n"] for r in by_project
        },
        "surviving_by_repo": {r["repo"]: r["n"] for r in by_repo},
        "unmapped_projects": {
            r["project"] or "(none)": {
                "issues": r["n"], "hits": int(r["hits"] or 0)
            }
            for r in unmapped
        },
        "environments": {r["environment"]: r["n"] for r in environments},
        "new_surviving_issues": new_issues[0]["n"] if new_issues else 0,
        "by_verdict": [
            {"verdict": r["verdict"], "severity": r["severity"], "n": r["n"]}
            for r in by_verdict
        ],
        "actionable": [dict(r) for r in actionable],
    }


def recent(limit: int = 30, status: str = "") -> list[dict]:
    """Recent issues, newest activity first."""
    if not _ready():
        return []
    clause = "WHERE status = :status " if status else ""
    return store.query(
        "SELECT issue_id, project, repo, stack, title, level, environment,"
        " status, gate_rule, hits, times_seen, users_affected, permalink,"
        f" first_seen_at, last_seen_at FROM {TABLE} {clause}"
        " ORDER BY last_seen_at DESC LIMIT :limit",
        {"limit": limit, "status": status},
    )
