"""
vomeos/worker/

Durable execution for Vome OS: its own broker, its own queues, its own
workers.

    celery -A vomeos.worker worker -Q support,default -c 4
    celery -A vomeos.worker beat

`-A vomeos.worker` resolves to the `app` exported here. Both entrypoints load
the modules named in VOMEOS_JOB_MODULES at startup, so the OS never names an
application in its own source.

Registering a job, from the application:

    from vomeos.worker import register_job

    register_job(
        "support.daily_digest",
        send_daily_digest,
        cron="0 17 * * *",
        queue="support",
        claim="daily",
    )

With no VOMEOS_BROKER_URL set, everything runs inline in the calling process.
That is what lets this land without changing the behaviour of the current
single-dyno deployment: nothing moves until the broker is configured.
"""

from vomeos.worker.app import (
    app,
    build_beat_schedule,
    describe,
    refresh_beat_schedule,
)
from vomeos.worker.events import (
    EventError,
    EventHandler,
    get_event_handler,
    list_event_handlers,
    register_event_handler,
)
from vomeos.worker.schedule import (
    CLAIM_DAILY,
    CLAIM_MONTHLY,
    CLAIM_NONE,
    CLAIM_WEEKLY,
    Job,
    JobError,
    get_job,
    list_jobs,
    load_job_modules,
    queues,
    register_job,
)

__all__ = [
    "CLAIM_DAILY",
    "CLAIM_MONTHLY",
    "CLAIM_NONE",
    "CLAIM_WEEKLY",
    "EventError",
    "EventHandler",
    "Job",
    "JobError",
    "app",
    "build_beat_schedule",
    "describe",
    "enqueue_agent",
    "enqueue_event",
    "enqueue_job",
    "get_event_handler",
    "get_job",
    "list_event_handlers",
    "list_jobs",
    "load_job_modules",
    "queues",
    "refresh_beat_schedule",
    "register_event_handler",
    "register_job",
]

# Celery's `-A vomeos.worker` looks for a module attribute named `app`,
# `celery` or `celery_app`. Exporting the alias too means either spelling
# works on the command line.
celery_app = app


def __getattr__(name):
    # Deferred so that importing the registry does not pull in the task
    # module, which imports the agent runtime and the Anthropic SDK.
    if name in ("enqueue_job", "enqueue_agent", "enqueue_event"):
        from vomeos.worker import tasks

        return getattr(tasks, name)
    raise AttributeError(f"module 'vomeos.worker' has no attribute {name!r}")


def autodiscover() -> None:
    """Load job modules and install the beat schedule.

    Called by the worker and beat bootstrap. Safe to call more than once.
    """
    refresh_beat_schedule()
