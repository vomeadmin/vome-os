"""
vomeos/worker/app.py

The Celery application. Vome OS's own broker, own queues, own workers.

WHY ITS OWN
-----------
Not django-core's cluster. The OS is an independent system: borrowing another
service's broker would mean its workers restart when that service deploys, its
queue depth is somebody else's capacity problem, and a bad task here degrades
a product backend. It gets its own Redis.

QUEUES
------
One queue per division, plus `default`. Routing is by the division prefix of
the task's subject, so a marketing batch of two thousand drafts cannot starve
the support queue that a customer is waiting on. Divisions are cheap: a queue
is a name, and a worker can serve several.

    celery -A vomeos.worker worker -Q support,default -c 4
    celery -A vomeos.worker worker -Q marketing -c 2
    celery -A vomeos.worker beat

EAGER FALLBACK
--------------
With no broker configured, tasks execute inline in the calling process. That
keeps the OS runnable in development and in tests without Redis, and means
adding the worker layer does not break the current single-dyno deployment on
the day it lands: nothing changes until VOMEOS_BROKER_URL is set.

ACKS LATE, ON PURPOSE
---------------------
`task_acks_late` means a task is acknowledged after it finishes, not when it
is picked up, so a worker killed mid-task (a deploy) returns the job to the
queue instead of losing it. The cost is that a job can be delivered twice,
which is exactly why every scheduled job claims its period first. See
`vomeos/worker/claim.py`.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_init
from kombu import Queue

from vomeos.worker import events as job_events
from vomeos.worker import schedule as job_schedule

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

BROKER_URL = (
    os.environ.get("VOMEOS_BROKER_URL")
    or os.environ.get("VOMEOS_REDIS_URL")
    or os.environ.get("REDIS_URL", "")
)

# Results are off by default. A scheduled job's outcome belongs in
# vomeos_job_runs, which is queryable and survives a Redis flush; a Celery
# result backend would be a second, weaker copy with a TTL.
RESULT_BACKEND = os.environ.get("VOMEOS_RESULT_BACKEND", "")

# Timezone for every cron entry. The company runs on Montreal time and the
# existing schedule is written in it.
TIMEZONE = os.environ.get("VOMEOS_TIMEZONE", "America/Montreal")

EAGER = not bool(BROKER_URL)

DEFAULT_QUEUE = "default"


def _queue_names() -> tuple[str, ...]:
    """Queues to declare.

    Registered jobs and event handlers contribute theirs, and VOMEOS_QUEUES
    can add more so a division's queue exists before its first job is written.
    """
    declared = {
        name.strip()
        for name in os.environ.get("VOMEOS_QUEUES", "").split(",")
        if name.strip()
    }
    return tuple(
        sorted(
            {
                DEFAULT_QUEUE,
                *declared,
                *job_schedule.queues(),
                *job_events.queues(),
            }
        )
    )


app = Celery(
    "vomeos",
    broker=BROKER_URL or None,
    backend=RESULT_BACKEND or None,
)

app.conf.update(
    timezone=TIMEZONE,
    enable_utc=True,
    # Run inline when there is no broker, so the OS works without Redis.
    task_always_eager=EAGER,
    task_eager_propagates=False,
    # Acknowledge after completion so a deploy re-queues rather than drops.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One task at a time per worker process. These are long, IO-bound jobs
    # that call models and third-party APIs; prefetching several just means
    # a killed worker returns a bigger pile.
    worker_prefetch_multiplier=1,
    task_default_queue=DEFAULT_QUEUE,
    task_queues=tuple(Queue(name) for name in _queue_names()),
    # A task that has not finished in an hour is wedged. Individual jobs
    # tighten this via their own time_limit.
    task_time_limit=3600,
    task_soft_time_limit=3300,
    # Keep the broker connection alive across a Redis restart rather than
    # crashing the worker.
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    # Celery imports these at worker startup, which is what registers
    # vomeos.run_job and vomeos.run_agent. Without it a worker starts with an
    # empty task registry and rejects everything beat sends it.
    imports=("vomeos.worker.tasks",),
)


def build_beat_schedule() -> dict:
    """Turn the job registry into Celery's beat schedule.

    The schedule is generated, never hand-written, so registering a job is the
    only thing an author has to do and the two can never disagree.
    """
    entries: dict[str, dict] = {}
    for job in job_schedule.list_jobs():
        minute, hour, day_of_month, month, day_of_week = job.cron.split()
        entries[job.key] = {
            "task": "vomeos.run_job",
            "schedule": crontab(
                minute=minute,
                hour=hour,
                day_of_month=day_of_month,
                month_of_year=month,
                day_of_week=day_of_week,
            ),
            "args": (job.key,),
            "options": {"queue": job.queue, "expires": 3600},
        }
    return entries


def refresh_beat_schedule() -> dict:
    """Load the job modules, then install their schedule. Called by beat."""
    job_schedule.load_job_modules()
    app.conf.beat_schedule = build_beat_schedule()
    app.conf.task_queues = tuple(Queue(name) for name in _queue_names())
    return app.conf.beat_schedule


@worker_init.connect
def _on_worker_start(**_kwargs) -> None:
    """Populate the job registry before the worker takes any task.

    A worker that has not loaded the application's job modules cannot look up
    a job key, so every `vomeos.run_job` would come back "unknown_job".
    """
    job_schedule.load_job_modules()


@beat_init.connect
def _on_beat_start(**_kwargs) -> None:
    """Load the jobs, then generate the schedule from them.

    Beat reads `beat_schedule` once at startup. Building it here rather than
    at import time is what lets the schedule come from the registry, which is
    only populated after the application's modules are imported.
    """
    refresh_beat_schedule()
    entries = len(app.conf.beat_schedule or {})
    print(f"[VOMEOS] beat scheduling {entries} job(s) in {TIMEZONE}")


def describe() -> dict:
    """Runtime shape of the worker layer, for the CLI and a health endpoint."""
    return {
        "broker": "configured" if BROKER_URL else "not set (eager mode)",
        "eager": EAGER,
        "timezone": TIMEZONE,
        "queues": list(_queue_names()),
        "jobs": [job.key for job in job_schedule.list_jobs()],
        "event_handlers": [
            handler.key for handler in job_events.list_event_handlers()
        ],
    }
