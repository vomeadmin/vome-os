---
name: checking-the-code
description: Before choosing a lane for a bug or a "does it work like this" question, read the code on the live branch and add a code_check to the card.
owner: support
---

On 2026-10-16 the code overturned two of the first eight triage calls. One
card said "open a task" for a crash the app already recovers from; another
followed an earlier reply that blamed the mobile app, when admin assignments
simply bypass the overlap rule by design. Both cards read well. The code is
what made them right.

**When to check.** Every clear_bug, vague_bug and user_education ticket, and
any ticket where "is this a bug or how it works" decides the lane. Skip it
for misdirected, account deletion, email updates and billing.

**How.** You have read-only tools on the code production runs.

1. Search for an anchor: the exact error text, a UI label the customer
   quoted, a setting or field name. `core` holds most product logic (Django
   apps such as form_app, opportunity_app, sequence_app, organization_app,
   notification_app). `web` is the React app; its search is unreliable, so
   list `src/views/org/<area>` and read from there.
2. Read the lines around the hit. Read the function, not just the line.
3. If something looks recently broken, check recent commits on that path.

Stop once you can answer, or when the budget runs low. Three good reads beat
eight shallow ones.

**Add this to the card:**

```
"code_check": {
  "verdict": "bug" | "works_as_designed" | "design_question" | "unclear"
             | "not_checked",
  "finding": "<what the code does, in one or two sentences>",
  "where": "<repo:path:line>",
  "fix": "<what a fix would change, or ''>",
  "fix_risk": "low" | "medium" | "high",
  "confidence": "high" | "medium" | "low"
}
```

**The verdict decides whether a fix is made (Sam, 2026-10-18).**

- **bug**: the code does not do what it was built to do. A low-risk bug at
  high or medium confidence becomes a pull request with no one asking.
- **works_as_designed**: it does what it was built to do; the customer
  expected something else. That is a how-to answer, never a fix.
- **design_question**: it does what it was built to do, but that behaviour
  contradicts how the feature is set up elsewhere (one setting overrides
  another, a rule applies in one place and not the next). Nothing is
  changed until Sam decides: change it, log it as a request, or answer it.

When unsure between bug and works_as_designed, prefer works_as_designed:
a wrong fix changes the product for everyone.

`where` is exactly `repo:path:line`, nothing after it; commentary goes in
`finding`. `fix_risk` is low only when the change is small, local, has no
migration, and does not touch auth, billing, permissions or shared data
paths.

**Let it change your call.** works_as_designed with high confidence makes a
"bug" a user_education. A bug the code confirms belongs in clear_bug even if
the customer described it vaguely. When the code and the thread disagree,
say so in the situation.

**Never let it reach the customer.** File names, functions, fields and
branches stay in the card. The customer hears product words only.
