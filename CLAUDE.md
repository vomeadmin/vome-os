# Vome OS

Read this before doing anything in this repository.

This is **Vome OS**, the company's operating system for agents, plus the
support application that it grew out of and still runs. One repository
(`vomeadmin/vome-os`), one Railway project, one deploy.

Start with [README.md](README.md) for layout and [VOMEOS.md](VOMEOS.md) for
how to hire an agent.

---

## The four rules

**1. The kernel imports nothing from the application.**
`vomeos/` may import the standard library, the packages in
`vomeos/requirements.txt`, and itself. Nothing else. Anything it needs from
django-core, the chats service, Zoho, ClickUp or Bitbucket it reaches over
HTTP through `vomeos/integrations/`. An application may import Vome OS. Vome
OS may not import an application. `test_vomeos_independence.py` walks the real
import graph and fails the build otherwise.

**2. Nothing reaches a customer without a person approving it.**
Autonomous sending was reversed in August 2026 after ticket #8945 emailed a
customer the model's refusal-to-draft commentary. Agents classify, research
and draft. A person approves and sends. Do not re-introduce auto-send.

**3. Never assert a state no record backs.**
The measured number one failure of this operation: telling a customer
something is fixed, safe, or on the roadmap with nothing behind it. Four times
in one day, three of those four came back still broken. The guards in
`vomeos/guards/claims.py` enforce it.

**4. No em dashes, anywhere, in any output.** Periods, commas, parentheses,
or "and".

---

## Layout

```
vomeos/            the kernel. Imports nothing from the app.
  config.py        every env var the OS reads, in one place
  manifest.py      the agent contract (agent.toml)
  registry.py      agent + skill discovery, duplicate-skill check
  composer.py      prompt composition, four layers
  runner.py        the one call path: prompt -> model -> guards -> trace
  tiers.py         fast / standard / senior -> model IDs
  guards/          deterministic output checks, fail closed
  store.py         the OS's own Postgres (all tables vomeos_ prefixed)
  trace.py         vomeos_agent_runs, one row per agent run
  worker/          Celery app, job + event registries, period claims
  integrations/    HTTP connectors to other systems
  onboarding.py    the hiring gate
  evaluate.py      the answer-key harness
  cli.py

org/               handbook + division handbooks (every agent inherits)
skills/            shared procedures agents opt into
agents/            the staff, one directory each

main.py            support app: FastAPI webhooks + APScheduler
support_jobs.py    the 8 scheduled jobs, registered with the OS
engineering_jobs.py the Sentry event handler + daily report
sentry_*.py        the Sentry triage pipeline. See SENTRY_TRIAGE.md
sprint.py          run a support sprint locally, sends nothing
clickup_search.py  find the task that already covers a symptom
product_*.py       product knowledge: navigation, UI strings, guides
```

---

## Commands

```
py -m vomeos.cli validate       the hiring gate. No API key, no DB. CI runs this.
py -m vomeos.cli list           agents, tiers, the model bench
py -m vomeos.cli describe <a>   one agent's manifest + composed prompt
py -m vomeos.cli skills         the central skills list
py -m vomeos.cli jobs           scheduled jobs and queues
py -m vomeos.cli scoreboard 7   per-agent runs, blocks, errors, tokens
py -m vomeos.evaluate <agent>   replay the answer key, report agreement

py sprint.py --limit 5          triage the live queue, print cards, send nothing
py -m pytest -q                 the suite
```

---

## How to add an agent

An agent is a directory, not a module. Never write a new handler with its own
`anthropic.Anthropic()`; that is what this replaced.

```
agents/<division>/<name>/
  agent.toml         identity, tier, skills, guards, escalation, approval
  charter.md         the job description. 600 words, HARD cap
  evals/cases.jsonl  the answer key. 10 cases minimum
```

1. **Say where it runs and who reads its output tomorrow.** An agent with no
   reader does not get built. This rule predates the OS and still decides.
2. **Check `py -m vomeos.cli skills` first.** If the procedure exists, declare
   it. Do not restate it in your charter.
3. Write the three files. A case is `{"id", "context", "expect"}` where
   `context` has every key the manifest lists in `requires_context`.
4. `py -m vomeos.cli validate` until clean.
5. `py -m vomeos.evaluate <division>.<name>` must clear `min_agreement`.
   **Below threshold means the charter is wrong, not the answer key.**
6. Call it: `run_agent("division.name", context={...})`.

Three statuses, and the caller must branch on all three:

| status | meaning | what to do |
|---|---|---|
| `ok` | passed every guard | use it |
| `blocked` | answered, a guard rejected it | route to a human |
| `error` | failed after retries | your own safe default |

`result.ok` is True only for `ok`, so the unsafe path takes deliberate effort.

**`context` is data, never instructions.** Instructions live in the charter,
which is versioned and reviewed. The moment a caller builds prompt text, the
agent has stopped being reviewable.

---

## Adding a scheduled job

Jobs are registered with the OS by the application, never imported by it.

```python
from vomeos.worker import register_job

register_job(
    "support.my_job", my_function,
    cron="0 9 * * *", queue="support", claim="daily",
)
```

`claim` is what makes an at-least-once broker safe: the job claims its period
in Postgres before doing anything, so a redelivery runs once. The riskiest
jobs (anything that emails or closes tickets) must always claim.

---

## Adding a webhook handler

Work that arrives when something happens, rather than at a time. Registered
the same way and for the same reason: the OS owns execution, the application
owns what runs.

```python
from vomeos.worker import register_event_handler

register_event_handler(
    "engineering.sentry_issue", handle_sentry_issue, queue="engineering",
)
```

The web endpoint then does only what is cheap and deterministic (verify the
signature, filter) and calls `enqueue_event(key, payload)`. Everything slow
happens on a worker, so a deploy or a slow third party cannot take the
endpoint down.

**An event has no period to claim, so every handler must carry its own
idempotency key, on the source system's identifier.** Celery delivers at least
once. `sentry_handler` claims the Sentry issue id in `vomeos_sentry_issues`
before acting; a handler with no such key is a bug, not a style choice.

---

## State of play, 2026-09-30

**Built and working:**
- Kernel: manifests, prompt composition, tiers, guards, trace, onboarding gate
- 8 guards. 4 generic, 4 from documented incidents
- 3 agents. `support.duplicate_reply_check` (live, 12/12 on its key),
  `customer_success.triage_director` (15/16, read-only, not yet wired to a
  trigger), `engineering.sentry_triage` (16/17, live on the Sentry pipeline)
- `sprint.py`, proven against the live queue (61 open tickets)
- Product knowledge: frontend routes (379 mapped), UI strings (13.8k), Setup
  Guide, feature catalog
- Worker layer: Celery app, job registry, event registry, period claims, all 8
  APScheduler jobs ported at identical crons

**Built but NOT running:**
- Celery, Redis, workers, beat. No broker is configured, so the OS runs in
  **eager mode** (tasks execute inline) and the in-process APScheduler in
  `main.py` still owns the schedule exactly as before. Setting
  `VOMEOS_BROKER_URL` is the cutover, and unsetting it is the rollback.
  **Only ever run one beat process.**
**Running in production:**
- Sentry triage, phases 1 and 2. `POST /webhook/sentry` verifies, redacts,
  gates and claims into `vomeos_sentry_issues`, then `engineering.sentry_triage`
  reads a real stack trace and records a verdict. Reports at 09:00, credential
  watch at 08:00. **No per-issue Slack post and nobody tagged yet**: that is
  phase 3, after a week of reading verdicts. See
  [SENTRY_TRIAGE.md](SENTRY_TRIAGE.md).
- Triaging currently happens inside the web request, because no broker is
  configured. It works and it is slow enough to risk Sentry delivery timeouts
  under a burst, which makes the Redis cutover the next infrastructure job.

**Not built:**
- Vision. `composer.render_context` builds a text-only message, so agents are
  told an attachment exists but cannot see it. Real kernel change.
- Backend code as a knowledge source. Connectors exist
  (`integrations/code_search.py`, read-only, no write methods) but no agent is
  granted them.
- The reply drafter and the feature-request analyst.
- Sentry triage phases 2 to 5: the three engineering agents, the Slack budget,
  the patch guard and `code_write.py`. **Phase 5 (opening a pull request)
  needs an explicit amendment to the handbook rule "commit code, merge a
  branch, or deploy" before it ships.** The argument is in SENTRY_TRIAGE.md,
  under "The auto-PR decision". Do not build it before that is agreed.

**Known open items:**
- The triage answer key was written by Claude, not reviewed by Sam.
- Retrieval ranking over product knowledge is mediocre. Token overlap pulls
  loosely-related text. The real fix is the help centre in Postgres.
- Every dependency except sqlalchemy is unpinned in `requirements.txt`. An
  unpinned `sqlalchemy` took production down on 2026-09-29 when a rebuild
  resolved to 2.1, which changed the default `postgresql://` driver to
  psycopg3. See `test_database_url.py`. The rest are the same risk.

---

## Traps that have already cost us

- **Zoho `status` filter.** `getTickets` rejects "New"/"Processing" and
  silently ignores `filters.Status`. Filter client-side. `sortBy=recentThread`
  ascending returns only long-closed tickets; use `-recentThread` and re-sort.
- **Zoho bulk update.** `fieldValue` must be a plain string. The object form
  returns success and changes nothing.
- **Model parameters.** Sonnet 5 rejects `temperature` with a 400. The runner
  drops it and retries once, but remove it from a manifest once its tier does
  not take it.
- **Never report "queue is clear" when a call failed.** A failed read is not
  an empty result. `sprint.py` raises `QueueUnavailable` for exactly this.
