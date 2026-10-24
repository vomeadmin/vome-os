# Support Sprint Playbook (snapshot of v2.1)

Condensed from "Support Sprint Playbook, Claude AI Operations Guide", v2.1 (Oct 19, 2026, owner Sam Fagen), including the later Oct 19 Slack routing update. Live doc `3dztm-3311` page `3dztm-1871` wins on any difference.

**Precedence.** Where the Playbook and any other page disagree (System Configuration, Engineering Slack Channels, VomeOS Code Access, CS Agent Workflow and Handoff, or a Lessons Log entry dated before Oct 19, 2026), the Playbook wins. Only a newer dated Lessons Log entry beats it, until that entry is promoted into the Playbook. The other reference files in this skill follow the same order.

## Contents
- What changed from v2.0
- Critical rules
- What runs before Sam sits down
- Two paths
- Running a sprint (agent path)
- Decisions by what the card shows
- Reply voice and the No-Confirmation Rule
- Reply recipients
- Special patterns
- After a fix: the follow-up chain
- Manual path: Zoho and ClickUp sync
- Slack routing
- Routing decisions from the Lessons Log
- Corrections and close-out

## What changed from v2.0

Work starts when a ticket arrives, not when Sam runs a sprint. A bug the agent cannot fix is routed to the engineer automatically. Zoho New or Processing means one thing: the client is owed a reply from Sam. "It's fixed" emails send and close on their own, agent fixes included. The engineer-triggered handlers (user education, needs client info) stay automatic. Every other agent-written client email, acknowledgments included, waits for Sam. #support-sprints is urgent only. Every bug notice Claude posts goes to #bug-reports. New tools: `walk_queue` and `route_queue`. The sprint no longer re-triages tickets to learn their status.

## Critical rules

1. **Never name an engineer in client-facing content.** "Our team" only. Names live in ClickUp and internal Slack.
2. **Client email needs Sam's explicit word, except the automatic sends.** A reply goes out only when Sam's own message in this conversation says to send it ("send", "approve", "go ahead") for that ticket or a range he named, passed verbatim as `sam_said`. Silence, "looks good" or moving on is not approval. If he asks for a change, show the complete new reply and wait; never edit and send in one step. If the customer wrote again since the draft, stop, run `ticket_first_pass` and show the new draft. Acknowledgments always wait for Sam.
   - **Automatic sends (intended):** the resolution email when a fix reaches production (the on-prod handler when an engineer moves a Priority Queue task to `on prod`, and the release watcher when an agent fix is proven live), short and closed automatically; the engineer-triggered user-education and needs-client-info handlers; auth and activation replies. None carry Sam's name: anything sent without his explicit approval is signed "Vome team" (Sam, 2026-10-23). Only what he approves is signed "Sam | Vome team". Nobody moves an Agent Fixes task to `on prod` by hand.
3. **No-Confirmation.** Never confirm, reproduce or diagnose a bug to a client ("we can see", "we identified"). Reading the code is not an investigation.
4. **Never say fixed, resolved, planned or on the roadmap** unless `fix_status` says live or a task backs it. A PR is not a fix. Forward-looking language needs a backlog task or Sam's go-ahead for that ticket. A logged feature request is only "logged with the product team" or "added to our team review"; "our team is working on it" only when the task is accepted or in progress.
5. **Nothing from the code reaches a customer.** No file names, fields, branches, commits or task numbers.
6. **Zoho status means one thing.** New or Processing = this client is owed a reply from Sam. Nothing automatic moves a ticket out of it: routing to an engineer and an agent fix PR both leave the status alone. Only the reply Sam sends moves it: an acknowledgment on work an engineer or the agent is doing moves it to In Progress (with the engineer's team, or with Sam when it waits on his merge); an answer closes it with no closure email; a question back sets Awaiting Client Response. If the customer writes again on an In Progress ticket, triage runs again and it returns to the queue.

## What runs before Sam sits down

1. **Triage.** The triage director triages every new ticket and customer reply: reads the thread and images, checks existing work, reads the live code, writes a card (category, lane, live question, code check with verdict `bug`, `works_as_designed`, `design_question` or `unclear`, why a person is needed, recommendation). The reply drafter writes the draft through the outbound guards. A sweep every 30 minutes catches anything a webhook missed.
2. **Agent fix.** A bug at high or medium confidence with a low-risk fix and an exact location: the fix author writes it, patch safety reviews it (no migrations, auth, billing, dependencies, CI or tests), and a PR opens into the dev branch with an Agent Fixes task.
3. **Automatic routing.** A bug the agent cannot fix (lane `engineer`, or the agent declined) gets a Priority Queue task for OnlyG (backend: data, API, auth, notifications, imports, kiosk backend) or Sanjay (web and mobile), with the agent's findings as the engineer summary and a private Zoho note. No Slack post, no client email, no Zoho status change. A ticket with the same problem as one already routed joins that task. Never routed automatically: design questions, product decisions, feature requests, and data, billing or account operations. Those wait for Sam.
4. **Urgent only interrupts.** Urgent = a very-high account (4,000 CAD+ ARR) or the same problem from a second account. The engineer is tagged in #bug-reports and Sam is pinged in #support-sprints; an urgent agent fix asks Sam to merge now. The Monday autonomy report goes to #vome-agent-log.
5. **Release.** The release watcher (every 15 minutes) proves a fix live (every commit on production, inside a green production build), sends the client's short resolution email and closes the ticket and tasks. Hourly, a fix that shipped untold gets the same. A shipped feature request gets a prepared reply that waits for Sam.

## Two paths

**Agent path (default):** Sam, with the Vome OS sprint tools loaded.

**Manual path:** anyone else (Ron or an engineer handing a ticket over from Slack), or the sprint tools are missing. If `sprint_queue` is not in the tool list, say in one line: "the sprint tools are not loaded: reconnect Vome OS in Settings, Connectors, then start a new chat." The manual path uses Zoho Desk for the queue and threads, `lookup_feature`, `search_code` and `read_code_file` for the first pass, and ClickUp for tasks with the sync table below. Sends still need Sam's explicit word, and nothing on this path closes the loop in Agent Fixes or teaches the agents; say so.

## Running a sprint (agent path)

The tickets have already been triaged and, where it applies, routed or fixed. The sprint tells clients what is already happening. Do not re-triage a ticket to learn its status: `engineer` and `agent_fix` are read live, and each re-triage costs about 0.50 USD. Never pass `refresh` to `ticket_first_pass` unless Sam asks.

1. **Follow-ups first** (`sprint_followups`): resolution drafts a guard held, PRs waiting on Sam's merge, fixes waiting on production, failed release checks, with who moves next.
2. **Count the queue** (`sprint_queue`) in one line: ready to send, fix first, needs decision, close quietly, nothing owed, held, untriaged.
3. **Batches of up to 25.** Important clients first (Enterprise, Ultimate, or Combined ARR over 1,500 CAD), then longest waiting. Group one account's tickets together, and tickets joined to the same engineer task together.
4. **One card per ticket:** ticket number, account, IMPORTANT flag, time waiting; the live question and two or three lines of situation; who has it (`engineer` with its live status, or `agent_fix`); the agents' first pass (lane, verdict and finding, recommendation); the reply in full exactly as the customer will get it, with To and Cc, never summarised; the one decision needed.
5. **Sam's short commands** ("send 1 to 6", "edit 4: ...", "log 9", "close 12"). Act on exactly what he named and report each result in one line.
6. **Walk the queue** when Sam asks: `walk_queue`, section by section, urgent first, one line per ticket (who has it and why, what reply it still needs). Ask Sam to confirm or correct each section. A correction that applies beyond one ticket is a lesson (`teach_agents`, in his words). Then `route_queue` for what he confirmed. Tickets under `old_card` were triaged before lanes existed: offer to re-run only those with `ticket_first_pass` and `refresh` (about 0.40 USD each), on Sam's yes.
7. **Before wrapping up,** `sprint_queue` again (no re-triage), since client replies often land minutes after the sends.

## Decisions by what the card shows

| Situation | Action |
|---|---|
| `engineer` set (already with OnlyG or Sanjay) | Send the acknowledgment, no timeline. Do not route again. Sending it moves Zoho to In Progress; the client hears again when it ships |
| Fix PR open (`agent_fix`) | Send the acknowledgment. Sam merges into the dev branch; the resolution then sends itself once live |
| Needs an engineer but `engineer` is empty | `route_queue` for that ticket (or `route_ticket`), backend to OnlyG, frontend and mobile to Sanjay. State the escalation reason. Any Slack notice goes to #bug-reports |
| `bug`, low risk, no PR | Offer `propose_fix` |
| `design_question` | Sam chooses: change it (`propose_fix`), log it (`log_ticket_request`), or answer as how it works |
| `works_as_designed` | How-to answer from the help centre and UI text, exact page and button names, article link. If the answer came from code with no article, say so and offer `run_faq` |
| Raw error report, chunk-load error, page crash | Agent hotfix, `close_ticket_quietly`, no reply. If the agent cannot fix it, route to Sanjay silently |
| Housekeeping ("thanks, it works", out-of-office, an error the code already handles) | `close_ticket_quietly`, no reply |
| Feature request | Always Sam's to log: `log_ticket_request` (posts to #vome-feature-requests). Sam decides log, build (Sam Accepted, Build Now) or no. Never logged automatically. Nothing promised before that |
| Data, billing, account operations (lookups under another email, merges, data fixes, contracts) | Flag for Sam, who handles it in Zoho. No action |
| Notification delay | Draft a Slack DM to OnlyG to check the endpoint logs and the Celery queue, rather than a bug task |

## Reply voice and the No-Confirmation Rule

Brief, light, professional, not descriptive. For a bug: "Thanks, our team is going to look into this." Never explain the cause or what we checked. Never apologise for or mention an earlier reply; at most: "Apologies for that. Our team is going to review this once again and see what more we can find." Ask the smallest question that unblocks the work; ask app versus website only if it changes what we do. Never name anyone on the team. Never judge an attachment ("super clear"); just thank them for it. Do not offer calls: relationship and subscription matters go to the account representative, technical details come through support. Resolution emails are two or three short sentences, worded for the client rather than as a template.

Start "Hi <first name>," (French: "Bonjour <first name>,"). End with:

```
Sam | Vome team
support.vomevolunteer.com
```

The customer's language, French with every accent; the sign-off stays in English. Translating Sam's approved English text faithfully is not a content change; anything beyond a faithful translation goes back for review. Plain text. No em dashes anywhere.

Never say: "we can see the issue", "we've confirmed the bug", "we've identified the problem", "the issue is with [component]", "we can see [user]'s account". Never tell a client their data is safe before it is verified; use future tense ("we will be checking") and keep the recoup instruction in the internal task.

Reviewer wording: Sam's verbatim text goes out with only greeting and signature. An instruction ("tell her that...") gets composed and shown first. Hold and ask on obvious errors (wrong name, fragment). Say "assigned" for sequences and "profiles" for volunteer records.

## Reply recipients

Reply to the person who actually wrote in, not the ticket contact (widget tickets can carry the volunteer as contact while an admin wrote in). Include the latest sender, their cc, and the ticket's secondary contacts; Zoho never adds those automatically. Never pull volunteer or third-party addresses from earlier in a forwarded chain. `send_ticket_reply` enforces this; on the manual path, read `secondaryContacts` before every send. Kimberly Crest replies CC janetmiller1320@gmail.com; Wellspring replies CC the listed secondary contacts (verify in the ticket).

## Special patterns

- **Third-party reports with no volunteer email** (the Bonnie pattern): ask for the volunteer's email, the form or opportunity name, and what happens when they try. No task until then.
- **Out-of-office replies and emoji reactions** are not client replies.
- **Misdirected:** "Thanks for reaching out! Vome is a technology company that helps organizations streamline their volunteer management operations. We are not the organizer of the event itself. For [activity], we recommend reaching out to [org] directly." Close after sending.
- **Account deletion:** "You can delete your account directly from Vome by going to your Settings page and selecting the Delete Account option. If you run into any trouble with that, just let us know and we'll help."
- **No-Zoho ClickUp tasks created by Ron:** when the fix is on prod, DM Ron (U01B6D235N1) with the link and close the task.
- **Duplicate accounts:** "can't see opportunities", "can't log in", "no account found" may be a second active account under another email. That is a merge (Sam's account operation), not a bug.
- **Existing work first:** ticket number, then the client's open tasks, then the symptom. Include closed tasks; reopen rather than duplicate when account and symptom match.
- **Group by root cause:** 2+ tickets with the same symptom in 24 hours, or a match to a live Sentry issue, is an incident (one fix task, one child per ticket).
- **Data integrity outranks display.** Use error-report build IDs to tie tickets across accounts.
- **"Several issues":** ask for the list.
- **Zoho API does not accept "Escalated"** as a status.

## After a fix: the follow-up chain

1. PR open: Sam merges it into the dev branch (`development` for core, `develop` for web). Agents never merge.
2. Merged: the Agent Fixes task moves to `on dev` by itself.
3. Production: Sam deploys web; OnlyG merges core to production.
4. Live: the release watcher proves it, sends the short resolution email and closes the ticket and tasks. A draft a guard held appears in `sprint_followups` instead.

An engineer's fix follows the same end: when the engineer moves the Priority Queue task to `on prod`, the on-prod handler emails the client and closes the ticket. A multi-repo fix is live only when every commit is on production. Never set `on prod` yourself.

## Manual path: Zoho and ClickUp sync

| Action | Zoho status | Zoho owner | ClickUp |
|---|---|---|---|
| Route to an engineer (no reply yet) | unchanged (New or Processing: the client is still owed a reply) | unchanged | `queued`, one assignee, private Zoho note |
| Acknowledgment sent on routed work | In Progress | Frontend Team or Backend Team, never Support Agent | `in progress` |
| Question sent to the client | Awaiting Client Response | unchanged | `awaiting client` |
| Answer or resolution sent | Closed | unchanged | closed |
| Close with no reply | Closed, `disableClosureNotification=true` | unchanged | close if exists |

Status changes that depend on a reply wait until it is sent. Zoho takes plain string values; `sendReply` sets status but not owner; Zoho does not accept "Escalated" through the API. Read each task back to confirm the custom fields saved. If Zoho is already In Progress and its task is `queued` or `on dev`, set the task to `in progress`; leave `needs client info`, `awaiting client`, `escalated`, `on prod`, `sleeping` and closed tasks alone and flag the mismatch.

## Slack routing

The agents' automatic routing posts nothing in Slack. Everything Claude posts goes by what it is:

| Channel | ID | What Claude posts |
|---|---|---|
| #bug-reports | C0CAJ269RAQ | Every bug notice: urgent pings, tickets Sam routes in a sprint, manual-path and Slack hand-offs, Ron's field reports, JavaScript error reports. Tag the engineer: OnlyG for data, API, auth, notifications, imports, kiosk backend, backend logic; Sanjay for UI, mobile, forms rendering, frontend behaviour. One-line summary, likely cause in one line, ClickUp link, Zoho link |
| #vome-feature-requests | C0ALL4VPWK0 | Feature requests from tickets (`log_ticket_request`), backend hand-offs (frontend work waiting on the backend), new functionality requests |
| #feature-releases | C0BCSQKM2N8 | Newly shipped or updated UI/UX, with the branch and what to test |
| #support-sprints | C0CA1QB2XSQ | Sam's ping for urgent work only |
| #vome-agent-log | C0AMGELBDB2 | Send log and the Monday autonomy report |

#eng-backend (C0AQ5U0GXEF) and #eng-frontend (C0AQ5TWQPTM) no longer receive bug notices. A notification-delay check is still a direct DM to OnlyG. When it is unclear whether something is a bug or a request, ask in one line.

## Routing decisions from the Lessons Log

Still current unless the Playbook says otherwise:
- Backend (OnlyG): notification rendering and templates, notification time offsets (compare admin and volunteer language and timezone settings), cross-org form or missing-from-database investigations, imports.
- Frontend (Sanjay): time-off where "cancel shifts" leaves reservations, duplicate hours across kiosks, hours missing from a report, password validator mismatch, sequence file-upload step not saving, chat stability (OnlyG confirms backend).
- Merges and data fixes are Sam's account operations (Playbook v2.1), not engineer tasks by default.
- Volunteers asking to book or change opportunities go to the organization. Volunteer hour corrections are the organization's (opportunity card, Contact admin).

## Corrections and close-out

Every correction teaches the agents. An edited reply sent through `send_ticket_reply` is learned automatically. A general rule goes to `teach_agents` in Sam's words; tell him the lesson number. A one-off fact about one ticket is not a lesson.

"Let's close this out": summarise what was sent, routed, logged, closed and left open; list commitments made to customers; draft a dated Sprint Lessons Log entry from Sam's corrections and new product facts, and show it before appending it.
