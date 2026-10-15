"""
sentry_morning.py

Run the Sentry pipeline's morning schedule now, in order, and print every
step. For seeing the workflow on demand instead of waiting for 08:00.

Run it INSIDE the worker, so it has the worker's variables and database:

    railway ssh --service vome-os-worker -- python scripts/sentry_morning.py

What it does, in the order the schedule does it:

    0. backfill    copy any verdict the ledger failed to store back from the
                   agent run trace (the 09:00 report does this too)
    1. 08:00       engineering.code_access: are the Bitbucket, GitHub and
                   Sentry credentials alive. Posts to Slack only if not.
    2. 08:30       engineering.sentry_regate: re-decide issues gated by a
                   rule that has since changed. Usually a no-op.
    3. re-analyse  run the senior analyst again on the most frequent
                   actionable prod-vome issues, against the real source.
                   Not part of the daily schedule: issues are analysed once,
                   when they arrive. This exists for issues analysed while
                   repository access was broken. Costs one senior model call
                   per issue, hence the cap.
    4. 09:00       engineering.sentry_report: the daily report. POSTS TO THE
                   SENTRY CHANNEL (#eng-all). Skip with --no-report.

Steps 1, 2 and 4 go through the same `vomeos.run_job` wrapper beat uses,
with force=True so they run even though today's period may be claimed.
Forcing today's run does not affect tomorrow's: each day is its own claim.

Usage:

    python scripts/sentry_morning.py                  # all steps, 5 re-analyses
    python scripts/sentry_morning.py --reanalyse 0    # schedule only
    python scripts/sentry_morning.py --no-report      # do not post to Slack
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


def _run_job(key: str) -> None:
    from vomeos.worker.tasks import run_job

    result = run_job.apply(args=(key, True)).get(propagate=False)
    print(f"{key}: {result}", flush=True)


def _reanalyse(limit: int) -> None:
    import sentry_handler
    import sentry_ledger
    import sentry_projects
    from vomeos import store
    from vomeos.integrations import sentry as sentry_api

    rows = store.query(
        f"SELECT * FROM {sentry_ledger.TABLE}"
        " WHERE project = 'prod-vome' AND verdict = 'actionable'"
        " ORDER BY times_seen DESC NULLS LAST, last_seen_at DESC"
        " LIMIT :limit",
        {"limit": limit},
    )
    if not rows:
        print("no actionable prod-vome issues in the ledger", flush=True)
        return

    for row in rows:
        project = sentry_projects.route(row["project"])
        if project is None:
            print(f"{row['issue_id']}: project not routed, skipped")
            continue
        signal = sentry_api.IssueSignal(
            issue_id=str(row["issue_id"]),
            project=row.get("project") or "",
            title=row.get("title") or "",
            culprit=row.get("culprit") or "",
            level=row.get("level") or "",
            environment=row.get("environment") or "",
            platform=row.get("platform") or "",
            exception_type=row.get("exception_type") or "",
            times_seen=int(row.get("times_seen") or 0),
            users_affected=int(row.get("users_affected") or 0),
            permalink=row.get("permalink") or "",
            reason="manual",
        )
        message = {
            "repo": project.repo,
            "repo_owner": project.owner,
            "repo_host": project.host,
            "repo_ref": project.ref,
            "stack": project.stack,
        }
        triage = {
            "verdict": row.get("verdict") or "",
            "severity": row.get("severity") or "",
            "summary": row.get("triage_summary") or row.get("title") or "",
            "infrastructure": bool(row.get("infrastructure")),
        }

        print(
            f"\n--- {signal.issue_id} ({triage['severity']}, seen "
            f"{signal.times_seen}x) {triage['summary'][:120]}",
            flush=True,
        )
        outcome = sentry_handler._investigate(signal, message, triage)
        after = sentry_ledger.get(signal.issue_id) or {}
        print(f"risk: {outcome.get('risk')}  slack: {outcome.get('slack')}")
        print(f"fix: {outcome.get('fix')}")
        print(f"cause: {(after.get('root_cause') or '')[:400]}")
        print(f"proposed: {(after.get('proposed_fix') or '')[:400]}",
              flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reanalyse", type=int, default=5,
                        help="actionable issues to re-analyse (0 to skip)")
    parser.add_argument("--no-report", action="store_true",
                        help="do not post the daily report to Slack")
    args = parser.parse_args()

    # Same registrations the worker loads, so every job key resolves.
    import engineering_jobs  # noqa: F401
    import sentry_ledger

    _step("0. backfill verdicts from the run trace")
    print(sentry_ledger.backfill_from_runs(), flush=True)

    _step("1. 08:00 credential check")
    _run_job("engineering.code_access")

    _step("2. 08:30 re-gate sweep")
    _run_job("engineering.sentry_regate")

    if args.reanalyse > 0:
        _step(f"3. re-analyse up to {args.reanalyse} actionable issues")
        _reanalyse(args.reanalyse)
    else:
        _step("3. re-analyse skipped")

    if args.no_report:
        _step("4. 09:00 report skipped (--no-report)")
    else:
        _step("4. 09:00 daily report (posts to the Sentry channel)")
        _run_job("engineering.sentry_report")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
