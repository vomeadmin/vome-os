---
name: choosing-the-lane
description: Put every ticket in one lane, and when it needs an engineer, say why from a fixed list, so Sam can see what still escalates and what would stop it.
owner: customer_success
---

Sam runs the sprint from batches. The lane decides which section a ticket
lands in and what he is asked to do, so it has to be right more than it has
to be detailed.

**Add these two fields to the card:**

```
"lane": "answer" | "agent_fix" | "engineer" | "feature" | "sam_ops"
        | "housekeeping",
"escalation_reason": "<one value below, or ''>"
```

- **answer**: a reply settles it (how-to, a question for them, a bug
  acknowledgment while a fix is in flight).
- **agent_fix**: the code check found a bug small enough to fix (see
  checking-the-code). The reply is an acknowledgment.
- **engineer**: a person on the engineering team has to look. Fill in
  `escalation_reason`.
- **feature**: they want something Vome does not do.
- **sam_ops**: data fixes, account lookups and merges, billing, contracts.
  Sam does these in Zoho; never an engineer, never an agent.
- **housekeeping**: nothing is owed and nothing needs doing. The customer
  confirmed it works ("thanks, all good now"), an out-of-office or
  auto-reply, a thank-you, or a "Vome Error Report" the code already
  handles. The ticket can close quietly. Never use it when the latest
  message asks anything, reports anything new, or sounds unhappy.

**escalation_reason** (engineer lane only), the first that applies:

- `data_lookup`: needs a record we cannot read (a delivery log, a specific
  account's data)
- `mobile_unreadable`: the behaviour lives in the mobile app
- `schema_or_migration`: a fix needs a model or migration change
- `auth_or_billing`: touches login, permissions, payments
- `out_of_repo`: the chats or integrations service, deployed by hand
- `low_confidence`: the code check could not settle it
- `product_decision`: the code works as built and changing it is a product
  call (pair with the code check verdict design_question)

Each reason names a gap: when one keeps coming up, that is what to fix so
fewer tickets need an engineer. Do not pick `engineer` for something an
answer or an agent fix would settle.
