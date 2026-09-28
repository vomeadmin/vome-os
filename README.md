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
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

One web process today, on Railway. The kernel runs in-process with the
support application. Worker processes and a broker are the next thing to
build, and they will be Vome OS's own, not borrowed from another service.

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
```

Every table the OS creates is prefixed `vomeos_`, so pointing it at a
database an application already uses is safe. Sharing an instance never means
sharing a schema.
