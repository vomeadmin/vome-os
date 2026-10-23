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
Every read carries the ref from `sentry_projects`. `django-core-dev` and
`django-core-prod` are the same repository at different branches, so reading
a dev traceback against `master` means reasoning about code nobody is
running, with line numbers that do not match the frames. The wrong branch is
worse than no branch, because it looks like an answer.

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

# What to fetch per file before windowing. Not what reaches the model: the
# window does that. Large because our biggest modules run past 4000 lines.
READ_BYTES = int(os.environ.get("SENTRY_CODE_READ_BYTES", "2000000"))

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
    return _span(lines, start, end, {lineno})


def _span(lines: list[str], start: int, end: int, marks: set[int]) -> str:
    """Lines start+1..end, numbered, with `>>` on the marked line numbers."""
    width = len(str(end))
    out = []
    for index in range(start, end):
        marker = ">>" if index + 1 in marks else "  "
        out.append(f"{marker} {str(index + 1).rjust(width)} | {lines[index]}")
    head = f"--- {{path}} lines {start + 1}-{end} ---"
    return head + "\n" + "\n".join(out)


def message_anchor(title: str) -> str:
    """The fixed text of a logged error, to find where it is raised.

    `logger.error("Error sending Vome birthday email: %s", exc)` reaches
    Sentry as a message event with no stack trace at all, titled
    "Error sending Vome birthday email: null value in column ...". The part
    before the first colon is the literal in our source; the rest is the
    runtime detail. An exception title ("IntegrityError: ...") gives no
    anchor, because those events carry a trace and the frames are better.
    """
    head = (title or "").split(":", 1)[0].strip().strip("\"'")
    if len(head) < 12 or " " not in head:
        return ""
    if re.fullmatch(r"[A-Z][A-Za-z]*(Error|Exception|Warning)", head):
        return ""
    return head


def _frames_from_message(client, repo: str, ref: str, anchor: str,
                         kwargs: dict, errors: list[str]):
    """(path, lineno, body) for files that contain a logged message.

    Code search says which files. The line is then found in the file as read
    at `ref`, because the search index follows the default branch and its
    line numbers need not match the code that actually ran.
    """
    result = client.search_code(f'"{anchor}"', repo=repo, **kwargs)
    if not result.ok:
        errors.append(f"message search: {result.error[:120]}")
        return []
    data = result.data if isinstance(result.data, dict) else {}
    paths = []
    for item in data.get("values") or data.get("items") or []:
        path = (item.get("file") or {}).get("path") or item.get("path") or ""
        if path and _is_ours(path) and path not in paths:
            paths.append(path)
    found = []
    for path in paths[:MAX_FILES]:
        read = client.read_file(
            repo, path, ref=ref, max_bytes=READ_BYTES, **kwargs
        )
        if not read.ok or not isinstance(read.data, str):
            errors.append(f"{path}: {read.error[:120] or 'unreadable'}")
            continue
        for index, line in enumerate(read.data.splitlines(), start=1):
            if anchor in line:
                found.append((path, index, read.data))
                break
    return found


def gather(trace: str, repo: str, ref: str, host: str = "bitbucket",
           owner: str = "", message: str = "") -> dict:
    """Files and recent commits behind a traceback.

    Returns `{"files": str, "commits": str, "read": [...], "errors": [...]}`.

    Never raises, and never silently returns an empty string for a failure.
    "We could not read the repository" and "the file is empty" have to look
    different to the analyst, or it will reason confidently about nothing.

    `message` is the issue title. When the trace names none of our files
    (a logged error rather than an exception), the code is found by
    searching for the message's fixed text instead.
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

    anchor = "" if frames else message_anchor(message)
    if anchor:
        located = _frames_from_message(
            client, repo, ref, anchor, kwargs, errors
        )
        for path, lineno, body in located:
            if budget <= 0:
                break
            chunk = _window(body, lineno).replace("{path}", path)
            chunk = (
                "(no stack trace: located by the logged message "
                f"'{anchor}')\n" + chunk
            )[:budget]
            budget -= len(chunk)
            chunks.append(chunk)
            read.append(f"{path}:{lineno}")
        frames = [(path, lineno) for path, lineno, _ in located]

    for path, lineno in ([] if anchor else frames):
        if budget <= 0:
            break
        result = client.read_file(
            repo, path, ref=ref, max_bytes=READ_BYTES, **kwargs
        )
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


# ---------------------------------------------------------------------------
# Feature builds
# ---------------------------------------------------------------------------
#
# A crash is fixed next to the line that raised, so a window is enough. A
# requested change is not: it usually adds a name the file does not import
# yet and copies logic from somewhere else. 2026-10-21, profile status on
# form submissions: the builder was handed three windows, could not see
# either file's imports or the get_is_invited it was told to copy, and
# rightly refused. So a feature build gets each edited file's top-level
# import blocks, a wider window, and the precedent code as read-only
# reference, under a larger budget.

FEATURE_WINDOW = int(os.environ.get("FEATURE_BUILD_WINDOW", "100"))
FEATURE_MAX_CHARS = int(os.environ.get("FEATURE_BUILD_MAX_CHARS", "60000"))
FEATURE_MAX_REFERENCES = int(os.environ.get("FEATURE_BUILD_MAX_REFS", "4"))

# A reference as the architect writes it: "core:path/file.py:123", the repo
# prefix optional, several separated by ";" or spaces.
_REFERENCE = re.compile(r"(?:\b[a-z]+:)?([\w./-]+\.py):(\d+)")
_IMPORT = re.compile(r"^(?:import\s+[\w.]+|from\s+[\w.]+\s+import\b)")


def import_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Top-level import blocks as (start, end), 0-based, end exclusive.

    The first block runs from line 1 so a module docstring comes with it.
    Later blocks (a mid-file `from x import (...)`, a `# noqa: E402` one)
    are their own spans. Blank and comment lines between statements keep a
    block together; indented imports inside functions are not the module's.
    """
    blocks: list[list[int]] = []
    index, total = 0, len(lines)
    while index < total:
        line = lines[index]
        if not _IMPORT.match(line):
            index += 1
            continue
        start = index
        depth = line.count("(") - line.count(")")
        while (depth > 0 or lines[index].rstrip().endswith("\\")) \
                and index + 1 < total:
            index += 1
            depth += lines[index].count("(") - lines[index].count(")")
        end = index + 1
        if blocks and all(not lines[i].strip()
                          or lines[i].lstrip().startswith("#")
                          for i in range(blocks[-1][1], start)):
            blocks[-1][1] = end
        else:
            blocks.append([start, end])
        index = end
    # The docstring above the first block, unless the file has a long
    # preamble, which is not worth the budget.
    if blocks and blocks[0][0] <= 80:
        blocks[0][0] = 0
    return [(start, end) for start, end in blocks]


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def references(text: str) -> list[tuple[str, int]]:
    """Every path:line in an architect's evidence or plan, in order."""
    seen: list[tuple[str, int]] = []
    for path, line in _REFERENCE.findall(text or ""):
        ref = (path, int(line))
        if ref not in seen:
            seen.append(ref)
    return seen


def gather_for_build(edits: list[tuple[str, int]], repo: str, ref: str,
                     host: str = "bitbucket", owner: str = "",
                     refs: list[tuple[str, int]] | None = None) -> dict:
    """Source for a requested change: what to edit, and what to copy.

    `edits` are the plan's anchors. Each edited file gets its import blocks
    and a window around every anchor in it. `refs` (the architect's
    evidence and any path:line in the plan) that land in an edited file
    widen that file; the rest are read as READ-ONLY reference windows.

    Returns `{"files": str, "read": [...], "errors": [...]}`, and like
    `gather` never returns an empty string for a failure.
    """
    client = _client_for(host)
    kwargs = {"owner": owner} if host == "github" and owner else {}
    if not client.configured(**kwargs):
        return {"files": "Repository access is NOT configured. No code was "
                         "read.",
                "read": [], "errors": ["connector not configured"]}

    edit_lines: dict[str, list[int]] = {}
    for path, line in edits:
        edit_lines.setdefault(path, []).append(int(line))
    extra: dict[str, list[int]] = {}
    for path, line in refs or []:
        if path in edit_lines:
            if int(line) not in edit_lines[path]:
                edit_lines[path].append(int(line))
        elif len(extra) < FEATURE_MAX_REFERENCES or path in extra:
            extra.setdefault(path, []).append(int(line))

    chunks: list[str] = []
    read: list[str] = []
    errors: list[str] = []
    budget = FEATURE_MAX_CHARS

    def add(path: str, lines_at: list[int], with_imports: bool) -> None:
        nonlocal budget
        if budget <= 0:
            errors.append(f"{path}: not read, the source budget ran out")
            return
        result = client.read_file(repo, path, ref=ref, max_bytes=READ_BYTES,
                                  **kwargs)
        body = result.data if result.ok and isinstance(result.data, str) \
            else ""
        if not body:
            errors.append(f"{path}: {result.error[:120] or 'empty response'}")
            return
        lines = body.splitlines()
        spans = list(import_blocks(lines)) if with_imports else []
        for lineno in lines_at:
            spans.append((max(0, lineno - 1 - FEATURE_WINDOW),
                          min(len(lines), lineno + FEATURE_WINDOW)))
        for start, end in _merge(spans):
            chunk = _span(lines, start, end, set(lines_at)).replace(
                "{path}", path)[:budget]
            budget -= len(chunk)
            chunks.append(chunk)
            if budget <= 0:
                break
        read.extend(f"{path}:{lineno}" for lineno in lines_at)

    chunks.append("=== FILES TO EDIT (import blocks and the lines around "
                  "each edit; `>>` marks the anchor) ===")
    for path, lines_at in edit_lines.items():
        add(path, lines_at, with_imports=True)
    if extra:
        chunks.append("=== READ-ONLY REFERENCE (precedent the plan cites; "
                      "do not edit these files) ===")
        for path, lines_at in extra.items():
            add(path, lines_at, with_imports=False)

    if not read:
        files_text = (
            "Could not read any source file. This is a repository access "
            "problem, NOT evidence that the code is fine:\n  "
            + "\n  ".join(errors or ["no anchors given"])
        )
    else:
        files_text = "\n\n".join(chunks)
        if errors:
            files_text += "\n\nNOT read:\n  " + "\n  ".join(errors)
    return {"files": files_text, "read": read, "errors": errors}


def describe() -> dict:
    """Budget settings, for the health check."""
    return {
        "max_files": MAX_FILES,
        "window_lines": WINDOW,
        "max_chars": MAX_TOTAL_CHARS,
        "commits": COMMITS,
    }
