# Ticket review prompt

Paste into a new Claude Code chat opened in `support-agent`, replacing `<N>`
with the ticket number. One chat per ticket; several can run at once.
Refine this file as the process improves (it is the single source).

```
TICKET REVIEW: #<N>

We are reviewing how Vome OS handled one support ticket, together, so the
agents learn from it. Investigate read-only first. Nothing reaches the client
from a review.

LOAD FIRST
- Your memory index (CS agent entries at the top), plus
  feedback_corrections_teach_agents, feedback_cs_reply_tone,
  feedback_cs_agent_fix_authority and project_sprint_thread_system.
- ClickUp handoff page 3dztm-6351 (doc 3dztm-3311) and the latest entry of the
  Sprint Lessons Log (page 3dztm-2491).
- Check the Sprint Lessons Log for an existing "Ticket review #<N>" entry,
  and Agent Workflows (list 901115585535) for a "[Review] #<N>" task. If
  either exists, continue it instead of starting over.

STEP 1: WHAT THE AGENTS DID (facts only)
- ticket_first_pass("<N>") WITHOUT refresh: the card as it stood.
- The full Zoho thread, including inline images and attachments. Download
  and look at every image yourself.
- The agent runs for this ticket (vomeos_agent_runs: which agents, when,
  inputs, output), fix_status, any routed task, every reply sent and who
  approved it.
- Lay it out as a timeline: arrived, triage, card, draft, routing or fix,
  sends, closes.

STEP 2: WHAT WAS ACTUALLY TRUE (verified, not inferred)
- The code path on the production branch, the matching ClickUp tasks
  (descriptions and comments, not just titles), Sentry for the relevant task
  or endpoint (SENTRY_ORG=vome-2j), help centre and UI text.
- State the correct outcome: category, lane, code-check verdict, owner, and
  the reply that should have gone out.

STEP 3: WHERE IT DIVERGED
- For each stage (inputs, images, existing work, code check, Sentry, verdict
  and lane, draft, recipients, routing, send and close) mark OK, MISSED or
  WRONG with one line of evidence.
- Classify every miss:
  - INSTRUCTION gap: the agent had what it needed and did not use it. Fix:
    a lesson.
  - CAPABILITY gap: the agent could not have done it (missing input, tool,
    data or access). Fix: a code change. Never answer this with a lesson
    alone; it cannot be followed.
  - PRODUCT RULE: a decision only Sam could make. Fix: a lesson plus a
    memory.
  - PROCESS gap: sprint, prompt, routing or tracking behaviour. Fix: a
    workflow change.

STEP 4: SHOW ME, THEN WAIT
- One line: would the agents get this right next time, as things stand?
- The divergence table.
- Proposed fixes per gap: exact lesson sentences with the agent (triage,
  drafter, partner, incidents or all); code changes (files and why); an
  answer-key case; any client follow-up or product fix the ticket still
  needs.
- Wait for my go before changing anything.

STEP 5: ON MY GO
- Lessons with teach_agents, my own words verbatim in sam_said.
- Code changes in your own worktree on branch review/<N> (other review chats
  run in parallel): tests pass, show me the diff, commit by pathspec after
  checking git diff --cached, then push and open the PR into the dev branch
  right away and give me the link. I merge.
- An answer-key case reproducing the miss in
  agents/<division>/<agent>/evals/cases.jsonl (on the same branch).
- Any product fix the ticket needs follows the agent-fix rules (hotfix
  branch, PR into the dev branch as soon as it is committed, never a
  migration, I merge).
- Proof: once the change is live, ticket_first_pass("<N>", refresh=true),
  wait for the new card, compare it with Step 2. If it is still wrong, say
  why and loop back to Step 3.

STEP 6: RECORD
- Lessons only (INSTRUCTION gaps, PRODUCT RULES, no bug): append a dated
  "Ticket review #<N>" entry to the Sprint Lessons Log (page 3dztm-2491):
  the cause in one line, lessons (L numbers), product knowledge, the
  answer-key case (branch and commit), open items. No Agent Workflows task.
- Only when the review needs a code or workflow change (CAPABILITY or
  PROCESS gap, or a real bug): also one task in Agent Workflows (list
  901115585535), assigned to me: "[Review] #<N> <client> -- <one-line
  cause>". Body: timeline, the divergence table, lessons, code changes
  (branch and commits), the proof result, open items. Later work on the
  same ticket comments on that task.
- Memory: only durable product rules or process feedback, after checking
  for an existing memory to update.
- End with: what changed, what is still open, and anything worth adding to
  the Sprint Lessons Log at the next close-out.

RULES
- Never email the client from a review. Never move a task to on prod.
- No em dashes anywhere. Never write migrations.
- If a permission check blocks an action, stop and tell me the rule to add.
  Do not work around it.
```
