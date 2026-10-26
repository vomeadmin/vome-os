---
name: investigating-in-depth
description: Work a ticket to its source the way an engineer does in Claude Code, with grep, file search and the engineer's own task notes, instead of stopping at the first plausible read.
owner: customer_success
---

Sam's test for this division is simple: if he opens the ticket in Claude
Code with the same model and gets a different answer, the agent was wrong.
His sessions win because they keep going. Yours has the budget to do the
same, so do the same.

**Start from what the customer saw.** A quoted error, a button label, a
column name. Grep it in `web` or `mobile` (it is usually in a translation
file), grep its key to find the component, then find the request that
component sends. That endpoint, in `core`, is where the behaviour lives.

**Follow the data, not the names.** View, serializer, model method, the
query that filters. A setting that exists in the role model but is never
read where the data is served does nothing there: grep where it is used,
not only where it is defined.

**Trace the customer's exact action.** "I removed them", "I changed the
time", "I re-sent it": find the endpoint for that action and read what it
writes. Most real bugs are a path that leaves a stale record behind or skips
a check another path makes.

**Check for a fix already in flight.** `recent_commits` on the file, and
`read_task` on any task the existing-work search named. If an engineer
already explained it, that explanation is your finding, with the task as
evidence.

**Use the right repo.** `core` for product logic, `database` for the
volunteer database service, `web` and `mobile` for screens and their text,
`chats` for chat, `integrations` for SSO and other connections. "Out of
repo" is not a finding while the repo is readable.

**Know when to stop.** You are done when you can say what the code does,
where (`repo:path:line`), and whether that is what it was built to do. If
the last open question is about one account's data, stop there and name the
record; more reading will not settle it.

**Write what you found, not what you did.** The card gets the conclusion
and its evidence. "Read 14 files" helps nobody; "<what the code does to
which records, under which condition>, `<repo:path:line>`" settles the
ticket.
