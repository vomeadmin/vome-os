"""
vomeos/diff.py

Apply a unified diff to a string, and refuse when it does not fit.

WHY THIS EXISTS
---------------
An agent proposes a patch, but neither Bitbucket nor GitHub accepts one.
Both commit whole file contents. So between "here is a diff" and "here is a
commit" something has to apply the change, and the only candidate is us.

WHY REFUSING IS THE FEATURE
---------------------------
Applying a diff is easy. Applying it to the wrong text is the dangerous part,
and it is not hypothetical: the file was read at one moment, the model
reasoned for twenty seconds, and the branch may have moved. A patch applied
at a shifted offset silently deletes the wrong lines and produces a commit
that looks deliberate.

So every context line is verified against the file before anything changes.
If one does not match, nothing is applied and the caller is told exactly
which line disagreed. A rejected patch costs a pull request nobody opened. A
misapplied one costs a corrupted file in a branch somebody merges.

There is deliberately no fuzz factor and no offset search. `patch(1)` will
happily apply a hunk three lines away from where it claimed to belong, which
is a convenience for a human watching the output and a hazard for a machine
that is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_FILE_HEADER = re.compile(r"^(---|\+\+\+) ")


class DiffError(ValueError):
    """The diff is malformed, or does not fit the file it names."""


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


def parse_hunks(diff: str) -> list[Hunk]:
    """Hunks of a single-file unified diff, in order."""
    hunks: list[Hunk] = []
    current: Hunk | None = None

    for raw in (diff or "").splitlines():
        header = _HUNK_HEADER.match(raw)
        if header:
            current = Hunk(
                old_start=int(header.group(1)),
                old_count=int(header.group(2) or 1),
                new_start=int(header.group(3)),
                new_count=int(header.group(4) or 1),
            )
            hunks.append(current)
            continue
        if current is None:
            continue
        if _FILE_HEADER.match(raw):
            # A second file's header. This applier is single file by design;
            # the caller splits a multi-file diff first.
            break
        if raw.startswith(("+", "-", " ")) or raw == "":
            # An empty line in a diff is a context line that was empty. The
            # trailing space is routinely stripped by editors and by JSON
            # round trips, so treating "" as " " is not laxness, it is the
            # only way to survive real inputs.
            current.lines.append(raw if raw else " ")
        elif raw.startswith("\\"):
            # "\ No newline at end of file". Carries no content.
            continue
        else:
            break

    return hunks


def apply(original: str, diff: str) -> str:
    """Return `original` with `diff` applied. Raise DiffError if it does not
    fit.

    Line endings are preserved: the result uses whatever the original used,
    because rewriting a file from CRLF to LF turns a one line fix into a
    whole file diff that nobody can review.
    """
    hunks = parse_hunks(diff)
    if not hunks:
        raise DiffError("no hunks found in diff")

    crlf = "\r\n" in original
    text = original.replace("\r\n", "\n")
    lines = text.split("\n")

    # Applied back to front so earlier edits do not shift later line numbers.
    for hunk in sorted(hunks, key=lambda h: h.old_start, reverse=True):
        lines = _apply_hunk(lines, hunk)

    result = "\n".join(lines)
    return result.replace("\n", "\r\n") if crlf else result


def _apply_hunk(lines: list[str], hunk: Hunk) -> list[str]:
    index = hunk.old_start - 1
    if index < 0 or index > len(lines):
        raise DiffError(
            f"hunk starts at line {hunk.old_start}, outside a file of "
            f"{len(lines)} lines"
        )

    replacement: list[str] = []
    cursor = index

    for entry in hunk.lines:
        marker, content = entry[0], entry[1:]
        if marker == " ":
            _expect(lines, cursor, content, hunk, "context")
            replacement.append(lines[cursor])
            cursor += 1
        elif marker == "-":
            _expect(lines, cursor, content, hunk, "removed")
            cursor += 1
        elif marker == "+":
            replacement.append(content)

    return lines[:index] + replacement + lines[cursor:]


def _expect(lines: list[str], cursor: int, content: str, hunk: Hunk,
            kind: str) -> None:
    """Verify one line, or refuse the whole patch.

    The error names the line number, what was expected and what is actually
    there, because "patch does not apply" with no detail is the least useful
    message in software.
    """
    if cursor >= len(lines):
        raise DiffError(
            f"hunk at line {hunk.old_start} ran past the end of the file "
            f"while looking for a {kind} line: {content!r}"
        )
    if lines[cursor].rstrip() != content.rstrip():
        raise DiffError(
            f"{kind} line {cursor + 1} does not match. "
            f"Diff expects {content!r}, file has {lines[cursor]!r}. "
            "The file has changed since it was read; nothing was applied."
        )


def files_in(diff: str) -> list[str]:
    """Target paths named by a possibly multi-file diff."""
    paths = []
    for raw in (diff or "").splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].split("\t")[0].strip()
            if path.startswith(("a/", "b/")):
                path = path[2:]
            if path and path != "/dev/null" and path not in paths:
                paths.append(path)
    return paths


def split_by_file(diff: str) -> dict[str, str]:
    """Break a multi-file diff into one diff per path.

    Both hosts commit one file at a time, so a two file patch is two commits
    on the same branch and they have to be separated before either is sent.
    """
    chunks: dict[str, list[str]] = {}
    current: str | None = None
    pending: list[str] = []

    for raw in (diff or "").splitlines():
        if raw.startswith("--- "):
            pending = [raw]
            current = None
            continue
        if raw.startswith("+++ "):
            path = raw[4:].split("\t")[0].strip()
            if path.startswith(("a/", "b/")):
                path = path[2:]
            current = path
            chunks.setdefault(current, [])
            chunks[current].extend(pending + [raw])
            pending = []
            continue
        if current:
            chunks[current].append(raw)

    return {path: "\n".join(body) + "\n" for path, body in chunks.items()}
