"""
vomeos/worker/tasks.py

The three tasks the OS ships. Everything else is data.

WHY ONLY THREE
--------------
A Celery task per job would mean the OS importing every application module
that defines one, which is exactly the dependency the OS is not allowed to
have. Instead there is one generic task per shape of work, each of which
looks its target up in a registry the application populated. Adding a job, an
agent or a webhook handler never requires a new task, a new import, or a
worker code change.

    vomeos.run_job(job_key)            run a registered scheduled job
    vomeos.run_agent(agent, context)   run one agent off the queue
    vomeos.run_event(key, payload)     handle one event from another system

The three shapes are "at a time", "ask a model", and "something happened".
All three report to the OS's own tables rather than to a Celery result
backend.
"""

from __future__ import annotations

import time

from vomeos.worker import claim as job_claim
from vomeos.worker.app import app
from vomeos.worker.events import EventError, get_event_handler
from vomeos.worker.schedule import JobError, get_job


@app.task(
    name="vomeos.run_job",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def run_job(self, job_key: str, force: bool = False) -> dict:
    """Run one registered scheduled job.

    Claims the period first, so a redelivery, a second beat, or a manual
    trigger on top of the cron cannot run the work twice. `force=True`
    releases the claim first, which is the supported way to re-run a job that
    a deploy killed mid-flight.
    """
    try:
        job = get_job(job_key)
    except JobError as exc:
        # An unknown key is a configuration problem, not a transient one.
        # Retrying it just fills the log.
        print(f"[VOMEOS] {exc}")
        return {"job": job_key, "status": "unknown_job", "error": str(exc)}

    claim_key = job.claim_key()
    if force and claim_key:
        job_claim.release(claim_key)

    if not job_claim.claim(claim_key, job.key, job.queue):
        return {"job": job.key, "status": "skipped", "reason": "already ran"}

    started = time.monotonic()
    try:
        result = job.fn()
    except Exception as exc:
        duration = int((time.monotonic() - started) * 1000)
        error = f"{type(exc).__name__}: {exc}"
        job_claim.finish(
            claim_key, status="error", duration_ms=duration, error=error
        )
        print(f"[VOMEOS] {job.key} failed after {duration}ms: {error}")
        # Release the claim so a retry can actually run. Without this the
        # retry would claim-skip and the failure would be permanent.
        if claim_key and self.request.retries < self.max_retries:
            job_claim.release(claim_key)
            raise self.retry(exc=exc)
        return {"job": job.key, "status": "error", "error": error}

    duration = int((time.monotonic() - started) * 1000)
    job_claim.finish(claim_key, status="ok", duration_ms=duration,
                     result=result)
    print(f"[VOMEOS] {job.key} finished in {duration}ms")
    return {"job": job.key, "status": "ok", "duration_ms": duration}


@app.task(
    name="vomeos.run_agent",
    bind=True,
    max_retries=1,
    default_retry_delay=30,
)
def run_agent_task(
    self,
    agent: str,
    context: dict,
    subject_type: str = "",
    subject_id: str = "",
) -> dict:
    """Run one agent off the queue.

    For work that should not block a webhook: a long thread to review, a batch
    of drafts. The agent's own trace row is written by the runner, so this
    returns only what a caller needs to branch on.

    No claim. An agent run is a read-and-decide; the thing that must not
    happen twice is the ACTION a caller takes on the verdict, and that is the
    caller's to guard.
    """
    from vomeos import run_agent

    result = run_agent(
        agent,
        context=context,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    if result.status == "error" and self.request.retries < self.max_retries:
        raise self.retry(exc=RuntimeError(result.error))
    return {
        "agent": result.agent,
        "run_id": result.run_id,
        "status": result.status,
        "data": result.data,
        "text": result.text if result.data is None else "",
        "why": result.why(),
    }


@app.task(
    name="vomeos.run_event",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def run_event(self, handler_key: str, payload: dict) -> dict:
    """Handle one event from another system, off the queue.

    For a webhook. The web process verifies the signature, does whatever
    filtering is free, and hands the payload here so a deploy, a slow third
    party or a model call cannot take the endpoint down with it.

    No claim: an event has no period to claim, and the OS cannot know what
    the source system considers the same event twice. Idempotency is the
    handler's own, keyed on the source's identifier, and every handler
    registered is required to have one. See `vomeos/worker/events.py`.
    """
    try:
        handler = get_event_handler(handler_key)
    except EventError as exc:
        # An unknown key is a configuration problem, not a transient one.
        print(f"[VOMEOS] {exc}")
        return {
            "handler": handler_key,
            "status": "unknown_handler",
            "error": str(exc),
        }

    started = time.monotonic()
    try:
        result = handler.fn(payload)
    except Exception as exc:
        duration = int((time.monotonic() - started) * 1000)
        error = f"{type(exc).__name__}: {exc}"
        print(f"[VOMEOS] {handler.key} failed after {duration}ms: {error}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"handler": handler.key, "status": "error", "error": error}

    duration = int((time.monotonic() - started) * 1000)
    print(f"[VOMEOS] {handler.key} finished in {duration}ms")
    return {
        "handler": handler.key,
        "status": "ok",
        "duration_ms": duration,
        "result": result if isinstance(result, dict) else {},
    }


def enqueue_job(job_key: str, force: bool = False):
    """Queue a scheduled job now, in addition to its cron.

    In eager mode this runs inline and returns the result, which is what makes
    the OS usable before Redis exists.
    """
    job = get_job(job_key)
    return run_job.apply_async(
        args=(job_key, force), queue=job.queue
    )


def enqueue_agent(
    agent: str,
    context: dict,
    *,
    subject_type: str = "",
    subject_id: str = "",
    queue: str = "",
):
    """Queue an agent run. Routes to the agent's division queue by default."""
    from vomeos.registry import get_agent

    target = queue or get_agent(agent).identity.division
    return run_agent_task.apply_async(
        args=(agent, context, subject_type, subject_id), queue=target
    )


def enqueue_event(handler_key: str, payload: dict, *, queue: str = ""):
    """Queue an event for its registered handler.

    Routes to the handler's own queue by default, so a flood from one source
    system cannot starve another division's work.

    In eager mode this runs inline, which is what keeps a webhook working
    before Redis exists. That is also why the caller must do its filtering
    before calling this: in eager mode, whatever is enqueued runs inside the
    web request.
    """
    target = queue or get_event_handler(handler_key).queue
    return run_event.apply_async(args=(handler_key, payload), queue=target)
