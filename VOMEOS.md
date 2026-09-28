# Vome OS

The company's operating system for agents.

It is its own system. It runs the agents that do our work, across every
division. It is not a feature of the support app, and it is not part of
django-core. Other repositories may be integrated with it. It depends on none
of them.

Written 2026-09-28. Cap: 1400 words.

---

## Layout

```
vomeos/            the kernel
  config.py        every environment value the OS reads
  manifest.py      the agent contract
  registry.py      discovery of agents and skills, the duplicate check
  composer.py      prompt composition
  tiers.py         tier name to model ID
  runner.py        the one call path
  guards/          deterministic output checks
  store.py         the OS's own database connection
  trace.py         vomeos_agent_runs
  onboarding.py    the hiring standard
  evaluate.py      the answer key harness
  integrations/    how the OS reaches other systems
  cli.py
org/               handbook and division handbooks
skills/            shared procedures agents opt into
agents/            the staff, one directory each
```

`org/`, `skills/` and `agents/` are found through `VOMEOS_HOME`, which
defaults to the directory holding `vomeos/`. That is what lets the OS be
lifted into its own repository without touching a line of code.

## The independence rule

**The OS talks to every other system over the network. It never imports one.**

No Django settings, no shared ORM models, no reading another repository's
database. If the OS needs a fact from django-core, it asks django-core's API.
The only permitted direction of dependency is inward: an application may
import Vome OS, Vome OS may not import an application.

This is enforced, not encouraged. `test_vomeos_independence.py` walks the
kernel's actual import graph on every test run and fails on anything outside
the standard library, the four packages in `vomeos/requirements.txt`, and
`vomeos` itself.

Three reasons, in order of how much they hurt:

1. **The OS has to be deployable on its own.** One import means it cannot
   start without that repository installed at a compatible version.
2. **Schema coupling is invisible until it breaks.** A model field renamed in
   django-core is a normal refactor there and an outage here, and no test in
   either repository would catch it.
3. **The blast radius has to stay small.** Agents are probabilistic. Giving
   probabilistic code direct ORM access to production is not a risk we take.
   Through an API, every write the OS can perform is one somebody exposed
   deliberately.

Integrations live in `vomeos/integrations/`. A connector is a small class with
an explicit method per operation, not a generic HTTP client an agent can point
anywhere. The operations are the contract, and adding one is reviewed.

## What an agent is

A directory, not a module.

```
agents/<division>/<name>/
  agent.toml        identity, tier, skills, guards, escalation, approval
  charter.md        the job description. 600 words, hard cap
  evals/cases.jsonl the answer key. 10 cases minimum
```

Everything else is shared. The kernel composes the prompt, picks the model,
retries, runs the guards, writes the trace. An author decides what the job is.
They do not decide how it runs, and they cannot skip a step by forgetting to
call something.

## What every agent inherits

Four layers, outermost first, and the outer ones win:

1. `org/handbook.md` who we are and the five rules. Every division.
2. `org/divisions/<division>.md` what this division does and does not do.
3. `skills/<name>.md` shared procedures the manifest opted into.
4. `charter.md` this agent's own job.

A charter that contradicts the handbook loses. That is why the order exists.

## Hiring one

1. **Say where it runs and who reads the output tomorrow.** An agent with no
   reader does not get built. This rule predates the OS and still decides
   everything.
2. **Check the skills registry first.** `py -m vomeos.cli skills`. If the
   procedure exists, declare it. Do not restate it in your charter.
3. Write `agent.toml`, `charter.md`, and at least 10 answer-key cases. A case
   is `{"id", "context", "expect"}`, where `context` carries every key the
   manifest lists in `requires_context`.
4. `py -m vomeos.cli validate`. Fix everything blocking.
5. `py -m vomeos.evaluate <division>.<name>`. It must clear the agent's
   `min_agreement`. Below that, the charter is wrong, not the answer key.
6. Call it. `run_agent("support.name", context={...})`.

## Calling one

```python
from vomeos import run_agent

result = run_agent(
    "support.duplicate_reply_check",
    context={"draft": draft, "thread": conversations_text},
    subject_type="zoho_ticket",
    subject_id=ticket_id,
)
if result.ok:
    verdict = result.get("duplicate")
else:
    verdict = SAFE_DEFAULT
```

`context` is **data**. Instructions live in the charter, which is versioned
and reviewed. The moment a caller starts building prompt text, the agent has
stopped being reviewable.

| status    | meaning                           | what to do            |
|-----------|-----------------------------------|-----------------------|
| `ok`      | passed every guard                | use it                |
| `blocked` | answered, a guard rejected it     | route to a human      |
| `error`   | failed after retries              | your own safe default |

`result.ok` is True only for `ok`, so the unsafe path takes deliberate effort.

## Model tiers

A manifest names a tier, never a model: `fast`, `standard`, `senior`, defined
in `vomeos/config.py` and overridable per environment. Model IDs go stale, a
capability level does not, and upgrading the whole bench is one edit.

A manifest may set `temperature`. Newer models reject it; the runner drops the
parameter and retries once rather than failing, so no list of which model
takes which knob has to be maintained. That retry costs a call, so remove the
field once the tier's model does not accept it.

## Guards

Declared in the manifest, run on every result, deterministic, fail closed.
`json_shape`, `client_message`, `non_empty`, `max_length`.

`client_message` exists because ticket #8945 was emailed a model's
refusal-to-draft commentary on 2026-09-07. The onboarding gate refuses to let
a `client_facing = true` agent ship without it.

No guard may call a model. A guard runs on the output of a model that has
already misbehaved, so grading it with a second model adds a second thing that
can fail open.

## The onboarding gate

`py -m vomeos.cli validate` is the hiring standard as code. It blocks on: a
division with no handbook, a missing or over-cap charter, a skill not in
`skills/`, an unknown tier or guard, `client_facing` without `client_message`,
JSON output without `json_shape`, and an answer key that is missing, thin, or
missing required context.

It needs no API key and no database, runs in under a second, and exits
non-zero. Make it a required check before merge.

The skill-similarity warning in the same pass is the chief-of-staff check.
Deliberately boring (word overlap, no model, no embeddings) because a check
that is slow or costs money gets switched off. It is a prompt to look, not a
verdict.

## The scoreboard

Every run writes to `vomeos_agent_runs`: agent, tier, model, status, guard
results, tokens, duration, subject.

```
py -m vomeos.cli scoreboard 7      per-agent runs, blocks, errors, tokens
py -m vomeos.cli runs <agent>      recent individual runs
```

`blocked` and `error` are counted separately. An error is the system failing.
A block is the system working.

The OS keeps its own state. `VOMEOS_DATABASE_URL` points at its database and
every table it creates is prefixed `vomeos_`, so sharing an instance with an
application never means sharing a schema. Losing the database degrades
observability, it does not stop agents.

## Continuous learning

`py -m vomeos.evaluate <agent>` replays the answer key and reports agreement.
That number is the only honest answer to "is this agent getting better", and a
prompt edit has to clear it before it ships.

When an agent gets a call wrong in production, the fix is a new answer-key
case first, then a charter change. A charter edit with no case behind it is a
guess, and the next person cannot tell whether it helped.

## Size discipline

Charters: 600 words, enforced. This document: 1400. When a procedure appears
in two charters it belongs in `skills/`. When a skill is used by no agent,
delete it.
