---
name: checking-for-existing-work
description: Before proposing new work or claiming something shipped, find the task that already covers it and read its real status.
owner: customer_success
---

Two questions, always in this order, before you propose anything.

**Does work already exist for this?** Search by symptom, not by the
customer's wording. Two people describing the same defect rarely use the same
words, and a search on their phrasing finds nothing while the task sits
plainly in the backlog. Search the active queue and the backlog, including
parked and declined items, because a declined request arriving for the fifth
time is information about priority rather than a new request.

Signals you have a match: the same module, the same user-visible symptom, the
same conditions. A different customer is not a different bug.

**What is its real status?** This is the question people skip, and it is the
expensive one.

A task that exists is not a task that shipped. Work in flight is queued, in
progress, or on a dev environment. Shipped means deployed to production or
closed. Only shipped backs a statement to a customer that something is fixed.

A task with no activity recorded against it has not been worked on, whatever
its status column says. If someone moved it without doing anything, the
status is a claim too.

**What to report.** Give the task identifier, its status, and whether that
status means shipped. If you found nothing, say you found nothing, plainly.
"No existing task found" is a useful answer. A vague answer that leaves the
reader unsure whether you looked is not.

**When you find a near miss.** A task covering part of the problem is worth
naming as a near miss rather than claiming as a match. Say which part it
covers and which part it does not.

**The closure check.** When a customer reports a symptom that a recently
closed task was supposed to fix, that closure is in doubt. Say so. Do not
open a parallel investigation and do not assume the customer is wrong.
