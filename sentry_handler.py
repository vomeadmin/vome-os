"""
sentry_handler.py

The Sentry pipeline's orchestration. Phase 1: ingress, gate, redaction and
ledger. No model call, no Slack, no pull request.

THE SPLIT, AND WHY IT IS WHERE IT IS
------------------------------------
Two halves, and which work happens on which side is the whole performance and
safety story.

**In the web request** (`handle_webhook`): verify the signature, parse,
redact, normalise, and run the gate. All of it is deterministic, none of it
touches a model or a third party, and it is over in microseconds. An issue the
gate drops is recorded and never reaches the queue at all, which is what makes
stage 2 of the funnel genuinely free.

**On the worker** (`handle_sentry_issue`): claim the issue in the ledger and,
from phase 2, everything expensive. It is here rather than in the request so
that a deploy, a slow Sentry API or a model call cannot take the endpoint down
with it, and so that a flood lands in a queue instead of in the web dyno.

Note the consequence of eager mode: with no `VOMEOS_BROKER_URL` set, the
enqueue runs inline in the web request. That is survivable for phase 1, where
the queued half is one INSERT, and it is exactly why the Redis cutover is a
prerequisite for phase 2.

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

import sentry_gate
import sentry_ledger
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

    # Phase 2 continues from here: run_agent("engineering.sentry_triage"),
    # then the ClickUp check, then the analyst. Deliberately not stubbed with
    # a half-implementation: shadow mode means shadow mode.
    return {
        "status": "recorded",
        "issue_id": signal.issue_id,
        "acted": True,
        "ledger": claim.recorded,
        "repo": (message or {}).get("repo", ""),
        "ref": (message or {}).get("repo_ref", ""),
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
        "*Sentry shadow report* (last 24h, no action taken)",
        "",
        f"Deliveries: {total_hits}",
        f"Distinct issues: {surviving + gated}",
        f"Survived the gate: {surviving}",
        f"New and surviving: {data.get('new_surviving_issues', 0)}",
        f"Dropped by the gate: {gated}",
    ]

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
        "Phase 1. Nothing was triaged, posted or fixed. These are the"
        " numbers the phase 2 thresholds should be set from."
    )
    return "\n".join(lines)


def run_shadow_report() -> dict:
    """Post yesterday's figures to Slack. One message a day, no interrupts."""
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
        "phase": "1 (shadow: gate and ledger only)",
        "handler": HANDLER_KEY,
        "queue": QUEUE,
        "gate": sentry_gate.describe(),
        "routing": sentry_projects.describe(),
        "sentry": sentry_api.describe(),
    }
