"""
test_vomeos_worker.py

Tests for the durable execution layer.

The ones that matter are the safety properties, not the happy path:

  * a job delivered twice runs once (the claim);
  * a failed job releases its claim so the retry can actually run;
  * the beat schedule is generated from the registry, so the two cannot
    disagree;
  * the migrated support jobs fire at the same times they fired under
    APScheduler, because a scheduling change smuggled in with an
    infrastructure change is the kind of bug nobody looks for.

Nothing here needs Redis or Postgres. The claim layer is exercised against a
fake so the logic is tested rather than the database.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vomeos.worker import schedule as job_schedule
from vomeos.worker.schedule import (
    CLAIM_DAILY,
    CLAIM_MONTHLY,
    CLAIM_NONE,
    CLAIM_WEEKLY,
    JobError,
    register_job,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test gets an empty registry and leaves one behind."""
    job_schedule.clear()
    yield
    job_schedule.clear()


def _noop():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_and_look_up():
    register_job("support.thing", _noop, cron="0 9 * * *", queue="support")
    job = job_schedule.get_job("support.thing")
    assert job.queue == "support"
    assert job.cron == "0 9 * * *"


def test_key_must_be_division_scoped():
    # Jobs and agents share an addressing scheme so one scoreboard can group
    # both. A bare name breaks that.
    with pytest.raises(JobError, match="division"):
        register_job("thing", _noop, cron="0 9 * * *")


def test_cron_must_have_five_fields():
    with pytest.raises(JobError, match="5 fields"):
        register_job("support.thing", _noop, cron="0 9 * *")


def test_unknown_claim_granularity_is_rejected():
    with pytest.raises(JobError, match="claim must be"):
        register_job(
            "support.thing", _noop, cron="0 9 * * *", claim="hourly"
        )


def test_two_jobs_cannot_share_a_key():
    register_job("support.thing", _noop, cron="0 9 * * *")
    with pytest.raises(JobError, match="already registered"):
        register_job("support.thing", lambda: None, cron="0 9 * * *")


def test_re_registering_the_same_job_is_idempotent():
    # Importing a job module twice must not explode.
    register_job("support.thing", _noop, cron="0 9 * * *")
    register_job("support.thing", _noop, cron="0 9 * * *")
    assert len(job_schedule.list_jobs()) == 1


def test_unknown_job_names_the_env_var_that_probably_caused_it():
    with pytest.raises(JobError, match="VOMEOS_JOB_MODULES"):
        job_schedule.get_job("support.nothing")


def test_queues_always_include_default():
    register_job("support.a", _noop, cron="0 9 * * *", queue="support")
    register_job("sales.b", _noop, cron="0 9 * * *", queue="sales")
    assert job_schedule.queues() == ["default", "sales", "support"]


# ---------------------------------------------------------------------------
# Claim keys: the idempotency window
# ---------------------------------------------------------------------------

def test_daily_claim_key_changes_at_midnight_not_within_the_day():
    job = register_job("support.d", _noop, cron="0 9 * * *", claim=CLAIM_DAILY)
    morning = datetime(2026, 9, 28, 7, 30, tzinfo=timezone.utc)
    evening = datetime(2026, 9, 28, 23, 59, tzinfo=timezone.utc)
    tomorrow = datetime(2026, 9, 29, 0, 1, tzinfo=timezone.utc)
    assert job.claim_key(morning) == job.claim_key(evening)
    assert job.claim_key(morning) != job.claim_key(tomorrow)


def test_weekly_claim_key_holds_across_the_week():
    job = register_job("support.w", _noop, cron="0 9 * * mon",
                       claim=CLAIM_WEEKLY)
    monday = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    next_monday = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    assert job.claim_key(monday) == job.claim_key(friday)
    assert job.claim_key(monday) != job.claim_key(next_monday)


def test_monthly_claim_key_holds_across_the_month():
    job = register_job("support.m", _noop, cron="0 9 1-7 * tue",
                       claim=CLAIM_MONTHLY)
    early = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
    next_month = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    assert job.claim_key(early) == job.claim_key(late)
    assert job.claim_key(early) != job.claim_key(next_month)


def test_claim_none_opts_out():
    job = register_job("support.n", _noop, cron="* * * * *", claim=CLAIM_NONE)
    assert job.claim_key() == ""


# ---------------------------------------------------------------------------
# The task: delivered twice, runs once
# ---------------------------------------------------------------------------

class FakeClaims:
    """In-memory stand-in for the Postgres claim table."""

    def __init__(self):
        self.held: set[str] = set()
        self.finished: list[tuple[str, str]] = []

    def claim(self, claim_key, job_key, queue=""):
        if not claim_key:
            return True
        if claim_key in self.held:
            return False
        self.held.add(claim_key)
        return True

    def finish(self, claim_key, *, status="ok", duration_ms=0,
               result=None, error=""):
        self.finished.append((claim_key, status))
        return True

    def release(self, claim_key):
        self.held.discard(claim_key)
        return True


@pytest.fixture
def fake_claims(monkeypatch):
    from vomeos.worker import tasks

    fake = FakeClaims()
    monkeypatch.setattr(tasks, "job_claim", fake)
    return fake


def test_a_job_delivered_twice_runs_once(fake_claims):
    from vomeos.worker.tasks import run_job

    calls = []
    register_job(
        "support.once", lambda: calls.append(1) or {"n": 1},
        cron="0 9 * * *", claim=CLAIM_DAILY,
    )

    first = run_job.run("support.once")
    second = run_job.run("support.once")

    assert first["status"] == "ok"
    assert second["status"] == "skipped"
    assert len(calls) == 1, "the job body ran twice despite the claim"


def test_force_releases_the_claim_so_a_rerun_actually_runs(fake_claims):
    # The supported answer to "a deploy killed it, run it again", which
    # currently needs a bespoke endpoint per job.
    from vomeos.worker.tasks import run_job

    calls = []
    register_job(
        "support.forced", lambda: calls.append(1),
        cron="0 9 * * *", claim=CLAIM_DAILY,
    )
    run_job.run("support.forced")
    run_job.run("support.forced", force=True)
    assert len(calls) == 2


def test_a_failing_job_releases_its_claim(fake_claims):
    # Without this the retry would claim-skip and the failure would be
    # permanent for the rest of the period.
    from vomeos.worker.tasks import run_job

    def boom():
        raise RuntimeError("nope")

    job = register_job(
        "support.boom", boom, cron="0 9 * * *", claim=CLAIM_DAILY
    )
    with pytest.raises(Exception):
        run_job.run("support.boom")
    assert job.claim_key() not in fake_claims.held


def test_an_unknown_job_reports_rather_than_retrying(fake_claims):
    # A missing key is configuration, not a transient fault. Retrying it just
    # fills the log for an hour.
    from vomeos.worker.tasks import run_job

    result = run_job.run("support.ghost")
    assert result["status"] == "unknown_job"


def test_a_job_failure_is_recorded_before_it_propagates(fake_claims):
    from vomeos.worker.tasks import run_job

    register_job(
        "support.recorded", lambda: (_ for _ in ()).throw(ValueError("x")),
        cron="0 9 * * *", claim=CLAIM_NONE,
    )
    result = run_job.run("support.recorded")
    assert result["status"] == "error"
    assert "ValueError" in result["error"]


# ---------------------------------------------------------------------------
# Beat schedule generation
# ---------------------------------------------------------------------------

def test_beat_schedule_is_generated_from_the_registry():
    from vomeos.worker.app import build_beat_schedule

    register_job("support.a", _noop, cron="30 7 * * *", queue="support")
    register_job("sales.b", _noop, cron="0 9 * * mon", queue="sales")

    entries = build_beat_schedule()
    assert set(entries) == {"support.a", "sales.b"}
    assert entries["support.a"]["task"] == "vomeos.run_job"
    assert entries["support.a"]["args"] == ("support.a",)
    assert entries["support.a"]["options"]["queue"] == "support"
    assert entries["sales.b"]["options"]["queue"] == "sales"


def test_beat_schedule_translates_cron_faithfully():
    from vomeos.worker.app import build_beat_schedule

    register_job("support.first_tue", _noop, cron="0 9 1-7 * tue")
    crontab = build_beat_schedule()["support.first_tue"]["schedule"]
    assert crontab.hour == {9}
    assert crontab.minute == {0}
    assert crontab.day_of_month == {1, 2, 3, 4, 5, 6, 7}
    assert crontab.day_of_week == {2}  # Tuesday


def test_empty_registry_produces_an_empty_schedule():
    from vomeos.worker.app import build_beat_schedule

    assert build_beat_schedule() == {}


# ---------------------------------------------------------------------------
# Migration fidelity: same jobs, same times
# ---------------------------------------------------------------------------

EXPECTED = {
    # key: (cron, queue, claim)
    "support.daily_digest": ("0 17 * * *", "support", CLAIM_DAILY),
    "support.kb_sync": ("0 2 * * *", "support", CLAIM_DAILY),
    "support.stale_sweep": ("30 7 * * *", "support", CLAIM_DAILY),
    "support.kb_health_scan": ("0 9 * * mon", "support", CLAIM_WEEKLY),
    "support.eng_report_friday": ("45 16 * * fri", "support", CLAIM_WEEKLY),
    "support.eng_report_monday": ("0 7 * * mon", "support", CLAIM_WEEKLY),
    "support.knowledge_refresh": ("0 3 * * sun", "support", CLAIM_WEEKLY),
    "support.kb_gap_pass": ("0 9 1-7 * tue", "support", CLAIM_MONTHLY),
}


def test_every_apscheduler_job_was_migrated():
    """All eight, at the same times, on the support queue.

    The times are the ones that were in main.py's APScheduler calls. Moving a
    job onto a queue must not also quietly move when it runs.
    """
    import support_jobs

    job_schedule.clear()
    support_jobs.register_all()

    got = {
        job.key: (job.cron, job.queue, job.claim)
        for job in job_schedule.list_jobs()
    }
    assert got == EXPECTED


def test_the_riskiest_job_claims_daily():
    """The stale sweep closes tickets on Zoho and ClickUp. Twice is bad."""
    import support_jobs

    job_schedule.clear()
    support_jobs.register_all()
    sweep = job_schedule.get_job("support.stale_sweep")
    assert sweep.claim == CLAIM_DAILY
    assert sweep.claim_key(), "the sweep must have a non-empty claim key"


# ---------------------------------------------------------------------------
# The cutover switch
# ---------------------------------------------------------------------------

def _assignment_block(path, name):
    """The full right-hand side of a parenthesised module-level assignment.

    Line based rather than a regex: a non-greedy regex stops at the first
    ')', which here is the end of the first os.environ.get(...) call, and
    silently inspects one line instead of the whole expression.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{name} = ("):
            break
    else:
        raise AssertionError(f"{path.name} no longer defines {name}")
    collected = []
    for line in lines[index + 1:]:
        if line.startswith(")"):
            return "\n".join(collected)
        collected.append(line)
    raise AssertionError(f"{name} assignment in {path.name} is unterminated")


def test_scheduler_handoff_ignores_a_bare_redis_url():
    """Adding Redis to the project must not silently stop the scheduler.

    Railway injects REDIS_URL into every service that references a Redis
    add-on. If main.py treated that as "beat owns the schedule now", then
    merely provisioning Redis would stop all eight scheduled jobs in the web
    process, before any worker or beat existed to take over, and nothing
    would report it.

    The worker's broker lookup MAY fall back to REDIS_URL, because a worker
    without a broker does nothing anyway. The scheduler handoff may not.
    """
    from pathlib import Path as _Path

    block = _assignment_block(
        _Path(__file__).parent / "main.py", "_VOMEOS_BROKER"
    )
    assert "VOMEOS_BROKER_URL" in block
    assert "VOMEOS_REDIS_URL" in block
    assert '"REDIS_URL"' not in block, (
        "main.py reads a bare REDIS_URL to decide scheduler ownership. "
        "Railway sets that automatically, so provisioning Redis would "
        "silently stop every scheduled job."
    )


def test_worker_broker_may_use_a_bare_redis_url():
    """The other half of the asymmetry, so nobody 'fixes' it symmetrically."""
    from pathlib import Path as _Path

    block = _assignment_block(
        _Path(__file__).parent / "vomeos" / "worker" / "app.py", "BROKER_URL"
    )
    assert '"REDIS_URL"' in block
