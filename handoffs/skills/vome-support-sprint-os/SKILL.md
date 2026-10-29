---
name: "vome-support-sprint-os"
description: Vome's support sprint operating guide with VomeOS code access (supersedes vome-support-sprint). Covers Zoho Desk tickets, ClickUp tasks, engineer hand-offs in Slack, client reply drafts, the Vome OS sprint tools (sprint_board, send_replies, ping_team, sprint_followups, sprint_queue, walk_queue, ticket_states, fix_status, send_ticket_reply, log_ticket_request), and reading Vome's repos, help centre and backlog through the VomeOS connector. Use whenever someone shares a Zoho Desk ticket link or number, says "support sprint", "triage the queue", "walk the queue", "take this", "route this", "draft a reply", "let's close this out", or asks to create, log or route a ClickUp task or check for a duplicate. Also use whenever someone asks how a Vome feature works, whether something is a bug or works as designed, where something lives in the code, whether a fix is live, where an In Progress ticket stands, build now or log for later, what to tell the engineer, or wants a customer how-to answer, even if they never mention the sprint or VomeOS.
---

# Vome Support Sprint (with VomeOS)

Skill version: 2026-10-29 (the sprint board). When Sam asks which version is loaded, quote this line.

This skill runs Vome's support operations: Zoho Desk tickets in, ClickUp tasks and engineer hand-offs out, client replies drafted for Sam's approval.

Since Playbook v2.1 (Oct 19, 2026) the agents do most of the processing before anyone sits down. Every ticket and customer reply is triaged on arrival, with a code check. Low-risk bugs get an agent fix PR. Bugs the agent cannot fix are routed to OnlyG or Sanjay automatically. The release watcher sends the "it's fixed" email once a fix is proven live. So the job here is mostly telling clients what is already happening, through the Vome OS tools built for it. Those tools carry the outbound guards, the Zoho status rule and the learning loop, which is why a hand-made equivalent is the wrong move whenever the tool is available.

The rules live in ClickUp docs that the team keeps current. `references/` carries a saved copy so the skill still works offline, but the live docs always win.

## Step 0: Load the live docs

Do this at the start of every conversation, before acting on anything.

ClickUp workspace `3604276`. Load the ClickUp tools with `tool_search` if they are deferred. Fetch with `clickup_get_document_pages`, `content_format: text/md`.

| Doc | Page | Page ID |
|---|---|---|
| `3dztm-3311` Support Sprint Playbook | Claude AI Operations Guide (v2.1 or later) | `3dztm-1871` |
| `3dztm-3311` | Sprint Lessons Log | `3dztm-2491` |
| `3dztm-3311` | CS Agent Workflow and Handoff | `3dztm-6351` |
| `3dztm-3331` System Configuration | Technical Reference | `3dztm-1891` |
| `3dztm-3331` | Engineering Slack Channels and Dev IDs | `3dztm-2911` |
| `3dztm-3331` | VomeOS Code Access and Repo Map | `3dztm-6331` |

If a page list changes, run `clickup_list_document_pages` with `max_page_depth: -1` on both docs and fetch every page.

- **Mode B and C (sprint):** fetch all six pages in two parallel calls (one per doc).
- **Mode A (Slack hand-off):** fetch the Playbook, the System Configuration, the Dev IDs page and the VomeOS page, so the reply stays fast. Add the Lessons Log whenever you will draft client-facing text, the routing is ambiguous, or you are checking for duplicates.
- **Mode D (product or code question):** the VomeOS page and the Lessons Log are enough unless a ticket action follows.
- **Precedence:** the Playbook page wins over every other page, except a Lessons Log entry dated after the Playbook version. Then the other live pages, then `references/`, then memory. If the live docs disagree with the request, follow the docs and say so in one line. If they disagree with `references/` or with this skill, use the docs and mention the saved copy is stale.
- **If a fetch fails:** work from `references/` and say once that you are using the saved copy from Oct 19, 2026 (Playbook v2.1). Changes made after that date are not in it.

Then load the Vome OS tools with `tool_search` ("VomeOS"; also try "sprint queue" and "walk queue"). Whether `sprint_queue` comes back decides the path below. If code results look thin or empty, run `connector_status` before concluding anything, and say what is unreachable.

## Two paths

**Agent path (the default):** Sam, with the Vome OS sprint tools loaded. Use the dedicated tool for each job:

| Job | Tool |
|---|---|
| Running a sprint: who is waiting on whom (send, tell_them_done, check, decide, internal, close_quietly) | `sprint_board` |
| Sam's "send all" (or "send all except ...") | `send_replies` with every ticket under send and tell_them_done minus his exceptions; a check ticket only when he names it, in `checked` |
| Chasing our team on a stalled In Progress ticket, no client email | `ping_team` |
| What is owed for shipped work, PRs waiting on Sam, failed release checks | `sprint_followups` (the board already carries these) |
| The queue with the agents' first pass, grouped (includes re-triaged In Progress tickets) | `sprint_queue` |
| Walking the queue section by section | `walk_queue` |
| One ticket's card, `engineer` and `agent_fix` read live | `ticket_first_pass` (no `refresh` unless Sam asks: each re-triage costs about $0.50). It re-runs triage by itself when the card is missing or older than the customer's latest message; if it answers "triaging", wait a minute or two and call it again |
| Where an In Progress ticket stands, and the line to tell the client | `ticket_states` (nightly reconcile of Zoho In Progress against ClickUp) |
| Routing a ticket whose `engineer` field is empty | `route_queue` after `walk_queue`, or `route_ticket` for one ticket |
| Hotfixes and raw error reports | `propose_fix`, then `fix_status`. Never write a fix by hand |
| Whether a fix is live before saying so | `fix_status` |
| Closing with no reply (Rule 12) and housekeeping | `close_ticket_quietly` |
| Sending any client reply | `send_ticket_reply`, Sam's words verbatim as `sam_said`. `text` empty sends the agent's draft exactly as shown; `text` set sends Sam's edit, signed, through the guards, and teaches the drafter |
| Feature requests from tickets | `log_ticket_request`, only on Sam's word: `build=true` for build now, without it for later (see "Logging a feature request") |
| What triage costs, how often the deep pass ran and corrected the card, the forecast | `triage_cost(days)` |
| A general correction from Sam | `teach_agents`, in his words |
| A how-to with no help article | Offer `run_faq` |

`propose_fix`, `send_ticket_reply`, `send_replies`, `ping_team`, `route_ticket`, `close_ticket_quietly`, `log_ticket_request` and `teach_agents` are Sam only. Triage already routes bugs to engineers, so routing by hand or with `route_queue` is only for a ticket whose `engineer` field is empty. An empty `engineer` field is not proof nothing exists, though: a task filed by hand from an earlier sprint does not show there. Read the card's `existing_work` line and search ClickUp for the ticket number before routing anything. Routing never changes the Zoho status (Rule 13).

**A chat older than the latest deploy has stale tools.** If `route_ticket`'s description still says it posts a Slack notice or sets Zoho to In Progress, this chat started before the Oct 19 deploy. Do not use it, because it would break Rule 13. Create the task by hand with the Zoho status unchanged, and tell Sam to start a new chat or reconnect Vome OS.

**Manual path:** a Slack hand-off from Ron or an engineer (Mode A), or the sprint tools are not loaded. It uses Zoho Desk, ClickUp and the VomeOS lookup and code tools directly, with the sync table in `references/sprint-playbook.md`. If `sprint_queue` is missing, say in one line: "The sprint tools are not loaded: reconnect Vome OS in Settings, Connectors, then start a new chat." Nothing on this path closes the loop in Agent Fixes or teaches the agents, so say that once too.

## Pick the mode

| Signal | Mode |
|---|---|
| A Zoho Desk link or ticket number plus "create a task", "log this", "route this", "can you take this" | A: Ticket to task |
| "Support sprint", "triage the queue", "run the batch", "what's waiting", "walk the queue" | B: Sprint loop |
| "Let's close this out" | C: Close out |
| "How does X work", "is this a bug", "check the code", "is it live yet", "where does this In Progress ticket stand", "what should I tell the engineer", a how-to question with no ticket | D: Product or code question |
| A single ticket, "draft a reply", "how do I answer this" | `ticket_first_pass` on the agent path, or a triage card (format in `references/sprint-playbook.md`) on the manual path |

## Rules that never bend

These come from the Playbook's critical rules and the Lessons Log. Each one exists because it was broken at least once.

1. **No engineer names in client-facing content.** Say "our team". Engineer names are fine in ClickUp comments and internal Slack.
2. **No client email goes out without Sam's explicit word, except the automatic sends.** The automatic sends are: the engineer-triggered user-education and needs-client-info handlers; the on-prod handler when an engineer moves a Priority Queue task to `on prod`; the release watcher's "it's fixed" email once an agent fix is proven live (it also closes the ticket); and auth and activation replies. Everything else waits for Sam, acknowledgments included. Approval is Sam's own words for that ticket or a range he named ("send", "approved", "send 1 to 10", "go ahead", or "send all", which covers only the board's send and tell_them_done lanes as just shown). Silence, "looks good" and moving on are not approval. In Mode A nothing is ever sent to a client. Zoho comments you add are private (`isPublic: false`).
3. **No-Confirmation Rule.** Never tell a client we can see, reproduce or have identified a bug. Reading the code is not an investigation, so a finding in the code never changes what the client is told.
4. **Never say fixed, resolved, planned or on the roadmap unless `fix_status` says live or a verified task backs it.** A PR is not a fix, and a change on the live branch is not proof it is deployed or matches the symptom. Forward-looking lines ("we will be looking into this") need a backlog task or Sam's go-ahead for that ticket.
5. **Nothing from the code reaches a client in code terms.** No file paths, function or component names, translation keys, branches, commits or task IDs. Describe the product in the words the customer sees on screen.
6. **Check for existing work before creating anything.** On the agent path, `ticket_first_pass` shows `engineer` and `agent_fix` live. Then `check_existing_work` and a ClickUp search: ticket number first, then the client's open tasks, then the symptom, including recently closed tasks in Done. Reopen rather than duplicate.
7. **One assignee per task.**
8. **Sign-off is "Sam | Vome team" followed by support.vomevolunteer.com.** Never "Vic". In French replies the sign-off stays in English.
9. **No em dashes anywhere**, in Slack, Zoho, ClickUp, drafts or doc edits. Use periods, commas, parentheses or "and".
10. **Reply in the client's language**, with full accents for French. A faithful translation of approved text is not a content change.
11. **Recipients:** the person who actually wrote in (not the ticket contact record), their cc, and the ticket's `secondaryContacts`. Zoho never adds those on its own. `send_ticket_reply` enforces this; on the manual path read `secondaryContacts` before every send.
12. **Closing a ticket without a reply:** `close_ticket_quietly` on the agent path. On the manual path, set `disableClosureNotification=true` as its own update, otherwise Zoho sends an unapproved closure email.
13. **Zoho New or Processing means the client is owed a reply from Sam, and only Sam's sent reply moves it.** Never change the Zoho status when routing: automatic routing, `route_queue`, `route_ticket` and a hand-made task all leave it alone. **An agent fix is the exception** (Sam, 2026-10-24): once a fix PR is open on a ticket we have answered before, the fix email is the reply, so it goes to In Progress with `teamId` `569440000043486190` (the Zoho team "Agent Fixes") and `assigneeId` null, never Support Agent (that is Sam's own list). The agents do this when they open the PR; for a fix registered by hand, set it yourself with `updateTicket`. A never-answered ticket stays New until Sam's acknowledgment. Sam's acknowledgment moves it to In Progress (team owner, or the Agent Fixes team for an agent fix) and the task to `in progress`; an answer closes it with no closure email; a question sets Awaiting Client Response. Once any reply has gone out, the ticket is never left in New or Processing: if nothing is routed or fixing, it goes to Awaiting Client Response (Sam, #9394). Until then a routed task stays `queued`. If the customer writes again on an In Progress ticket, triage runs again and the ticket comes back to the queue.
14. **Fixes go through the agent.** `propose_fix`, then `fix_status`. Never write, patch or merge a fix by hand, and never move any task to `on prod` (the on-prod handler would email the client).
15. **Show composed text before sending.** Sam's verbatim text goes out with only greeting and signature added. An instruction ("tell her that...") gets composed and shown, then waits for his word. This has caught wrong names more than once.
16. **Importance flag:** IMPORTANT CLIENT when the account is Enterprise or Ultimate tier, or Combined ARR is over $1,500 CAD. Urgent (interrupts Sam) is an account at 4,000 CAD+ ARR or the same problem from a second account.

**Slack:** every bug notice Claude posts goes to #bug-reports (`C0CAJ269RAQ`) with the assigned engineer tagged. #eng-backend and #eng-frontend no longer take bug notices. Feature requests and backend hand-offs go to #vome-feature-requests, and #support-sprints is for urgent pings only. Channel IDs are in `references/ids-and-fields.md`.

## VomeOS in one screen

Full reference, the repo table, the module map and known limitations: `references/vomeos-code-guide.md`.

**How-to answers (lookup order)**
1. `lookup_feature` with the client's question in plain words. If nothing on point comes back, try once more with the feature's likely on-screen name ("overlapping reservations" rather than "booking two shifts at once") before concluding there is no FAQ.
2. `where_in_app` for exact page names and button labels. Quote them as the customer sees them.
3. Help centre or UI text answers it clearly: answer from that and link the article. Check the code only if something looks off or the article is old.
4. No FAQ covers it: the code is the source of truth. Answer from what it actually does, in product words, and mark the card "Source: code, no FAQ" so Sam vets it closely.
5. Help centre and code disagree: the code wins. Flag the article for an update.
6. No article, or a stale one: offer `run_faq` so the next client gets a link. Run it on Sam's yes.

**Reading code correctly**
- `search_code` indexes each repo's default branch. Always open the hit with `read_code_file`, which reads the live branch (`is_live_branch: true`), before relying on it.
- Read the top of a file first. Core modules usually open with a docstring stating the design decision. Check `guides/` and app READMEs before deep-diving.
- Chats and integrations deploy to prod manually, so their live branch may be ahead of production. Mobile fixes reach users only with the store build.

**Engineer executive summary** (goes in the ClickUp description, see `references/templates.md`): Likely cause, Where (repo, path, lines), Potential fix, Confidence (and why), Not verified. When the agents' first pass already has a finding, use it rather than repeating the search.

**Backlog tools**
- `check_existing_work` before any create, any "known issue" line, and any forward-looking line. `shipped: false` means not shipped, whatever the status name.
- Feature requests are always Sam's to log, never automatic. From a ticket: `log_ticket_request` on his word. Otherwise `log_feature_request` (refuses if something similar exists unless `confirm_new` is true after reading the candidates) or `add_request_to_existing` for a +1.

## Mode A: Ticket to task (the Slack hand-off, manual path)

Trigger: an engineer or Ron shares a Zoho Desk ticket and wants it taken. Triage has usually got there first, so the most common right answer is "already routed" rather than a new task. The goal: no duplicate, a real lead for the engineer, the Zoho status untouched, and a short confirmation. Do it in one pass without asking permission for each step. Creating the task and the private note need no approval; contacting the client does.

1. **Read the ticket.** Extract the ticket ID from the link (or search by ticket number). Fetch the ticket, then its threads, then each thread as plain text. Read the whole thread, never the description alone. Zoho attachments cannot be read through tools, so note that they exist.
2. **See what the agents already did.** If `ticket_first_pass` is available, call it without `refresh`: lane, verdict, finding, `engineer`, `agent_fix`.
   - `engineer` set, or `existing_work` names an open task for this ticket: it is already routed. Do not create a task and do not route again. Report the task, add anything new as a comment, and go to step 9.
   - Lane is housekeeping (the client withdrew, confirmed a fix, or wrote an out-of-office): nothing is owed. Check the Zoho status; if the ticket is already closed but its task is still open, flag the mismatch and offer to close the task. Report and stop.
   - `agent_fix` set: check `fix_status` and report it. No engineer task.
   - Lane is design question, feature request, or data, billing or account operations: those wait for Sam. Report and stop.
3. **Enrich.** Look up the contact in Zoho CRM by email, then the account, tier and Combined ARR. Apply the importance flag. If the lookup fails, say "tier unknown" and do not guess.
4. **Decide what it is.** Check the special cases below first. Run `lookup_feature` when it might be a how-to or works-as-designed. Otherwise it is a bug, routed by the table: backend (data, API, auth, SSO, 2FA, notifications, imports, kiosk backend, backend logic) to OnlyG, frontend (UI, mobile, forms rendering, frontend behaviour) to Sanjay. If the person asking is an engineer, assign it to that engineer. If it clearly belongs to the other team, flag it in one line and follow the engineer's instruction.
5. **Investigate the code** if the first pass has no finding. Map the symptom to a module and repo, search for anchors (exact error text, a quoted UI string, a setting or field name), read the hits on the live branch, and form the executive summary. Spend a few calls, not dozens; if nothing solid turns up, say so under Not verified. A finding can change the routing (a frontend-looking symptom caused by a serializer is Backend).
6. **Check for duplicates.** `check_existing_work` plus a ClickUp search: ticket number, then the client's open tasks, then the symptom, across Priority Queue, Agent Fixes, Raw Intake, Accepted Backlog, Sleeping, Declined and recently closed tasks in Done. Also check the same symptom on other accounts, and whether the user has a second active account (that is a merge, which is Sam's account operation, not a bug).
   - **Open task found:** no new task. Comment with the new ticket link, anything new, and your findings if the task lacks them. Then reconcile: if Zoho is already In Progress and the task is `queued` or `on dev`, set it to `in progress` and say what it was. If the task is `needs client info`, `awaiting client`, `escalated`, `on prod`, `sleeping` or closed, leave it and mention the mismatch.
   - **Closed task for a live symptom:** reopen or update it and say the closure may not have held.
7. **Create the task** only when nothing exists. If Sam is the one asking and `route_ticket` is loaded, use it (it writes the engineer summary from the agents' findings). Otherwise create it by hand in Priority Queue `901113386257` with the custom fields from `references/ids-and-fields.md` (Type, Platform, Module, Source Zoho Ticket, Zoho Ticket Link, Highest Tier and Combined ARR when known, Tags when one clearly applies), the description template in `references/templates.md`, and one assignee. Priority: urgent for data integrity, locked-out paid accounts or a hard client date; high for IMPORTANT CLIENT; otherwise normal. Status `queued`.
8. **Read the task back** and confirm the status and custom fields saved (a blank Module field has happened when a dropdown ID was invalid). Fix anything blank.
9. **Sync Zoho:** a private comment (`isPublic: false`, HTML) linking the task. **Leave the Zoho status and owner as they are.** The client is still owed Sam's reply (Rule 13). In Progress with the team owner (Frontend Team `569440000023159001`, Backend Team `569440000023160001`) happens only when Sam's acknowledgment is sent.
10. **Confirm in Slack** in the thread where you were asked, using the format below. If you created or routed a bug and the request did not come from #bug-reports, post the short engineer notice there too.
11. **Do not contact the client.** If a reply is still owed (Zoho New or Processing), offer in one line to draft the one-line acknowledgment for Sam's approval. If nothing is owed (withdrawn, already closed, housekeeping), offer the close instead. "Take this" or "route this" is never approval to send anything.

### Special cases

| Situation | What to do |
|---|---|
| Raw JavaScript or console error ("Vome Error Report", stack trace, TypeError, chunk-load error) | Agent hotfix: `propose_fix`, then `fix_status`, then `close_ticket_quietly`, no client reply. Never write the fix by hand. If the agent declines or Sam is not the one asking, a silent frontend task for Sanjay and a notice in #bug-reports. Include the build ID, since it ties tickets across accounts. Any tier |
| Vague bug, or a third party reports for volunteers with no volunteer email (the Bonnie pattern) | No task. Say what is missing (volunteer email, form or opportunity name, what happens when they try) and offer to draft an info-request for Sam |
| Feature request | `lookup_feature` and `check_existing_work` first. Bring the evidence to Sam: logging is always his call, never automatic. On his word, `log_ticket_request` (or `add_request_to_existing` if it exists) |
| How-to or admin configuration fix | No task. VomeOS lookup order and `references/product-knowledge.md`; offer a draft answer |
| Housekeeping ("thanks, it works", out-of-office) | `close_ticket_quietly` on Sam's word, or leave it for the sprint |
| Notification delays | Not a bug task by default. Offer a Slack DM to OnlyG to check the endpoint logs and the Celery queue |
| Data, billing, account operations (merges, lookups under another email, data fixes, licence changes) | Sam's, done directly in Zoho. No engineering task, no auto-reply. Relationship and subscription matters go to Ron |
| Misdirected ticket (the client thinks Vome organizes the event) | No task. Offer the misdirected template |
| Account deletion request | No task. Direct to Settings, then Delete Account |
| Time zone or date offset symptom | Check the timezone and DST cluster in the Lessons Log and link the existing tasks |

### Slack confirmation format

A few lines, no greetings or thanks. In a thread or channel, tag the person who asked; in a 1:1 DM, do not. Slack link syntax. Slack is internal, so engineer names, file paths and technical detail are fine here.

Already routed by triage (the common case):

```
Already routed: <https://app.clickup.com/t/TASKID|Existing task title> (queued, assigned to @Sanjay).
Agents' finding: [one line] ([repo] [path] L[x]-[y]).
Added a comment with [what was new]. Zoho #9578 left in New: the client is still owed Sam's reply. No new task, nothing sent.
Want me to draft the one-line ack for Sam?
```

Nothing owed (withdrawn or already closed):

```
Already tracked: <https://app.clickup.com/t/TASKID|Existing task title> (queued, @OnlyG).
The client withdrew it and Zoho #9578 is already Closed, so nothing is owed. No new task, nothing sent.
The task is still queued against a closed ticket. Want me to close it as withdrawn?
```

New task:

```
Task created: <https://app.clickup.com/t/TASKID|Short task title>
Assigned to @Sanjay. Web, Forms, Bug, priority normal. Org X is Enterprise (important client).
Likely cause: [one line] ([repo] [path] L[x]-[y]). Details in the task.
Private note on Zoho #9339. Status left as it is until Sam acknowledges the client. Notice posted in #bug-reports.
No client reply sent. Want me to draft the ack for approval?
```

Never end on a recap longer than the confirmation itself.

## Mode B: The sprint loop

Triggered by "Let's run a support sprint", "We're on a support sprint" or "walk the queue". The Playbook's "Running a sprint (agent path)" section is the source of truth. In short:

The sprint is the tickets a client is waiting on, sent with one word. The agents are right 95 to 99 percent of the time, so the job is to surface the few that need Sam, not to re-explain every card. Old In Progress tickets where we wrote last, or where the client has been quiet for weeks, are never a reply: they are internal, and the team gets pinged instead (Sam, 2026-10-29).

1. **Do not re-triage.** Every ticket was triaged on arrival and again on every customer reply; `engineer` and `agent_fix` are read live. Never pass `refresh` to `ticket_first_pass` unless Sam asks.
2. **Open with `sprint_board`.** One line of counts, then the pipeline one line each (PRs waiting on Sam's merge, fixes waiting on production, failed release checks). No `sprint_followups` call: the board carries it. If `sprint_board` is not in the tool list, use `sprint_queue` and tell Sam to start a new chat or reconnect Vome OS.
3. **send, then tell_them_done:** one compact card per ticket, important clients first. **The first line is always the `header` exactly as the tool gives it** (`Item N | #ticket | client (tier, ARR) | contact <email> | waiting | IMPORTANT`), then the `links` line, one line of situation, and **the reply in full exactly as the client will get it, with To and Cc**. No per-card analysis or decision line: these are clean. Before showing them, still read each against Rules 1, 3, 4, 5 and 9; if one slips, move it to check yourself with the reason. End the lane with one line: "Send all N (tickets ...)?"
4. **check:** one card per ticket with its `flags` in plain words, the draft in full, and a corrected version when a flag is about the wording (for "asks the client for something", the corrected version drops the question and the card names the lookup our team does instead). Never edit silently: a silent edit sent through `send_ticket_reply` would teach the drafter something Sam never said.
5. **decide:** one card each with `why_yours` and the one decision. **internal:** one line each (ticket, client, days quiet, state, task and assignee, `next`). Never draft a client reply for these. **close_quietly:** one line each. **not_listed** and **untriaged:** counts only.
6. **Act on exactly what Sam names** and report each result in one line:
   - "send all" or "send all except 9602 9723": `send_replies` with every ticket under send and tell_them_done as just shown, minus his exceptions, and `sam_said` verbatim. It refuses any flagged ticket not in `checked`, so never put a check ticket there unless Sam named it.
   - "send 9602" for a check ticket: `send_replies(tickets="9602", checked="9602")`, or `send_ticket_reply` with `text` when he edited it. After an edit, show the complete new reply and wait.
   - "ping 9188 9426": `ping_team`, with his note if he gave one.
   - "close 9678", "log 9659", "route 9745 onlyg": the matching tool.
7. **A flag Sam overrules twice, or a lane he moves a ticket out of,** is a lesson: ask for the rule in one line and send it to `teach_agents` in his words.
8. **Walk the queue** only when Sam asks: `walk_queue`, then `route_queue` for what he confirmed, only for tickets whose `engineer` is empty.
9. **Before wrapping up,** `sprint_board` again, since replies land minutes after the sends.

**The deep pass.** A card's `deep_pass` says whether the senior second look ran, what it changed in the first card and why. When Sam disagrees with a card the deep pass already checked, that is a strong lesson: ask him for the rule and send it to `teach_agents` in his words.

**In Progress tickets.** When Sam asks where one stands, or a reply needs "our team is on it" or "it's fixed" with a record behind it, use `ticket_states`. Its states: fixed (live or on dev), shipped, our team is on it, stalled 14+ days (engineer nudged), unassigned, task closed, fell through. Each comes with the line to tell the client; still check that line against Rules 3 and 4 before it goes in a draft.

Decisions by what the card shows (full table in `references/sprint-playbook.md`):
- `engineer` set, or a fix PR open: the one-line acknowledgment ("Thanks, our team is going to look into this."), no timeline, for Sam's "send". Do not route again.
- Needs an engineer but `engineer` is empty: `route_queue` or `route_ticket`, with the escalation reason. Zoho status unchanged.
- `bug`, low risk, no PR: offer `propose_fix`. `design_question`: Sam picks fix, log or answer.
- `works_as_designed`: how-to with exact page and button names and the article link; offer `run_faq` if no article.
- Raw error report or housekeeping: `close_ticket_quietly`, no reply.
- Feature request: Sam's to log with `log_ticket_request`. Data, billing and account operations: flag for Sam.
- Resolution emails are not drafted in the sprint: they send on their own. The ones that got stuck (a guard held them, the on-prod email never fired, an engineer task closed with nobody telling the client) appear on the board under tell_them_done, or under check when there is no resolution draft yet.

**Manual fallback** (only when `sprint_queue` is not in the tool list; say so in one line first): count New and Processing tickets in Zoho, take batches of 25 important first, triage each as a card with a Code check line, present the batch, and execute only what Sam approves, following Mode A steps 5 to 9 and Rule 13. Log each approved send to #vome-agent-log (`C0AMGELBDB2`).

## Logging a feature request: build now or for later

Sam's words decide where it goes, not the verb "log".

- **Build now:** `log_ticket_request(ticket, build=true)` when he says "build", "build now", "I'll take it", "I want to handle this", "put it on my list", or "log it" while saying he will do it himself. It lands in Sam Accepted (Build Now) `901115573620`, assigned to Sam, high priority. That list is his working list: only what he will build himself, soon.
- **For later:** `log_ticket_request(ticket)` when he says "log", "log it", "backlog it", "for the future", "not now". It lands in Raw Intake `901113386484`, unassigned, for him to triage when he chooses.
- **Unclear:** ask one line, "Build now or log for later?" Never guess.
- Always pass `title` (a short capability name in product words, never the client's question) and `module`.
- It checks for duplicates first and logs nothing if any look similar. Show Sam the candidates and say which, if any, is the same request. On his word, `existing_task_id` adds this client to that task (with `build=true`, the task also moves to Sam Accepted), or `confirm_new=true` logs it as new.
- A backlog task he now wants to build himself: `triage_feature_request(task_id, "build_now", priority="high", assign_to_me=true)`.
- The client hears nothing until Sam approves a reply, and the reply says "added to our team review" either way. "Our team is working on it" only when the task is accepted or in progress.

## Mode C: Close this out

When Sam says "Let's close this out":

1. **Summarise** what was sent, routed, logged, closed and left open, and list the commitments made to clients.
2. **Gather lessons** from the session: Sam's corrections, process instructions, recurring issues, new product facts, and anything confirmed in the code (including module map corrections and VomeOS limitations hit). Edits sent through `send_ticket_reply` are learned automatically; a general rule goes to `teach_agents` in Sam's words on his yes, with the lesson number reported.
3. **Draft** a dated Sprint Lessons Log entry, each item a reusable rule tagged with its ticket numbers. Sections: New rules and decisions, Product knowledge captured (say "confirmed in code" where it was), Patterns reinforced (short), Clusters to watch, Conflicts and open items (including suggested VomeOS page corrections), Commitments outstanding. Fact-check against the live Playbook and flag conflicts or duplicates instead of silently adding them.
4. **Show the entry and wait for Sam's yes,** then append it to page `3dztm-2491` with `clickup_update_document_page`, `content_edit_mode: append`.
5. **Report** the open questions that need a decision.

The Playbook, System Configuration and VomeOS pages change only when Sam explicitly asks, and then with `append` unless a full rewrite was requested, so nothing is lost by accident.

## Mode D: Product or code question

For "how does X work", "is this a bug", "is it live yet", "check the code", or a question asked while drafting.

1. Run the lookup order above. Answer from the help centre or UI text when it is clear, with the exact page and button names from `where_in_app`, and cite where the answer came from.
2. When the question is about real behaviour (edge cases, what happens to data, why something is blocked), read the code: map module to repo, search, read the live branch, read the module docstring.
3. Answer in two layers: the product answer in customer words (safe to paste into a reply), then an internal note with the evidence (files, lines, caveats) and anything that would surprise a client. Keep the internal note out of any draft.
4. No help article covers it: offer `run_faq`.
5. "Is it fixed": `fix_status` before anything else. "Where does this In Progress ticket stand": `ticket_states`.
6. If the code reveals a likely defect, say so internally and offer to route it (Mode A) or `propose_fix`. Do not tell the client.

## Style

- **In Slack:** plain, direct, no greeting or sign-off, short.
- **Client drafts:** brief, light, not descriptive. Never explain the cause or apologise for an earlier reply. "Assigned" (not "attached") for sequences, "profiles" (not "accounts") for volunteer records. No offers of a call.
- **Product answers:** VomeOS first, then `references/product-knowledge.md`. If neither settles it, say it is unverified rather than inventing a UI path.

## Reference files

How the whole system fits together: the workflow map at https://claude.ai/artifact/3a7sh6MMwjQ9X2LtJbh2Go.

- `references/sprint-playbook.md`: Playbook v2.1. Critical rules, what the agents do before a sprint, both paths, the decisions table, reply voice, recipients, special patterns, the manual sync table, Slack routing. Read in Mode B or before drafting any client reply.
- `references/agent-workflow.md`: lanes, Agent Fixes, the release chain, incidents. Read when you need to know what the agents already did.
- `references/vomeos-code-guide.md`: VomeOS and sprint tools, repos and live branches, lookup order, fix gate, investigation flow, module map, known limitations, worked examples. Read before any code investigation.
- `references/ids-and-fields.md`: every ClickUp, Zoho and Slack ID, custom field and option IDs, list IDs, statuses, parameter shapes. Read before creating or updating a task.
- `references/templates.md`: ClickUp task description, Zoho private comment, #bug-reports notice, client templates, pre-send checklist.
- `references/product-knowledge.md`: how-to answers and product facts captured in sprints, including facts confirmed in code.

These are a snapshot of the six live pages pulled on October 19, 2026: Playbook v2.1 (with the later Oct 19 Slack routing update), the Sprint Lessons Log through Oct 16, CS Agent Workflow and Handoff (with its v2.1 superseded notes), System Configuration v1.2 (with its v2.1 superseded notes), Engineering Slack Channels and Dev IDs (Oct 19), and VomeOS Code Access v1.0. Where these disagree, the Playbook wins. The live docs are the source of truth.
