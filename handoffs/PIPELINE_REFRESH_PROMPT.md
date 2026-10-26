# Pipeline cleanup: Zoho and ClickUp in sync, engineer work taken over where an agent can

You are a Claude Code agent working in `support-agent` (the "Vome OS" agent system) for Sam, Vome's founder and the only person who approves client communication. Vome is a volunteer management platform.

**Your goal, in Sam's words (2026-10-24):** "Everything on Zoho and ClickUp in sync perfectly. Anything we can retriage, triage or handle on behalf of the frontend or backend team, we just handle it for them and move it into the agent workflows."

That means two outcomes:

1. **Sync.** Every open Zoho ticket and every open ClickUp task tells the same story: who owns it, what state it is in, what the client was last told. Nothing owed to a client is hidden.
2. **Takeover.** Every item sitting with OnlyG (backend) or Sanjay (frontend) gets a fresh, deep card. Whatever an answer, an agent fix or a close can settle leaves the engineer's queue. Only real engineering work stays with them.

"Clean" is measurable, and Phase 6 defines it. Work towards that definition, not towards a list of tasks.

**Terminology.** When Sam says "move it into agent workflows" for a ticket, he means the agent-owned path:
- A fix goes to **Agent Fixes** (ClickUp list 901115573468).
- An answer goes to his sprint as a drafted reply.

The ClickUp list called **Agent Workflows** (901115585535) is something else. It logs failures of the agents themselves. Never file client tickets there.

## Read first (in this order)

1. `WORKFLOWS.md`: flow diagrams and the gap register (G1 to G62). It lags the code, so verify any status before relying on it.
2. `CLAUDE.md`, then `handoffs/CLAUDE_AI_SPRINT_PROMPT.md` and `handoffs/PLAYBOOK_v2.1.md`.
3. ClickUp (workspace 3604276), through the ClickUp connector (the local `.env` ClickUp token returns 401):
   - doc 3dztm-3311: page 3dztm-6351 (CS agent workflow) and page 3dztm-2491 (Sprint Lessons Log; read every entry from 2026-10-19 on, through L51 at least);
   - doc 3dztm-3331: page 3dztm-6331 (code access and repo map).
4. The code behind each step you touch:
   - `backlog_sweep.py`: the engineer takeover tool (inventory, triage, sheet, apply answer|fix|close|note). Read its module docstring first.
   - `ticket_reconciler.py`: the nightly Zoho In Progress to ClickUp join.
   - `shadow_triage.py` and `deep_triage.py`.
   - `ticket_fix.py`: `propose_for_ticket` and `_file_tasks`.
   - `resolution_scope.py`, `signatures.py`, `zoho_actions.py`.
   - `sprint_batch.py`: `sync_handled_elsewhere`.
   - `sprint_thread.py`: `_send`, `_after_send` and `_engineer_shipped`.

## What is true as of 2026-10-24 afternoon

These correct older notes, including any pasted context you were given.

**Triage.** The triage director has 25 tool calls, grep and find-files over full hourly copies of all six repos, and `read_task` for ClickUp. A deep-pass investigator (`deep_triage.py`, 60 calls, Opus 5.5 at high effort) re-checks a card when any of these hold:
- the card is unsure or has no lane;
- the lane is engineer or sam_ops;
- the client is important.

Its card replaces the first one. Runs are logged in `vomeos_triage_runs`, and `triage_cost` reports spend.

**Stale cards.** Any card older than 2026-10-23 15:36 ET predates the deep pass. Treat it as stale.

**The engineer queue, checked 2026-10-24.** 89 open Priority Queue tasks are assigned to OnlyG or Sanjay:

| ClickUp status | Count | Swept before? |
|---|---|---|
| queued / in progress | 48 | 20 carded on 2026-10-21 to 23 (all stale), 28 never carded |
| awaiting client response | 38 | Never. `backlog_sweep.SWEEP_STATUSES` skips this status |
| on dev | 2 | Never |
| declined | 1 | Never (#9628, should not still be open) |

None of the 89 has a backlog card from after the deep-pass cutoff. The 38 at awaiting client response date back as far as July, and many are likely answered, closed in Zoho or abandoned. They are the cheapest wins (close or tell), so include every open status in the inventory, not only `queued` and `in progress`. Some newer tasks (#9631, #9633 and similar) were filed by shadow triage and may already hold a post-cutoff card in the sprint store; check before paying to re-triage them.

The last sweep (`backlog_sweep_out/applied.json`) applied 2 fixes, 5 quiet closes and 5 answers. Two more fixes failed on 2026-10-23 (`fix_2026-10-23.log`): #9008 had no VomeApp PR target (now default), and #9318 got HTTP 403 opening a vome-react PR (token scope, check before retrying).

**Fix targets.** VomeApp (`development`), chats and integrations are all fix PR targets by default (`sentry_projects._FIX_TARGETS`). No environment variable is needed.

**Nightly reconcile at 06:30** (`ticket_reconciler.py`). It labels every Zoho In Progress ticket. On Oct 23 there were 71:

| State | Count |
|---|---|
| task_closed | 28 |
| no_task | 17 |
| engineer_active | 18 |
| engineer_stalled | 5 |
| unassigned | 2 |
| fix_live | 1 |

It re-triages up to 15 a night, never auto-routes, and nudges stalled tasks once per 14 days. `ticket_states` in Claude.ai shows the result.

**Agent Fixes in Zoho (new, 2026-10-24).** A ticket with an open agent fix is In Progress, owned by the Zoho team "Agent Fixes" (`569440000043486190`) with no agent. Two exceptions:
- A ticket we never answered stays New until Sam's acknowledgment goes out.
- A PR closed unmerged sends the ticket back to Processing on Support Agent.

Check `git status` and `git log`: the code (`zoho_actions.set_agent_fixing` and `mark_agent_fix`) may still be uncommitted from another session. If it is uncommitted, do not commit or revert it. Ask Sam. Until it is deployed, set the shape by hand with Zoho `updateTicket`: `status: In Progress, teamId: 569440000043486190, assigneeId: null`.

**A "fixed" email waits for every issue (G62).** `resolution_scope.check()` runs before every automatic fixed email, and it fails closed. It holds the email when:
- another task on the ticket is still open; or
- the thread raises a problem that the shipped fixes don't cover.

When a ticket has a second problem, give it its own task linked to the same Zoho ticket, so the check can see it.

**Signatures.** Anything sent without Sam's explicit approval is signed "Vome team" (`signatures.AUTO_SENDER`). Sprint drafts stay signed "Sam | Vome team".

**CC.** A Vome teammate (Ron, usually) who was ever on a client message's To or CC stays on every reply (L47, `zoho_actions.ticket_teammates`).

**Sends.** When `send_ticket_reply` carries Sam's own text, it skips the stale "team is on it" check. Sam's words must contain send, approve or go ahead.

**Status after a reply on a closed engineer task** (committed in 9d574d3). The reply is the answer:
- Closed for CLOSE_AFTER categories;
- otherwise Awaiting Client Response.

**Auto-send.** `RELEASE_AUTO_SEND` defaults to true in code, while gap G21 still says "decide". Report the mismatch; do not change it.

## How a ticket moves to Agent Fixes (the exact recipe)

This is the recipe `ticket_fix._file_tasks` follows, and the one any hand-filed fix must follow too:

1. **Parent.** Create it in Agent Fixes at `in progress`, named `[#NNNN] <summary> -- P3`. The description carries its own line `Fix PR: <real PR url>`. Fields:
   - Tags = Agent
   - Type = Bug
   - Merge Owner = Sam
   - Release Check = Waiting

   The field and option IDs are in `ticket_fix._file_tasks`.
2. **Child.** Move the ticket's existing engineer task under the parent, never a copy:
   - status `in progress`;
   - renamed `[#NNNN] <Account> -- <summary> (linked to fix)`;
   - it keeps its Zoho Ticket Link field.

   Create a new child only when the ticket has no task of its own. A task the ticket was wrongly joined to is not its own task.

   Before moving the task in, close any duplicate child with the same Zoho link. Release resolutions are keyed per child, so two live children would each email the client.
3. **Fix Commit.** Leave it empty. `promote_merged` writes the merge sha after Sam merges, and the release watcher proves it live. Never set `on prod` by hand.

   Exception, a fix that spans two repos: put a placeholder in Fix Commit (`HOLD-until-core-and-web-both-merged`), so the first merge is not promoted alone. Set it by hand once both PRs have merged.
4. **Zoho.** Set the Agent Fixes team shape above, and add a private note linking the PR. The client is not emailed.

**Check this before applying any fix.** `backlog_sweep.apply_one("fix")` calls `ticket_fix.propose_for_ticket` and then `release_task`, which closes the engineer task. If `_file_tasks` creates a new child rather than moving the engineer task in, the result breaks rule 2. Fix that in code, with a test, before the first `apply fix` (Phase 5, gap 0).

## Hard rules (never bend)

- **No client email without Sam's explicit word,** shown to him in full first. You send nothing. You draft, and Sam sends from his sprint.
- **Never set a ClickUp task to `on prod`, `needs client info` or `user education`** by hand. Each one emails the client.
- **Client copy:**
  - no em dashes;
  - no file paths, functions, branches or task IDs;
  - never "fixed", "coming" or "planned" without a backing record.
- **Writes to Zoho or ClickUp:** dry-run first, show Sam the list, and apply only the groups he approves. A Zoho close always uses `disableClosureNotification=true` (or `close_ticket_quietly`).
- **Releasing an engineer task:**
  - a comment saying who took it and why;
  - the engineer removed as assignee;
  - status Closed.

  Never a status that emails anyone.
- **Git:** work on a branch (`review/pipeline-cleanup`) and open a PR into `main`, which auto-deploys to Railway. Never push to `main` yourself. Never commit another session's uncommitted work. Never write Django migrations in any repo.
- **Corrections from Sam:** each one becomes a live lesson (`py -m vomeos.lessons add` or `teach_agents`). For triage, it also becomes an eval case: `railway run -s vome-os-worker py triage_cases.py add ...`.
- **Workflow failures:** log every one to ClickUp Operations > Agent Workflows (901115585535).
- **Production credentials:**
  - Use `railway run -s vome-os-worker` for anything that needs them.
  - Use `railway ssh -s vome-os-worker -- python -c "..."` when the code must reach the internal database or Redis.
  - On Windows, give `railway run` the full `python.exe` path and a script file; inline `py -c` prints nothing.
  - Never print secrets.
- **Spend:** a deep re-triage costs about $0.30 to $1.50. Give Sam the estimate before Phase 2, and ask again before any single batch over $25.

## Phase 0: where the code stands (read only)

Report in five lines:
- `git status`;
- what is uncommitted and whose it looks like;
- whether `origin/main` is what the worker runs (Railway deployment);
- the current lesson count;
- whether the Agent Fixes Zoho team code is live.

## Phase 1: inventory (read only, no model spend)

Build one table that joins three sources:

- **Zoho:** every ticket that is not Closed (New, Processing, In Progress, Awaiting Client Response, On Hold), with its team and owner. Zoho's `status` parameter rejects custom statuses, so use the `filters.criteria` form (see `ticket_reconciler.zoho_tickets`).
- **ClickUp:**
  - every open task in Priority Queue (901113386257) assigned to OnlyG or Sanjay;
  - every open task in Agent Fixes;
  - every open task in Sam Accepted.

  Match tasks to tickets by the Zoho Ticket Link field and by `#N` in the task name. `backlog_sweep.py inventory` already does most of this, so extend it rather than writing a second one.
- **The sprint store** (`sprint_store.by_ticket`): card age, lane and draft status. Also record each ticket's last inbound and last outbound message times.

Classify every row into exactly one drift class:

1. Engineer task closed or `on prod`, Zoho still open, client never told.
2. Zoho In Progress with no task and no fix.
3. Open engineer task untouched 14+ days, or unassigned.
4. Sprint item whose Zoho ticket no longer exists. #9539 returns 404, yet `sprint_queue` still lists it.
5. Answered by us after the client's last message, but the status is still New or Processing.
6. Awaiting Client Response, but the client has written since.
7. Owner mismatch. Each pairing must hold:

   | Zoho team | ClickUp |
   |---|---|
   | Backend | OnlyG |
   | Frontend | Sanjay |
   | Agent Fixes | an open Agent Fixes parent |

   Also flag an open fix still on Support Agent.
8. Open ClickUp task whose Zoho ticket is Closed (orphaned work).
9. Duplicate tasks for one ticket, or two live Agent Fixes children with the same Zoho link.
10. Engineer item with no card from after 2026-10-23 15:36 ET. This is the takeover candidate pool.
11. Agent fix merged to the dev branch, waiting on production (informational).
12. Healthy.

**Deliverable.** A review sheet grouped by class, written to `backlog_sweep_out/`, which is gitignored because it holds customer names. For each class give:
- the count;
- each ticket's number, account and a one-line reason;
- the action you propose.

Add the total triage spend estimate for classes 1, 2, 3, 6 and 10. **Stop and wait for Sam's approval per class.**

## Phase 2: triage what Sam approved

Use `backlog_sweep.py triage` for engineer items, and the reconciler's re-triage for the rest (`shadow_triage._handle(..., force=True, auto_route=False)`). Both run under the worker environment. The deep pass runs automatically on engineer-lane and unsure cards. Report progress in one line per batch, with the running spend.

## Phase 3: the takeover sheet

Run `backlog_sweep.py sheet`, extended to cover every triaged row. Group by what would happen:

| Group | What happens | Engineer task |
|---|---|---|
| answer | A drafted reply joins Sam's sprint | Released |
| fix | Agent PR, Agent Fixes parent, Zoho on the Agent Fixes team | Moved in as the child (not closed) |
| tell | Shipped but the client was never told: a sprint draft, only if `resolution_scope` agrees every issue shipped | Already closed |
| close | Nothing is owed: Zoho closed quietly | Released |
| keep | Real engineering work: the first pass is commented on the task, and the Zoho team is set to match | Stays |
| Sam | Product, data, billing or account decisions, with one line each | Untouched |

Each row gives the card's verdict, its confidence and the exact file and line when it is a bug. **Stop and wait for Sam's approval per group.** He may pick items out of a group.

## Phase 4: apply

Use the existing tools only:
- `backlog_sweep.py apply <action> <keys> --confirm`;
- `close_ticket_quietly`;
- `route_ticket` and `route_queue`;
- the Zoho status set used by `sprint_thread._after_send`.

Dry-run every group first and report each result in one line. Sam sends the drafted replies himself from his sprint.

## Phase 5: close the code gaps that cause drift

Fix each gap in code with a test, so it cannot recur. A lesson alone is not enough (Lessons Log, 2026-10-22). Work in this order:

0. **The backlog sweep's fix path must move the engineer task in as the child,** never close it and create a copy (see the recipe above).
1. **Deleted Zoho tickets.** `sync_handled_elsewhere` should finish a sprint item whose Zoho ticket returns 404, and the reconciler should drop it.
2. **Empty drafts.** The reply drafter returns an empty draft when a card makes the reply conditional on Sam checking something (868mmf2eh, seen on #9621). It should write the answer with the condition stated, or a holding reply.
3. **Holding replies that hide a settled answer.** `needs_human` cards get only a holding reply (`shadow_triage.HOLDING_CATEGORIES`), even when the deep pass confirmed a complete answer at high confidence (#9549).
4. **Status after a build-now reply.** A reply on a ticket Sam is building himself (Sam Accepted) sets Awaiting Client Response, so the 30-day stale sweep may close it (#9619). It should be In Progress with Sam.
5. **Re-routing.**
   - `route_ticket` cannot re-route a ticket whose engineer field points at a closed task.
   - The incident matcher over-joins across accounts (868mkd9k2).
6. **Owner sync.** Make drift class 7 impossible going forward. When a task's assignee changes, or a task moves to or from Agent Fixes, the Zoho team should follow, through the existing ClickUp webhook. No client email.
7. **CRM tier mismatches (G17).** Cards flag a 0 CAD account that its ClickUp task calls Enterprise (#9623, #9618, #9570). Match on the Zoho account, not only the contact email.

Update `WORKFLOWS.md` (diagram statuses and the gap register) for everything you change or verify, and regenerate the map with `py scripts/render_workflow_map.py`.

## Phase 6: definition of clean (prove it)

You are done when all of these hold. Report each one with evidence.

- **Inventory.** The Phase 1 re-run shows zero rows in drift classes 1 to 10, except tickets Sam deliberately parked, listed by name.
- **Engineer queues.** Every open OnlyG or Sanjay task has a post-cutoff card in the keep group, and its Zoho team matches.
- **Agent Fixes.**
  - Every open parent has a real `Fix PR:` line.
  - Every child is the ticket's own task.
  - Every ticket with an open fix sits on the Agent Fixes Zoho team (or New, if never answered).
- **Sprint.** `sprint_queue` shows 0 untriaged tickets. `ticket_states` contains only engineer_active, fix_live and fix_on_dev, plus parked tickets.
- **Tests.** `py -m pytest` passes in full. Database tests need `SPRINT_TEST_DATABASE_URL`.
- **Evals.**
  - `py -m vomeos.evaluate customer_success.triage_investigator` is at or above 85%.
  - Run the triage director evals and report the score. Its answer key is partly unreviewed (G15).
- **Cost.** `triage_cost` over the cleanup, plus a one-line forecast.

## How to report

Keep updates short: one finding per line, with evidence (ticket number, file and line, or task link). When the code and a document disagree, the code wins: fix the document and say so. When something needs Sam's decision, ask in one line and give your recommendation.
