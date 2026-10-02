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

import sentry_event
import sentry_gate
import sentry_ledger
import sentry_notify
import sentry_projects
import sentry_redact
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

    return {
        "status": "triaged" if triage else "recorded",
        "issue_id": signal.issue_id,
        "acted": True,
        "ledger": claim.recorded,
        "repo": (message or {}).get("repo", ""),
        "ref": (message or {}).get("repo_ref", ""),
        "verdict": (triage or {}).get("verdict", ""),
        "severity": (triage or {}).get("severity", ""),
    }


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

    actionable = data.get("actionable") or []
    if actionable:
        lines.append("")
        lines.append("*Called actionable:*")
        for row in actionable:
            sev = (row.get("severity") or "?").upper()
            infra = " [infra]" if row.get("infrastructure") else ""
            summary = row.get("triage_summary") or row.get("title") or ""
            lines.append(f"  {sev}{infra} {summary[:150]}")
            if row.get("permalink"):
                lines.append(f"       {row['permalink']}")

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

    channel = os.environ.get("SLACK_CHANNEL_ENG_ALERTS", "")
    if not channel:
        print("[SENTRY] no SLACK_CHANNEL_ENG_ALERTS set, report not posted")
        print(text)
        return {"status": "no_channel", "summary": data}

    try:
        from slack import client

        client.chat_postMessage(channel=channel, text=text)
    except Exception as exc:
        print(f"[SENTRY] could not post shadow report: {exc}")
        return {"status": "error", "error": str(exc), "summary": data}

    return {"status": "ok", "summary": data}


def describe() -> dict:
    """Pipeline health, for /health and the CLI."""
    return {
        "enabled": _enabled(),
        "phase": "2 (triage verdicts recorded, nothing acted on)",
        "handler": HANDLER_KEY,
        "queue": QUEUE,
        "gate": sentry_gate.describe(),
        "routing": sentry_projects.describe(),
        "notify": sentry_notify.describe(),
        "sentry": sentry_api.describe(),
    }
