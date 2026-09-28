"""
vomeos/worker/schedule.py

The job registry. What runs, when, and on whose queue.

WHY THIS EXISTS
---------------
Today every recurring job is an `APScheduler.add_job` call inside `main.py`,
in the web process. Three problems with that, in order of severity:

1. **It double-fires the moment there is a second dyno.** APScheduler runs
   in-process, so two web processes means two schedulers means the daily
   digest goes out twice and the stale sweep closes tickets twice. That is a
   hard ceiling on scaling the web tier, and it is invisible until you scale.

2. **A deploy kills whatever was running.** There is a commit in this
   repository titled "Allow a forced knowledge refresh after a deploy kills
   one". That is the symptom.

3. **The schedule is code.** Changing when the Monday report runs is a code
   change, a review and a deploy.

This makes the schedule *data*, held in one registry, from which the beat
schedule is generated.

WHY A REGISTRY AND NOT IMPORTS
------------------------------
The OS may not import the application (see `vomeos/integrations/__init__.py`).
So the OS cannot reach into `main.py` and enumerate jobs. Instead the
application registers itself:

    from vomeos.worker import register_job

    register_job(
        "support.daily_digest",
        send_daily_digest,
        cron="0 17 * * *",
        queue="support",
        claim="daily",
    )

The OS holds the registry and owns execution. The application owns what the
jobs do. The dependency points inward, as it must.

Worker and beat processes populate the registry by importing the modules named
in `VOMEOS_JOB_MODULES`, so which application is being served is configuration
rather than a hardcoded import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

# Claim granularities. A job that claims "daily" can run at most once per
# calendar day no matter how many workers pick it up, which is what makes an
# at-least-once broker safe for a job that emails people.
CLAIM_NONE = ""
CLAIM_DAILY = "daily"
CLAIM_WEEKLY = "weekly"
CLAIM_MONTHLY = "monthly"

VALID_CLAIMS = (CLAIM_NONE, CLAIM_DAILY, CLAIM_WEEKLY, CLAIM_MONTHLY)


class JobError(ValueError):
    """A job was registered with something the scheduler cannot use."""


@dataclass(frozen=True)
class Job:
    """One recurring unit of work."""

    key: str
    fn: Callable[[], object] = field(compare=False, repr=False)
    cron: str
    queue: str = "default"
    # How the job protects itself against being delivered twice.
    claim: str = CLAIM_DAILY
    # Seconds. A job still running after this is killed, so a wedged job
    # cannot hold a worker slot forever.
    time_limit: int = 1800
    description: str = ""

    def claim_key(self, now: datetime | None = None) -> str:
        """The idempotency key for the current period.

        Same shape as the existing `sweeper_runs.run_key` convention, so the
        concept is already familiar in this codebase.
        """
        moment = now or datetime.now(timezone.utc)
        if self.claim == CLAIM_DAILY:
            return f"{self.key}:{moment:%Y-%m-%d}"
        if self.claim == CLAIM_WEEKLY:
            year, week, _ = moment.isocalendar()
            return f"{self.key}:{year}-W{week:02d}"
        if self.claim == CLAIM_MONTHLY:
            return f"{self.key}:{moment:%Y-%m}"
        return ""


_JOBS: dict[str, Job] = {}


def _validate_cron(expression: str) -> tuple[str, ...]:
    """Five-field cron: minute hour day month day_of_week."""
    fields = tuple(str(expression or "").split())
    if len(fields) != 5:
        raise JobError(
            f"cron {expression!r} must have 5 fields "
            "(minute hour day month day_of_week)"
        )
    return fields


def register_job(
    key: str,
    fn: Callable[[], object],
    *,
    cron: str,
    queue: str = "default",
    claim: str = CLAIM_DAILY,
    time_limit: int = 1800,
    description: str = "",
) -> Job:
    """Register a recurring job. Idempotent for the same key and callable.

    `key` is `<division>.<name>`, matching how agents are addressed, so a
    scoreboard can group jobs and agents the same way.
    """
    if "." not in key:
        raise JobError(
            f"job key {key!r} must be '<division>.<name>' so it can be routed "
            "and reported alongside agents"
        )
    if claim not in VALID_CLAIMS:
        raise JobError(
            f"job {key!r}: claim must be one of {VALID_CLAIMS!r}, "
            f"got {claim!r}"
        )
    if not callable(fn):
        raise JobError(f"job {key!r}: fn is not callable")
    _validate_cron(cron)

    existing = _JOBS.get(key)
    if existing is not None and existing.fn is not fn:
        raise JobError(
            f"job {key!r} is already registered to a different callable; "
            "two jobs cannot share a key"
        )

    job = Job(
        key=key,
        fn=fn,
        cron=cron,
        queue=queue,
        claim=claim,
        time_limit=time_limit,
        description=description,
    )
    _JOBS[key] = job
    return job


def get_job(key: str) -> Job:
    if key not in _JOBS:
        known = ", ".join(sorted(_JOBS)) or "(none)"
        raise JobError(
            f"no job registered as {key!r}. Known jobs: {known}. "
            "Is the registering module listed in VOMEOS_JOB_MODULES?"
        )
    return _JOBS[key]


def list_jobs() -> list[Job]:
    return sorted(_JOBS.values(), key=lambda j: j.key)


def queues() -> list[str]:
    """Every queue the registered jobs use, plus the default."""
    return sorted({"default", *(job.queue for job in _JOBS.values())})


def clear() -> None:
    """Empty the registry. For tests."""
    _JOBS.clear()


def load_job_modules(modules: str = "") -> list[str]:
    """Import the modules that register jobs.

    Called by worker and beat at startup. The list comes from
    VOMEOS_JOB_MODULES (comma separated) so the OS never names an application
    in its own source.

    Import failures are reported and skipped rather than raised: one broken
    application module must not stop every other job in the fleet from
    running.
    """
    raw = modules or os.environ.get("VOMEOS_JOB_MODULES", "")
    names = [name.strip() for name in raw.split(",") if name.strip()]
    loaded: list[str] = []
    for name in names:
        try:
            __import__(name)
            loaded.append(name)
        except Exception as exc:
            print(f"[VOMEOS] could not load job module {name!r}: {exc}")
    if names:
        print(
            f"[VOMEOS] loaded {len(loaded)}/{len(names)} job module(s); "
            f"{len(_JOBS)} job(s) registered"
        )
    return loaded
