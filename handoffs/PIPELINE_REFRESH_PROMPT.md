# Pipeline refresh: get Vome support to a clean, trustworthy state

You are a Claude Code agent working in `support-agent` (the "Vome OS" agent system) for Sam, Vome's founder and the only person who approves client communication. Vome is a volunteer management platform.

**Your goal:** when Sam opens a support sprint in Claude.ai, every ticket's state is true and consistent across Zoho Desk, ClickUp and the sprint store. Every open ticket has a current card, and nothing owed is hidden. "Clean" is measurable, and Phase 4 defines it. Work towards that definition, not towards a list of tasks.

## Read first (in this order)

1. `WORKFLOWS.md`: 12 flow diagrams and the gap register (G1 to G61). It lags the code, so verify any status before relying on it.
2. `CLAUDE.md`, then `handoffs/CLAUDE_AI_SPRINT_PROMPT.md` and `handoffs/PLAYBOOK_v2.1.md`.
3. ClickUp (workspace 3604276):
   - doc 3dztm-3311: page 3dztm-6351 (CS agent workflow) and page 3dztm-2491 (Sprint Lessons Log; read every entry from 2026-10-19 on);
   - doc 3dztm-3331: page 3dztm-6331 (code access and repo map).
4. The code behind each step you touch:
   - `shadow_triage.py` and `deep_triage.py`
   - `ticket_reconciler.py`
   - `sprint_batch.py`: `sync_handled_elsewhere` and `triage_untriaged`
   - `sprint_thread.py`: `_send`, `_after_send` and `_engineer_shipped`
   - `sprint_tools.py`, `backlog_sweep.py` and `ticket_routing.py`

## What is true as of 2026-10-23 evening

These correct older notes, including any pasted context you were given.

- **Triage.** The triage director has 25 tool calls. It can grep and find files in full copies of all six repos, refreshed hourly by `support.code_copies`, and can read ClickUp tasks with `read_task`. When a card is unsure, lacks a lane, sits in the engineer or sam_ops lane, or is for an important client, the deep-pass investigator (`deep_triage.py`, 60 calls) re-checks it. Its card replaces the first one before drafting, fixing and routing. Every run is logged in `vomeos_triage_runs`, and `triage_cost` in Claude.ai reports the cost.
- **Cards older than 2026-10-23 15:36 ET predate all of the above.** Treat them as stale.
- **VomeApp is a fix PR target** (`development`). So are chats and integrations. This is in the `sentry_projects._FIX_TARGETS` default, so no environment variable is needed.
- **Worker variables.** `VOMEOS_CODE_REPOS` and `VOMEOS_CODE_WRITE_REPOS` list all six repos. `VOMEOS_PR_TARGETS` and `VOMEOS_CODE_EXTRA_BRANCHES` are unset, so the code defaults apply.
- **GitHub token.** `vome-os-sentry-write-web` has pull request read and write and expires 2027-10-22.
- **Nightly reconcile at 06:30** (`ticket_reconciler.py`). It reads every Zoho In Progress ticket (71 on Oct 23) and labels each one:

  | State | Count | What the reconciler does |
  |---|---|---|
  | task_closed | 28 | Re-triage, up to 15 a night |
  | no_task | 17 | Re-triage |
  | engineer_stalled | 5 | One ClickUp nudge per 14 days |
  | unassigned | 2 | Listed for Sam |
  | engineer_active | 18 | Nothing |
  | fix_live | 1 | Nothing (the release watcher owns it) |

  Re-triage never auto-routes (`auto_route=False`). `ticket_states` in Claude.ai shows the result.
- **Sends.** `send_ticket_reply` with Sam's own text skips the stale "team is on it" check (deployed). Sam's words must contain send, approve or go ahead.
- **Status after a reply on a closed engineer task.** A local, possibly uncommitted change in `sprint_thread._after_send` makes that reply the answer: Closed for CLOSE_AFTER categories, otherwise Awaiting Client Response. Before, it was set to In Progress "emailed when it ships", which never happens. Check `git log` and keep it.
- **Auto-send.** `RELEASE_AUTO_SEND` defaults to true in code (`release_resolutions.py`), while gap G21 still says "decide". Report that mismatch; do not change it.

## Hard rules (never bend)

- **No client email without Sam's explicit word,** shown to him in full first. You send nothing. Draft, and Sam sends from his sprint.
- **Never set a ClickUp task to `on prod`, `needs client info` or `user education`** by hand. Each one emails the client.
- **Client copy:** no em dashes; no file paths, functions, branches or task IDs; never "fixed", "coming" or "planned" without a backing record.
- **Writes to Zoho or ClickUp:** dry-run first, show Sam the list, apply only the groups he approves. A Zoho close always uses `disableClosureNotification=true`, or `close_ticket_quietly`.
- **Git:** work on a branch (`review/pipeline-refresh`) and open a PR into `main`, which auto-deploys to Railway. Never push to `main` yourself. Never write Django migrations in any repo.
- **Corrections from Sam:** each one becomes a live lesson (`py -m vomeos.lessons add` or `teach_agents`) and, for triage, an eval case (`railway run -s vome-os-worker py triage_cases.py add ...`).
- **Workflow failures:** log every one to ClickUp Operations > Agent Workflows (list 901115585535).
- **Production credentials:** use `railway run -s vome-os-worker` for anything that needs them, or `railway ssh -s vome-os-worker -- python -c "..."` when the code must reach the internal database or Redis. Never print secrets.
- **Spend:** a re-triage with the deep pass costs about $0.30 to $1.50. Ask Sam before any batch over $25.

## Phase 1: inventory (read only, no writes)

Build one table of every Zoho ticket that is not Closed: New, Processing, In Progress, Awaiting Client Response and On Hold. Zoho's `status` parameter rejects custom statuses, so use the `filters.criteria` form (see `ticket_reconciler.zoho_tickets`).

For each ticket, join:

- the sprint item (`sprint_store.by_ticket`): card age, lane, draft status;
- every linked ClickUp task: Priority Queue, Agent Fixes and Sam Accepted, matched by the Zoho Ticket Link field and by `#N` in the task name;
- the last inbound and last outbound message times.

Then classify every row into exactly one drift class:

1. Engineer task closed or on prod, Zoho still open, client never told.
2. In Progress with no task and no fix.
3. Open task untouched 14+ days, or unassigned.
4. Sprint item whose Zoho ticket no longer exists. For example, #9539 returns Zoho 404, yet `sprint_queue` still lists it.
5. Answered by us after the client's last message, but the status is still New or Processing.
6. Awaiting Client Response where the client has written since. This is the stale sweep's "waiting on us" bucket; see `STALE_SWEEP_STUCK_TICKETS.md`.
7. Card older than 2026-10-23 15:36 ET (stale card).
8. Agent fix merged to the dev branch, waiting on production (informational).
9. Open Priority Queue task whose Zoho ticket is Closed (orphaned engineer work).
10. Healthy.

**Deliverable:** a review sheet grouped by class. Give a count, each ticket's number, account and one-line reason, the action you propose, and the estimated cost per group. Write it to a gitignored folder (`backlog_sweep_out/` is already ignored), because it holds customer names. **Stop and wait for Sam's approval per group.**

## Phase 2: apply what Sam approves

Use the existing tools only:

- the reconciler's re-triage (`shadow_triage._handle(..., force=True, auto_route=False)`);
- `backlog_sweep.py apply ... --confirm`;
- `close_ticket_quietly`;
- `route_ticket` and `route_queue`;
- the Zoho status set used by `sprint_thread._after_send`.

Dry-run every group first and report each result in one line. Re-triaged tickets land in the sprint as cards with drafts. Sam sends them.

## Phase 3: close the code gaps that cause drift

Fix each gap in code with a test, so it cannot recur. A lesson alone is not enough (Lessons Log, 2026-10-22). Do them in this order:

1. **Deleted Zoho tickets.** `sync_handled_elsewhere` should finish a sprint item whose Zoho ticket returns 404, and the reconciler should drop it.
2. **Empty drafts.** The reply drafter returns an empty draft when the card makes the reply conditional on Sam checking something (ClickUp 868mmf2eh). This was seen on #9621: blocked, empty. It should write the answer with the condition stated, or a holding reply.
3. **Holding replies that hide a settled answer.** `needs_human` cards get only a holding reply (`shadow_triage.HOLDING_CATEGORIES`), even when the deep pass confirmed a complete answer at high confidence. #9549's deep card had the full corrected answer, but its draft said "our team is looking into this".
4. **Status after a build-now reply.** A reply on a ticket whose request Sam is building himself (Sam Accepted, Build Now) sets Zoho to Awaiting Client Response. It should be In Progress with Sam, or the 30-day stale sweep may close it (#9619).
5. **Re-routing.** `route_ticket` cannot re-route a ticket whose engineer field points at a closed task. The incident matcher over-joins across accounts (868mkd9k2).
6. **CRM tier mismatches (G17).** Cards repeatedly flag a 0 CAD account that its ClickUp task calls Enterprise or high tier (#9623, #9618, #9570). Match on the Zoho account, not only the contact email.

Update `WORKFLOWS.md` (diagram statuses and the gap register) for everything you change or verify, and regenerate the map with `py scripts/render_workflow_map.py`.

## Phase 4: definition of clean (prove it)

You are done when all of these hold. Report each with evidence.

- The Phase 1 inventory re-run shows zero rows in drift classes 1 to 7 and 9, except tickets Sam deliberately parked, listed by name.
- `sprint_queue` shows 0 untriaged tickets. Every listed card was triaged after 2026-10-23 15:36 ET.
- `ticket_states` contains only engineer_active, fix_live and fix_on_dev, plus parked tickets.
- `py -m pytest` passes in full.
- `py -m vomeos.evaluate customer_success.triage_investigator` is at or above 85%. Run the triage director evals and report the score (its answer key is partly unreviewed, gap G15).
- `triage_cost` over the period, and a one-line forecast.

## How to report

Keep updates short, with each finding on one line and evidence (ticket number, file and line, or task link). When the code and a document disagree, the code wins: fix the document and say so. If something needs Sam's decision, ask in one line with your recommendation.
