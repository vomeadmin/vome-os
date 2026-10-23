---
name: checking-the-code
description: Before choosing a lane for a bug or a "does it work like this" question, read the code on the live branch and add a code_check to the card.
owner: support
---

On 2026-10-16 the code overturned two of the first eight triage calls. Both
cards read well. The code is what made them right. The goal is a card exact
enough that a fix PR opens without anyone sitting down to it.

**When to check.** Every clear_bug, vague_bug and user_education ticket, and
any ticket where "is this a bug or how it works" decides the lane. Skip it
for misdirected, account deletion, email updates and billing.

**How.** You have read-only tools on the code production runs.

1. Search for an anchor: the exact error text, a UI label the customer
   quoted, a setting or field name. `grep_code` is a regex over a full copy
   of the live code: use it to trace a field or endpoint across an app
   (`path: "sequence_app"`), and `find_files` to find a screen or module by
   name (`**/*Kiosk*`). `core` holds most product logic (Django apps such
   as form_app, opportunity_app, sequence_app, organization_app,
   notification_app). `web` is the React app (`src/views/org/<area>`),
   `mobile` the app, `chats` the chat service, `integrations` SSO and
   other connections. A UI label usually lives in a translation file:
   grep it, then grep its key to find the component.
2. Read the lines around the hit. Read the function, not just the line.
   Follow the call: the view, its serializer, the model method it uses.
3. If something looks recently broken, check recent commits on that path.
   If the existing-work search names a task, `read_task` it: an engineer's
   comment often settles the ticket outright.
4. **Trace what the customer did.** "I tried them and then removed them"
   names an action: search for its endpoint (`remove`, `archive`, `delete`
   plus the object) and see what it does to the rows. A remove that only
   empties a field or switches a flag off leaves the record in place, and
   anything still counting it is a bug (#9501, L37).
5. **A fix we shipped did not help?** Read what it changed, then find the
   path it missed. That path, with its line, is the card.

Stop once you can answer, or when the budget runs low. Three good reads beat
eight shallow ones. "I could not read how X works" is never a finding while
calls remain: read X. When the budget does run out, name in `finding` the
one file or question still open, so the investigator that picks up an
unsure card starts there instead of repeating your reads.

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

- **bug**: the code does not do what it was built to do. A low or medium
  risk bug at high or medium confidence becomes a pull request with no one
  asking.
- **works_as_designed**: it does what it was built to do; the customer
  expected something else. That is a how-to answer, never a fix.
- **design_question**: it does what it was built to do, but that behaviour
  contradicts how the feature is set up elsewhere (one setting overrides
  another, a rule applies in one place and not the next). Nothing is
  changed until Sam decides: change it, log it as a request, or answer it.

When unsure between bug and works_as_designed, prefer works_as_designed:
a wrong fix changes the product for everyone.

**Two checks before "by design" (#9501).** A delete refused because
"something still uses it" counted old sequence edit drafts that are switched
off and never removed, so a step she had replaced blocked it forever. The
card called that deliberate.

- When a refusal or limit counts records, ask whether those records can go
  stale (switched off, replaced, superseded, never deleted). A stale record
  that blocks the customer makes it a bug.
- Judge what the customer saw from the screen's own source: the endpoint
  that screen calls and its UI strings. Another endpoint's error text is not
  what they read.

`where` is exactly `repo:path:line`, nothing after it; commentary goes in
`finding`.

**fix_risk decides who acts (Sam, 2026-10-22).**

- **low**: a small local logic change that writes nothing new. PR, Sam
  merges.
- **medium**: changes what gets written or deleted, or spans a few files,
  with no migration and nothing on the high list. PR, reviewed and merged
  by OnlyG (backend) or Sanjay (web, mobile).
- **high**: a migration, auth, permissions, billing, bulk data changes, or
  more than one repository. Engineer task with the exact line, no PR.

Pick by the change, not by how serious the bug is.

**Let it change your call.** works_as_designed with high confidence makes a
"bug" a user_education. A bug the code confirms belongs in clear_bug even if
the customer described it vaguely. When the code and the thread disagree,
say so in the situation.

**Never let it reach the customer.** File names, functions, fields and
branches stay in the card. The customer hears product words only.
