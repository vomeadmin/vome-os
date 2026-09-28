# Vome OS

The company's operating system for agents.

One repository, one deploy. It holds the kernel that runs every agent, the
handbooks they all inherit, the shared skills they opt into, and the agents
themselves. It also still holds the support application that this system grew
out of, which is now its first and largest client.

Start with **[VOMEOS.md](VOMEOS.md)**: what an agent is, how to hire one, and
the rules the kernel enforces.

---

## Layout

```
vomeos/         the kernel. Imports nothing from any application.
org/            handbook and division handbooks. Every agent inherits these.
skills/         shared procedures agents opt into.
agents/         the staff. One directory per agent.

main.py         the support application: FastAPI webhooks and the scheduler
ops/            the ticket command center API
*_handler.py    support workflows (ClickUp status, Calendly, on-prod, ...)
support_jobs.py the support division's scheduled jobs, registered with the OS
guides/         how the support automation works and why
```

The top four are Vome OS. The rest is the support application, which is a
client of it. That direction is enforced, not just intended: see below.

## The one rule that governs everything

**The kernel talks to every other system over the network. It never imports
one.**

Not django-core, not the chats service, not the integrations service, and not
the support application in this same repository. Anything the OS needs from
another system it asks for over HTTP through `vomeos/integrations/`.

The only permitted direction of dependency is inward. An application may
import Vome OS. Vome OS may not import an application.

`test_vomeos_independence.py` walks the kernel's real import graph on every
test run and fails on anything outside the standard library, the packages in
`vomeos/requirements.txt`, and `vomeos` itself. That is what makes the rule a
rule rather than an intention, and what keeps the option of splitting the OS
into its own repository open at no ongoing cost.

## Commands

```
py -m vomeos.cli validate        the onboarding gate. No API key, no database.
py -m vomeos.cli list            every agent, division and model tier
py -m vomeos.cli describe <a>    one agent's manifest and composed prompt
py -m vomeos.cli skills          the central skills list
py -m vomeos.cli scoreboard 7    per-agent runs, blocks, errors, tokens
py -m vomeos.evaluate <agent>    replay the answer key, report agreement

py -m pytest test_vomeos.py test_vomeos_independence.py -q
```

`validate` exits non-zero on anything blocking. Make it a required check
before merge: it is the difference between hiring standards and good
intentions.

## Deploying

```
web:    uvicorn main:app --host 0.0.0.0 --port $PORT
worker: celery -A vomeos.worker worker -Q support,default --concurrency 4
beat:   celery -A vomeos.worker beat
```

Vome OS's own broker and workers, not another service's cluster.

### The cutover is one environment variable

Exactly one thing may own the schedule, or every job runs twice.

**Today, with no `VOMEOS_BROKER_URL` set:** nothing has changed. The
in-process APScheduler in `main.py` starts as it always has, and the worker
layer runs inline. Landing this code changes no behaviour.

**Once `VOMEOS_BROKER_URL` is set:** `main.py` does not start APScheduler,
and Celery beat owns the schedule instead. Rollback is unsetting the same
variable.

To cut over, **in this order**:

1. Add a Redis service to the Railway project. Do not set anything on `web`
   yet.
2. Create a `worker` service from this repo. Give it every variable the `web`
   service has (it runs the actual jobs, so it needs Anthropic, Zoho,
   ClickUp, Slack and the database), plus `VOMEOS_BROKER_URL` and
   `VOMEOS_JOB_MODULES=support_jobs`.
3. Create a `beat` service the same way. Confirm its log says
   `beat scheduling 8 job(s)`.
4. Only now set `VOMEOS_BROKER_URL` on `web`. That is the moment APScheduler
   stands down and beat takes over.

Rollback at any point is removing `VOMEOS_BROKER_URL` from `web`.

**Only ever run one beat service, at one replica.** Two beats means two of
every job, the same bug as two web dynos today. Workers scale freely; beat
does not.

### Why `web` ignores a bare `REDIS_URL`

The worker's broker lookup accepts `VOMEOS_BROKER_URL`, `VOMEOS_REDIS_URL` or
a plain `REDIS_URL`. The scheduler handoff in `main.py` deliberately does
**not** read the last one.

Railway injects `REDIS_URL` into any service that references a Redis add-on.
If `main.py` treated that as "beat owns the schedule now", then simply adding
Redis to the project would stop all eight scheduled jobs in the web process,
before any worker existed to take over, and nothing would report it. Handing
over the schedule has to be an explicit act, so it takes an explicit
`VOMEOS_` variable that no platform sets on its own.

`test_vomeos_worker.py` pins both halves of that asymmetry.

### Why this exists

APScheduler runs in the web process. That means a second web dyno is a second
scheduler, so the daily digest goes out twice and the stale sweep closes
tickets twice. It is a hard ceiling on the web tier and it is invisible until
you scale. A deploy also kills whatever was mid-flight, which is why there is
a commit here titled "Allow a forced knowledge refresh after a deploy kills
one".

Celery is at-least-once, so every scheduled job claims its period in Postgres
(`vomeos_job_runs`) before doing anything. Delivered twice, runs once. That is
the same pattern `database.claim_sweeper_run` already used, lifted into the OS
so every job gets it for free.

## Environment

The OS reads only `VOMEOS_*` variables, all of them resolved in
`vomeos/config.py`. Sensible defaults mean none are required to start:

```
VOMEOS_HOME                where org/, skills/ and agents/ live
VOMEOS_DATABASE_URL        the OS's own database (falls back to DATABASE_URL)
VOMEOS_MODEL_FAST          the fast bench
VOMEOS_MODEL_STANDARD      the standard bench
VOMEOS_MODEL_SENIOR        the senior bench
VOMEOS_CHARTER_WORD_CAP    how long a job description may be
VOMEOS_MIN_EVAL_CASES      how big an answer key must be

VOMEOS_BROKER_URL          the Redis the workers use. Unset means eager mode
                           and APScheduler keeps the schedule.
VOMEOS_JOB_MODULES         comma separated modules that call register_job
VOMEOS_QUEUES              extra queues to declare before their first job
VOMEOS_TIMEZONE            cron timezone (default America/Montreal)
```

Every table the OS creates is prefixed `vomeos_`, so pointing it at a
database an application already uses is safe. Sharing an instance never means
sharing a schema.
