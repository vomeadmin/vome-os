---
name: learning-from-sam
description: When Sam gives feedback that should apply beyond the item in front of you, record it as a lesson so every agent's next run follows it.
owner: support
---

Sam is the only reviewer, and every correction he makes is meant to stick.
"Too long" on one draft is usually "too long" on all of them. If a correction
lives only in this conversation, the next ticket repeats the mistake and he
makes the same correction again.

**When to record a lesson.** Sam's message states, or clearly implies, how
something should be done in general: tone, length, what to leave out, words
to use or avoid, how to treat a kind of ticket, how to route, what a card
should flag. "Never mention the earlier reply", "kiosk chunk errors don't
need a reply", "say user, not volunteer", "stop asking app or website".

**When not to.** A fix that only makes sense on this ticket: a name, a date,
a recipient, a fact about one customer's account. Questions. Approvals. One
ticket's decision ("hold 6", "send 3").

**How.** Add to your output:

```
"lessons": [
  {"agent": "<who should learn it>", "rule": "<one imperative sentence>"}
]
```

`agent` is `customer_success.reply_drafter` for anything about customer
replies, `customer_success.triage_director` for categories, routing and what
cards say, `customer_success.sprint_partner` for how you talk to Sam, and
`*` only for something every agent should know. Write the rule so it can be
followed without the example: "In a resolution reply, do not mention earlier
replies, even a wrong one." Name the situation when it is not universal.

In your reply, say in one line what you learned. The system confirms it with
a lesson number Sam can undo with "forget N". Leave `lessons` empty when
nothing general was said, which is most of the time.
