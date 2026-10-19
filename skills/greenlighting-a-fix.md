---
name: greenlighting-a-fix
description: When a real bug has a fix you can describe but should not build unasked, ask Sam for a one-word greenlight instead of sending it to an engineer, and say plainly what he is approving.
owner: engineering
---

Sam, 2026-10-19: in the first #bug-reports dry run, guest shift picks had a
confirmed bug and a concrete fix (core:form_app/generic_volunteer/
serializers.py:1296), but the fix was high risk, so it went straight to
OnlyG and Sanjay. He did not want that. When a fix solves the problem, he
wants to say yes once and have it built, without the engineers.

**Choose greenlight** when `code_check.verdict` is `bug`, you can say what
the change is and where, and no hard limit applies. Size and risk are not
reasons to choose engineer any more: they are what Sam is approving. A
backend handoff that asks for behaviour the code is missing is a greenlight
too, if you can name the code that changes.

**Choose engineer** only when you cannot describe the change (no location,
no idea what it is), or a hard limit applies. Name it in `hard_limit`:

- `migration`: a new or changed model field, table, index or any schema
  change. Migrations are generated on deploy here and never hand-written.
- `infrastructure`: hosting, DNS, domains, CDN, deployment, environment or
  settings values (cookie, CORS, CSRF settings included).
- `data`: fixing rows, backfills, billing, account operations.

A greenlight never lifts these. Sam's yes cannot build them, so say so.

**Fill in for every greenlight:**

- `code_check.files`: every spot the fix changes, one `repo:path:line` each,
  across repos when the fix needs both (`core:...`, `web:...`). Keep it to
  what is needed: a pull request may change at most three files and sixty
  lines per repo.
- `code_check.references`: files the fix copies from, one `repo:path:line`
  each ("make it match step_status/views.py" means that file goes here).
  The fix author is shown them read only, so it uses their real imports
  and helpers instead of guessing. The first greenlit build failed without
  one.
- `code_check.fix`: the change in one line, the way Sam will read it.
- `risk_plain`: what could break, in product words ("guests could be booked
  into full shifts if the capacity check is skipped"). Not "medium risk".
- `fix_risk`: honest. A greenlight is not a reason to call it low.

**When Sam replies in the thread** with a question or a change to the fix,
your context `thread` holds it. Answer the question in `answer`, take his
change into `code_check.fix` and `files`, and return greenlight again so he
can say yes to the revised fix.
