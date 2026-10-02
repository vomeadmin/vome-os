"""
engineering_jobs.py

The engineering division's work, registered with Vome OS.

Same shape as `support_jobs.py`: the OS owns scheduling, queueing, claiming
and reporting, and this file owns what runs and when. The dependency points
inward, so this imports `vomeos` and `vomeos` never imports this.

A worker or beat process loads it through VOMEOS_JOB_MODULES:

    VOMEOS_JOB_MODULES=support_jobs,engineering_jobs

Two kinds of registration here, which is new. Support only ever registered
scheduled jobs. Engineering also registers an EVENT HANDLER, because its work
arrives when Sentry says something broke rather than at a time of day. See
`vomeos/worker/events.py`.

The engineering queue is separate from support on purpose. A deploy that
makes ten thousand errors fire at once must not stall the support queue that
a customer is waiting on. That is why queues are per division, and this is the
first case that will actually test it.
"""

from __future__ import annotations

import os

from vomeos.worker import CLAIM_DAILY, register_event_handler, register_job

QUEUE = "engineering"


def _register_sentry() -> None:
    """The Sentry triage pipeline: one event handler and one report."""
    from sentry_handler import (
        HANDLER_KEY,
        handle_sentry_issue,
        run_shadow_report,
    )

    # Event handler. Runs when the webhook queues an issue that survived the
    # gate. Its idempotency is the ledger claim on the Sentry issue id, which
    # is what makes an at-least-once broker safe here.
    register_event_handler(
        HANDLER_KEY,
        handle_sentry_issue,
        queue=QUEUE,
        time_limit=600,
        description="Triage one Sentry issue",
    )

    # Re-decide issues gated for a reason that has since changed, 08:30.
    #
    # Between the credential watch and the report, so a backlog released by
    # a config change is triaged before the report that would mention it.
    #
    # Daily rather than one-off on purpose: it makes "widen the allowlist"
    # behave the way everyone expects every time, instead of only working
    # for issues Sentry has never sent before. Usually a no-op.
    from sentry_handler import run_regate_sweep

    register_job(
        "engineering.sentry_regate",
        run_regate_sweep,
        cron=(
            f"{os.environ.get('SENTRY_REGATE_MINUTE', '30')} "
            f"{os.environ.get('SENTRY_REGATE_HOUR', '8')} * * *"
        ),
        queue=QUEUE,
        claim=CLAIM_DAILY,
        time_limit=900,
        description="Re-decide issues gated by a rule that has changed",
    )

    # The daily report. One message a day and no interrupts.
    #
    # 09:00 rather than with the support digest at 17:00: this is a "what
    # happened overnight" report and it is read at the start of the day. It
    # also stays clear of the Monday reports at 07:00 and the help centre
    # health scan at 09:00 on the support queue, which is a different worker.
    register_job(
        "engineering.sentry_report",
        run_shadow_report,
        cron=(
            f"{os.environ.get('SENTRY_DIGEST_MINUTE', '0')} "
            f"{os.environ.get('SENTRY_DIGEST_HOUR', '9')} * * *"
        ),
        queue=QUEUE,
        claim=CLAIM_DAILY,
        time_limit=300,
        description="Daily Sentry pipeline report",
    )


def _register_code_access() -> None:
    """Watch the credentials the pipeline depends on.

    Daily at 08:00, before the 09:00 Sentry report, so a dead token is known
    about before the report that depends on it goes out.

    Silent when healthy. It posts only when a credential has stopped working
    or is within 30 days of expiring, because every one of these tokens
    expires inside a year and an expired token is completely silent: the
    analyst just reports "could not reach the repository" on every issue.
    Same failure shape as the suspended ClickUp webhook.
    """
    from code_access_monitor import check_code_access

    register_job(
        "engineering.code_access",
        check_code_access,
        cron=(
            f"{os.environ.get('CODE_ACCESS_MINUTE', '0')} "
            f"{os.environ.get('CODE_ACCESS_HOUR', '8')} * * *"
        ),
        queue=QUEUE,
        claim=CLAIM_DAILY,
        time_limit=300,
        description="Watch Bitbucket, GitHub and Sentry credentials",
    )


def register_all() -> None:
    """Register everything engineering owns. Idempotent."""
    _register_sentry()
    _register_code_access()


# Importing this module registers the work, which is what
# VOMEOS_JOB_MODULES=support_jobs,engineering_jobs relies on.
register_all()
