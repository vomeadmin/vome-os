# Vome support agent: workflow map

**Source of truth for how customer success runs end to end.** Every box in
every diagram carries a status class, and every non-live box has a row in the
gap register at the bottom. The published map is generated from this file:

    py scripts/render_workflow_map.py      # writes workflow_map.html

Published map (republish the generated file to this URL, never a new one):
https://claude.ai/artifact/3a7sh6MMwjQ9X2LtJbh2Go

To propose a change: edit a diagram or add a register row, set its status,
and regenerate. A new AI session should read this file, `CLAUDE.md`, and the
ClickUp handoff page (doc 3dztm-3311, page 3dztm-6351) before acting.

**As of:** 2026-10-17

## Status key

| Class | Meaning |
|---|---|
| `live` | Running in production and proven on real traffic |
| `deployed` | Pushed and running, not yet proven on a real case |
| `built` | Written and tested locally, not pushed yet |
| `proposed` | Agreed or recommended, not built |
| `broken` | Broken, blind, or at risk of breaking |
| `decide` | Waiting on a decision from Sam |

<!-- map:diagram id=ticket title="1. A ticket, from email to closed" -->
```mermaid
flowchart TD
  A["Client emails support<br/>or replies"]:::live --> B["Zoho Desk webhook<br/>/webhook/zoho-ticket, /zoho-update"]:::live
  B --> L1["Legacy intake: classify, CRM tier,<br/>internal note, #vome-tickets brief"]:::decide
  B --> L2["Instant auto-ack<br/>(blind, retired: ENABLE_AUTO_ACK off)"]:::built
  B --> S["Shadow triage on the worker<br/>customer_success.shadow_triage"]:::deployed
  S --> S1["Thread + screenshots<br/>(inline images, attachments)"]:::deployed
  S --> S2["Existing work + release state<br/>(ticket no., account, symptom; Agent Fixes)"]:::deployed
  S --> S3["Code check on the live branch<br/>(read-only tools, 10 calls)"]:::built
  S1 & S2 & S3 --> T["Triage director (senior)<br/>card: category, live question, code check"]:::deployed
  T --> I["Incident matcher (fast)<br/>last 24h tickets + Sentry"]:::deployed
  I -->|high confidence| IA["Immediate mention to Sam<br/>in the day thread"]:::deployed
  T --> D["Reply drafter (standard)<br/>every outbound guard"]:::deployed
  T --> F["Ticket fix ladder<br/>bug + high + low risk -> PR"]:::built
  D & F --> C["Numbered card in today's<br/>#support-sprints thread"]:::deployed
  C --> P["Sam talks to the sprint partner<br/>(questions, edits, decisions)"]:::deployed
  P -->|"send (his words)"| SE["Zoho sendReply to sender,<br/>their cc + secondary contacts"]:::deployed
  SE --> CL["Close the loop: Zoho status,<br/>Agent Fixes child and parent"]:::built
  P -->|route| R["Priority Queue task + engineer<br/>Slack notice + Zoho In Progress"]:::deployed
  P -->|log / build| FR["Raw Intake / Sam Accepted"]:::deployed
  P -->|hold / done| H["Carried to tomorrow / finished"]:::deployed
  SE --> M["Measured: sent unedited?<br/>edits logged (autonomy report)"]:::built
```

<!-- map:diagram id=fix title="2. A bug, from code check to customer told" -->
```mermaid
flowchart TD
  CC["Code check says bug,<br/>high confidence, low risk"]:::built --> FA["Fix author writes a diff<br/>(or declines)"]:::built
  FA --> PS["Patch safety: no migrations,<br/>auth, billing, deps, CI, tests"]:::live
  PS --> PR["PR into development / develop<br/>(never merged by an agent)"]:::built
  PR --> AF["Agent Fixes parent + child<br/>Merge Owner set, status in progress"]:::built
  AF --> MG["OnlyG (core) / Sanjay (web)<br/>review and merge"]:::live
  MG --> PM["PR merged: Fix Commit filled,<br/>parent + children to on dev"]:::built
  HM["Fix made in a Claude Code session<br/>(hotfix, merged to dev branch)"]:::live --> OD
  PM --> OD["Agent Fixes parent at on dev<br/>with Fix Commit"]:::live
  OD --> PROMO["Human promotes dev -> master<br/>(batched)"]:::live
  PROMO --> BUILD["Prod build green<br/>(GitHub Actions / Bitbucket pipeline)"]:::live
  BUILD --> W["Release watcher every 15 min<br/>merged + built + live stamp"]:::live
  W -->|all commits live| LIVE["Release Check Live +<br/>'Fix live' post in the day thread"]:::live
  W -->|build failed / 2h stuck| FAIL["Release Check Failed + alert"]:::deployed
  LIVE --> RD["Resolution draft per client,<br/>as a numbered card (retry on error)"]:::broken
  RD --> AUTO{"RELEASE_AUTO_SEND on?<br/>(off; never needs_human)"}:::decide
  AUTO -->|off| SEND["Sam says 'send N'<br/>in the thread"]:::deployed
  AUTO -->|on, draft passed guards| AS["Sent automatically"]:::decide
  SEND & AS --> CLOSE["Zoho Closed (no closure email)<br/>child + parent Closed"]:::built
```

<!-- map:diagram id=sentry title="3. A production error, from Sentry to a fix" -->
```mermaid
flowchart TD
  E1["Django core errors<br/>(prod-vome)"]:::live --> WH["Sentry issue.created webhook"]:::live
  E2["vome-react errors<br/>(DSN not in the build: zero events)"]:::broken
  E3["Chats / integrations services<br/>(no Sentry at all)"]:::broken
  E4["Mobile app (VomeApp)<br/>(source unreadable, 403)"]:::broken
  WH --> G["Gate + redaction + ledger<br/>vomeos_sentry_issues"]:::live
  BF["Daily backfill 08:15<br/>(regressions, missed issues)"]:::deployed --> G
  G --> TR["Sentry triage (cheap)<br/>noise or actionable"]:::live
  TR --> AN["Bug analyst reads the code<br/>frames, or the logged message"]:::deployed
  AN --> FX["Fix author + patch safety<br/>(SENTRY_AUTO_PR_ENABLED on)"]:::deployed
  FX --> PRS["PR into dev branch"]:::deployed
  AN --> AFS["Agent Fixes task,<br/>one per root cause (cap 5/day)"]:::deployed
  AN --> SL["Slack thread per issue,<br/>daily report 09:00"]:::live
  AN --> INC["Feeds the incident matcher<br/>(ticket <-> Sentry)"]:::deployed
```

<!-- map:diagram id=engineer title="4. Engineer-triggered replies (ClickUp status changes)" -->
```mermaid
flowchart LR
  PQ["Priority Queue task<br/>status change"]:::live --> HOOK["ClickUp webhook<br/>(health checked hourly)"]:::live
  HOOK --> ONP["on prod -> resolution<br/>drafted and AUTO-SENT"]:::decide
  HOOK --> NCI["needs client info -><br/>info request AUTO-SENT"]:::decide
  HOOK --> UE["user education -><br/>answer AUTO-SENT"]:::decide
  HOOK --> ESC["escalated -> DM Sam"]:::live
  ONP & NCI & UE --> SIG["Signed 'Sam | Vome team'<br/>(was 'Vic')"]:::built
  ONP & NCI & UE --> OG["Outbound guard before send"]:::live
  ONP & NCI & UE -.-> ST["Route these drafts through the<br/>sprint thread instead?"]:::decide
```

<!-- map:diagram id=feature title="5. A feature request" -->
```mermaid
flowchart LR
  TK["Ticket triaged as<br/>feature_request"]:::deployed --> CARD["Card, no draft<br/>(waits for Sam)"]:::deployed
  CARD --> DEC["Sam: log / build / no"]:::deployed
  DEC -->|log| RI["Raw Intake"]:::deployed
  DEC -->|build| SA["Sam Accepted (Build Now)"]:::deployed
  DEC --> POST["Post in #vome-feature-requests<br/>with client, ARR, ticket"]:::proposed
  CARD --> DUP["Duplicate check before logging<br/>(check_existing_work)"]:::proposed
  SA --> SHIP["When it ships, draft the<br/>client update for Sam"]:::proposed
```

<!-- map:diagram id=learning title="6. Every correction teaches the agents" -->
```mermaid
flowchart TD
  ED["Sam edits a draft or<br/>asks for a redraft (thread)"]:::built --> LK["Lesson keeper: a general rule,<br/>or a fix for this ticket only?"]:::built
  FB["Sam gives general feedback<br/>in conversation"]:::built --> PL["Sprint partner records it<br/>(skill: learning-from-sam)"]:::built
  CC["Sam corrects an agent in a<br/>Claude Code session"]:::built --> CLI["Session records it:<br/>py -m vomeos.lessons add"]:::built
  LK -->|general| LS[("vomeos_lessons<br/>active lessons per agent")]:::built
  PL --> LS
  CLI --> LS
  LK -->|ticket only| NONE["Nothing learned<br/>(names, dates, recipients)"]:::built
  LS --> PROMPT["Added to that agent's prompt<br/>on its next run"]:::built
  LS --> ANN["'Learned L7 ... forget 7 to undo'<br/>posted in the thread"]:::built
  ED --> LOG["vomeos_draft_edits<br/>(before / after)"]:::built
  LOG --> AR["Autonomy report, Mon 09:30<br/>unedited %, edits, live lessons"]:::built
  AR --> PRO["Claude Code session promotes proven<br/>lessons into charters + answer keys"]:::built
  PRO --> EV["Evals per agent (drafter 15/15,<br/>matcher 12/12, partner 12/12,<br/>keeper 10/11, triage 14/16)"]:::live
  PRO -.-> AUTOPR["Promotion as an automatic PR<br/>to support-agent"]:::proposed
  AR --> AUTO["Earned auto-send per category<br/>(50 sent, 95% unedited)"]:::proposed
  KB["Help centre: sync 02:00, health<br/>Mon 09:00, gap pass monthly"]:::live
  NOFAQ["'Code, no FAQ' answers become<br/>article suggestions"]:::proposed
```

<!-- map:diagram id=director title="7. Sam runs the sprint from batches (agreed 2026-10-17, not built)" -->
```mermaid
flowchart TD
  WH["Zoho webhook, plus a queue sweep<br/>every 30 min for missed tickets"]:::proposed --> ST["Shadow triage stores card + draft<br/>(no card posted per ticket)"]:::proposed
  RL["Fix proven live (Agent Fixes Live,<br/>or a linked task on prod)"]:::live --> RS["Prepared reply for every fixed ticket<br/>the client has not been told about"]:::proposed
  ST & RS --> B["Batch: weekdays 9, 13, 17; weekends 11 if needed<br/>one top-level message, previous one superseded"]:::proposed
  B --> FULL["Every ready reply shown in full,<br/>exactly as the client will receive it"]:::proposed
  B --> SEC["Sections: ready to send, fix first, decisions,<br/>close quietly, in flight, waiting on a release"]:::proposed
  FULL --> CMD["Sam: send ready / send 101 106 / edit / hold till /<br/>route / log / build / no / close quiet"]:::proposed
  CMD -->|"send (his words)"| SE["Sent at once, any hour<br/>(no business-hours window)"]:::proposed
  CMD --> CF["Anything open is in the next batch<br/>with its age; permanent item numbers"]:::proposed
  CL["'clear the queue': batches of 10,<br/>important then oldest, next one pre-triaged"]:::proposed --> B
  INC["Incident (high confidence)"]:::deployed -->|only interruption| PING["Immediate mention"]:::deployed
  B --> ESC["What went to engineers and why<br/>(batch footer + Monday report)"]:::proposed
```

## Schedule (America/Montreal, run by Celery beat on `vome-os-worker`)

| When | Job | Status |
|---|---|---|
| every 15 min | `support.release_watcher` (also promotes merged fix PRs and drafts resolutions) | live |
| hourly :30 | `support.clickup_webhook_health` | live |
| 02:00 daily | `support.kb_sync` | live |
| 07:30 daily | `support.stale_sweep` | live |
| 08:00 daily | `engineering.code_access` (credential watch) | live |
| 08:15 daily | `engineering.sentry_backfill` | deployed |
| 08:30 daily | `engineering.sentry_regate` | live |
| 09:00 daily | `engineering.sentry_report` | live |
| 10, 13, 16, 19 weekdays | `support.sprint_update` (to be replaced by batches, G23) | deployed |
| 9, 13, 17 weekdays; 11 Sat and Sun if needed | `support.sprint_batch` | proposed |
| every 30 min | `support.queue_sweep` (G26) | proposed |
| 17:00 daily | `support.daily_digest` | live |
| Mon 07:00, Fri 16:45 | engineering reports | live |
| Mon 09:30 | `support.autonomy_report` | built |
| Mon 09:00 | `support.kb_health_scan` | live |
| Sun 03:00 | `support.knowledge_refresh` | live |
| 1st Tue 09:00 | `support.kb_gap_pass` | live |

Events (on the worker): `customer_success.shadow_triage` (Zoho webhooks),
`customer_success.sprint_command` (Sam's thread messages),
`engineering.sentry_issue` (Sentry webhook).

## Gap register

<!-- map:register -->
| id | area | item | status | why it matters | next step | owner |
|---|---|---|---|---|---|---|
| G1 | release | Resolution drafts made automatically when a fix goes Live | broken | First production pass (Oct 17 14:00 ET) failed for #9415, #9436, #9500: sprint tables did not exist yet | Push the pending batch (table setup + retry up to 3); the next pass drafts all three | Sam |
| G21 | release | Auto-send resolutions once a fix is proven live (`RELEASE_AUTO_SEND`) | decide | Removes the last manual step for clean resolutions | Turn on once the Monday autonomy report shows resolutions going out unedited; never applies to needs_human | Sam |
| G2 | engineer | Engineer-triggered replies still auto-send (on prod, needs client info, user education) | decide | They skip Sam's review; the playbook text says nothing auto-sends | Keep as is, or turn them into sprint thread drafts | Sam |
| G3 | intake | #vome-tickets brief and legacy classifier run beside shadow triage | decide | Two cards per ticket, two places to look | Retire once the sprint thread is trusted (the old reply handling depends on it) | Sam |
| G4 | sentry | vome-react sends nothing to Sentry | broken | Frontend crashes reach us only as tickets | Put REACT_APP_SENTRY_DSN into the build (CRA inlines it at build time) | Sanjay |
| G5 | sentry | Chats and integrations services have no Sentry | broken | Their errors are invisible | Add the SDK to both services | OnlyG |
| G6 | code | Mobile source is unreadable (VomeApp 403) | broken | Code checks cannot read the app | Token with read on samfagen15/VomeApp, add `mobile=` to VOMEOS_CODE_REPO_MAP | Sam |
| G7 | creds | vomeadmin GitHub token expires 2026-10-31 | broken | Release watcher web checks and web code reads stop | Renew, update VOMEOS_GITHUB_TOKEN_VOMEADMIN on worker and web | Sam |
| G8 | data | content_mode email failures (Oct 15 20:11 to Oct 16 16:01 UTC) | broken | Some birthday, bulk and enrollment emails may not have sent | Confirm it stopped; count what failed; add db_default on new NOT NULL columns | OnlyG |
| G9 | feature | Feature requests are not posted to #vome-feature-requests | proposed | Sam's single list of decisions misses them | Post on log/build with client, ARR, ticket | agent |
| G10 | feature | No duplicate check before logging a feature request | proposed | Risk of duplicate backlog items | Run check_existing_work before creating | agent |
| G11 | feature | Client not told when a Sam Accepted request ships | proposed | Promise loop never closes | Watch Sam Accepted for closed tasks, draft the update | agent |
| G12 | incident | Incident alert does not create the fix parent with one child per ticket | proposed | Grouping is manual | On a confirmed incident, create the parent and children | agent |
| G13 | learning | Every correction becomes a live lesson (thread edits, conversation, Claude Code) | built | The same correction kept coming back | Push; then say `lessons` in the thread to see them | Sam |
| G22 | learning | Lesson promotion into charters is done by hand weekly | proposed | Live lessons pile up if nobody promotes them | Have the Monday job open a PR with the proposed charter and answer-key changes | agent |
| G14 | learning | Earned auto-send per category | proposed | The goal state; needs data first | After 50 sent at 95% unedited, Sam approves a category | Sam |
| G15 | triage | Triage answer key was written by an agent, not reviewed | broken | 88% agreement against an unreviewed key | Sam reviews 16 cases once | Sam |
| G16 | docs | CLAUDE.md "state of play" predates the broker cutover | built | New sessions start from wrong facts | Refreshed 2026-10-17 (see CLAUDE.md) | agent |
| G17 | crm | CRM often finds no deal, so ARR shows 0 and tier "low" | broken | Important clients can look unimportant on cards | Map Zoho account to CRM account, not only by email | agent |
| G18 | knowledge | "Source: code, no FAQ" answers do not become article suggestions | proposed | The same question is answered from code each time | Queue a help centre article draft for Sam | agent |
| G19 | slack | First real message in the sprint thread not yet proven | deployed | If Slack does not deliver thread events, Sam's replies go nowhere | Sam replies in the thread once; check the agent answers | Sam |
| G23 | director | Batches replace per-ticket cards and the 10/1/4/7 day thread (weekdays 9, 13, 17; weekends 11 if needed) | proposed | Sam scrolls cards to act; updates list items without the drafts | Build `sprint_batch.py`; shadow triage stops posting cards | agent |
| G24 | director | Permanent item numbers across days | proposed | Carried items get new numbers and their cards stay in yesterday's thread, out of reach of commands | Global number per item; old day-thread numbers keep resolving during the switch | agent |
| G25 | director | Clear-the-queue mode | proposed | 44 open on Oct 17 with only 2 carded; the backlog clears 8 per weekday update | Batches of 10, important then oldest, next batch triaged ahead | agent |
| G26 | intake | Queue sweep every 30 min replaces the 8-per-update backfill | proposed | A missed webhook leaves a ticket invisible until a weekday update | Silent triage of any open ticket with no card, with a cap | agent |
| G27 | director | What still goes to engineers, and why | proposed | Sam cannot see how much escalates or what would prevent it | `escalation_reason` on the card and route; batch footer; Monday report | agent |
| G28 | learning | Trust tiers per category (order and report only) | proposed | Proven categories should need less of Sam | Full reply text is ALWAYS shown (Sam, 2026-10-17); tiers only rank and report | agent |
| G29 | director | `close quiet` housekeeping lane | proposed | "Thanks, works now" and handled error reports still take a decision each | Triage `lane=housekeeping`; Zoho Closed with no closure email, on Sam's word | agent |
| G31 | release | Every fixed ticket the client was not told about gets a prepared reply | proposed | Fixes ship and the client never hears (Oct 9: six closed tasks came back) | Sweep open and awaiting tickets with a live fix and no outbound since; "it has been updated, have a look and let us know" in the next batch | agent |
| G20 | items 1-5 | Close the loop, code checks, ticket fixes, autonomy report, auto-ack off, Sam signature, resolution drafts | built | Not pushed yet | Review the diff, push | Sam |
