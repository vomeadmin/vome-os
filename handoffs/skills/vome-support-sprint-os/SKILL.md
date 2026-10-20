---
name: vome-support-sprint-os
description: Vome's support sprint operating guide with VomeOS code access (supersedes vome-support-sprint). Covers Zoho Desk tickets, ClickUp tasks, engineer hand-offs in Slack, client reply drafts, and reading Vome's repos, help centre and backlog through the VomeOS connector. Use whenever someone shares a Zoho Desk ticket link or number, says "support sprint", "triage the queue", "take this", "route this", "draft a reply", "let's close this out", or asks to create, log or route a ClickUp task or check for a duplicate. Also use whenever someone asks how a Vome feature works, whether something is a bug or works as designed, where something lives in the code, what the backend or frontend actually does, what to tell the engineer, or wants a customer how-to answer, even if they never mention the sprint or VomeOS.
---

# Vome Support Sprint (with VomeOS)

This skill runs Vome's support operations: Zoho Desk tickets in, ClickUp tasks and engineer hand-offs out, client replies drafted for approval. It is Ron's original support sprint skill rebuilt with the VomeOS connector, which reads the help centre, the live UI text, the product backlog, the Sprint Lessons Log and the code on each repo's live branch.

VomeOS changes two things. How-to answers can be checked against what the product actually does, and when no FAQ exists the code is the source of truth. Bug hand-offs can tell the engineer the likely cause and a potential fix instead of only the symptom. Everything else (approval, tone, sync rules) stays as it was, because those rules exist to protect clients and they still apply.

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
- **Precedence:** the Playbook page first (it wins over the other pages, except a newer dated Lessons Log entry), then the other live pages, then `references/`, then memory. If the live docs disagree with the request, follow the docs and say so in one line. If they disagree with `references/` or with this skill, use the docs and mention the saved copy is stale.
- **If a fetch fails:** work from `references/` and say once that you are using an old saved copy (Playbook v1.9, Lessons Log through 2026-10-09, VomeOS page v1.0). It predates the agent workflow: never route, change Zoho status or send on its authority without Sam confirming.

Load VomeOS tools with `tool_search` ("VomeOS"). If code results look thin or empty, run `connector_status` before concluding anything, and say what is unreachable.

## Pick the mode

| Signal | Mode |
|---|---|
| A Zoho Desk link or ticket number plus "create a task", "log this", "route this", "can you take this" | A: Ticket to task |
| "Support sprint", "triage the queue", "run the batch", "what's waiting", "walk the queue" | B: Sprint loop |
| "Let's close this out" | C: Close out |
| "How does X work", "is this a bug", "check the code", "what should I tell the engineer", a how-to question with no ticket | D: Product or code question |
| A single ticket, "draft a reply", "how do I answer this" | Triage that one ticket with the card format in `references/sprint-playbook.md` |

## Rules that never bend

These come from the Playbook's critical rules and the Lessons Log. Each one exists because it was broken at least once.

1. **No engineer names in client-facing content.** Say "our team" or "our engineering team". Engineer names are fine in ClickUp comments and internal Slack.
2. **No client email goes out without explicit approval.** Silence is not approval, and moving to the next ticket is not approval. Approval is words like "send", "approved", "send 1 to 10", "send this off". In Mode A nothing is ever sent to a client. Zoho comments you add are private (`isPublic: false`).
3. **No-Confirmation Rule.** Never tell a client we can see, reproduce or have identified a bug before engineering has investigated it. Reading the code is not engineering investigation, so a finding in the code never changes what the client is told.
4. **Never say fixed, resolved, on the roadmap, or "your data is safe" unless a verified task or check backs it.** A change on the live branch is not proof it is deployed or that it matches the client's symptom. Forward-looking lines ("we will be looking into this") need a backlog task or the reviewer's explicit go-ahead for that ticket.
5. **Nothing from the code reaches a client in code terms.** No file paths, function or component names, translation keys, branches, commits or task IDs. Describe the product in the words the customer sees on screen.
6. **Check for an existing task before creating one.** Run `check_existing_work` and search ClickUp, including recently closed tasks in Done. Closed tasks with a live symptom are common: reopen rather than duplicate.
7. **One assignee per task.**
8. **Sign-off is "Sam | Vome team" followed by support.vomevolunteer.com.** Never "Vic". In French replies the sign-off stays in English.
9. **No em dashes anywhere**, in Slack, Zoho, ClickUp, drafts or doc edits. Use periods, commas, parentheses or "and".
10. **Reply in the client's language**, with full accents for French. A faithful translation of approved text is not a content change.
11. **Match the audience of the latest client message, and read the ticket's `secondaryContacts` field before every send.** Zoho does not auto-CC them. Reply to the person who actually wrote in, not the ticket contact record (widget tickets can carry the volunteer as contact while an admin wrote in).
12. **Closing a ticket without a reply:** set `disableClosureNotification=true`, otherwise Zoho sends an unapproved closure email.
13. **Zoho New or Processing means the client is owed a reply from Sam.** Routing a ticket or opening a fix never moves it out of that status. Only a sent reply does: an acknowledgment on routed work moves Zoho to In Progress (team owner) and the ClickUp task to `in progress` together; an answer closes it; a question sets Awaiting Client Response. Until the acknowledgment goes out, a routed task stays `queued` and Zoho stays as it is.
14. **Importance flag:** IMPORTANT CLIENT when the account is Enterprise or Ultimate tier, or Combined ARR is over $1,500 CAD. Otherwise standard.
15. **Show composed text before sending.** Reviewer text written verbatim goes out with only greeting and signature added. When the reviewer gives an instruction ("tell her that...") or asks to add something, show the composed draft and wait for approval. This has caught wrong names more than once.

## VomeOS in one screen

Full reference, the repo table, the module map and known limitations: `references/vomeos-code-guide.md` (mirrors the live page `3dztm-6331`).

**How-to answers (lookup order)**
1. `lookup_feature` with the client's question in plain words.
2. `where_in_app` for exact page names and button labels.
3. Help centre or UI text answers it clearly: answer from that and link the article. Check the code only if something looks off or the article is old.
4. No FAQ covers it: the code is the source of truth. Answer from what it actually does, in product words, and mark the card "Source: code, no FAQ" so the reviewer vets it closely. Suggest a help article.
5. Help centre and code disagree: the code wins. Flag the article for an update.

**Reading code correctly**
- `search_code` indexes each repo's default branch. Always open the hit with `read_code_file`, which reads the live branch (`is_live_branch: true`), before relying on it.
- Read the top of a file first. Core modules usually open with a docstring stating the design decision. Check `guides/` and app READMEs before deep-diving.
- Chats and integrations deploy to prod manually, so their live branch may be ahead of production. Mobile fixes reach users only with the store build.

**Engineer executive summary** (goes in the ClickUp description, see `references/templates.md`)
- Likely cause, Where (repo, path, lines), Potential fix, Confidence (and why), Not verified.
- The Slack notice stays short: one-line summary, likely cause in one line, @-mention, ClickUp and Zoho links.

**Backlog tools**
- `check_existing_work` before any create, any "known issue" line, and any forward-looking line. `shipped: false` means not shipped, whatever the status name.
- A request from a sprint ticket: `log_ticket_request` (Sam only), build now or for later, as below. `log_feature_request` is only for a request with no sprint ticket; it logs to Raw Intake. Both refuse if something similar exists, unless `confirm_new` is true after you have read the candidates. `add_request_to_existing` +1s a client on an existing item. `triage_feature_request` works for Sam only.

**Logging a feature request: build now or for later.** Sam's words decide where it goes, not the verb "log".
- **Build now**, `log_ticket_request(ticket, build=true)`: "build", "build now", "I'll take it", "I want to handle this", "put it on my list", or "log it" while saying he will do it himself. It lands in Sam Accepted (Build Now), assigned to Sam, high priority. Sam Accepted is his working list: only what he will build himself, soon.
- **For later**, `log_ticket_request(ticket)`: "log", "log it", "backlog it", "for the future", "not now". It lands in Raw Intake, unassigned, for him to triage when he chooses.
- **Unclear:** ask one line, "Build now or log for later?" Never guess.
- Always pass `title` (a short capability name in product words, never the client's question) and `module`.
- It checks for duplicates first and logs nothing if any look similar. Show him the candidates and say which, if any, is the same request. On his word, `existing_task_id` adds this client to one (with `build`, that task also moves to Sam Accepted), or `confirm_new=true` logs it new.
- A backlog task he now wants to build himself: `triage_feature_request(task_id, "build_now", priority="high", assign_to_me=true)`.
- The client hears nothing until Sam approves a reply, and the reply says "added to our team review" either way.

## Mode A: Ticket to task (the Slack hand-off)

Trigger: an engineer or Ron shares a Zoho Desk ticket link and wants a ClickUp task. The goal is one tidy task with a real lead in it, the Zoho ticket synced, and a short confirmation in the Slack thread. Do all of it in one pass without asking permission for each step. This is the standard pairing, so no approval is needed for the task, the private comment or the status change.

1. **Read the ticket.** Extract the ticket ID from the link (or search by ticket number). Fetch the ticket, then its threads, then each thread as plain text. Read the whole thread, never the description alone. Zoho attachments cannot be read through tools, so note that they exist.
2. **Enrich.** Look up the contact in Zoho CRM by email, then the account, tier and Combined ARR. Apply the importance flag. If the lookup fails, say "tier unknown" and do not guess.
3. **Decide what it is.** Check the special cases below first. Run `lookup_feature` when it might be a how-to or works-as-designed, since a surprising share of "bugs" are documented behaviour. Otherwise it is a bug, routed by the table: backend (data, API, auth, SSO, 2FA, kiosk backend, notifications, backend logic) to OnlyG, frontend (UI, mobile, forms rendering, frontend behaviour) to Sanjay. If the person asking is an engineer, assign it to that engineer. If the ticket clearly belongs to the other team, flag it in one line and follow the engineer's instruction.
4. **Investigate the code.** Map the symptom to a module and repo (`references/vomeos-code-guide.md`), search for anchors (the exact error text, a quoted UI string, a setting or field name), read the hits on the live branch, and form the executive summary. Spend a few calls, not dozens: if nothing solid turns up, say so in the summary under Not verified rather than guessing. A code finding can also change the routing (a frontend-looking symptom caused by a serializer is Backend), so revisit step 3.
5. **Check for duplicates.** `check_existing_work` plus a ClickUp search for the ticket number, org name and symptom across Priority Queue, Raw Intake, Sam Accepted (Build Now), Accepted Backlog, Sleeping, Declined and recently closed tasks in Done. Also check the same symptom on other accounts, and whether the user has a second active account (a duplicate account means a merge, not a bug).
   - **Found an open task:** do not create a new one. Comment with the new ticket link, anything new, and your code findings if the task lacks them. Link tasks if the second ticket is a separate cluster. Then reconcile status (rule 13): if Zoho is In Progress and the task is `queued` or `on dev`, set it to `in progress` and say in the confirmation what it was before. If the task is `needs client info`, `awaiting client`, `escalated`, `on prod`, `sleeping` or closed, leave it and mention the mismatch.
   - **Found a closed task for a live symptom:** reopen or update it and say the closure may not have held.
6. **Create the task** in Priority Queue `901113386257` with the custom fields from `references/ids-and-fields.md`: Type, Platform, Module, Source (Zoho Ticket), Zoho Ticket Link, Highest Tier and Combined ARR when known, and Tags when one clearly applies. Mirror recent Priority Queue title style. Use the description template in `references/templates.md`, including the executive summary. Priority: urgent for data integrity, locked-out paid accounts, or a hard client date; high for IMPORTANT CLIENT; otherwise normal. Status `queued` until the client has been acknowledged (rule 13). Triage often routes bugs automatically: if the ticket already has an engineer task, report it instead of creating one.
7. **Read the task back** and confirm the status and custom fields saved. A blank Module field has happened when a dropdown ID was invalid. Fix anything blank.
8. **Sync Zoho.** Private comment (`isPublic: false`, HTML) linking the task. Leave the status as it is: the client has not been told yet (rule 13). When the acknowledgment is sent (by Sam, from the sprint), set In Progress with the owner Frontend Team (`569440000023159001`) or Backend Team (`569440000023160001`), never Support Agent, and move the task to `in progress`. Plain string values only. `sendReply` sets status but not owner, so owner is always a separate update. Zoho does not accept "Escalated" as a status through the API. Spot-check the ticket afterwards.
9. **Confirm in Slack**, in the thread where you were asked, using the format below. If the request came from a DM or another channel, also post the concise engineer notice in #eng-frontend (`C0AQ5TWQPTM`) or #eng-backend (`C0AQ5U0GXEF`).
10. **Do not contact the client.** If an acknowledgment would normally follow, offer in one line to draft it for approval. "Route this" is never approval to send the ack.

### Special cases

| Situation | What to do |
|---|---|
| Raw JavaScript or console error ("Vome Error Report", stack trace, TypeError) | An agent hotfix: the agent fixes it and the ticket closes quietly with no client reply. Only if the agent cannot fix it, a silent frontend task for Sanjay. Include the build ID, since it ties tickets across accounts. Any tier. |
| Vague bug, or a third party reports for volunteers with no volunteer email (the Bonnie pattern) | No task. Tell the requester what is missing (volunteer email, form or opportunity name, what happens when they try) and offer to draft an info-request. |
| Feature request | Run `lookup_feature` and `check_existing_work` first, since it may already exist or already be logged. Bring the evidence to Sam: logging is always his call, never automatic. Only when he confirms, log it with `log_ticket_request`: `build=true` for build now (Sam Accepted `901115573620`), otherwise Raw Intake `901113386484` for later (see "Logging a feature request"). |
| How-to or admin configuration fix | No task. Use the VomeOS lookup order and `references/product-knowledge.md`, and offer a draft answer. |
| Notification delays | Not a bug task by default. The reviewer prefers a direct Slack DM to OnlyG asking him to check the endpoint logs and the Celery queue. |
| Billing, account transition, license restructure | Goes to the reviewer. No engineering task, no auto-reply. Relationship and subscription matters go to Ron. |
| Misdirected ticket (the client thinks Vome organizes the event) | No task. Offer the misdirected template. |
| Account deletion request | No task. Direct to Settings, then Delete Account. |
| "Can't see opportunities", "can't log in", "no account found" | Check for a second active account under another email first. If found, it is a merge (Backend). |
| Time zone or date offset symptom | Check the timezone and DST cluster in the Lessons Log and link the existing tasks. |
| Ticket already has a ClickUp Zoho Ticket Link | Treat as existing task. Report it, do not recreate. |

### Slack confirmation format

A few lines, no greetings or thanks. In a thread or channel, tag the person who asked; in a 1:1 DM, do not. Slack link syntax. Slack is internal, so engineer names, file paths and technical detail are fine here.

```
Task created: <https://app.clickup.com/t/TASKID|Short task title>
Assigned to @Sanjay. Web, Forms, Bug, priority normal. Org X is Enterprise (important client).
Likely cause: [one line from the executive summary] ([repo] [path] L[x]-[y]). Details in the task.
Private note added on Zoho #9339. Status left as it is until the client is acknowledged.
No client reply sent. Want me to draft the ack for approval?
```

If a duplicate was found:

```
Already tracked: <https://app.clickup.com/t/TASKID|Existing task title> (open, assigned to @OnlyG).
I added Zoho #9340 to it as a comment with the code findings and left Zoho unchanged. No new task created.
```

Never end on a recap longer than the confirmation itself.

## Mode B: The sprint loop

Triggered by "Let's run a support sprint", "We're on a support sprint" or "walk the queue". Follow the Playbook's "Running a sprint (agent path)" section; it is the source of truth. In short:

- **The agents have already done the processing.** Every ticket was triaged when it arrived and again on every customer reply; bugs the agent can fix have a PR, and bugs it cannot are already with OnlyG or Sanjay. The sprint tells clients what is already happening. Do not triage from scratch and do not re-triage to learn a status: use `sprint_followups`, `sprint_queue` and `walk_queue`, which read it live. Pass `refresh` to `ticket_first_pass` only when Sam asks.
- **Sends go through `send_ticket_reply` only,** on Sam's explicit word, passed verbatim. Never send a sprint reply through Zoho directly: that skips the guards, the status rule and the learning.
- **Acknowledgments are one line:** "Thanks, our team is going to look into this." No timeline.
- **Feature requests** are presented to Sam to log; never logged without him.

**Manual fallback** (only when `sprint_queue` is not in the tool list; say so in one line first): count New and Processing tickets in Zoho, take batches of 25 important first, triage each as a card with a Code check line, present the batch, and execute only what the reviewer approves, following Mode A steps 4 to 8 and rule 13. Log each approved send to #vome-agent-log (`C0AMGELBDB2`). Nothing on this path closes the loop in Agent Fixes or teaches the agents.

Reviewer wording rules from past sprints:
- Reviewer text goes out verbatim with only greeting and signature. Show any other change before sending.
- Obvious error in reviewer text (wrong client name, fragment): hold and ask in one line.
- One-line instruction ("tell them we are going to further investigate"): compose close to verbatim and show it first. Put the fuller technical lead in the engineering task.

## Mode C: Close this out

When the reviewer says "Let's close this out":

1. **Gather** from the session: edits to drafts, process instructions, recurring issues, new product facts, and anything confirmed in the code (including module map corrections and VomeOS limitations hit).
2. **Draft** each as a reusable rule, tagged with the ticket numbers that prompted it. Product facts confirmed in code say so ("confirmed in code").
3. **Fact-check** against what happened and the live Playbook. Flag conflicts or duplicates instead of silently adding them.
4. **Append** a dated entry to the Sprint Lessons Log (page `3dztm-2491`) with `clickup_update_document_page`, `content_edit_mode: append`. Sections: New rules and decisions, Product knowledge captured, Patterns reinforced (short), Clusters to watch, Conflicts and open items, Commitments outstanding. List suggested VomeOS page corrections under Conflicts and open items rather than editing that page.
5. **Report** a point-form summary and the open questions that need a decision.

Write to the Lessons Log only on this command. The Playbook, System Configuration and VomeOS pages change only when the reviewer explicitly asks for it, and then use `append` unless a full rewrite was requested, so nothing is lost by accident.

## Mode D: Product or code question

For "how does X work", "is this a bug", "check the code", or a question asked while drafting.

1. Run the lookup order above. Answer from the help centre or UI text when it is clear, and cite where the answer came from.
2. When the question is about real behaviour (edge cases, what happens to data, why something is blocked), read the code: map module to repo, search, read the live branch, read the module docstring.
3. Answer in two layers: the product answer in customer words (safe to paste into a reply), then an internal note with the evidence (files, lines, caveats) and anything that would surprise a client. Keep the internal note out of any draft.
4. If the code reveals a likely defect, say so internally and offer to route it (Mode A). Do not tell the client.

## Style

- **In Slack:** plain, direct, no greeting or sign-off, short.
- **Client drafts:** plain updates, no throat-clearing. "Assigned" (not "attached") for sequences, "profiles" (not "accounts") for volunteer records. "We will revert back as soon as we have an update" rather than offering a call.
- **Product answers:** VomeOS first, then `references/product-knowledge.md`. If neither settles it, say it is unverified rather than inventing a UI path.

## Reference files

- `references/vomeos-code-guide.md`: VomeOS tools, repos and live branches, lookup order, investigation flow, module map, internal docs in the repos, known limitations, worked examples. Read before any code investigation.
- `references/ids-and-fields.md`: every ClickUp, Zoho and Slack ID, custom field and option IDs, list IDs, parameter shapes. Read before creating or updating a task.
- `references/templates.md`: ClickUp task description (with executive summary), Zoho private comment, engineer Slack notice, client templates, pre-send checklist.
- `references/sprint-playbook.md`: categories, sync table, triage card format, tone rules, reply recipients, statuses, special patterns. Read in Mode B or before drafting any client reply.
- `references/product-knowledge.md`: how-to answers and product facts captured in sprints, including facts confirmed in code.

These are a snapshot of Playbook v1.9 (Oct 15, 2026), System Configuration v1.2 with the Oct 15 VomeOS note, the VomeOS page v1.0, and the Lessons Log through Oct 9, 2026. They predate the agent workflow (Playbook v2.0 and v2.1). The live docs are the source of truth; this skill was last aligned with Playbook v2.1 on October 19, 2026.
