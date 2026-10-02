"""
vomeos/guards/patch.py

Deterministic checks on a proposed code change.

WHY A GUARD AND NOT A PROMPT
----------------------------
Every rule here could be written into a charter, and a charter is an
instruction a model can reason its way around, especially one that has just
convinced itself a change is obviously safe. This runs on the output, after
the reasoning, and it cannot be argued with.

The rule from VOMEOS.md holds: no guard may call a model. A guard runs on the
output of a model that has already misbehaved, so grading it with a second
model adds a second thing that can fail open.

THE MIGRATION RULE IS NOT A GENERIC CAUTION
-------------------------------------------
Any path under `migrations/` is refused outright, with no override and no
configuration. In the restored core and chats repositories the server
generates migrations on deploy, so a hand-written migration committed by an
agent is a production incident rather than a bad patch. It is also precisely
the change an agent is most likely to classify as small and obvious: a column
that should be nullable looks like a one-line diff.

WHAT "SAFE" MEANS HERE
----------------------
Not "correct". Nothing deterministic can tell whether a fix works. These
rules bound the blast radius of being wrong: few files, few lines, nothing
that touches authentication, payments, settings or tests, and nothing that
adds a dependency. A wrong patch inside those bounds is a rejected pull
request. A wrong patch outside them is an outage.
"""

from __future__ import annotations

import os
import re

from vomeos.guards import GuardResult, guard
from vomeos.text import strip_code_fence

# Size limits. Deliberately small: the fixes worth automating are null checks
# and wrong keywords, not refactors.
MAX_FILES = int(os.environ.get("SENTRY_PATCH_MAX_FILES", "3"))
MAX_LINES = int(os.environ.get("SENTRY_PATCH_MAX_LINES", "60"))

# Paths that may never be touched by an agent, in any repository.
#
# Each entry is here because changing it badly is expensive in a way a failed
# test would not catch.
FORBIDDEN = tuple(
    re.compile(p, re.I)
    for p in (
        # Generated on deploy. A hand-written one is an incident.
        r"(^|/)migrations/",
        # Settings and environment: one wrong line takes the service down,
        # and secrets live next door.
        r"(^|/)settings(\.py|/)",
        r"(^|/)(\.env|env\.py|config/secrets)",
        r"(^|/)wsgi\.py$|(^|/)asgi\.py$|(^|/)manage\.py$",
        # Authentication, permissions, payments. Being wrong here is not a
        # bug report, it is a breach or a refund.
        r"(^|/)(auth|authentication)_?app/",
        r"(^|/)(billing|payment|stripe)",
        r"(^|/)permissions?\.py$",
        # Dependencies and CI. An agent must not change what gets installed
        # or what gets run.
        r"(^|/)(requirements[^/]*\.txt|Pipfile|poetry\.lock|package\.json)$",
        r"(^|/)(package-lock\.json|yarn\.lock|Procfile|Dockerfile)$",
        r"(^|/)\.github/|(^|/)bitbucket-pipelines\.yml$",
        # Infrastructure as code.
        r"(^|/)(terraform|\.tf$|k8s/|helm/)",
    )
)

# Tests may be added but never weakened. A patch that deletes assertions is
# a patch that makes itself pass.
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$"
                        r"|[^/]*\.test\.[jt]sx?$|[^/]*_test\.py$", re.I)

_DIFF_FILE = re.compile(r"^\+\+\+ [ab]/(.+?)(\t|$)", re.M)
_OLD_FILE = re.compile(r"^--- [ab]/(.+?)(\t|$)", re.M)
_HUNK = re.compile(r"^@@ ", re.M)


def changed_files(diff: str) -> list[str]:
    """Every path a unified diff writes to."""
    seen: list[str] = []
    for match in _DIFF_FILE.finditer(diff or ""):
        path = match.group(1).strip()
        if path and path != "/dev/null" and path not in seen:
            seen.append(path)
    return seen


def changed_line_count(diff: str) -> int:
    """Added plus removed lines, ignoring the diff's own headers."""
    count = 0
    for line in (diff or "").splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            count += 1
    return count


def _allowlist() -> tuple[str, ...]:
    raw = os.environ.get("SENTRY_PATCH_PATH_ALLOWLIST", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def inspect(diff: str) -> tuple[bool, str]:
    """Every reason this diff may not be applied. (ok, detail)."""
    text = strip_code_fence(diff or "").strip()
    if not text:
        return False, "empty patch"

    if not _HUNK.search(text) or not _DIFF_FILE.search(text):
        return False, (
            "not a unified diff: expected '+++ b/path' and '@@' hunk headers"
        )

    files = changed_files(text)
    if not files:
        return False, "no target files in the diff"

    # A diff that renames or deletes a file. Out of scope for an automated
    # fix whatever the paths involved.
    if "/dev/null" in _OLD_FILE.findall(text):
        return False, "patch creates a file; only edits are permitted"

    for path in files:
        for pattern in FORBIDDEN:
            if pattern.search(path):
                if "migrations/" in path.replace("\\", "/"):
                    return False, (
                        f"{path} is a migration. Migrations are generated on "
                        "deploy in these repositories and are never written "
                        "by hand. This is refused with no override."
                    )
                return False, (
                    f"{path} is on the forbidden path list "
                    f"({pattern.pattern})"
                )
        if _TEST_PATH.search(path):
            return False, (
                f"{path} is a test file. A fix that edits its own tests is "
                "a fix that makes itself pass."
            )

    allowed = _allowlist()
    if allowed:
        for path in files:
            if not any(path.startswith(prefix) for prefix in allowed):
                return False, (
                    f"{path} is outside SENTRY_PATCH_PATH_ALLOWLIST; widen it "
                    "deliberately rather than by accident"
                )

    if len(files) > MAX_FILES:
        return False, (
            f"touches {len(files)} files, over the limit of {MAX_FILES}: "
            + ", ".join(files)
        )

    lines = changed_line_count(text)
    if lines > MAX_LINES:
        return False, f"changes {lines} lines, over the limit of {MAX_LINES}"

    return True, f"{len(files)} file(s), {lines} line(s) changed"


def extract_diff(text: str) -> tuple[str, bool]:
    """Pull the diff out of an agent's output. (diff, a_patch_was_offered).

    Handles both shapes, because the guard cannot know which it is looking
    at. The fix author returns a JSON object with the patch in a `diff`
    field, and a bare diff is what a simpler caller would pass.

    `False` for the second value means no patch was proposed at all, which
    is a normal and good outcome: refusing is the right answer more often
    than not. A guard that failed closed on a refusal would make refusing
    impossible, which is the opposite of what it is for.
    """
    import json

    cleaned = strip_code_fence(text or "").strip()
    if cleaned.startswith("{"):
        try:
            value = json.JSONDecoder().raw_decode(cleaned)[0]
        except ValueError:
            # Unparseable JSON is json_shape's problem, not ours. Saying
            # nothing here keeps one failure reported once.
            return "", False
        if isinstance(value, dict):
            if value.get("attempted") is False:
                return "", False
            diff = str(value.get("diff") or "")
            return diff, bool(diff.strip())
    return cleaned, bool(cleaned)


@guard("patch_safety")
def _patch_safety(text: str, ctx: dict) -> GuardResult:
    """Reject a diff whose blast radius is larger than we will accept.

    Passes when no patch was proposed. Refusing to write a fix is a correct
    answer, and a guard that treated it as a failure would make the safe
    option unavailable.
    """
    override = ""
    if isinstance(ctx, dict):
        override = str(ctx.get("diff") or "")

    if override:
        diff, offered = override, True
    else:
        diff, offered = extract_diff(text)

    if not offered:
        return GuardResult(
            "patch_safety", True, "no patch proposed, nothing to check"
        )

    ok, detail = inspect(diff)
    return GuardResult("patch_safety", ok, detail)


def describe() -> dict:
    """Current limits, for the health check."""
    return {
        "max_files": MAX_FILES,
        "max_lines": MAX_LINES,
        "forbidden_patterns": len(FORBIDDEN),
        "path_allowlist": list(_allowlist()) or ["(none: all paths allowed)"],
        "migrations": "refused, no override",
    }
