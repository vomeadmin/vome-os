"""
vomeos/guards/claims.py

Guards against asserting a state that no record backs.

WHY THIS EXISTS
---------------
This is not a theoretical risk. It is the measured number one failure of the
support operation, recorded in the Sprint Lessons Log on 2026-09-02:

    "Never tell a client something is fixed without a verified task behind
    it. This was the single biggest failure pattern of the day and it
    appeared four times... Three of those four clients came back saying it
    was still broken."

The same day produced three more of the same shape: "on our roadmap" with no
task anywhere, "your data is intact" before anyone had checked, and two
replies signed by the wrong identity.

Every one of those is an assertion about the world that nobody had verified.
That class is enumerable, so it can be blocked deterministically rather than
prompted against. A prompt that says "do not claim a fix" is advice. A guard
that refuses to pass the draft is a rule.

THE EVIDENCE PATTERN
--------------------
These guards do not try to decide whether a claim is true. They check whether
the caller supplied evidence for it, and the evidence is a ClickUp task in a
status that means the work actually shipped.

    run_agent(..., guard_context={
        "verified_fix_task": "868kzkb9b",
        "verified_fix_status": "on prod",
    })

No evidence means the claim cannot be made. That puts the burden on the
caller to have looked, which is exactly the step that was being skipped.

FALSE POSITIVES
---------------
Kept narrow on purpose. The playbook's own warning applies: "a guard that
fires on ordinary replies gets switched off". Every pattern here matches
language the playbook explicitly forbids, so a match is a real violation
rather than an unlucky phrasing.
"""

from __future__ import annotations

import re

from vomeos import config
from vomeos.guards import GuardResult, guard
from vomeos.text import body_without_signature

# Statuses that mean the work actually shipped. Anything else (queued, in
# progress, on dev) is work in flight, and telling a client about work in
# flight as though it were done is the exact failure being blocked.
SHIPPED_STATUSES = frozenset({"on prod", "closed", "done", "complete"})

# ---------------------------------------------------------------------------
# Claim patterns
# ---------------------------------------------------------------------------

_FIX_CLAIM = re.compile(
    r"\b(?:has\s+been|have\s+been|is|are|now)\s+(?:fixed|resolved|corrected)\b"
    r"|\b(?:we|our\s+team)\s+(?:have|'ve|has)\s+(?:fixed|resolved|deployed|"
    r"pushed|shipped|released)\b"
    r"|\b(?:we|our\s+team)\s+(?:fixed|resolved|deployed|pushed|shipped)\b"
    r"|\bpushed\s+(?:an?\s+)?(?:update|fix|change|patch)\b"
    r"|\bdeployed\s+(?:an?\s+)?(?:update|fix|change|patch)\b"
    r"|\bshould\s+(?:now\s+)?(?:be\s+)?(?:working|resolved|fixed|sorted|"
    r"back\s+to\s+normal)\b"
    r"|\bshould\s+be\s+(?:all\s+)?(?:set|good)\b"
    r"|\bthis\s+is\s+now\s+live\b"
    r"|\bthe\s+fix\s+is\s+(?:live|out|deployed)\b",
    re.IGNORECASE,
)

_ROADMAP_CLAIM = re.compile(
    r"\bon\s+(?:our|the)\s+roadmap\b"
    r"|\bin\s+(?:our|the)\s+roadmap\b"
    r"|\b(?:we|our\s+team)\s+(?:are|'re|is)\s+planning\s+to\b"
    r"|\bit(?:'s|\s+is)\s+(?:already\s+)?(?:planned|scheduled)\b"
    r"|\bslated\s+for\s+(?:an?\s+)?(?:upcoming|future)\b",
    re.IGNORECASE,
)

# The No-Confirmation Rule, verbatim from the playbook's "Do not say" list.
# These claim technical understanding of a bug before engineering has looked.
_CONFIRMATION_CLAIM = [
    ("confirms the bug before engineering has investigated", re.compile(
        r"\bwe\s+can\s+see\s+(?:the\s+)?(?:issue|problem|error|bug)\b"
        r"|\bwe(?:'ve|\s+have)\s+confirmed\b"
        r"|\bwe(?:'ve|\s+have)\s+identified\s+(?:the\s+)?(?:issue|problem|"
        r"bug|cause|root)\b"
        r"|\bwe\s+(?:can|were\s+able\s+to)\s+reproduce\b"
        r"|\bwe(?:'ve|\s+have)\s+replicated\b",
        re.IGNORECASE)),
    ("names the faulty component to the client", re.compile(
        r"\bthe\s+(?:issue|problem|bug)\s+is\s+(?:with|in|caused\s+by)\s+"
        r"(?:the|our)\b",
        re.IGNORECASE)),
    ("asserts data is safe before anyone has verified it", re.compile(
        r"\byour\s+data\s+(?:is|are|remains?)\s+(?:safe|intact|fine|secure|"
        r"unaffected|there)\b"
        r"|\bnothing\s+(?:has\s+been|was)\s+lost\b"
        r"|\bno\s+data\s+(?:has\s+been|was)\s+lost\b",
        re.IGNORECASE)),
    ("claims to have looked at an account before anyone did", re.compile(
        r"\bwe\s+can\s+see\s+(?:\w+(?:'s)?\s+)?(?:account|profile|"
        r"submission|record)\b",
        re.IGNORECASE)),
]

# Quality judgments about an attachment nobody has opened yet.
_UNSEEN_ATTACHMENT = re.compile(
    r"\b(?:super|really|very|crystal|perfectly)\s+(?:clear|helpful|useful)\b"
    r"|\bthat(?:'s|\s+is)\s+(?:super|really|very)\s+helpful\b",
    re.IGNORECASE,
)


def _evidence(ctx: dict) -> tuple[str, str]:
    task = str(ctx.get("verified_fix_task") or "").strip()
    status = str(ctx.get("verified_fix_status") or "").strip().lower()
    return task, status


def _has_shipped_evidence(ctx: dict) -> bool:
    task, status = _evidence(ctx)
    return bool(task) and status in SHIPPED_STATUSES


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

@guard("no_unverified_claims")
def _no_unverified_claims(text: str, ctx: dict) -> GuardResult:
    """A fix may only be announced when a shipped task backs it.

    Pass requires `verified_fix_task` plus a `verified_fix_status` in
    SHIPPED_STATUSES. A task sitting at queued or in progress is not
    evidence: that was the Dallas Retirement Village case, where a client was
    told hour approval "should be restored" while the task had no work
    recorded against it.
    """
    body = body_without_signature(text or "", config.SIGNATURE_DOMAIN)
    match = _FIX_CLAIM.search(body)
    if not match:
        return GuardResult("no_unverified_claims", True)

    if _has_shipped_evidence(ctx):
        task, status = _evidence(ctx)
        return GuardResult(
            "no_unverified_claims", True,
            f"fix claim backed by task {task} ({status})",
        )

    task, status = _evidence(ctx)
    if task:
        detail = f"task {task} is {status or 'of unknown status'}, not shipped"
    else:
        detail = "no verified task was supplied"
    return GuardResult(
        "no_unverified_claims", False,
        f'claims a fix ("{match.group(0).strip()}") but {detail}. Confirm '
        "the work exists and shipped, or remove the claim.",
    )


@guard("no_roadmap_without_task")
def _no_roadmap_without_task(text: str, ctx: dict) -> GuardResult:
    """Roadmap language requires a task to point at.

    From the lessons log: a client was told a sync was on the roadmap when
    nothing existed anywhere, and a task had to be created retroactively to
    make the statement true.
    """
    body = body_without_signature(text or "", config.SIGNATURE_DOMAIN)
    match = _ROADMAP_CLAIM.search(body)
    if not match:
        return GuardResult("no_roadmap_without_task", True)

    task = str(ctx.get("roadmap_task") or ctx.get("verified_fix_task") or "")
    if task.strip():
        return GuardResult(
            "no_roadmap_without_task", True,
            f"roadmap claim backed by task {task.strip()}",
        )
    return GuardResult(
        "no_roadmap_without_task", False,
        f'uses roadmap language ("{match.group(0).strip()}") with no task to '
        "point at. Log the request first, or drop the promise.",
    )


@guard("no_confirmation")
def _no_confirmation(text: str, ctx: dict) -> GuardResult:
    """The No-Confirmation Rule.

    Never confirm, diagnose, or describe a bug's status to a client before
    engineering has investigated. Resolution replies are the exception and
    they carry shipped evidence, so this guard stands down for those.
    """
    body = body_without_signature(text or "", config.SIGNATURE_DOMAIN)

    reasons = []
    for label, pattern in _CONFIRMATION_CLAIM:
        match = pattern.search(body)
        if match:
            reasons.append(f'{label} (matched "{match.group(0).strip()}")')

    attachment = _UNSEEN_ATTACHMENT.search(body)
    if attachment and not ctx.get("attachment_reviewed"):
        reasons.append(
            "judges the quality of an attachment nobody has confirmed "
            f'looking at (matched "{attachment.group(0).strip()}")'
        )

    if not reasons:
        return GuardResult("no_confirmation", True)

    # A resolution reply on a shipped fix is allowed to describe what was
    # wrong, because by then engineering has actually investigated.
    if _has_shipped_evidence(ctx) and ctx.get("resolution_reply"):
        task, status = _evidence(ctx)
        return GuardResult(
            "no_confirmation", True,
            f"resolution reply on shipped task {task} ({status})",
        )

    return GuardResult("no_confirmation", False, "; ".join(reasons))


@guard("required_signature")
def _required_signature(text: str, ctx: dict) -> GuardResult:
    """Approved replies go out under one identity, not the agent's.

    The signature rule was breached twice in a single sprint, both signed
    "Vic / Support Team". The agent classifies, enriches and drafts. A person
    approves and sends, and the signature says so.
    """
    required = config.REQUIRED_SIGNATURE
    if not required:
        return GuardResult("required_signature", True)

    body = text or ""
    if required.lower() not in body.lower():
        return GuardResult(
            "required_signature", False,
            f'signature must read "{required}"',
        )

    for forbidden in config.forbidden_signatures():
        if re.search(rf"\b{re.escape(forbidden)}\b", body, re.IGNORECASE):
            return GuardResult(
                "required_signature", False,
                f'signed as "{forbidden}"; approved replies go out as '
                f'"{required}"',
            )
    return GuardResult("required_signature", True)
