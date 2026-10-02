# The Sentry worker, A to Z

What happens between an error in production and a pull request, every step,
in order. What each switch does. What to check when it goes quiet.

[SENTRY_TRIAGE.md](SENTRY_TRIAGE.md) is the design and the reasoning behind
each decision. This is the operational one: how it runs and how to run it.

Written 2026-10-02.

---

## The one paragraph version

Sentry groups errors into issues. When a brand new issue appears it calls a
webhook. The webhook verifies the signature, strips personal data, and applies
seven free rules that discard most of it. What survives goes on a queue. A
worker claims the issue in Postgres so it can only ever be handled once, fetches
a real stack trace, and asks a cheap model whether a person should care. If the
answer is yes, a senior model reads the actual source at the branch that was
running and says what is wrong. The result goes to Slack: a thread per issue,
only `s1` interrupting anyone, five top-level posts a day maximum. If the fix is
small and certain, a third model writes a diff, a deterministic guard inspects
it, the diff is applied to the real file and verified line by line, and a pull
request opens against a branch nobody deploys from. A human merges it or does
not.

---

## The route

```
  A production error
        |
        v
  Sentry groups it into an issue.  Events -> issues happens here, for free.
        |                           We never subscribe to events.
        v
  issue.created fires ONCE per group, ever.
        |
        v
  POST /webhook/sentry                                      [web process]
        |
        |-- 1. verify the HMAC signature       bad -> 403, and only this 403
        |-- 2. parse, then REDACT              emails, cookies, tokens, device
        |                                      ids, local variables
        |-- 3. normalise                       two Sentry payload shapes -> one
        |-- 4. the gate, 7 rules, no model, no network:
        |        issue_category   performance findings are not bugs
        |        level            below error
        |        project          not triaged / dev / unmapped
        |        environment      second line only
        |        exception_type   scanners, disconnects, retryables
        |        title            connectivity, chunk loading
        |        culprit          extensions, node_modules, Django shell
        |
        |      dropped -> one ledger row, reason recorded, nothing queued
        |
        v
  enqueue_event -> queue: engineering                        [worker]
        |
        v
  claim the issue id in vomeos_sentry_issues
        |   INSERT ... ON CONFLICT DO NOTHING
        |   already there -> stop. This is the anti-bombardment guarantee.
        v
  fetch the latest event, redact it, render the stack trace
        |
        v
  engineering.sentry_triage            fast tier, ~1s
        |   noise | actionable, s1 | s2 | s3, infrastructure true | false
        |
        |-- noise -> recorded, stop. No senior call is ever spent on it.
        v
  actionable
        |
        |-- read the source at the branch that ran      sentry_code
        |-- read recent commits touching it
        |-- ask ClickUp whether a task already covers it
        v
  engineering.bug_analyst              senior tier
        |   root cause, evidence, proposed fix,
        |   risk low|medium|high, confidence, needs_migration, duplicate_of
        v
  Slack                                 sentry_slack
        |   s1  -> post now, tag someone, open a thread
        |   else-> wait for the 09:00 report
        |   cap  -> 5 top-level posts a day, then everything waits
        v
  risk == low AND no migration AND confidence not low?
        |
        |-- no  -> stop. Reported, not fixed. This is most issues.
        v
  engineering.fix_author               senior tier
        |   a unified diff, or an honest refusal
        v
  patch_safety guard                   deterministic, no model
        |   migrations, settings, auth, payments, deps, CI, tests -> refused
        |   more than 3 files or 60 lines -> refused
        v
  apply the diff to the REAL file, verify every context line
        |   does not fit -> refused. The branch moved while we were thinking.
        v
  SENTRY_AUTO_PR_ENABLED == true?
        |
        |-- no  -> the diff goes in the Slack thread. Dry run.
        v
  create branch -> commit -> open pull request
        |   never merge, never force push, never a default branch
        v
  a human reviews it
```

---

## Who does what

| Agent | Tier | Reads | Decides | Score |
|---|---|---|---|---|
| `sentry_triage` | fast | title, exception, stack trace | worth anyone's time | 16/17 |
| `bug_analyst` | senior | the above plus real source, commits, ClickUp | what is wrong, how risky to fix | 12/13 |
| `fix_author` | senior | root cause plus the source again | the diff, or no | 12/12 |

The order is the economics. A fast model discards noise so a senior model
never sees it, and the senior analyst only ever hands work to the fix author
when it has already judged the change small and certain. Each stage is more
expensive than the one before it and sees less.

## Who gets told

One channel, `#eng-all`. Routing is in the mention, not the channel.

| Issue | Tagged |
|---|---|
| backend | OnlyG |
| frontend or mobile | Sanjay |
| infrastructure or config | Siraj and OnlyG, Sam copied |

Infrastructure is decided per issue, not per project: an S3 failure surfaces
in a backend project like any other exception. The triage agent sets the flag
and a deterministic heuristic backs it up.

---

## Every switch, and what happens when you flip it

| Variable | Default | Off means | On means |
|---|---|---|---|
| `SENTRY_PIPELINE_ENABLED` | true | webhook answers 200 and does nothing | normal |
| `SENTRY_PROJECT_ALLOWLIST` | production projects | which Sentry projects enter at all | |
| `SENTRY_SLACK_ENABLED` | **false** | verdicts recorded, nobody told | posts to #eng-all |
| `SENTRY_SLACK_INTERRUPT_AT` | `s1` | which severities may interrupt | |
| `SENTRY_SLACK_MAX_POSTS_PER_DAY` | 5 | hard cap on top-level posts | |
| `SENTRY_AUTO_PR_ENABLED` | **false** | diffs go in Slack only | branches and PRs are created |
| `VOMEOS_CODE_REPOS` | empty = all reachable | which repos may be READ | |
| `VOMEOS_CODE_WRITE_REPOS` | **empty = NONE** | which repos may be WRITTEN | |
| `VOMEOS_PR_ALLOW_PROTECTED_TARGET` | false | a PR into `development` is refused | permitted |

The two allowlists are deliberately opposite. Forgetting the read one costs
some extra reading. Forgetting the write one would make every repository
writable, so empty means nothing.

---

## The scheduled jobs

| When | Job | What |
|---|---|---|
| 08:00 | `engineering.code_access` | are the tokens alive and not expiring |
| 08:30 | `engineering.sentry_regate` | release issues gated by a rule that has since changed |
| 09:00 | `engineering.sentry_report` | the digest: counts, verdicts, every actionable issue with its analysis |

All three are silent or routine. Only the 09:00 report always posts.

The 08:00 credential watch posts **only** when something is broken or within
30 days of expiring, and it tags Sam because the tokens live under his
accounts. Everything here expires inside a year, and an expired token is
completely silent: the analyst simply reports "could not reach the
repository" on every issue.

---

## When it goes quiet

Silence is ambiguous and that is the hardest thing about this system. Work
down this list in order.

**1. Is the pipeline receiving anything?**

```bash
curl -s https://vome-support-agent-production.up.railway.app/health
```

Every key under `sentry` should be `true`. If `SENTRY_WEBHOOK_SECRET` is
false the endpoint is 403ing every delivery and has been the whole time.

**2. Is anything in the ledger?**

```bash
curl -s -H "Authorization: Bearer $OPS_TOKEN" \
  .../sentry/recent | py -m json.tool
```

`by_status` all `gated` with nothing `seen` means the gate is eating
everything. `gate_drops_by_rule` says which rule.

**3. Nothing new is not the same as nothing happening.** `issue.created`
fires once per group, ever. An existing issue recurring does not fire it. A
quiet day is normal; prod-vome produces about three new issues a day.

**4. Did you just widen the allowlist?** Widening affects new issues only.
Run the re-gate sweep to release the backlog:

```bash
curl -s -X POST -H "Authorization: Bearer $OPS_TOKEN" .../sentry/regate
```

**5. Are the credentials alive?**

```bash
curl -s -H "Authorization: Bearer $OPS_TOKEN" .../sentry/credentials
```

**6. What is the configuration actually doing?**

```bash
curl -s -H "Authorization: Bearer $OPS_TOKEN" .../sentry/status
```

Shows the gate, the routing table, the Slack budget and the patch limits as
the running process sees them, not as you believe them to be.

---

## A project that receives nothing

A Sentry project with no events is not an instrumented service. It is an
empty box with a name on it, and it reads as coverage on every dashboard,
which is worse than an obvious gap.

Tell the two apart with **spans**, not errors. A browser or server SDK sends
performance transactions whether or not anything breaks, so:

- zero errors, some spans: the SDK works and nothing has broken. Good.
- zero errors AND zero spans: the SDK is dormant. Nothing is arriving.

```
search_events(projectSlug='prod-vome-web', dataset='spans', period='7d')
```

For a Create React App build like vome-react, the usual cause is the DSN.
`REACT_APP_*` variables are inlined at BUILD time, so setting one on the
hosting platform at runtime leaves `undefined` in the bundle and
`Sentry.init` silently does nothing. The variable has to exist when the
build command runs.

Ten second check from the app itself: open DevTools, Network tab, filter on
`ingest.sentry.io`. No requests means no DSN.

## When it is too loud

In order of how much you want to reach for them:

1. `SENTRY_SLACK_MAX_POSTS_PER_DAY` lower. Blunt and immediate.
2. Add the exception type to `SENTRY_IGNORE_EXCEPTION_TYPES`. No deploy.
3. Narrow `SENTRY_PROJECT_ALLOWLIST`.
4. `SENTRY_SLACK_ENABLED=false`. Verdicts keep being recorded and the daily
   report keeps arriving; only the interrupts stop.
5. `SENTRY_PIPELINE_ENABLED=false`. The endpoint answers 200 and does
   nothing, so Sentry does not retry for a day.

Hitting the daily cap regularly is information, not an inconvenience.
Something upstream is wrong and the cap is what makes that visible rather
than burying it under its own output.

---

## What it can never do

Enforced by the absence of code, not by instruction:

- **Merge, force push, or delete a branch.** No implementation exists in
  `vomeos/integrations/code_write.py`. A test asserts the method surface.
- **Write to `master`, `main`, `development`, `develop`, `staging`** or any
  release branch.
- **Touch anything under `migrations/`.** No override, no configuration.
  Migrations are generated on deploy in these repositories, so a hand-written
  one is an incident rather than a bad patch, and it is exactly the change an
  agent is most likely to think is small and obvious.
- **Touch settings, credentials, authentication, permissions, payments,
  dependency files, CI configuration, or tests.** A fix that edits its own
  tests is a fix that makes itself pass.
- **Change more than 3 files or 60 lines.**
- **Apply a patch that no longer fits.** Every context line is checked
  against the real file first. A patch applied at a shifted offset silently
  deletes the wrong lines and produces a commit that looks deliberate.
- **Reach a customer.** No agent here is `client_facing`, and code context
  never leaves the company.

---

## Cost per issue, roughly

- Gated: nothing. No model, no queue.
- Noise: one fast call.
- Actionable, not fixable: one fast call plus one senior call plus a few
  repository reads.
- Actionable and fixable: two senior calls.

At about three new production issues a day with most of them gated or noise,
this is small. The shape that would make it expensive is the gate failing
open, which is what the daily report's drop counts are for.

---

## The feedback loop, which is the part that matters

When a verdict is wrong, it becomes an **answer-key case first and a charter
change second**. That rule is in `VOMEOS.md` and it is the difference between
an agent that improves and one that just gets more confident.

```bash
py -m vomeos.evaluate engineering.sentry_triage
py -m vomeos.evaluate engineering.bug_analyst
py -m vomeos.evaluate engineering.fix_author
```

A charter edit has to clear the key before it ships. If the score drops, the
edit was a guess.

Two real examples already in the case files, both marked `KEY CORRECTED`:

- The analyst was asked to call a non-null violation a migration problem. It
  said the fix was at the call site and was right.
- The fix author refused all twelve cases and looked broken. The cause was
  that the answer key's source snippets had gaps in their line numbers while
  the real code reader emits contiguous lines, and you cannot write a unified
  diff against non-contiguous context. The agent was right and the test data
  was wrong.

When an agent refuses everything, check what you actually handed it before
you change the charter.
