"""
sentry_handler.py

The Sentry pipeline's orchestration. Phase 2: ingress, gate, redaction,
ledger, and one triage verdict per new issue. No per-issue Slack post, nobody
tagged, no pull request.

THE SPLIT, AND WHY IT IS WHERE IT IS
------------------------------------
Two halves, and which work happens on which side is the whole performance and
safety story.

**In the web request** (`handle_webhook`): verify the signature, parse,
redact, normalise, and run the gate. All of it is deterministic, none of it
touches a model or a third party, and it is over in microseconds. An issue the
gate drops is recorded and never reaches the queue at all, which is what makes
stage 2 of the funnel genuinely free.

**On the worker** (`handle_sentry_issue`): claim the issue in the ledger,
fetch one real event for its stack trace, and ask `engineering.sentry_triage`
whether a person should ever see it. It is here rather than in the request so
that a deploy, a slow Sentry API or a model call cannot take the endpoint down
with it, and so that a flood lands in a queue instead of in the web dyno.

WHY THE VERDICT GOES NOWHERE YET
--------------------------------
Phase 2 records what the agent decided and stops. The only place a verdict
surfaces is the daily report, printed next to the issue that produced it.

That is the evaluation period, and skipping it would be the expensive
mistake. The agent scores 94% on its answer key, which is a number from
seventeen cases somebody wrote; it is not evidence about live traffic. A week
of reading real verdicts next to real issues is what tells you whether to let
it interrupt anyone, and that has to happen before it can.

EAGER MODE NOW COSTS SOMETHING
------------------------------
With no `VOMEOS_BROKER_URL` set, the enqueue runs inline, so the Sentry API
fetch and the model call both happen inside the webhook request. In phase 1
that was one INSERT and did not matter. It is now a few seconds, which is
close enough to Sentry's delivery timeout to start failing under a burst.

A timeout is survivable rather than harmful: Sentry retries, the redelivery
hits the ledger claim, `first_seen` is False and nothing runs twice. That is
exactly what the claim is for. But it shows up as failed deliveries in
Sentry's log and it serialises triage behind one web worker, so the Redis
cutover has stopped being advisable and started being the next thing to do.

ALWAYS ANSWER 2xx
-----------------
After the signature check passes, every path returns success. Sentry retries a
non-2xx, and a retry storm on a payload we were always going to drop is worse
than a lost log line. Failures are caught, printed and reported in the body.
The one exception is a bad signature, which is a 403 and must stay one.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone

import sentry_code
import sentry_event
import sentry_fix
import sentry_gate
import sentry_ledger
import sentry_notify
import sentry_projects
import sentry_redact
import sentry_slack
from vomeos.integrations import sentry as sentry_api

# The queued handler's address, registered in engineering_jobs.py.
HANDLER_KEY = "engineering.sentry_issue"
QUEUE = "engineering"

# A queue message larger than this drops its payload and lets the worker
# re-fetch from the Sentry API instead. Redis will happily hold a megabyte
# per message, which is how a queue quietly becomes a database.
MAX_QUEUE_BYTES = int(os.environ.get("SENTRY_QUEUE_MAX_BYTES", "65536"))


def _queue(message: dict):
    """Hand the message to the engineering queue.

    A named seam rather than an inline import so the webhook path can be
    tested without a broker, and so the one place that queues is findable.
    """
    from vomeos.worker import enqueue_event

    return enqueue_event(HANDLER_KEY, message, queue=QUEUE)


def _enabled() -> bool:
    """The pipeline's own switch, separate from the auto-PR switch.

    Defaults to on. Turning it off leaves the endpoint answering 2xx and
    doing nothing, which is the right shape for an incident: stop the
    pipeline without making Sentry retry every delivery for a day.
    """
    return os.environ.get("SENTRY_PIPELINE_ENABLED", "true").lower() != "false"


# ---------------------------------------------------------------------------
# The web half
# ---------------------------------------------------------------------------

def handle_webhook(
    raw_body: bytes, signature: str, resource: str = ""
) -> dict:
    """Verify, filter and hand off. Runs in the web request.

    Returns a dict describing what happened. `status` is one of:
        rejected  bad signature. The caller must answer 403.
        ignored   not a hook we subscribed to, or the pipeline is off.
        gated     a rule dropped it. Recorded, not queued.
        queued    it survived and is on the engineering queue.
    """
    if not sentry_api.verify_signature(raw_body, signature):
        return {"status": "rejected", "reason": "bad signature"}

    if not _enabled():
        return {"status": "ignored", "reason": "pipeline disabled"}

    if resource and resource not in sentry_api.SUPPORTED_RESOURCES:
        return {"status": "ignored", "reason": f"resource {resource}"}

    try:
        body = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        print("[SENTRY] unparseable webhook body")
        return {"status": "ignored", "reason": "unparseable"}

    # Structural fields are read from the parsed body and NEVER passed
    # through the text scrubber. Only the free-text fields are scrubbed.
    #
    # The first version of this normalised from the redacted payload, which
    # looked safer and was not. A Sentry issue id is a 16 digit number that
    # often begins with 4 or 5, so the real id 4506427274952704 matched the
    # payment card pattern and arrived downstream as "[card]". The issue id is
    # the ledger's primary key, so every issue would have collapsed into one
    # row, the first would have claimed it, and every issue after that would
    # have been marked "already known" and dropped. Silently.
    #
    # So the rule is: an identifier is never rewritten, and a scrubber only
    # ever touches prose. `title` and `culprit` are the prose here, and they
    # are the only two fields that can carry a customer's data.
    signal = sentry_api.normalize(body, resource)
    if signal is None:
        return {"status": "ignored", "reason": "not an issue payload"}

    signal = replace(
        signal,
        title=sentry_redact.redact_text(signal.title),
        culprit=sentry_redact.redact_text(signal.culprit),
    )

    # The payload blob keeps the full treatment. Nothing downstream of this
    # line has seen an unredacted one.
    payload = sentry_redact.redact(body)

    decision = sentry_gate.check(signal)
    if decision.blocked:
        sentry_ledger.record(
            signal,
            status=sentry_ledger.STATUS_GATED,
            gate_rule=decision.rule,
            gate_detail=decision.detail,
        )
        print(
            f"[SENTRY] gated {signal.short()} "
            f"({decision.rule}: {decision.detail})"
        )
        return {
            "status": "gated",
            "issue_id": signal.issue_id,
            "rule": decision.rule,
            "detail": decision.detail,
        }

    # Which repository this issue belongs to is a fixed fact, resolved here
    # from the routing table rather than asked of a model later.
    project = sentry_projects.route(signal.project)
    message = {
        "signal": asdict(signal),
        "payload": payload,
        "repo": project.repo if project else "",
        "repo_owner": project.owner if project else "",
        "repo_host": project.host if project else "",
        # The branch that produced this traceback. Carried per issue because
        # dev and prod are the same repo at different refs, and reading a dev
        # stack trace against main means reasoning about code that is not
        # running, with line numbers that do not match.
        "repo_ref": project.ref if project else "",
        "stack": project.stack if project else "",
    }
    if len(json.dumps(message, default=str)) > MAX_QUEUE_BYTES:
        # Keep the signal, drop the bulk. The worker can re-fetch the event
        # from Sentry, which is a better trade than a fat queue.
        message["payload"] = None
        message["payload_dropped"] = True

    try:
        _queue(message)
    except Exception as exc:
        # A broker that is briefly unreachable must not make Sentry retry
        # for an hour. Record what we know and move on.
        print(f"[SENTRY] could not queue {signal.issue_id}: {exc}")
        sentry_ledger.record(
            signal,
            repo=project.repo if project else "",
            stack=project.stack if project else "",
        )
        return {
            "status": "queued",
            "issue_id": signal.issue_id,
            "queued": False,
            "error": str(exc),
        }

    target = f"{project.repo}@{project.ref}" if project else "unmapped"
    print(f"[SENTRY] queued {signal.short()} ({signal.reason}) -> {target}")
    return {
        "status": "queued",
        "issue_id": signal.issue_id,
        "queued": True,
        "repo": project.repo if project else "",
        "ref": project.ref if project else "",
    }


# ---------------------------------------------------------------------------
# The worker half
# ---------------------------------------------------------------------------

def handle_sentry_issue(message: dict) -> dict:
    """Claim the issue and, from phase 2, triage it.

    Registered as an event handler, so this is what runs on the engineering
    queue. Phase 1 stops after the claim: the point of shadow mode is to find
    out how many issues a day actually get here before anything is built on
    top of that number.
    """
    raw_signal = (message or {}).get("signal") or {}
    if not raw_signal.get("issue_id"):
        return {"status": "ignored", "reason": "no issue id"}

    # Take only the keys the dataclass knows about, so a message written by
    # an older or newer deploy is handled rather than raising. A queue always
    # holds messages from the version before this one.
    known = sentry_api.IssueSignal.__dataclass_fields__
    signal = sentry_api.IssueSignal(
        **{k: v for k, v in raw_signal.items() if k in known}
    )

    claim = sentry_ledger.record(
        signal,
        repo=(message or {}).get("repo", ""),
        stack=(message or {}).get("stack", ""),
    )

    if not claim.first_seen:
        # Already known. This is the branch that most deliveries take, and
        # doing nothing here is the entire anti-bombardment guarantee.
        print(f"[SENTRY] already known: {signal.short()}")
        return {
            "status": "known",
            "issue_id": signal.issue_id,
            "acted": False,
        }

    repo = (message or {}).get("repo", "") or "unmapped"
    ref = (message or {}).get("repo_ref", "")
    where = f"{repo}@{ref}" if ref else repo
    print(
        f"[SENTRY] new issue {signal.short()} "
        f"({signal.level or 'no level'}, {signal.reason}) in {where}"
    )

    triage = _triage(signal, repo)

    outcome = {
        "status": "triaged" if triage else "recorded",
        "issue_id": signal.issue_id,
        "acted": True,
        "ledger": claim.recorded,
        "repo": (message or {}).get("repo", ""),
        "ref": (message or {}).get("repo_ref", ""),
        "verdict": (triage or {}).get("verdict", ""),
        "severity": (triage or {}).get("severity", ""),
    }

    # Noise stops here, which is most of what gets this far. Spending a
    # senior-tier call on something a cheap model just called noise would
    # undo the entire point of having a cheap model first.
    if not triage or triage.get("verdict") != "actionable":
        return outcome

    outcome.update(_investigate(signal, message or {}, triage))
    return outcome


def _investigate(signal, message: dict, triage: dict) -> dict:
    """Phases 3 to 5 for one actionable issue.

    Analyse against the real code, check whether ClickUp already has it,
    report to Slack, and propose a patch when the analyst says the fix is
    small and certain.

    Every step degrades rather than raising. An issue that reaches here has
    already been claimed, so an exception would lose it permanently, and a
    partial result (a diagnosis with no patch, or a patch with no PR) is
    worth more than nothing.
    """
    issue_id = signal.issue_id
    repo = message.get("repo", "")
    ref = message.get("repo_ref", "")
    host = message.get("repo_host", "bitbucket")
    owner = message.get("repo_owner", "")

    trace = sentry_event.fetch_trace(issue_id)
    code = sentry_code.gather(trace, repo, ref, host=host, owner=owner)
    existing = _existing_tasks(signal)

    analysis = _analyse(signal, triage, trace, code, existing, repo, ref)

    issue = {
        "issue_id": issue_id,
        "project": signal.project,
        "title": signal.title,
        "culprit": signal.culprit,
        "exception_type": signal.exception_type,
        "permalink": signal.permalink,
        "severity": triage.get("severity", ""),
        "triage_summary": triage.get("summary", ""),
        "infrastructure": triage.get("infrastructure", False),
        "repo": repo,
        "ref": ref,
        "stack": message.get("stack", ""),
    }

    posted = sentry_slack.report_issue(issue, analysis)

    proposal = {"attempted": False, "reason": "no analysis"}
    pull_request = {"opened": False, "reason": "no patch"}
    if analysis:
        proposal = sentry_fix.propose(analysis, {
            "source": code.get("files", ""),
            "repo": repo, "branch": ref, "issue_id": issue_id,
        })
        if proposal.get("diff"):
            issue.update(posted_thread(issue, posted))
            pull_request = sentry_fix.open_pull_request(issue, proposal, {
                "repo": repo, "branch": ref, "host": host, "owner": owner,
                "root_cause": analysis.get("root_cause", ""),
            })
        # Stored whatever happened. An issue below the interrupt bar never
        # reaches Slack in the moment, so without this the diff would be
        # paid for and then thrown away.
        sentry_ledger.update(
            issue_id,
            fix_diff=(proposal.get("diff") or "")[:12000] or None,
            fix_reason=(proposal.get("reason") or "")[:500] or None,
        )
        _report_proposal(issue, posted, proposal, pull_request)

    return {
        "status": "analysed",
        "analysed": bool(analysis),
        "risk": (analysis or {}).get("risk", ""),
        "duplicate_of": (analysis or {}).get("duplicate_of", ""),
        "slack": posted,
        "fix": {"attempted": proposal.get("attempted"),
                "reason": proposal.get("reason")},
        "pull_request": pull_request,
    }


def posted_thread(issue: dict, posted: dict) -> dict:
    """Carry a freshly created thread id onto the issue dict."""
    if posted.get("thread_ts"):
        return {"slack_ts": posted["thread_ts"],
                "slack_channel": posted.get("channel", "")}
    row = sentry_ledger.get(issue["issue_id"]) or {}
    return {"slack_ts": row.get("slack_ts", ""),
            "slack_channel": row.get("slack_channel", "")}


def _existing_tasks(signal) -> str:
    """Has ClickUp already got this?

    Never lets a failed search look like an empty one. `clickup_search`
    returns prose that says which it was, and the analyst's charter tells it
    to treat an unavailable search as unknown rather than as none.
    """
    try:
        from clickup_search import describe_matches

        query = f"{signal.exception_type} {signal.title}".strip()
        return describe_matches(query, limit=4)
    except Exception as exc:
        return (
            f"Existing-task search FAILED ({exc}). Treat existing work as "
            "UNKNOWN, not as none."
        )


def _analyse(signal, triage: dict, trace: str, code: dict, existing: str,
             repo: str, ref: str) -> dict | None:
    """Run `engineering.bug_analyst`. None when it could not answer."""
    try:
        from vomeos import run_agent

        result = run_agent(
            "engineering.bug_analyst",
            context={
                "summary": triage.get("summary", ""),
                "severity": triage.get("severity", ""),
                "title": signal.title,
                "exception_type": signal.exception_type,
                "culprit": signal.culprit,
                "stack_trace": trace or "(no stack trace available)",
                "source": code.get("files", ""),
                "recent_commits": code.get("commits", ""),
                "existing_tasks": existing,
                "repo": repo,
                "branch": ref,
            },
            subject_type="sentry_issue",
            subject_id=signal.issue_id,
        )
    except Exception as exc:
        print(f"[SENTRY] analysis failed for {signal.issue_id}: {exc}")
        return None

    if not result.ok:
        print(
            f"[SENTRY] analysis {result.status} for {signal.issue_id}: "
            f"{result.why()}"
        )
        return None

    analysis = {
        "root_cause": result.get("root_cause", ""),
        "evidence": result.get("evidence", ""),
        "proposed_fix": result.get("proposed_fix", ""),
        "risk": str(result.get("risk", "")).lower(),
        "confidence": str(result.get("confidence", "")).lower(),
        "needs_migration": bool(result.get("needs_migration", False)),
        "duplicate_of": str(result.get("duplicate_of", "") or ""),
        "files": result.get("files") or [],
    }
    sentry_ledger.update(
        signal.issue_id,
        status=sentry_ledger.STATUS_ANALYSED,
        clickup_task_id=analysis["duplicate_of"] or None,
        root_cause=analysis["root_cause"][:2000],
        proposed_fix=analysis["proposed_fix"][:2000],
        analysed_at=datetime.now(timezone.utc),
        analysed_times_seen=int(signal.times_seen or 0),
    )
    print(
        f"[SENTRY] analysed {signal.issue_id}: risk={analysis['risk']} "
        f"confidence={analysis['confidence']} "
        f"migration={analysis['needs_migration']}"
    )
    return analysis


def _report_proposal(issue: dict, posted: dict, proposal: dict,
                     pull_request: dict) -> None:
    """Put the diff, or the reason there is not one, in the issue's thread.

    In the thread rather than as a new message, because a proposed fix is an
    update about an issue somebody has already been told about. Without a
    thread it says nothing at all rather than starting a second alert.
    """
    if not issue.get("slack_ts"):
        return

    if pull_request.get("opened"):
        text = (
            f"Pull request opened: {pull_request['url']}\n"
            f"_Not reviewed by a person. Branch `{pull_request['branch']}`._"
        )
    elif proposal.get("diff"):
        why = pull_request.get("reason", "")
        text = (
            "A fix was written but not pushed"
            + (f" ({why})" if why else "")
            + f".\n\n*Why* {proposal.get('rationale', '')}\n"
            + f"*Unsure about* {proposal.get('risks', '')}\n"
            + "```\n" + proposal["diff"][:2500] + "\n```"
        )
    else:
        text = f"No automated fix attempted: {proposal.get('reason', '')}"

    sentry_slack.reply_in_thread(issue, text)


def _triage(signal, repo: str) -> dict | None:
    """Ask `engineering.sentry_triage` whether a person should see this.

    Phase 2 records the verdict and stops. Nothing is posted, nobody is
    tagged, and the daily report is the only place the verdicts surface. That
    is deliberate: a week of reading verdicts next to the issues that produced
    them is how you find out whether this agent is any good, and that has to
    happen before anything acts on what it says.

    Returns None when the agent could not be run or its answer was rejected.
    The issue stays in the ledger either way, so nothing is lost: it simply
    has no verdict, and the report counts it as untriaged rather than quietly
    treating it as noise.
    """
    if not os.environ.get("VOMEOS_BROKER_URL"):
        # Not a refusal, just a warning. See EAGER MODE above: this is
        # correct but slow, and slow here means Sentry retries.
        print(
            "[SENTRY] triaging inside the web request (no broker configured)"
        )

    trace = sentry_event.fetch_trace(signal.issue_id)

    try:
        from vomeos import run_agent

        result = run_agent(
            "engineering.sentry_triage",
            context={
                "title": signal.title,
                "exception_type": signal.exception_type,
                "culprit": signal.culprit,
                "stack_trace": trace or "(no stack trace available)",
                "project": signal.project,
                "repo": repo,
            },
            subject_type="sentry_issue",
            subject_id=signal.issue_id,
        )
    except Exception as exc:
        # A model outage must not lose the issue. It is already claimed.
        print(f"[SENTRY] triage failed for {signal.issue_id}: {exc}")
        return None

    if not result.ok:
        # `blocked` means a guard rejected the answer and `error` means it
        # never produced one. Both are a human's problem, not a verdict.
        print(
            f"[SENTRY] triage {result.status} for {signal.issue_id}: "
            f"{result.why()}"
        )
        sentry_ledger.update(
            signal.issue_id,
            status=sentry_ledger.STATUS_SEEN,
            run_ids=[result.run_id] if result.run_id else [],
        )
        return None

    verdict = str(result.get("verdict", "") or "").lower()
    severity = str(result.get("severity", "") or "").lower()
    summary = str(result.get("summary", "") or "")

    sentry_ledger.update(
        signal.issue_id,
        status=sentry_ledger.STATUS_TRIAGED,
        verdict=verdict,
        severity=severity,
        triage_summary=summary[:500],
        infrastructure=bool(result.get("infrastructure", False)),
        run_ids=[result.run_id] if result.run_id else [],
    )
    print(
        f"[SENTRY] triage {signal.issue_id}: {verdict}/{severity} "
        f"{'(infra) ' if result.get('infrastructure') else ''}{summary[:80]}"
    )
    return {
        "verdict": verdict,
        "severity": severity,
        "summary": summary,
        "infrastructure": bool(result.get("infrastructure", False)),
    }


# ---------------------------------------------------------------------------
# The shadow-mode report
# ---------------------------------------------------------------------------

def _format_report(data: dict) -> str:
    if not data.get("available"):
        return "Sentry shadow report: ledger unavailable, no figures."

    by_status = data.get("by_status", {})
    surviving = sum(
        n for s, n in by_status.items() if s != sentry_ledger.STATUS_GATED
    )
    gated = by_status.get(sentry_ledger.STATUS_GATED, 0)
    hits = data.get("hits_by_status", {})
    total_hits = sum(hits.values())

    lines = [
        "*Sentry triage report* (last 24h, nothing acted on)",
        "",
        f"Deliveries: {total_hits}",
        f"Distinct issues: {surviving + gated}",
        f"Survived the gate: {surviving}",
        f"New and surviving: {data.get('new_surviving_issues', 0)}",
        f"Dropped by the gate: {gated}",
    ]

    # The verdicts, and then the actionable issues with the agent's own
    # summary. Printed together on purpose: this report exists so a person
    # can grade the agent by reading its verdict next to the issue, and a
    # count on its own cannot be graded.
    verdicts = data.get("by_verdict") or []
    if verdicts:
        lines.append("")
        lines.append("Triage verdicts:")
        for row in verdicts:
            label = row["verdict"]
            if label == "actionable":
                label = f"actionable {row['severity']}"
            lines.append(f"  {label}: {row['n']}")

    # The actionable issues, in full. This is where the analysis lands for
    # everything below the interrupt bar, which is most of it: an s2 never
    # posts to Slack in the moment, so without this section the analyst's
    # work would be paid for and then seen by nobody.
    actionable = data.get("actionable") or []
    if actionable:
        lines.append("")
        lines.append("*Actionable:*")
        for row in actionable:
            sev = (row.get("severity") or "?").upper()
            infra = " [infra]" if row.get("infrastructure") else ""
            summary = row.get("triage_summary") or row.get("title") or ""
            lines.append("")
            lines.append(f"*{sev}*{infra} {summary[:200]}")

            if row.get("root_cause"):
                lines.append(f"  _Cause_ {row['root_cause'][:300]}")
            if row.get("proposed_fix"):
                lines.append(f"  _Fix_ {row['proposed_fix'][:300]}")
            if row.get("clickup_task_id"):
                lines.append(
                    f"  _Possibly already ClickUp task "
                    f"{row['clickup_task_id']}_"
                )
            if row.get("pr_url"):
                lines.append(f"  _Pull request_ {row['pr_url']}")
            elif row.get("fix_diff"):
                lines.append(
                    "  _A patch was written and not pushed. "
                    "`/sentry/recent` has it._"
                )
            elif row.get("fix_reason"):
                lines.append(f"  _No patch_ {row['fix_reason'][:160]}")
            if row.get("slack_ts"):
                lines.append("  _Already has a thread above._")
            if row.get("permalink"):
                lines.append(f"  {row['permalink']}")

    drops = data.get("gate_drops_by_rule") or {}
    if drops:
        lines.append("")
        lines.append("Dropped by rule:")
        for rule, counts in drops.items():
            lines.append(
                f"  {rule}: {counts['issues']} issues, {counts['hits']} deliveries"
            )

    repos = data.get("surviving_by_repo") or {}
    if repos:
        lines.append("")
        lines.append("Surviving by repository:")
        for repo, n in repos.items():
            lines.append(f"  {repo}: {n}")

    projects = data.get("surviving_by_project") or {}
    if projects:
        lines.append("")
        lines.append("Surviving by Sentry project:")
        for project, n in projects.items():
            lines.append(f"  {project}: {n}")

    # An unmapped project is a configuration gap, so it gets its own section
    # rather than one line in a table of drop rules. Somebody made a Sentry
    # project and this pipeline does not know what code is behind it.
    unmapped = data.get("unmapped_projects") or {}
    if unmapped:
        lines.append("")
        lines.append("*Unmapped Sentry projects, add them to sentry_projects.py:*")
        for project, counts in unmapped.items():
            lines.append(
                f"  {project}: {counts['issues']} issues,"
                f" {counts['hits']} deliveries, all discarded"
            )

    environments = data.get("environments") or {}
    if environments:
        lines.append("")
        lines.append(
            "Environments: "
            + ", ".join(f"{k} {v}" for k, v in environments.items())
        )
        if "(unknown)" in environments:
            lines.append(
                "  An unknown environment passes the gate on purpose. If this"
                " is most of the traffic, the issue payload carries no"
                " environment and the alert rule needs to send events."
            )

    lines.append("")
    lines.append(
        "Phase 2. Verdicts are recorded and nothing is acted on. Read a few"
        " of these against the real issue and tell me where the agent is"
        " wrong: a disagreement becomes an answer-key case, which is the"
        " only thing that makes it better."
    )
    return "\n".join(lines)


def run_shadow_report() -> dict:
    """Post yesterday's figures to Slack. One message a day, no interrupts.

    Still one message and still nobody tagged, even now that there are
    verdicts in it. Per-issue posts and mentions are phase 3, after a human
    has read a week of these and decided the verdicts are worth acting on.
    """
    data = sentry_ledger.summary(days=1)
    text = _format_report(data)

    # Same resolver as the per-issue posts, so the report and the issues it
    # describes can never end up in two different channels. See
    # sentry_notify.channel().
    channel = sentry_notify.channel()
    if not channel:
        print("[SENTRY] no SENTRY_AUTOMATIONS set, report not posted")
        print(text)
        return {"status": "no_channel", "summary": data}

    try:
        from slack import client

        client.chat_postMessage(channel=channel, text=text)
    except Exception as exc:
        print(f"[SENTRY] could not post shadow report: {exc}")
        return {"status": "error", "error": str(exc), "summary": data}

    return {"status": "ok", "summary": data}


def run_regate_sweep(limit: int = 25) -> dict:
    """Re-decide issues that were gated for a reason that has since changed.

    Widening `SENTRY_PROJECT_ALLOWLIST` does nothing to the backlog on its
    own, because `issue.created` fires once per group and the ledger never
    reconsiders an id it has seen. This closes that gap: an issue is triaged
    once per gate configuration rather than once ever.

    THE PART THAT IS EASY TO GET WRONG
    ----------------------------------
    These rows were rejected at the project rule, which is third of seven.
    The exception type, title and culprit rules never ran on them. So the
    gate is re-run in full, and anything that fails a different rule goes
    back to gated carrying the NEW reason, rather than being let through on
    the strength of having once been excluded for an unrelated reason.

    Capped per run so that switching on a noisy project does not trigger a
    hundred model calls in one job.
    """
    triaged = sentry_projects.triaged_slugs()
    rows = sentry_ledger.claim_regate(triaged, limit=limit)
    if not rows:
        return {"status": "nothing_to_do", "released": 0}

    passed, regated, failed = 0, 0, 0
    for row in rows:
        # `category` is not a ledger column, so it rebuilds as the default
        # "error". That is safe only because of rule ORDER: issue_category
        # is rule 1 and project is rule 3, so anything gated at project had
        # already passed the category rule and genuinely was an error. If
        # those two are ever reordered, this stops being true.
        signal = sentry_api.IssueSignal(
            issue_id=str(row.get("issue_id") or ""),
            project=row.get("project") or "",
            title=row.get("title") or "",
            culprit=row.get("culprit") or "",
            level=row.get("level") or "",
            environment=row.get("environment") or "",
            platform=row.get("platform") or "",
            exception_type=row.get("exception_type") or "",
            times_seen=int(row.get("times_seen") or 0),
            users_affected=int(row.get("users_affected") or 0),
            permalink=row.get("permalink") or "",
            reason="regate",
        )

        decision = sentry_gate.check(signal)
        if decision.blocked:
            # Still gated, by a rule that was never reached the first time.
            #
            # update() rather than record(): the row already exists, and
            # record()'s second branch only bumps counters. It deliberately
            # never rewrites status or a verdict, so a later sighting cannot
            # undo a decision, which means it is the wrong tool here.
            sentry_ledger.update(
                signal.issue_id,
                status=sentry_ledger.STATUS_GATED,
                gate_rule=decision.rule,
                gate_detail=decision.detail,
            )
            regated += 1
            print(
                f"[SENTRY] regate kept {signal.issue_id} gated "
                f"({decision.rule})"
            )
            continue

        project = sentry_projects.route(signal.project)
        repo = row.get("repo") or (project.repo if project else "")
        if _triage(signal, repo):
            passed += 1
        else:
            failed += 1

    print(
        f"[SENTRY] regate: {len(rows)} released, {passed} triaged, "
        f"{regated} still gated, {failed} could not be triaged"
    )
    return {
        "status": "ok",
        "released": len(rows),
        "triaged": passed,
        "still_gated": regated,
        "failed": failed,
    }


def describe() -> dict:
    """Pipeline health, for /health and the CLI."""
    return {
        "enabled": _enabled(),
        "phase": (
            "5 (triage, analysis, Slack, patch proposal; PRs gated by "
            "SENTRY_AUTO_PR_ENABLED)"
        ),
        "handler": HANDLER_KEY,
        "queue": QUEUE,
        "gate": sentry_gate.describe(),
        "routing": sentry_projects.describe(),
        "notify": sentry_notify.describe(),
        "slack": sentry_slack.describe(),
        "code": sentry_code.describe(),
        "fix": sentry_fix.describe(),
        "sentry": sentry_api.describe(),
    }
