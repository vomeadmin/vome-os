"""
sentry_redact.py

Strip personal and secret data out of a Sentry payload before it is stored,
prompted or posted.

WHY THIS RUNS FIRST, BEFORE ANYTHING ELSE
-----------------------------------------
A Sentry event carries whatever was in scope when the process blew up. In
practice that means customer email addresses, user IDs, IP addresses, request
bodies, query strings, cookies, session tokens, `Authorization` headers, and
the local variables of every frame in the stack.

That payload is about to go three places it can never be taken back from: a
Postgres row, a model prompt, and a Slack channel. So redaction is the first
thing that happens to a webhook body, before the gate reads it and before
anything is queued. The invariant the rest of the pipeline relies on is that
no unredacted Sentry data exists anywhere downstream of `handle_webhook`.

THIS IS THE SECOND LINE, NOT THE FIRST
--------------------------------------
Turn on Sentry's own server-side data scrubbing as well. Data that never
reaches Sentry cannot leak from Sentry, and scrubbing at our end only protects
the copy we make. Both, not either.

WHAT IS DELIBERATELY KEPT
-------------------------
Stack frames, file paths, line numbers, function names, module names, release
versions and commit SHAs. All of it is needed to diagnose anything, none of it
is personal, and a redactor that eats the stack trace has made the pipeline
useless to protect data that was not at risk.

Frame local variables are kept but scrubbed, key by key and value by value.
They are the single most useful thing in a Sentry event for working out what
actually happened, and the single most likely place for a password to be
sitting in a variable called `password`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

# Cap on any single string we keep. A 200KB SQL statement in `extra` informs
# nothing that its first 2000 characters did not.
MAX_STRING = int(os.environ.get("SENTRY_REDACT_MAX_STRING", "2000"))

# Cap on nesting. Sentry payloads are deep but not unbounded, and a cycle or a
# hostile payload should not be able to spin us.
MAX_DEPTH = int(os.environ.get("SENTRY_REDACT_MAX_DEPTH", "12"))

# Cap on the number of items kept from any one list.
MAX_ITEMS = int(os.environ.get("SENTRY_REDACT_MAX_ITEMS", "50"))

REDACTED = "[redacted]"

# ---------------------------------------------------------------------------
# Key rules. Matched case-insensitively against the key, with separators
# stripped, so "X-Api-Key", "api_key" and "apikey" are all one rule.
# ---------------------------------------------------------------------------

# Dropped entirely. The value is replaced, never truncated or hashed, because
# a prefix of a token is still a token to anyone attacking it.
DROP_KEYS = frozenset(
    {
        "authorization", "proxyauthorization", "cookie", "cookies",
        "setcookie", "apikey", "xapikey", "xsupportapikey", "xauthtoken",
        "token", "accesstoken", "refreshtoken", "idtoken", "bearertoken",
        "password", "passwd", "pwd", "secret", "clientsecret", "privatekey",
        "sessionid", "session", "sessionkey", "csrftoken", "csrfmiddlewaretoken",
        "auth", "credentials", "signature", "xsignature",
        "creditcard", "cardnumber", "cvv", "cvc", "ssn", "sin",
        "dateofbirth", "dob",
    }
)

# Replaced with a stable hash. The same person is recognisably the same
# person across issues, which is what "how many users are affected" needs,
# without us holding who they are.
HASH_KEYS = frozenset(
    {
        "email", "emailaddress", "username", "ipaddress", "ip",
        "remoteaddr", "xforwardedfor", "phone", "phonenumber",
        "firstname", "lastname", "fullname", "name",
        # Mobile device identifiers. These matter more than they look: a
        # device id is persistent and singles out one person's handset, which
        # makes it personal data under GDPR even though it contains no name.
        #
        # They need an explicit rule because the value-level patterns
        # deliberately leave long hex strings alone (a 40 character hex string
        # is a git SHA far more often than a secret), so a device id would
        # otherwise sail straight through into Postgres and Slack.
        #
        # Hashed rather than dropped, so "how many devices are affected" stays
        # answerable without us holding whose they are.
        # `device_app_hash` is the one that actually appears in Sentry's App
        # context, rendered in the UI as a "Device" row holding a 40 character
        # hex string. It is this install on this handset.
        "deviceapphash", "deviceuniqueidentifier",
        "deviceid", "deviceuniqueid", "installationid", "udid",
        "idfa", "idfv", "advertisingid", "androidid", "vendorid",
        "pushtoken", "devicetoken",
    }
)

# Request headers worth keeping. Everything else in a headers container is
# dropped, because an allowlist is the only way to be right about a container
# whose keys are chosen by whoever sent the request.
HEADER_ALLOWLIST = frozenset(
    {
        "contenttype", "contentlength", "useragent", "referer", "referrer",
        "accept", "acceptlanguage", "acceptencoding", "host", "origin",
        "xrequestid", "xcorrelationid", "xrequestedwith",
    }
)

# Containers whose keys are attacker or user controlled, so the allowlist
# applies inside them rather than the drop list.
HEADER_CONTAINERS = frozenset({"headers", "header"})


# Keys whose values are identifiers or structural metadata, never prose.
# Their values are truncated but never pattern-scrubbed.
#
# Belt and braces alongside the Luhn check. Both exist because corrupting an
# identifier is silent: nothing errors, the data just stops meaning what it
# said, and the first symptom is a pipeline that quietly does nothing.
ID_KEYS = frozenset(
    {
        "id", "issueid", "groupid", "eventid", "projectid", "organizationid",
        "shortid", "culprit", "level", "platform", "slug", "type",
        "build", "appbuild", "appversion", "appidentifier", "release",
        "version", "count", "usercount", "timesseen", "lineno", "colno",
        "filename", "function", "module", "abspath", "permalink", "weburl",
    }
)


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# ---------------------------------------------------------------------------
# Value rules. Applied to every string we keep, including ones whose key
# looked innocent, because `extra.debug_info` is where a token ends up.
# ---------------------------------------------------------------------------

_VALUE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Email addresses, anywhere in any string. The most common piece of
    # personal data in an error message.
    (
        re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.I),
        "[email]",
    ),
    # JSON Web Tokens. Three base64url segments, and the header almost always
    # starts `eyJ`.
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
        "[jwt]",
    ),
    # A secret introduced by its own name: token=..., "api_key": "...",
    # password: ... and so on.
    (
        re.compile(
            r"(?i)\b(bearer|token|api[_-]?key|secret|password|passwd|pwd|"
            r"authorization)\b\s*[:=]?\s*[\"']?([A-Za-z0-9._\-]{8,})",
        ),
        r"\1=[redacted]",
    ),
    # AWS access key IDs.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws-key]"),
    # Slack and GitHub style prefixed tokens.
    (
        re.compile(r"\b(xox[baprs]|ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_-]{10,}"),
        "[token]",
    ),
    # Stripe keys, live and test.
    (re.compile(r"\b[sr]k_(live|test)_[A-Za-z0-9]{10,}\b"), "[stripe-key]"),
    # Payment card numbers. See _redact_card below: the issuer prefix alone
    # is not enough, because a Sentry issue id is a 16 digit number that
    # frequently starts with 4 or 5.
    # PEM private key blocks.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        "[private-key]",
    ),
)


# Candidate card numbers: an issuer prefix and 13 to 19 digits, optionally
# grouped. Validated by Luhn before anything is replaced.
_CARD_CANDIDATE = re.compile(
    r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))"
    r"[ -]?\d{4}[ -]?\d{4}[ -]?\d{2,7}\b"
)


def _luhn(digits: str) -> bool:
    """The check digit algorithm every real card number satisfies."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_card(match: re.Match) -> str:
    """Replace a card number, but only if it really is one.

    WHY THE LUHN CHECK IS NOT OPTIONAL
    ----------------------------------
    The issuer prefix on its own is not a card detector, it is a "starts with
    4 or 5" detector, and a Sentry issue id is a 16 digit number that often
    starts with 4 or 5. The real issue id 4506427274952704 matched the
    prefix-only pattern and was rewritten to "[card]".

    That is not a cosmetic bug. The issue id is the ledger's primary key and
    the pipeline's entire notion of identity, so every issue would have
    collapsed into one row, the first would have won the claim, and every
    issue after it would have been marked "already known" and silently
    discarded. The pipeline would have looked healthy and done nothing.

    Luhn rejects that id. It is also what distinguishes a card from any other
    long number, which is what the pattern was reaching for in the first
    place.
    """
    candidate = match.group(0)
    digits = re.sub(r"[ -]", "", candidate)
    if len(digits) < 13 or not _luhn(digits):
        return candidate
    return "[card]"


def _salt() -> str:
    """Salt for identifier hashing.

    Falls back to the webhook secret so there is always a real secret behind
    the hash. An unsalted hash of an email address is not anonymisation: the
    space of email addresses is small enough to enumerate.
    """
    return (
        os.environ.get("SENTRY_HASH_SALT")
        or os.environ.get("SENTRY_WEBHOOK_SECRET")
        or "vomeos-unsalted"
    )


def pseudonymize(value: Any) -> str:
    """A short, stable, salted hash of an identifier."""
    text = str(value)
    if not text:
        return ""
    digest = hashlib.sha256((_salt() + text).encode("utf-8")).hexdigest()
    return f"u_{digest[:12]}"


def _truncate_text(value: str) -> str:
    if len(value) > MAX_STRING:
        return value[:MAX_STRING] + f"... [truncated at {MAX_STRING}]"
    return value


def redact_text(value: str) -> str:
    """Run every value rule over one string, then truncate it."""
    if not isinstance(value, str) or not value:
        return value
    cleaned = value
    for pattern, replacement in _VALUE_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = _CARD_CANDIDATE.sub(_redact_card, cleaned)
    return _truncate_text(cleaned)


def _redact_value(value: Any, depth: int, in_headers: bool) -> Any:
    if depth > MAX_DEPTH:
        return "[depth-limit]"
    if isinstance(value, dict):
        return _redact_mapping(value, depth, in_headers)
    if isinstance(value, (list, tuple)):
        items = list(value)[:MAX_ITEMS]
        out = [_redact_value(item, depth + 1, in_headers) for item in items]
        if len(value) > MAX_ITEMS:
            out.append(f"... [{len(value) - MAX_ITEMS} more omitted]")
        return out
    if isinstance(value, str):
        return redact_text(value)
    return value


def _redact_pair(key: Any, value: Any, depth: int, in_headers: bool) -> Any:
    """Apply the key rules to one key and value."""
    norm = _norm_key(key)

    if norm in DROP_KEYS:
        return REDACTED
    if in_headers and norm not in HEADER_ALLOWLIST:
        # Allowlist inside a headers container: the keys are chosen by the
        # sender, so anything not recognised is assumed sensitive.
        return REDACTED
    if norm in HASH_KEYS and isinstance(value, (str, int)):
        return pseudonymize(value) if str(value) else ""
    if norm in ID_KEYS and isinstance(value, str):
        # An identifier, not prose. Truncate it, never rewrite it.
        return _truncate_text(value)

    child_headers = in_headers or norm in HEADER_CONTAINERS
    return _redact_value(value, depth + 1, child_headers)


def _redact_mapping(data: dict, depth: int, in_headers: bool) -> dict:
    out: dict = {}
    for key, value in list(data.items())[: MAX_ITEMS * 4]:
        out[key] = _redact_pair(key, value, depth, in_headers)
    return out


def redact(payload: Any) -> Any:
    """Redact a whole Sentry payload. Returns a new object, never mutates.

    Safe to call on anything: a webhook body, one event, a list of frames.
    """
    if isinstance(payload, dict):
        return _redact_mapping(payload, 0, False)
    return _redact_value(payload, 0, False)


def summarize_redaction(before: Any, after: Any) -> dict:
    """How much was removed, for the shadow-mode report.

    Not a security control. It is here so that when someone asks whether the
    redactor is doing anything, the answer is a number rather than a belief.
    """
    try:
        raw = len(json.dumps(before, default=str))
        clean = len(json.dumps(after, default=str))
    except (TypeError, ValueError):
        return {"bytes_before": 0, "bytes_after": 0, "redactions": 0}
    try:
        redactions = json.dumps(after, default=str).count(REDACTED)
    except (TypeError, ValueError):
        redactions = 0
    return {
        "bytes_before": raw,
        "bytes_after": clean,
        "redactions": redactions,
    }
