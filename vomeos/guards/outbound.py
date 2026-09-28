"""
vomeos/guards/outbound.py

The last check before anything a model wrote leaves the company.

WHY THIS EXISTS
---------------
On 2026-09-07, ticket #8945 sent a customer an email in which
the model, instead of writing a client reply, explained to its operator why it
could not write one. It quoted internal dev notes, named an engineer, listed
unverified bugs, referenced the system prompt and internal routing rules, and
offered to "draft that structured update template now". It was signed as the
support team and the ticket was closed immediately after.

The only gate at the time was:

    can_send = bool(contact_email) and bool(draft) and len(draft.strip()) >= 20

That is "has an address and is at least 20 characters". It does not check that
the text is actually a message to a customer.

The model behaved reasonably. It was told to explain that a feature works as
intended, on a ticket that was in fact seven open bugs, and it refused. The
bug was that any string of 20+ characters was treated as sendable.

WHY IT LIVES IN THE KERNEL
--------------------------
This started as one module inside the support app, wired into three code paths
by hand. As soon as there is more than one division sending things outward,
"remember to call the guard" stops being a policy and becomes a coin flip. So
the OS owns it, every agent declares it in its manifest, and the onboarding
gate refuses to let a client-facing agent ship without it.

DESIGN RULES
------------
Deterministic. No model call, no network. This runs on the output of a model
that has already misbehaved, so asking another model to grade it adds a second
thing that can fail open. Every check here is a regex or a length test.

Fails closed. A rejected message is not sendable and the caller must route it
to a human. There is no "send it anyway and log a warning" path.

Narrow patterns. A false positive costs one draft going to a person. A false
negative costs a customer seeing our internals. But a guard that fires on
ordinary support copy gets switched off, so no pattern may match normal text
such as "our team is looking into it" or "we have pushed an update".
"""

from __future__ import annotations

import re

from vomeos import config
from vomeos.text import body_without_signature

# ---------------------------------------------------------------------------
# Blocking patterns
# ---------------------------------------------------------------------------

_META_PATTERNS = [
    # The model addressing its operator or talking about its own task.
    ("refers to its prompt or instructions", re.compile(
        r"\b(?:the|my|this)\s+(?:system\s+)?prompt\b"
        r"|\bsystem\s+prompt\b"
        r"|\bmy\s+instructions\b"
        r"|\bas\s+an\s+AI\b"
        r"|\bas\s+a\s+language\s+model\b",
        re.IGNORECASE)),
    ("declines or defers the task", re.compile(
        r"\bI\s+(?:cannot|can't|can\s+not|am\s+unable\s+to|won't|will\s+not)"
        r"\s+(?:write|draft|produce|generate|complete|comply|confirm)\b"
        r"|\bwhy\s+I\s+(?:cannot|can't|can\s+not)\b"
        r"|\bI\s+need\s+to\s+pause\b"
        r"|\bI\s+must\s+(?:never|not)\b"
        r"|\bthere\s+is\s+nothing\s+to\s+respond\s+to\b"
        r"|\bwhat\s+should\s+actually\s+happen\b",
        re.IGNORECASE)),
    ("talks about drafting instead of being the message", re.compile(
        r"\b(?:the|this|that|a|requested)\s+draft\b"
        r"|\bdraft\s+(?:response|reply|message|update|template)\b"
        r"|\bI\s+(?:can|could|will|'ll)\s+draft\b"
        r"|\bbefore\s+drafting\b"
        r"|\bplaceholders?\b",
        re.IGNORECASE)),
    ("asks the reader to authorize more work", re.compile(
        r"\bwould\s+you\s+like\s+me\s+to\b"
        r"|\bif\s+you\s+would\s+like,?\s+I\s+can\b"
        r"|\blet\s+me\s+know\s+if\s+you\s+want\s+me\s+to\b"
        r"|\bshall\s+I\b",
        re.IGNORECASE)),
]

_INTERNAL_PATTERNS = [
    ("exposes internal notes or tooling", re.compile(
        r"\bdev(?:'s|s')?\s+notes?\b"
        r"|\bengineer(?:'s|s')?\s+notes?\b"
        r"|\binternal\s+note\b"
        r"|\bClickUp\b"
        r"|\bZoho\b"
        r"|\bSlack\b"
        r"|\bthe\s+dev(?:s)?\b"
        r"|\bthe\s+engineer(?:s)?\b"
        r"|\bengineering\s+team\b",
        re.IGNORECASE)),
    ("exposes internal process or routing", re.compile(
        r"\brouting\s+rules?\b"
        r"|\baccount\s+tier\b"
        r"|\breviewed\s+by\s+\w+\s+before\s+sending\b"
        r"|\bper\s+(?:our\s+)?(?:internal\s+)?(?:routing|escalation)\b",
        re.IGNORECASE)),
    ("discusses unverified or unconfirmed fix status", re.compile(
        r"\bnot\s+verified\b"
        r"|\bunverified\b"
        r"|\bwithout\s+confirming\b"
        r"|\bfix\s+claim\b"
        r"|\bbefore\s+verification\b",
        re.IGNORECASE)),
]

_FORMAT_PATTERNS = [
    # Outbound mail is sent as plain text, so markdown markup never renders.
    # Its presence means the text was not written as an email.
    ("contains markdown markup", re.compile(
        r"\*\*|^#{1,6}\s|```", re.MULTILINE)),
]

_MIN_CHARS = 20


def validate_client_message(
    draft: str,
    *,
    category: str = "",
    contact_name: str = "",
    require_signature: bool = True,
) -> dict:
    """Decide whether `draft` is safe to send to someone outside the company.

    Returns::

        {
          "ok": bool,          # False means DO NOT SEND, route to a human
          "reasons": [str],    # blocking reasons, safe to show in Slack
          "warnings": [str],   # non-blocking notes
        }

    Fails closed: an empty or unparseable draft is not ok.
    """
    reasons: list[str] = []
    warnings: list[str] = []

    text = (draft or "").strip()
    if not text:
        return {"ok": False, "reasons": ["draft is empty"], "warnings": []}

    if len(text) < _MIN_CHARS:
        reasons.append("draft is too short to be a real reply")

    if len(text) > config.OUTBOUND_MAX_CHARS:
        reasons.append(
            f"draft is {len(text)} characters, over the "
            f"{config.OUTBOUND_MAX_CHARS} limit for an auto-sent reply"
        )

    body = body_without_signature(text, config.SIGNATURE_DOMAIN)

    for group in (_META_PATTERNS, _INTERNAL_PATTERNS, _FORMAT_PATTERNS):
        for reason, pattern in group:
            match = pattern.search(body)
            if match:
                matched = match.group(0).strip()
                reasons.append(f'{reason} (matched "{matched}")')

    # Internal teammate names in the body. The signature is already stripped,
    # so a message legitimately signed by Sam does not trip this.
    contact_tokens = {
        token.lower() for token in re.findall(r"[A-Za-z]+", contact_name or "")
    }
    for name in config.internal_names():
        if name in contact_tokens:
            continue  # the recipient is called this; not an internal leak
        if re.search(rf"\b{re.escape(name)}\b", body, re.IGNORECASE):
            reasons.append(f'names an internal team member ("{name}")')

    if require_signature and config.SIGNATURE_DOMAIN not in text:
        reasons.append("signature block is missing")

    # Non-blocking. Every drafting charter forbids em dashes, so one getting
    # through means the instructions were not followed, which is worth seeing.
    if "—" in text or "–" in text:
        warnings.append("contains an em dash or en dash")

    ok = not reasons
    if not ok:
        label = f"[GUARD] {category or 'outbound'} draft BLOCKED: "
        print(label + "; ".join(reasons))
    return {"ok": ok, "reasons": reasons, "warnings": warnings}


def guard_failure_notice(category: str, result: dict) -> str:
    """Slack-ready explanation of why a message was held back."""
    lines = [
        ":no_entry: *Outbound guard blocked this reply. Nothing was sent "
        "to the client and the ticket was left open.*",
        f"*Category:* {category or 'unknown'}",
        "*Why:*",
    ]
    lines += [f"> {reason}" for reason in result.get("reasons", [])]
    if result.get("warnings"):
        lines.append("*Also noted:* " + "; ".join(result["warnings"]))
    lines.append(
        "Rewrite it in the thread and send, or cancel. Do not click send "
        "on the draft below without reading it."
    )
    return "\n".join(lines)
