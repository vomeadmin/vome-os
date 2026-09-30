# Redis cutover: moving the schedule onto Vome OS workers

Everything in this document is already built and tested. Nothing here is new
code. This is the operational sequence for turning it on, and the order
matters because getting it wrong silently stops every scheduled job.

Written 2026-09-29.

---

## What you are changing

Today the 9 scheduled jobs run on **APScheduler, inside the web process**.
That works, and it has two problems:

- **A second web dyno means a second scheduler.** Every job fires twice. The
  daily digest goes out twice, the stale sweep closes tickets twice. This is a
  hard ceiling on scaling the web tier and it is invisible until you scale.
- **A deploy kills whatever was mid-flight.** There is a commit in this
  repository titled "Allow a forced knowledge refresh after a deploy kills
  one". That is the symptom.

After the cutover, **Celery beat** owns the schedule and **workers** run the
jobs, with a Postgres claim per period so a job delivered twice runs once.

## The one dangerous fact

`main.py` starts APScheduler **only when `VOMEOS_BROKER_URL` is unset.**

Setting that variable on the **web** service is the cutover. If you set it
before a worker and a beat service are actually running, **all 9 jobs stop and
nothing tells you.** No digest, no KB sync, no stale sweep, no webhook health
check.

So `VOMEOS_BROKER_URL` goes on the web service **last**, and only after you
have watched beat log its schedule.

---

## The sequence

### Step 1: add Redis

Railway project, **New**, **Database**, **Redis**. Nothing else changes yet.
Do not set any variable on the web service.

Open the Redis service's Variables tab and find its **private** URL, usually
`REDIS_PRIVATE_URL`, with a host ending `.railway.internal`. Private is
correct here: all three services are in the same project, so traffic stays
internal and costs no egress.

### Step 2: create the worker service

New service, **same repository**, `main` branch.

Custom start command:

```
celery -A vomeos.worker worker -Q support,default --concurrency 4 --loglevel info
```

Variables: **every variable the web service has**, plus:

```
VOMEOS_BROKER_URL  = ${{Redis.REDIS_PRIVATE_URL}}
VOMEOS_JOB_MODULES = support_jobs
```

The worker runs the actual job bodies, so it needs `ANTHROPIC_API_KEY`, the
Zoho credentials, `CLICKUP_API_TOKEN`, `SLACK_BOT_TOKEN`, `DATABASE_URL` and
the channel IDs. A worker with only the broker connects happily and then fails
every job, which looks like a Celery problem and is not. Railway's shared
variables are the clean way to avoid maintaining three copies.

**Verify before moving on.** The worker log should show it connecting to Redis
and registering `vomeos.run_job` and `vomeos.run_agent`. It should not show
`could not load job module`.

### Step 3: create the beat service

New service, same repository, same branch.

```
celery -A vomeos.worker beat --loglevel info
```

Same variables as the worker.

**Verify before moving on.** The log must contain:

```
[VOMEOS] beat scheduling 9 job(s) in America/Montreal
```

If it says 0 jobs, `VOMEOS_JOB_MODULES` is wrong and beat will schedule
nothing. Fix that before step 4.

**One beat service, one replica, forever.** Two beats is two of every job,
which is the same bug as two web dynos. Workers scale freely. Beat does not.

### Step 4: cut over

Only now, on the **web** service:

```
VOMEOS_BROKER_URL = ${{Redis.REDIS_PRIVATE_URL}}
```

The web service redeploys and its log should say:

```
[MAIN] VOMEOS_BROKER_URL is set: Vome OS beat owns the schedule.
In-process APScheduler not started.
```

**Rollback is deleting that one variable from the web service.** APScheduler
starts again on the next deploy and you are exactly where you were.

### Step 5: watch the first real job

The daily digest at 17:00 America/Montreal is the best canary: it is visible,
it is daily, and you will notice immediately if it fires twice or not at all.

Check afterwards:

```
py -m vomeos.cli jobs          what beat should be scheduling
py -m vomeos.cli scoreboard 7  runs, blocks, errors
```

And in Postgres, `vomeos_job_runs` has one row per claimed period with its
status, duration and result.

---

## What can go wrong, and what it looks like

| Symptom | Cause |
|---|---|
| Beat logs `0 job(s)` | `VOMEOS_JOB_MODULES` not set, or set on the wrong service |
| Worker takes a task and returns `unknown_job` | Same, on the worker |
| Jobs run twice | Two beat services, or two replicas of one |
| Jobs stop entirely, no errors | `VOMEOS_BROKER_URL` set on web before beat was running |
| Worker connects but every job fails | Worker missing the application env vars |
| `Name or service not known` on Redis | Used the public URL from inside Railway, or private from outside |

---

## What this does not change

- **Webhooks stay on the web service.** Zoho, ClickUp, Calendly and Slack all
  keep posting to the same URL. Do not rename the Railway service; those
  webhooks point at its domain.
- **No agent starts sending anything.** The cutover is about *when work runs*,
  not about what it is allowed to do. Nothing reaches a customer without a
  person approving it, before or after.
- **Job times do not move.** All 9 crons were ported unchanged and a test
  asserts it (`test_vomeos_worker.py::test_every_apscheduler_job_was_migrated`).
