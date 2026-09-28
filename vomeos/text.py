"""
vomeos/text.py

Text utilities the kernel needs, owned by the kernel.

WHY THIS EXISTS
---------------
The outbound guard has to know where a message body ends and its signature
block begins, because the signer's own name legitimately appears in the
signature and must not trip the internal-names check. That logic used to come
from the support app's `signatures` module, which made the OS unable to boot
without it.

These functions are the kernel's own copy of that narrow piece. Note what is
deliberately NOT here: the sender identities themselves (who signs what, in
which language, with which block). Those are an application's business. The OS
only needs to recognise the shape of a sign-off, not to produce one.
"""

from __future__ import annotations

import re

# A rule, a row of dashes, a line of underscores. Anything that is only
# separator characters.
_SEPARATOR_RE = re.compile(r"^[\s─\-=_*~]+$")

# Closing words a model emits despite being told not to sign off. English and
# French, because support answers in the language the customer wrote in.
_CLOSING_WORDS = frozenset({
    "best", "regards", "best regards", "kind regards", "warm regards",
    "warmly", "best wishes", "sincerely", "cheers", "thanks", "thank you",
    "cordialement", "sincerement", "sincèrement",
    "bien a vous", "bien à vous", "merci",
})

# Standalone trailing lines that are part of a sign-off rather than the body.
_SIGNOFF_LINES = frozenset({
    "vic", "sam", "vome team", "sam | vome team", "sam | vome support",
    "vome support", "equipe vome", "équipe vome",
    "support team", "vome volunteer", "support.vomevolunteer.com",
})

_FENCE_OPEN = re.compile(r"\A```(?:[a-zA-Z0-9_-]+)?\s*\n?")
_FENCE_CLOSE = re.compile(r"\n?```\s*\Z")


def strip_code_fence(text: str) -> str:
    """Remove a wrapping markdown code fence.

    Models wrap JSON in fences despite instructions. Tolerating it once here
    is better than a private regex in every caller.
    """
    cleaned = _FENCE_OPEN.sub("", (text or "").strip())
    return _FENCE_CLOSE.sub("", cleaned).strip()


def strip_trailing_signoff(text: str) -> str:
    """Drop a trailing sign-off block from the end of `text`.

    Removes closing words, standalone name and domain lines, separators and
    blank lines, from the bottom up, stopping at the first line that is real
    body content. Text that does not end in a recognisable sign-off comes back
    unchanged.
    """
    lines = (text or "").rstrip("\n").split("\n")
    while lines:
        last = lines[-1].strip()
        normalised = last.rstrip(",").lower()
        if (
            not last
            or _SEPARATOR_RE.match(last)
            or normalised in _CLOSING_WORDS
            or normalised in _SIGNOFF_LINES
        ):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def body_without_signature(text: str, signature_domain: str) -> str:
    """The message body with its appended signature block removed.

    The signature is found by its domain, which is the one token guaranteed to
    be in every signature block and never in ordinary support copy.
    """
    body = text or ""
    if signature_domain:
        index = body.rfind(signature_domain)
        if index != -1:
            body = body[:index]
    return strip_trailing_signoff(body)
