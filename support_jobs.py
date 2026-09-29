"""
support_jobs.py

The support division's recurring jobs, registered with Vome OS.

This is the application side of the worker layer. The OS owns scheduling,
queueing, claiming and reporting; this file owns what runs and when. The
dependency points inward: this imports `vomeos`, `vomeos` never imports this.

A worker or beat process loads it through VOMEOS_JOB_MODULES:

    VOMEOS_JOB_MODULES=support_jobs

Every job here previously lived as an `APScheduler.add_job` call in
`main.py`. The crons, hours and env overrides are carried across unchanged, so
moving a job to the queue does not also change when it runs. The one thing
that does change is safety: each job now claims its period in Postgres before
doing anything, so two workers, a redelivery after a deploy, or a manual
trigger on top of the cron cannot run it twice.

CRON NOTES
----------
Celery's crontab ANDs its fields, the same way APScheduler's CronTrigger does.
That matters for the help centre gap pass, where `day="1-7"` plus
`day_of_week="tue"` is how you spell "the first Tuesday of the month". Written
as a five-field cron that is `0 9 1-7 * tue`.

Times are America/Montreal, set once on the Celery app.
"""

from __future__ import annotations

import os

from vomeos.worker import (
    CLAIM_DAILY,
    CLAIM_MONTHLY,
    CLAIM_NONE,
    CLAIM_WEEKLY,
    register_job,
)

QUEUE = "support"


def _hour(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _run_weekly_knowledge_refresh():
    """Imported lazily so a heavy analysis module is not pulled in at boot."""
    from ticket_analyzer import run_weekly_knowledge_refresh

    return run_weekly_knowledge_refresh()


def register_all() -> None:
    """Register every support job. Idempotent, safe to call more than once."""

    # Daily digest, 17:00. The oldest job here and the one people actually
    # read every day.
    from slack_digest import send_daily_digest

    register_job(
        "support.daily_digest",
        send_daily_digest,
        cron="0 17 * * *",
        queue=QUEUE,
        claim=CLAIM_DAILY,
        time_limit=600,
        description="Daily ticket digest to Slack",
    )

    # Help centre index sync, 02:00. Feeds article search for everything else,
    # so it runs before the passes that read it.
    from kb_sync import run_kb_sync

    register_job(
        "support.kb_sync",
        run_kb_sync,
        cron="0 2 * * *",
        queue=QUEUE,
        claim=CLAIM_DAILY,
        time_limit=1800,
        description="Sync help centre articles into Postgres",
    )

    # Stale awaiting-client sweep, 07:30. Runs LIVE: it closes tickets on both
    # Zoho and ClickUp and posts one Slack report. The claim matters most here
    # of anywhere, because a double run closes tickets twice.
    from stale_waiting_client_sweeper import run_stale_waiting_client_sweep

    register_job(
        "support.stale_sweep",
        run_stale_waiting_client_sweep,
        cron="30 7 * * *",
        queue=QUEUE,
        claim=CLAIM_DAILY,
        time_limit=1800,
        description="Close tickets abandoned in awaiting-client",
    )

    # Help centre health scan, Monday 09:00.
    from kb_search import run_kb_health_scan

    register_job(
        "support.kb_health_scan",
        run_kb_health_scan,
        cron="0 9 * * mon",
        queue=QUEUE,
        claim=CLAIM_WEEKLY,
        time_limit=1800,
        description="Weekly help centre health scan",
    )

    # Engineering reports. Friday closes the week, Monday opens it. Hours stay
    # env-overridable because the right time depends on where the engineers
    # are: 17:00 ET is the middle of the night in Asia.
    #
    # Friday runs at 16:45 rather than 17:00 to stay clear of the daily
    # digest, which already occupies 17:00 every day.
    from weekly_engineering_report import run_friday_report, run_monday_report

    register_job(
        "support.eng_report_friday",
        run_friday_report,
        cron=(
            f"{_hour('ENG_REPORT_FRIDAY_MINUTE', '45')} "
            f"{_hour('ENG_REPORT_FRIDAY_HOUR', '16')} * * fri"
        ),
        queue=QUEUE,
        claim=CLAIM_WEEKLY,
        time_limit=1200,
        description="Friday engineering report: what shipped this week",
    )
    register_job(
        "support.eng_report_monday",
        run_monday_report,
        cron=(
            f"{_hour('ENG_REPORT_MONDAY_MINUTE', '0')} "
            f"{_hour('ENG_REPORT_MONDAY_HOUR', '7')} * * mon"
        ),
        queue=QUEUE,
        claim=CLAIM_WEEKLY,
        time_limit=1200,
        description="Monday engineering report: what is on the plate",
    )

    # Weekly self-learning pass, Sunday 03:00. Analyses tickets and ClickUp
    # tasks closed since the last run and regenerates the knowledge book.
    # After the 02:00 KB sync and well clear of the Monday reports. Capped per
    # pass, so the historical backlog is worked down over several weeks.
    register_job(
        "support.knowledge_refresh",
        _run_weekly_knowledge_refresh,
        cron=(
            f"{_hour('KNOWLEDGE_REFRESH_MINUTE', '0')} "
            f"{_hour('KNOWLEDGE_REFRESH_HOUR', '3')} * * sun"
        ),
        queue=QUEUE,
        claim=CLAIM_WEEKLY,
        time_limit=3600,
        description="Weekly self-learning pass over closed tickets and tasks",
    )

    # Help centre gap pass, first Tuesday of the month at 09:00.
    #
    # Monthly rather than weekly on purpose. The output is "write these five
    # articles", which is a month of someone's slack time, and filing the same
    # five topics every week trains everyone to ignore the report.
    #
    # Tuesday rather than Monday to stay clear of the health scan (Mon 09:00)
    # and the Monday report (Mon 07:00), both of which already post to Slack.
    from kb_gap import run_monthly_kb_gap_pass

    register_job(
        "support.kb_gap_pass",
        run_monthly_kb_gap_pass,
        cron=(
            f"{_hour('KB_GAP_MINUTE', '0')} "
            f"{_hour('KB_GAP_HOUR', '9')} 1-7 * tue"
        ),
        queue=QUEUE,
        claim=CLAIM_MONTHLY,
        time_limit=3600,
        description="Monthly help centre gap clustering",
    )

    # ClickUp webhook health, every hour on the half hour.
    #
    # Hourly, and the only sub-daily job here, because a suspended webhook is
    # completely silent: no failed request, no timeout, nothing in the logs,
    # and the board still looks normal. Every hour it stays suspended is
    # another set of ON PROD tasks whose client emails are never sent. A daily
    # check would have let the 2026-09-29 outage run most of a day.
    #
    # CLAIM_NONE on purpose. The claim table is keyed by period and has no
    # hourly granularity, and this job does not need one: reactivating an
    # already-active webhook is a no-op and Slack is only touched when
    # something is actually wrong, so a double run costs one extra API call.
    from clickup_webhook_monitor import check_clickup_webhook_health

    register_job(
        "support.clickup_webhook_health",
        check_clickup_webhook_health,
        cron="30 * * * *",
        queue=QUEUE,
        claim=CLAIM_NONE,
        time_limit=120,
        description="Watch and auto-recover the ClickUp status webhook",
    )


# Importing this module registers the jobs, which is what
# VOMEOS_JOB_MODULES=support_jobs relies on.
register_all()
