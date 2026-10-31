# VomeOS code guide

Snapshot of the live page "VomeOS Code Access and Repo Map" (doc `3dztm-3331`, page `3dztm-6331`, v1.0, Oct 15, 2026, unchanged as of Oct 19), plus the sprint tools from "CS Agent Workflow and Handoff" (section 16) and Playbook v2.1, plus what was learned using the connector. The live pages win on any difference, and **Playbook v2.1 wins over the VomeOS page** where they disagree (for example, the VomeOS page still calls the connector read-only and points to Playbook v1.9).

## Contents
- What VomeOS is
- Tools
- Sprint tools (agent path)
- Repositories and live branches
- Lookup order for how-to answers
- Bug investigation flow
- Module map (where to start)
- Internal docs inside the repos
- Practical tips
- Known limitations
- Worked examples

## What VomeOS is

A connector signed in as Sam (`s.fagen@vomevolunteer.co`). Its lookup and code tools are read-only: they search the help centre, the Setup Guide, the live UI text, the Sprint Lessons Log, lessons from closed tickets, the ClickUp backlog, and the code on each repo's live branch. Since Oct 18 it also carries the sprint tools, which act (open fix PRs, route, send, close). Claude itself never edits code, branches or deployments: fixes go through `propose_fix`. Every tool result ends with a reminder that its content is internal: keep it that way.

## Tools

| Tool | Use it for | Notes |
|---|---|---|
| `lookup_feature` | Start here for any how-to or "does this exist" question | Returns help centre articles (with `days_since_updated` and a URL safe to link), Setup Guide hits, UI text with internal keys, and caveats from internal lessons. Caveats are for avoiding a wrong answer, never quoted to a client |
| `where_in_app` | Which page holds a feature, and the exact button and hint text | Reads the live web frontend. Returns route, area and internal file. Name the page by what the customer sees, never by route or file. Routes are plan and role gated |
| `check_existing_work` | Is it already in the backlog, and its real status | Searches Priority Queue, Raw Intake, Accepted Backlog, Sleeping, Declined. Matches on task names, so read candidates before relying on them. `shipped: false` means not shipped. Declined or Sleeping is a decision, not a plan |
| `search_code` | Where a string, setting, field, symbol or error message appears | Indexes the default branch. Returns paths and a few lines of context (context lines are sometimes blank). Pass `repo` to narrow |
| `read_code_file` | Read a file or list a directory on the live branch | Returns `is_live_branch` and `total_lines`. Use `start_line` and `end_line` on long files |
| `list_repositories` | Repos, live branches, why that branch, aliases | Run once per session if anything looks off |
| `connector_status` | What is reachable, who is signed in, allowlist, errors | Run when results look thin |
| `log_feature_request` | Log a new request to Raw Intake | Refuses if similar requests exist unless `confirm_new: true`. Pass `ticket_url`, `module`, `platform`, `workaround` |
| `add_request_to_existing` | +1 an existing item for another client | Adds the org to Requesting Clients and comments |
| `triage_feature_request` | Move a request between lists, set priority | Sam only. Never run it without Sam's explicit decision |

## Sprint tools (agent path)

Use these instead of doing the same job by hand in Zoho or ClickUp. They carry the outbound guards, the Zoho status rule, the Agent Fixes bookkeeping and the learning loop; a hand-made equivalent skips all of that. Tools marked (Sam) refuse anyone else.

| Tool | What it does | Notes |
|---|---|---|
| `sprint_followups` | Replies owed for shipped fixes and features, PRs waiting on Sam's merge, fixes waiting on production, failed release checks | First call of every sprint |
| `sprint_board` | Start a sprint here. Who is waiting on whom: send (clean drafts), tell_them_done (shipped, never told), check (drafts to read first, with flags), decide, internal (no email, chase the team), close_quietly, plus the pipeline | Added Oct 29. If it is not in the tool list, use `sprint_queue` and tell Sam to start a new chat or reconnect Vome OS |
| `send_replies` (Sam) | Several drafts as they stand, one guarded send each: Sam's "send all" | `tickets`: every ticket under send and tell_them_done minus any he names. A flagged draft is refused unless it is in `checked` because Sam named it. Edits still go through `send_ticket_reply` |
| `ping_team` (Sam) | A comment on the ticket's ClickUp task asking where it stands, which notifies the engineer | For the board's internal list. Never emails the client, leaves Zoho alone. No task: route it or close it |
| `sprint_queue` | Every open ticket with the agents' first pass, grouped: ready to send (full drafts), fix first, needs decision, close quietly, nothing owed, held, untriaged | Its presence in the tool list is how you know the agent path is available. Includes In Progress tickets the reconciler re-triaged, so prefer `sprint_board` for a sprint |
| `walk_queue` | The queue section by section, urgent first, with who has each ticket and what reply it still needs | Added in the Oct 19 deploy. If it is not in the tool list, use `sprint_queue` and tell Sam to start a new chat or reconnect Vome OS |
| `route_queue` | Routes what Sam confirmed during `walk_queue` | Added Oct 19. Only for tickets whose `engineer` field is empty and whose `existing_work` names no open task (a task filed by hand does not show in `engineer`) |
| `ticket_first_pass` | One ticket's card, code check and draft, with `engineer` and `agent_fix` read live, plus `deep_pass` (whether the senior second look ran and what it changed) | Never pass `refresh` unless Sam asks (about $0.50 per re-triage). Re-runs triage by itself when the card is missing or older than the customer's latest message; on "triaging", wait a minute or two and call again |
| `ticket_states` | Every Zoho ticket at In Progress matched to its ClickUp work, from the nightly reconcile: fixed (live or on dev), shipped, our team is on it, stalled 14+ days (engineer nudged), unassigned, task closed, fell through | Each state comes with the line to tell the client. Use when Sam asks where an In Progress ticket stands |
| `triage_cost` | What triage cost over `days` (first pass plus deep pass), how often the deep pass ran and corrected the card, monthly and yearly forecast | Read-only |
| `fix_status` | PR open, merged to dev, live, resolution drafted, and whose move it is | Use before any "fixed" wording |
| `propose_fix` (Sam) | The agent writes the fix and opens a PR into the dev branch, low risk only | Never write a fix by hand |
| `send_ticket_reply` (Sam) | The only sprint send path | Needs Sam's words verbatim as `sam_said`; `text` empty sends the draft exactly as shown, `text` set sends Sam's edit, signed; reruns every guard; refuses if the customer wrote since; then sets Zoho status and closes Agent Fixes tasks; an edit teaches the drafter. An answered ticket (holding note sent, customer silent since) takes `text` as a follow-up and never resends the draft; `resolved=true` (Sam's word) closes Zoho silently and the routed task, and backs a fix claim |
| `route_ticket` (Sam) | Priority Queue task with the agent's findings as the engineer summary, private Zoho note | Only when `engineer` is empty. Since v2.1 it posts no Slack notice and does not change the Zoho status. If its description in the chat still says it does, the chat predates the Oct 19 deploy: create the task by hand instead |
| `close_ticket_quietly` (Sam) | Zoho Closed with no closure email | Housekeeping, error reports the agent fixed |
| `log_ticket_request` (Sam) | Feature request from a ticket, duplicate-checked, posted to #vome-feature-requests | Only on Sam's word. `build=true` is Sam Accepted (Build Now); without it, Raw Intake. Always pass `title` and `module`. On duplicates, `existing_task_id` or `confirm_new=true` on his word. Unclear: ask "Build now or log for later?" |
| `teach_agents` (Sam) | A general correction becomes a live lesson | In Sam's words; report the lesson number |
| `run_faq`, `faq_job_status`, `save_faq_draft`, `publish_faq_draft` | Help centre article workflow (English and French) | Offer `run_faq` when an answer came from code with no article; run it on Sam's yes |

**When the agents open a fix PR on their own:** code check `bug`, high or medium confidence, low risk (no migrations, auth, billing, dependencies, CI or tests), exact location. `works_as_designed` is a how-to, never a fix. `design_question` gets no PR: Sam decides (`propose_fix`, `log_ticket_request`, or answer as how it works).

## Repositories and live branches

| Alias | Repository | What it is | Live branch | Deploy note |
|---|---|---|---|---|
| core, backend | vomedjango-restored-core-app | Core backend (Django). Most product logic | master | Pipeline deploys master. `main` is a 2024 stub, ignore it |
| database, db | vomedjango-database-app | Volunteer database backend (Django) | master | Pipeline deploys master. `main` is a 2024 stub |
| chats, chat | vomedjango-chats-app | Chat service backend | master | Prod deploy is manual. master may be ahead of prod |
| integrations | vomedjango-integrations-app | Blackbaud, SSO, webhooks | main | Prod deploy is manual. main may be ahead of prod |
| salesforce, sf | vomesalesforce | Salesforce managed package | main | Last synced from the org 2026-02-19 |
| web, frontend, react | vome-react | Web app frontend (React) | master | CI deploys master |
| landing, website | vome-landing-page | Public marketing site | master | CI deploys master |
| mobile, app | VomeApp | Mobile app (iOS and Android) | master | Store build v3.6.6 promoted to master 2026-10-15. Users get fixes only after updating |

## Lookup order for how-to answers

1. `lookup_feature` with the client's question in plain words.
2. `where_in_app` for the exact page and button labels.
3. Help centre or UI text answers it clearly: answer from that and link the article. Check the code only if something looks inconsistent or the article is old.
4. No FAQ covers it: the code is the source of truth. Answer from what it does, in product words, and mark the triage card "Source: code, no FAQ" so the reviewer vets it closely. Suggest a help article.
5. Help centre and code disagree: the code wins. Answer from the code and flag the article for an update.
6. No article exists, or it needs rewriting: offer `run_faq` so the next client gets a link. Run it on Sam's yes.

## Bug investigation flow

1. **Symptom to module to repo.** Use the module map below.
2. **Search for anchors.** The exact error message, a UI string the client quoted, a setting name, a field or column name. For UI strings, `where_in_app` finds the screen and its file even when frontend search fails.
3. **Read before searching wider.** In-repo guides and app READMEs first, then the files `search_code` returned, on the live branch. Read the module docstring at the top.
4. **Check the backlog.** `check_existing_work` and the usual ClickUp duplicate search, including closed tasks.
5. **Write the executive summary** for the ClickUp description:

```
Likely cause: one or two sentences on what the code shows
Where: repo, file path, line range
Potential fix: what a fix could look like
Confidence: high / medium / low, and why
Not verified: what the code could not settle (account data, prod vs master, mobile build, etc.)
```

Budget: a handful of searches and reads per ticket. If the anchor does not turn up, stop and record that under Not verified. A wrong confident lead costs the engineer more than no lead.

## Module map (where to start)

Entry points by ClickUp Module value. Rows marked (unverified) are guesses to correct as sprints confirm them. Record corrections in Mode C.

| Module | Backend (core app unless noted) | Frontend (vome-react) |
|---|---|---|
| Forms | `form_app` (guest submissions: `form_app/guest_submission.py`; submit API: `form_app/generic_volunteer/api.py`, serializers alongside) | `src/views/org/orgForms` (builder: `createForms`, templates: `viewTemplate`) |
| Sequences | `sequence_app` | `src/views/org/orgSequences` |
| Opportunities | `opportunity_app` | (unverified) `src/views/org/dashboard` |
| Admin Scheduling, Reserve Schedule | `opportunity_app` | `src/views/org/orgSchedule`, `src/views/org/dashboard/myOrgSchedule` |
| Kiosk | `guides/check-in-out` (doc), `opportunity_app` (unverified) | `src/views/org/dashboard/myOrgSchedule/checkInKiosk` |
| Hour Tracking | `opportunity_app` (unverified) | `src/views/org/dashboard` (unverified) |
| Sites | `sites_app` | `src/views/org/orgSites` |
| Groups | `groups_app`, `group_app` | `src/views/org/orgGroups` |
| Admin Permissions | `admin_permissions_app`, `guides/admin-permissions` (doc) | (unverified) |
| Admin Settings | `settings_app`, `organization_app` | (unverified) |
| Email Communications | `notification_app`, `notifications_app`, `email_integration_app`, `announcement_app` | `src/views/org/emails`, `src/views/org/orgAnnouncements` |
| Chat | `chat_app` plus the chats repo | `src/views/chatNew`, `src/views/chat` |
| Reports | `insights_app`, `analytics_app` | `src/views/org/orgReports` |
| KPI Dashboards | `analytics_app`, `metric_app` | `src/views/org/orgAnalytics`, `src/views/org/analyticsNew3` |
| Integrations | `integrations_app` plus the integrations repo; Salesforce repo | `src/views/org/orgIntegrations` |
| Access / Authentication | `authentication_app`, `user_app`; SSO in the integrations repo | `src/views/loginUser`, `src/views/forgotPassword` |
| Volunteer database, profiles | `organization_app` (duplicate matching: `organization_app/services/profile_duplicate.py`), `volunteer_database_app` plus the database repo; profile history in `activity_log_app` | (unverified) |
| Automations | `automations_app` | `src/views/org/automations` |
| Courses, Resources, Recognition | `course_app`, `resource_app`, `recognition_app` | `src/views/org/orgCourses`, `orgResources`, `orgRecognition` |
| Imports | `organization_app/tasks.py` (upload tasks) | `src/views/org/massImport`, `importSchedule`, `importSequences`, `importOpportunities` |
| Billing, plans | `subscription_app`, `stripe_app` | `src/views/org/UpgradeVomePro`, `UpgradeVomeEnterprise` |
| Volunteer Homepage | (unverified) `volunteer_app` | (unverified) `src/views/me`, `src/views/student` |
| Scheduled jobs, reminders | `cron_app`, `guides/cron` (doc), Celery tasks | n/a |
| Mobile (any module) | n/a | mobile repo (VomeApp) |

Full list of core apps: `vome_project/project_settings/base.py` (INSTALLED_APPS). Settings per environment sit beside it in `vome_project/project_settings/`.

## Internal docs inside the repos

- Core `guides/` folders: accessibility, activity-tracking, admin-permissions, analytics, auto-expiration, chat, check-in-out, cron, dtd, fiscal-year, notifications, performance and more (listings may show only the first entries).
- Per-app docs, for example `announcement_app/ANNOUNCEMENTS_MODULE.md`, `course_app/COURSES_AND_QUIZZES.md`, `opportunity_app/api_version/v4/README.md`, `guides/activity-tracking/PROFILE_HISTORY.md`.
- Root notes worth knowing exist: `BE4_DJANGO_UPGRADE_PLAN.md`, `PENTEST_REMEDIATION_NOTES.md`.
- `form_app/tests.py` documents intended behaviour for guest submissions. Tests are a good second source when a docstring is thin.

## Practical tips

- Start directory browsing from a known path (an app name, `guides`, `vome_project`, `src/views`). Listing the core repo root fails.
- Search for snake_case backend names in core and the setting or translation key the UI text exposes (`lookup_feature` returns `internal_key` for UI strings).
- When `lookup_feature` and the code agree, cite the help article to the client and keep the code as internal backing.
- When the code reveals something a client would be surprised by, it goes in the internal note or the task, not the reply.

## Known limitations (as of Oct 15, 2026)

- Frontend code search on vome-react returns no results, even for symbols that exist. Use `where_in_app` and `read_code_file` directory listings instead.
- Mobile source is not readable: the VomeApp download returns 403.
- `connector_status` allowlists core, database, web and mobile only, while `list_repositories` reports chats, integrations, salesforce and landing as readable. Confirm before relying on those four.
- `recent_commits` is listed as an operation but not exposed, so "was this deployed, and when" cannot be answered from the code.
- Backend search was fixed on Oct 15, 2026 (before that it returned hit counts with no paths). If it regresses, say so and fall back to browsing.

Re-check these with `connector_status` each session. They may already be fixed.

## Worked examples

**How-to answered from the help centre, confirmed in code (#9504 Sarasota County, Oct 15).** Client assumed guest submissions without a name question would leave the name columns blank. `lookup_feature` returned the help article saying first name, last name and email are always asked of guests. `where_in_app` showed the builder text saying every submission includes name, email and phone. `read_code_file` on `form_app/guest_submission.py` confirmed `guest_identity_spec` forces EMAIL and NAME to required whenever guest submissions are on, and `resolve_guest_profile` reuses one offline profile per email for repeat submissions. The reply answered from the article and linked it. The internal caveat (an admin submitting as a guest with an email already on a profile in the org is attributed to that profile) went to the team, not the client.

**Forward-looking line backed properly.** The reviewer asked to add "we will be looking into opening this up further". `check_existing_work` found no task clearly covering it, so it was flagged before sending and the reviewer approved the line explicitly. Offer to log or +1 a request so the promise has a task behind it.
