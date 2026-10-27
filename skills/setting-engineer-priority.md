---
name: setting-engineer-priority
description: Set `priority` on a bug card from Vome's one engineer ladder, so the Priority Queue shows what to work on first and only real emergencies ping an engineer in Slack.
owner: engineering
---

Sam, 2026-10-27: in two weeks OnlyG got 75 posts in #eng-backend, 17 of
them "URGENT", and 29 of his 65 Priority Queue tasks had no priority at all,
the worst security findings among them. Slack is for urgent items; ClickUp
is the backlog, in priority order. The task is filed at the priority you
set, and **only `urgent` pings the engineer**. Everything else reaches him
in his morning list, so keep `urgent` rare.

- **urgent**: production down or degraded for many users, client data lost
  or being lost, a security hole anyone can use today (reading or changing
  another org's data, account takeover, an auth bypass), or a hard date
  that is days away.
- **high**: a real bug clients hit in production, or a security gap that
  needs an admin or unusual access to use.
- **normal**: a bug with a workaround, a narrow case, or a one-off data
  check or audit.
- **low**: cosmetic, cleanup, or only on a dev branch.

A data check beside a fix ("was this ever used?") is filed one step below
the report's priority, never urgent, so you do not need to rate it.

When unsure between two levels, choose the lower one and say why in
`analysis`. Sam raises a priority in a second; an urgent ping that was not
urgent costs the engineer his focus.
