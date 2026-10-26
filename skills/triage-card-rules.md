---
name: triage-card-rules
description: The fixed category list, the five cases that are always needs_human, and the exact type of every card field, so any agent that writes or rewrites a triage card produces one the sprint, the drafter and the router can read.
owner: customer_success
---

Downstream code reads the card field by field. A category it does not know,
or a sentence where a true/false belongs, breaks the draft and the route.

**category** is exactly one of:

- `clear_bug` specific behaviour, reproducible steps, or a screenshot. A raw
  client error report (stack trace, "Vome Error Report") is a clear_bug.
- `vague_bug` a problem with no detail: no user, no error, no steps
- `user_education` confusion about a feature that works as designed
- `feature_request` genuinely new functionality
- `admin_action` fixed by a configuration change on their side
- `misdirected` they think Vome runs the event
- `email_update` wrong or changed account email
- `account_deletion` a volunteer wanting their account removed
- `escalated` an engineer handed this over for a product decision
- `awaiting_client` we asked or answered and they have not replied. An
  out-of-office, an auto-reply or an emoji reaction is not a reply, so a
  thread whose newest inbound is one of those is awaiting_client
- `needs_human` you cannot responsibly categorise it

There is no other category. Billing, access and data questions are
categorised by these rules and put in the right lane; they are not
categories of their own.

**Always needs_human**, whatever else is true:

- billing, contract, licensing or admin transitions
- a prior reply told the customer something that looks wrong (fixed, safe,
  on the roadmap) and the thread shows it was not
- the customer is clearly angry and the account is Enterprise, Ultimate,
  or over $1,500 CAD combined ARR
- it may be a request for something that already exists, and nothing so
  far settled it
- no readable content, only a forward or an empty body

**Field types**

- `confidence`: "high" | "medium" | "low"
- `important_client`: true or false, never a sentence. True for
  Enterprise, Ultimate, or over $1,500 CAD combined ARR
- `lane` and `escalation_reason`: the values in choosing-the-lane
- `code_check.verdict`: bug | works_as_designed | design_question |
  unclear | not_checked
- every other field: a string ("" when empty)
