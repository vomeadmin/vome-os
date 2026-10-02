"""
sentry_code.py

Read the code a traceback points at, on the branch that was actually running.

WHY THIS IS A SEPARATE STEP
---------------------------
The triage agent decides whether an issue is worth anyone's time from the
trace alone. The analyst has to say *why* it broke, and that is not answerable
from a traceback: a traceback tells you which line raised, not what that line
assumed. `role.site.name` failing tells you `site` was None. Only the file
tells you whether `site` is nullable, whether the queryset should have
filtered it out, and whether somebody changed that three days ago.

So before the analyst runs, this pulls the files the trace names and the
commits that recently touched them.

ON THE BRANCH THAT RAN
----------------------
Every read carries the ref from `sentry_projects`. `dev-vome-app` and
`prod-vome` are the same repository at different branches, so reading a dev
traceback against `master` means reasoning about code nobody is running, with
line numbers that do not match the frames. The wrong branch is worse than no
branch, because it looks like an answer.

THE BUDGET IS THE DESIGN
------------------------
A stack trace can name fifteen files and some of ours run to four thousand
lines. Handing all of that to a model costs real money and buries the
relevant twenty lines. So: our own files only, nearest-to-the-raise first,
windowed around the line that actually failed, and a hard total cap. An
analyst reading three right files beats one reading thirty.
"""

from __future__ import annotations

import os
import re

from vomeos.integrations import code_search

# Files to pull. The frame that raised is almost always the answer; the two
# or three above it are the context that explains it.
MAX_FILES = int(os.environ.get("SENTRY_CODE_MAX_FILES", "4"))

# Lines kept either side of a frame's line number. Wide enough to show a
# whole short function, narrow enough that four files stay readable.
WINDOW = int(os.environ.get("SENTRY_CODE_WINDOW", "60"))

# Hard cap on everything handed to the analyst.
MAX_TOTAL_CHARS = int(os.environ.get("SENTRY_CODE_MAX_CHARS", "24000"))

COMMITS = int(os.environ.get("SENTRY_CODE_COMMITS", "8"))

# Paths that are never ours. Reading Django's own source to diagnose our bug
# is a reliable way to spend tokens learning that Django works.
_VENDOR = tuple(
    re.compile(p, re.I)
    for p in (
        r"(^|/)(django|rest_framework|celery|kombu|boto3|botocore)/",
        r"(^|/)(site-packages|dist-packages|node_modules)/",
        r"(^|/)(jwt|psycopg2|storages|redis|requests|urllib3|httpx)/",
        r"(^|/)(react-native|node_modules)/",
        r"^/?(usr|lib|opt)/",
    )
)

_FRAME = re.compile(r'File "([^"]+)", line (\d+)')


def _is_ours(path: str) -> bool:
    return not any(pattern.search(path or "") for pattern in _VENDOR)


def frames_of_interest(trace: str) -> list[tuple[str, int]]:
    """Our own files from a rendered trace, nearest the raise first.

    The renderer already puts in-app frames first, so order is preserved and
    duplicates are dropped: the same file appearing three times is one file.
    """
    seen: dict[str, int] = {}
    for path, lineno in _FRAME.findall(trace or ""):
        if not _is_ours(path):
            continue
        if path not in seen:
            seen[path] = int(lineno)
    return list(seen.items())[:MAX_FILES]


def _client_for(host: str):
    if host == "github":
        return code_search.GitHub()
    return code_search.Bitbucket()


def _window(text: str, lineno: int) -> str:
    """The lines around the one that raised, numbered.

    Numbered because the analyst has to be able to say "line 412" and have
    that mean the same thing to the person reading its answer.
    """
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(0, lineno - 1 - WINDOW)
    end = min(len(lines), lineno + WINDOW)
    width = len(str(end))
    out = []
    for index in range(start, end):
        marker = ">>" if index + 1 == lineno else "  "
        out.append(f"{marker} {str(index + 1).rjust(width)} | {lines[index]}")
    head = f"--- {{path}} lines {start + 1}-{end} ---"
    return head + "\n" + "\n".join(out)


def gather(trace: str, repo: str, ref: str, host: str = "bitbucket",
           owner: str = "") -> dict:
    """Files and recent commits behind a traceback.

    Returns `{"files": str, "commits": str, "read": [...], "errors": [...]}`.

    Never raises, and never silently returns an empty string for a failure.
    "We could not read the repository" and "the file is empty" have to look
    different to the analyst, or it will reason confidently about nothing.
    """
    frames = frames_of_interest(trace)
    client = _client_for(host)
    kwargs = {"owner": owner} if host == "github" and owner else {}

    if not client.configured(**kwargs):
        return {
            "files": "Repository access is NOT configured. No code was read.",
            "commits": "Commit history unavailable for the same reason.",
            "read": [],
            "errors": ["connector not configured"],
        }

    chunks: list[str] = []
    read: list[str] = []
    errors: list[str] = []
    budget = MAX_TOTAL_CHARS

    for path, lineno in frames:
        if budget <= 0:
            break
        result = client.read_file(repo, path, ref=ref, **kwargs)
        if not result.ok:
            errors.append(f"{path}: {result.error[:120]}")
            continue
        body = result.data if isinstance(result.data, str) else ""
        if not body:
            errors.append(f"{path}: empty response")
            continue
        chunk = _window(body, lineno).replace("{path}", path)
        chunk = chunk[:budget]
        budget -= len(chunk)
        chunks.append(chunk)
        read.append(f"{path}:{lineno}")

    if chunks:
        files_text = "\n\n".join(chunks)
    elif errors:
        files_text = (
            "Could not read any source file. This is a repository access "
            "problem, NOT evidence that the code is fine:\n  "
            + "\n  ".join(errors)
        )
    else:
        files_text = (
            "The trace names no files of ours, only framework code. There is "
            "nothing of ours to read."
        )

    commits_text = _recent_commits(
        client, repo, ref, frames, kwargs, errors
    )

    return {
        "files": files_text,
        "commits": commits_text,
        "read": read,
        "errors": errors,
    }


def _recent_commits(client, repo: str, ref: str, frames, kwargs,
                    errors: list[str]) -> str:
    """What changed lately, on the branch that ran.

    The question behind most regression triage is "what shipped just before
    this started". Scoped to the file that raised when we know it, because
    the last eight commits to a busy repository are usually about something
    else entirely.
    """
    path = frames[0][0] if frames else ""
    result = client.recent_commits(
        repo, path=path, limit=COMMITS, ref=ref, **kwargs
    )
    if not result.ok and path:
        # The path filter can legitimately match nothing. Fall back to the
        # branch as a whole rather than reporting no history.
        result = client.recent_commits(repo, limit=COMMITS, ref=ref, **kwargs)

    if not result.ok:
        errors.append(f"commits: {result.error[:120]}")
        return (
            "Commit history could not be read. Treat 'what changed recently' "
            "as UNKNOWN, not as nothing."
        )

    rows = _commit_rows(result.data)
    if not rows:
        scope = f" touching {path}" if path else ""
        return f"No recent commits on {ref}{scope}."

    header = f"Recent commits on {ref}" + (f" touching {path}:" if path else ":")
    return header + "\n" + "\n".join(rows[:COMMITS])


def _commit_rows(data) -> list[str]:
    """Normalise the two commit shapes. Bitbucket and GitHub disagree."""
    values = []
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        values = data["values"]          # Bitbucket
    elif isinstance(data, list):
        values = data                     # GitHub
    rows = []
    for item in values:
        if not isinstance(item, dict):
            continue
        if "hash" in item:                # Bitbucket
            sha = str(item.get("hash", ""))[:8]
            message = str(item.get("message", "")).strip().splitlines()
            author = ((item.get("author") or {}).get("raw") or "")
            date = str(item.get("date", ""))[:10]
        else:                             # GitHub
            sha = str(item.get("sha", ""))[:8]
            commit = item.get("commit") or {}
            message = str(commit.get("message", "")).strip().splitlines()
            author = ((commit.get("author") or {}).get("name") or "")
            date = str((commit.get("author") or {}).get("date", ""))[:10]
        subject = message[0] if message else ""
        who = author.split("<")[0].strip()
        rows.append(f"  {sha} {date} {who[:22]:22} {subject[:70]}")
    return rows


def describe() -> dict:
    """Budget settings, for the health check."""
    return {
        "max_files": MAX_FILES,
        "window_lines": WINDOW,
        "max_chars": MAX_TOTAL_CHARS,
        "commits": COMMITS,
    }
