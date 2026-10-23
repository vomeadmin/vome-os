"""
vomeos/tools.py

Read-only tools an agent may call while it thinks: search the code, read a
file, list recent commits.

WHY THIS EXISTS
---------------
On 2026-10-16 the code overturned 2 of the first 8 triage calls (#9534, a
chunk-load error the app already handles; #9477, a reply that blamed the
mobile app for an admin assignment that simply bypasses the overlap rule).
Both cards read well. Both were wrong. The difference was a person reading
the code before choosing a lane.

A single-shot agent cannot do that: it cannot know which file to read until
it has read the ticket. So this is the one place the kernel lets a model
drive its own lookups, and it is deliberately narrow:

  * READ ONLY. The tools wrap `integrations.code_search`, which has no write
    methods. There is no tool here that changes anything, anywhere.
  * GRANTED IN THE MANIFEST. `job.tools` names the tools an agent may call,
    and `job.max_tool_calls` caps them. An agent with no grant is sent no
    tools at all.
  * NEVER TO A CLIENT-FACING AGENT. The onboarding gate refuses a manifest
    that is client_facing and declares code tools, because a reply that
    quotes a file path tells a customer how our system is built (#8945).
  * BOUNDED OUTPUT. Every result is truncated, and a file read returns a
    window of lines, so one large module cannot swamp the context.

REPOSITORIES
------------
Agents name a repository by alias. `VOMEOS_CODE_REPO_MAP` maps an alias to
host, owner, slug and the branch production runs:

    core=bitbucket:vomedjango/vomedjango-restored-core-app@master,...

Reads always use that live branch, never the default branch, because the
default branch is not always what production runs (core's `main` is a 2024
stub).

OTHER BRANCHES
--------------
A bug found while building (Ron's #bug-reports, 2026-10-19) may exist only
on the integration branch, or only in the reporter's own unmerged work. So a
tool call may name `branch`, but only one listed for that repo in
`VOMEOS_CODE_EXTRA_BRANCHES` (`core=development,web=develop|RON_DEVELOP`).
Anything else is refused. With no `branch`, reads stay on the live branch.

GREP, FIND AND TASKS (2026-10-23)
---------------------------------
`grep_code` and `find_files` work on a full copy of the live commit
(code_mirror.py), so an agent can trace a field across an app the way a
person does in Claude Code, instead of guessing exact strings for a host's
index. `read_task` reads one ClickUp task and its comments: what the
engineer found is often the whole answer. Chats and integrations joined the
map the same day; until then every chat ticket ended "out_of_repo".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

from vomeos.integrations import code_search

DEFAULT_REPO_MAP = (
    "core=bitbucket:vomedjango/vomedjango-restored-core-app@master,"
    "database=bitbucket:vomedjango/vomedjango-database-app@master,"
    "chats=bitbucket:vomedjango/vomedjango-chats-app@master,"
    "integrations=bitbucket:vomedjango/vomedjango-integrations-app@main,"
    "web=github:vomeadmin/vome-react@master,"
    "mobile=github:vomeadmin/VomeApp@master"
)

DEFAULT_EXTRA_BRANCHES = (
    "core=development,database=development,chats=development,"
    "integrations=development,web=develop|RON_DEVELOP"
)

MAX_RESULT_CHARS = int(os.environ.get("VOMEOS_TOOL_MAX_CHARS", "16000"))
READ_WINDOW = 250  # lines returned by default around a requested range

REPO_DESCRIPTIONS = {
    "core": "Django backend (most product logic)",
    "database": "volunteer database service",
    "chats": "chat service (messages, attachments, chat lists)",
    "integrations": "integrations service (SSO, Salesforce, calendars)",
    "web": "React web app",
    "mobile": "React Native mobile app",
}


@dataclass(frozen=True)
class Repo:
    alias: str
    host: str
    owner: str
    slug: str
    branch: str


def repo_map() -> dict[str, Repo]:
    raw = os.environ.get("VOMEOS_CODE_REPO_MAP", DEFAULT_REPO_MAP)
    repos: dict[str, Repo] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part or ":" not in part:
            continue
        alias, rest = part.split("=", 1)
        host, rest = rest.split(":", 1)
        path, _, branch = rest.partition("@")
        owner, _, slug = path.partition("/")
        repos[alias.strip()] = Repo(alias.strip(), host.strip(),
                                    owner.strip(), slug.strip(),
                                    branch.strip() or "master")
    return repos


def extra_branches() -> dict[str, tuple[str, ...]]:
    """alias -> branches a tool call may read besides the live one."""
    raw = os.environ.get("VOMEOS_CODE_EXTRA_BRANCHES", DEFAULT_EXTRA_BRANCHES)
    out: dict[str, tuple[str, ...]] = {}
    for part in raw.split(","):
        alias, _, names = part.partition("=")
        branches = tuple(b.strip() for b in names.split("|") if b.strip())
        if alias.strip() and branches:
            out[alias.strip()] = branches
    return out


def _on_branch(repo: Repo, branch: str) -> Repo | str:
    """The repo read at `branch`, or the refusal text."""
    if not branch or branch == repo.branch:
        return repo
    allowed = extra_branches().get(repo.alias, ())
    if branch not in allowed:
        listed = ", ".join((repo.branch,) + allowed)
        return (f"Branch {branch!r} is not readable on {repo.alias}. "
                f"Use: {listed}.")
    return replace(repo, branch=branch)


# ---------------------------------------------------------------------------
# Definitions, in the Messages API tool format
# ---------------------------------------------------------------------------

def _repo_property() -> dict:
    names = sorted(repo_map())
    return {
        "type": "string",
        "enum": names,
        "description": ", ".join(
            f"{n} = {REPO_DESCRIPTIONS.get(n, n)}" for n in names) + ".",
    }


def definitions(names) -> list[dict]:
    """Tool schemas for the names an agent was granted, in that order."""
    defs = {
        "search_code": {
            "name": "search_code",
            "description": (
                "Find where a string, setting, field, error message or UI "
                "text appears in one repository. Returns file paths and "
                "matching lines. For a regex, a folder filter or lines of "
                "context, use grep_code."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": _repo_property(),
                    "query": {"type": "string",
                              "description": "Exact text to look for."},
                },
                "required": ["repo", "query"],
            },
        },
        "read_code_file": {
            "name": "read_code_file",
            "description": (
                "Read a file (numbered lines) or list a directory, on the "
                "branch production runs. Give start_line/end_line for long "
                "files; a window of lines is returned."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": _repo_property(),
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["repo", "path"],
            },
        },
        "recent_commits": {
            "name": "recent_commits",
            "description": (
                "Recent commits on the production branch, optionally only "
                "those touching one path. Answers 'what changed lately'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": _repo_property(),
                    "path": {"type": "string"},
                },
                "required": ["repo"],
            },
        },
        "grep_code": {
            "name": "grep_code",
            "description": (
                "Regex search over a full copy of one repository at the "
                "live commit, like ripgrep. Returns path:line: text for "
                "every match (up to 80). Use `path` to stay in one app or "
                "folder (`sequence_app`, `src/views/org`, `**/*.py`) and "
                "`context` for surrounding lines. The fastest way to trace "
                "a field, an endpoint, a serializer or a UI string."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": _repo_property(),
                    "pattern": {"type": "string",
                                "description": "Python regex, e.g. "
                                               "'def (get|list)_shifts' or "
                                               "'reservation_limit'."},
                    "path": {"type": "string",
                             "description": "Optional folder or glob."},
                    "context": {"type": "integer",
                                "description": "Lines around each match, "
                                               "0 to 5."},
                    "case_sensitive": {"type": "boolean"},
                },
                "required": ["repo", "pattern"],
            },
        },
        "find_files": {
            "name": "find_files",
            "description": (
                "Find files by name in a full copy of one repository: a "
                "glob (`**/*Kiosk*.js`, `opportunity_app/*.py`) or a word "
                "that appears in the path (`reservation`)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": _repo_property(),
                    "pattern": {"type": "string"},
                },
                "required": ["repo", "pattern"],
            },
        },
        "read_task": {
            "name": "read_task",
            "description": (
                "Read one ClickUp task: name, status, assignees, dates, "
                "description and its comments (newest last). Use it on a "
                "task the existing-work search names, to see what the "
                "engineer actually found, decided or shipped."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "Task id (868abc123) or URL."},
                },
                "required": ["task"],
            },
        },
    }
    extra = extra_branches()
    if extra:
        listed = "; ".join(f"{a}: {', '.join(b)}" for a, b in sorted(
            extra.items()))
        branch = {
            "type": "string",
            "description": "Optional. Omit to read what production runs. "
                           f"Otherwise one of: {listed}.",
        }
        for name, d in defs.items():
            if name not in NON_CODE_TOOLS:
                d["input_schema"]["properties"]["branch"] = branch
    return [defs[n] for n in names if n in defs]


CODE_TOOLS = ("search_code", "read_code_file", "recent_commits",
              "grep_code", "find_files")
# Read-only and internal, but not about the code: no repo, no branch.
NON_CODE_TOOLS = ("read_task",)
ALL_TOOLS = CODE_TOOLS + NON_CODE_TOOLS


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _client(repo: Repo):
    return code_search.GitHub() if repo.host == "github" \
        else code_search.Bitbucket()


def _kwargs(repo: Repo) -> dict:
    return {"owner": repo.owner} if repo.host == "github" else {}


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n... (truncated)"


def _search(repo: Repo, query: str) -> str:
    # The branch goes to both hosts so a full copy greps the code that runs
    # (see code_mirror.py), not whatever the host's index holds.
    result = _client(repo).search_code(query, repo=repo.slug,
                                       **_kwargs(repo), ref=repo.branch)
    if not result.ok:
        return f"Search failed: {result.error[:200]}. This is NOT 'no match'."
    data = result.data if isinstance(result.data, dict) else {}
    lines = []
    for item in (data.get("values") or data.get("items") or [])[:15]:
        path = (item.get("file") or {}).get("path") or item.get("path")
        hits = []
        for match in item.get("content_matches") or []:
            for line in match.get("lines") or []:
                text = "".join(seg.get("text", "")
                               for seg in line.get("segments") or [])
                if text.strip():
                    hits.append(f"  {line.get('line')}: {text.strip()[:160]}")
        for match in item.get("text_matches") or []:
            fragment = " ".join((match.get("fragment") or "").split())
            if fragment:
                hits.append(f"  ~ {fragment[:160]}")
        lines.append(path + ("\n" + "\n".join(hits[:4]) if hits else ""))
    if not lines and data.get("incomplete_results"):
        return (f"Search failed: GitHub's index of {repo.alias} is incomplete "
                "and the full-copy fallback failed "
                f"({str(data.get('mirror_error', ''))[:120]}). This is NOT "
                "'no match'; read the files directly.")
    if not lines:
        where = (f"the live code ({data['mirror']['ref']} at "
                 f"{data['mirror']['sha']})" if data.get("mirror")
                 else "the default branch")
        return (f"No matches for {query!r} in {repo.alias}. Searched "
                f"{where}, so confirm by reading.")
    if data.get("mirror"):
        lines.insert(0, f"(searched {data['mirror']['ref']} at "
                        f"{data['mirror']['sha']})")
    return "\n".join(lines)


def _read(repo: Repo, path: str, start: int | None, end: int | None) -> str:
    result = _client(repo).read_file(
        repo.slug, path.strip("/"), ref=repo.branch, max_bytes=2_000_000,
        **_kwargs(repo),
    )
    if not result.ok:
        return f"Read failed: {result.error[:200]}."
    data = result.data
    # A directory comes back as a listing, not text.
    if isinstance(data, list):
        names = [f"{d.get('name')}{'/' if d.get('type') == 'dir' else ''}"
                 for d in data if isinstance(d, dict)]
        return "Directory:\n" + "\n".join(names[:200])
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        names = [v.get("path", "") + ("/" if v.get("type") ==
                                      "commit_directory" else "")
                 for v in data["values"]]
        return "Directory:\n" + "\n".join(names[:200])
    if isinstance(data, dict) and data.get("content") and \
            data.get("encoding") == "base64":
        import base64

        data = base64.b64decode(data["content"]).decode("utf-8", "replace")
    if not isinstance(data, str):
        return f"Unreadable response for {path}."
    lines = data.splitlines()
    first = max(1, int(start or 1))
    last = min(len(lines), int(end or first + READ_WINDOW - 1))
    if last - first > 400:
        last = first + 400
    width = len(str(last))
    body = "\n".join(f"{str(n).rjust(width)} | {lines[n - 1]}"
                     for n in range(first, last + 1))
    return (f"{repo.alias}:{path} on {repo.branch}, lines {first}-{last} "
            f"of {len(lines)}\n{body}")


def _commits(repo: Repo, path: str = "") -> str:
    result = _client(repo).recent_commits(
        repo.slug, path=path, limit=10, ref=repo.branch, **_kwargs(repo),
    )
    if not result.ok:
        return f"Commit history failed: {result.error[:200]}."
    data = result.data
    values = data.get("values") if isinstance(data, dict) else data
    rows = []
    for c in values or []:
        if not isinstance(c, dict):
            continue
        sha = str(c.get("hash") or c.get("sha") or "")[:8]
        commit = c.get("commit") or {}
        message = str(c.get("message") or commit.get("message") or "")
        date = str(c.get("date") or (commit.get("author") or {})
                   .get("date") or "")[:10]
        rows.append(f"{sha} {date} {message.strip().splitlines()[0][:90]}"
                    if message.strip() else f"{sha} {date}")
    return "\n".join(rows) or "No commits."


def _copy(repo: Repo):
    """(root, sha) of the live code, or raises."""
    from vomeos.integrations import code_mirror

    # Same reach as search and read: VOMEOS_CODE_REPOS, when set, is the
    # list of repositories an agent may see at all.
    if not code_search._repo_permitted(repo.slug):
        raise RuntimeError(code_search._denial(repo.slug, None))
    if repo.host == "github":
        headers = code_search.GitHub().headers(repo.owner)
    else:
        headers = code_search.Bitbucket().headers()
    return code_mirror.local_copy(repo.host, repo.owner, repo.slug,
                                  repo.branch, headers)


def warm_copies() -> dict:
    """Make sure every repo has a copy at its live commit, so no triage run
    waits on a download (vome-react takes a couple of minutes, and Railway's
    disk starts empty after each deploy). Only a changed head downloads."""
    import time

    out = {}
    for alias, repo in sorted(repo_map().items()):
        started = time.monotonic()
        try:
            _, sha = _copy(repo)
            out[alias] = f"{sha[:12]} in {time.monotonic() - started:.0f}s"
        except Exception as exc:
            out[alias] = f"failed: {str(exc)[:160]}"
    print(f"[VOMEOS] code copies: {out}")
    return out


def _grep(repo: Repo, pattern: str, path: str, context, case: bool) -> str:
    from vomeos.integrations import code_mirror

    if not pattern.strip():
        return "Give a pattern."
    try:
        root, sha = _copy(repo)
    except Exception as exc:
        return (f"Grep failed: {str(exc)[:200]}. This is NOT 'no match'; use "
                "search_code or read_code_file instead.")
    lines, matches, files = code_mirror.grep(
        root, pattern, path_glob=path, ignore_case=not case,
        context=context or 0)
    where = f"{repo.alias}@{repo.branch} ({sha[:12]})" + (
        f" under {path}" if path else "")
    if not matches:
        return f"No match for /{pattern}/ in {where}."
    more = (f" Showing the first {len(lines)} lines; narrow with `path`."
            if matches > code_mirror.GREP_MAX_MATCHES else "")
    return (f"{matches} match(es) in {files} file(s), {where}.{more}\n"
            + "\n".join(lines))


def _find(repo: Repo, pattern: str) -> str:
    from vomeos.integrations import code_mirror

    try:
        root, sha = _copy(repo)
    except Exception as exc:
        return (f"Find failed: {str(exc)[:200]}. This is NOT 'no file'; list "
                "the folder with read_code_file instead.")
    paths, total = code_mirror.find(root, pattern)
    where = f"{repo.alias}@{repo.branch} ({sha[:12]})"
    if not total:
        return f"No file matches {pattern!r} in {where}."
    more = f" (first {len(paths)} of {total})" if total > len(paths) else ""
    return f"{total} file(s) in {where}{more}:\n" + "\n".join(paths)


def _task(ref: str) -> str:
    from vomeos.integrations import clickup_read

    return clickup_read.task_text(ref)


def execute(name: str, arguments: dict) -> str:
    """Run one tool call. Always returns text, never raises."""
    try:
        if name == "read_task":
            return _cap(_task(str(arguments.get("task") or "")))
        repo = repo_map().get(str(arguments.get("repo") or ""))
        if repo is None:
            return (f"Unknown repo {arguments.get('repo')!r}. Use one of: "
                    f"{', '.join(sorted(repo_map()))}.")
        repo = _on_branch(repo, str(arguments.get("branch") or "").strip())
        if isinstance(repo, str):
            return repo
        if name == "search_code":
            out = _search(repo, str(arguments.get("query") or ""))
        elif name == "read_code_file":
            out = _read(repo, str(arguments.get("path") or ""),
                        arguments.get("start_line"),
                        arguments.get("end_line"))
        elif name == "recent_commits":
            out = _commits(repo, str(arguments.get("path") or ""))
        elif name == "grep_code":
            out = _grep(repo, str(arguments.get("pattern") or ""),
                        str(arguments.get("path") or ""),
                        arguments.get("context"),
                        bool(arguments.get("case_sensitive")))
        elif name == "find_files":
            out = _find(repo, str(arguments.get("pattern") or ""))
        else:
            return f"No tool named {name!r}."
        return _cap(out)
    except Exception as exc:
        return f"Tool {name} failed: {type(exc).__name__}: {exc}"


def summarize(calls: list[dict]) -> str:
    """One line per call, for the trace and the log."""
    parts = []
    for c in calls:
        args = json.dumps(c.get("input"), ensure_ascii=False)[:80]
        parts.append(f"{c.get('name')}({args})")
    return "; ".join(parts)
