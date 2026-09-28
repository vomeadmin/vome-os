"""
vomeos/worker/tasks.py

The two tasks the OS ships. Everything else is data.

WHY ONLY TWO
------------
A Celery task per job would mean the OS importing every application module
that defines one, which is exactly the dependency the OS is not allowed to
have. Instead there is one generic task that takes a job key and looks it up
in the registry, and one that takes an agent address and runs it. Adding a job
or an agent never requires a new task, a new import, or a worker code change.

    vomeos.run_job(job_key)          run a registered scheduled job
    vomeos.run_agent(agent, context) run one agent off the queue

Both are idempotent-by-claim where it matters and both report to the OS's own
tables rather than to a Celery result backend.
"""

from __future__ import annotations

import time

from vomeos.worker import claim as job_claim
from vomeos.worker.app import app
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
