# CS agent workflow (snapshot)

Condensed from "CS Agent Workflow and Handoff (Oct 16, 2026)" (doc `3dztm-3311`, page `3dztm-6351`), pulled Oct 19, 2026. That page records how the agents were built; **Playbook v2.1 wins over it wherever they disagree**, and the page itself marks two sections as superseded (noted below). Read this when you need to know what the agents already did to a ticket, or why something is in Agent Fixes.

## Lanes the triage director assigns

| Lane | Qualifies when | What happens |
|---|---|---|
| Housekeeping | Client confirmed the fix, or an error report the code already handles | `close_ticket_quietly`, no reply |
| Answer | Works as designed, missing info, walk-back | Agent drafts from help centre, UI text and code; Sam approves |
| Agent fix | Low risk: no migration, not auth or billing, about 3 files or fewer, behaviour clear | PR into the dev branch (`development` for core, `develop` for vome-react), Agent Fixes task |
| Chunk-load or page crash ("Vome Error Report") | Always | Agent hotfix, closed quietly, no reply |
| Engineer | Data integrity, migrations, shared core paths, low confidence | Routed automatically to OnlyG or Sanjay (Priority Queue, one assignee, private Zoho note, no Slack, no status change) |
| Feature request | Client asks for something Vome does not do | Sam decides log, build or no (`log_ticket_request`) |
| Data, billing, account ops | Lookups under other emails, merges, data fixes, billing | Not touched by the agent. Sam, in Zoho |

## Agent Fixes and the release chain

- Agent Fixes list `901115573468`: one parent per fix (no Zoho link), one child per Zoho ticket. Fields Fix Commit, Merge Owner, Release Check (IDs in `ids-and-fields.md`).
- Merged into the dev branch: tasks move to `on dev` by themselves. Sam merges agent PRs and deploys web; OnlyG merges core to production.
- The release watcher (every 15 minutes) proves every commit is on `master` inside a green production build, then sends the short resolution email and closes the ticket and tasks (Playbook v2.1; this replaces the approval step the handoff page describes in 3.5 and 4.1). A guard-held draft appears in `sprint_followups`.
- A multi-repo fix is live only when every commit is on production. Nobody moves an Agent Fixes task to `on prod` by hand.

## Incidents

2 or more tickets with the same symptom in 24 hours, or a ticket matching a live Sentry issue, is an incident: one fix task, one child per ticket, immediate ping to Sam. Grouping found the Oct 15 profile-creation regression (six tickets, one fix, closed in about 4 hours).

## Superseded sections of the handoff page

- **3.4 Zoho statuses:** New or Processing no longer means "waiting for triage". It means the client is owed a reply from Sam (Playbook Rule 6).
- **16, `route_ticket`:** routing no longer posts a Slack notice or sets Zoho to In Progress.
- **3.5 and 4.1:** "it's fixed" emails for agent fixes now send on their own once proven live.

## Things the agents never do

Merge PRs, write migrations, touch data, billing or account operations, set `on prod`, or send anything outside the automatic sends without Sam's word.
