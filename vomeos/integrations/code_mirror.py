"""
vomeos/integrations/code_mirror.py

Search a GitHub repository by downloading its code, for when GitHub's own
code search cannot answer.

WHY THIS EXISTS
---------------
GitHub's REST code search never finished indexing vome-react (about 500 MB of
history, mostly images). Every query, even `useState`, came back 200 with no
items and `incomplete_results: true`. The triage agent read that as "this
code does not exist" and gave up on #9582 (2026-10-19), a bug that a plain
grep of the repository found in minutes.

So when GitHub reports an incomplete index with nothing in it, this does what
an engineer would do locally: get the code and grep it.

WHY THIS IS AN ALLOWED EXCEPTION TO "OVER HTTP AND NOT A CLONE"
---------------------------------------------------------------
`code_search.py` rules out clones because a copy on disk silently drifts from
the real branch, and an agent reasoning about stale code is worse than one
with none. This copy cannot drift silently:

  * Before searching, the branch head is read from GitHub (at most once a
    minute per process). A different commit means a fresh download first.
  * Every result names the commit it searched, so a reader can see exactly
    what code the answer came from.
  * It is read only. The download is one GET of the branch tarball, with the
    same read token `code_search.GitHub` already uses. There is no git, no
    push, and no way to write.
  * Only code is kept: images, fonts, build output and anything over 1 MB are
    skipped while streaming, so vome-react is ~68 MB on disk, not ~370 MB.

Railway disks are ephemeral, so after a deploy the first search downloads
again (tens of seconds for vome-react). That is the price of never trusting
an old copy.

BITBUCKET TOO, AND GREP AS A TOOL (2026-10-23)
-----------------------------------------------
Bitbucket's code search indexes the default branch only (core's is
`development`, not the `master` production runs), and the triage agent
could only ask it for exact strings. A person in Claude Code greps a full
copy with a regex and finds files by name, and on the Oct 21 backlog sweep
that gap showed: 40 of 64 triage runs spent their whole budget and 46 of 79
cards ended "unclear". So the same live-commit copy now exists for the
Bitbucket repositories, and `local_copy`, `grep` and `find` let an agent
use it the way an engineer would. Bitbucket's archive is read with the same
token as its API; when that is refused, a shallow read-only `git clone`
with the token is tried, and a failure is remembered for ten minutes so a
broken credential costs one attempt, not one per call.

ENVIRONMENT
-----------
    VOMEOS_CODE_MIRROR_DIR       where copies live (default: the temp dir)
    VOMEOS_CODE_MIRROR_TIMEOUT   seconds allowed for one download (300)
    VOMEOS_CODE_MIRROR_SHA_TTL   seconds a branch-head lookup is reused (60)
"""

from __future__ import annotations

import fnmatch
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

import httpx

from vomeos.integrations.base import IntegrationResult

SYSTEM = "github-mirror"
API = "https://api.github.com"
BITBUCKET_API = "https://api.bitbucket.org/2.0"
BITBUCKET_WEB = "https://bitbucket.org"
FAILURE_TTL = 600  # seconds a failed download is not retried

CACHE_DIR = Path(
    os.environ.get("VOMEOS_CODE_MIRROR_DIR")
    or Path(tempfile.gettempdir()) / "vomeos_code_mirror"
)
DOWNLOAD_TIMEOUT = int(os.environ.get("VOMEOS_CODE_MIRROR_TIMEOUT", "300"))
SHA_TTL = int(os.environ.get("VOMEOS_CODE_MIRROR_SHA_TTL", "60"))

MAX_FILES = 20            # same page size as the GitHub search it replaces
MAX_LINES_PER_FILE = 6
MAX_LINE_CHARS = 200
MAX_FILE_BYTES = 1_000_000

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", "out", "coverage",
    "vendor", "Pods", ".gradle", ".expo", "__pycache__",
}
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".mov", ".mp3",
    ".wav", ".zip", ".gz", ".lock", ".map", ".pyc", ".so", ".class",
    ".jar", ".aab", ".apk", ".ipa",
}

_DONE = ".complete"
_sha_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_failures: dict[tuple[str, str, str], tuple[float, str]] = {}

GREP_MAX_MATCHES = 80
FIND_MAX_RESULTS = 200


def _fail(error: str) -> IntegrationResult:
    return IntegrationResult(SYSTEM, "search_code", False, error=error)


# ---------------------------------------------------------------------------
# Which commit is live
# ---------------------------------------------------------------------------

def _head_sha(owner: str, repo: str, ref: str, headers: dict) -> str:
    """The commit `ref` points at now, or the default branch when empty."""
    key = (owner, repo, ref)
    cached = _sha_cache.get(key)
    if cached and time.monotonic() - cached[0] < SHA_TTL:
        return cached[1]
    response = httpx.get(
        f"{API}/repos/{owner}/{repo}/commits/{ref or 'HEAD'}",
        headers={**headers, "Accept": "application/vnd.github.sha"},
        timeout=30,
    )
    response.raise_for_status()
    sha = response.text.strip()
    _sha_cache[key] = (time.monotonic(), sha)
    return sha


# ---------------------------------------------------------------------------
# Download, keeping only code
# ---------------------------------------------------------------------------

def _wanted(rel: PurePosixPath, size: int) -> bool:
    if size > MAX_FILE_BYTES:
        return False
    if any(part in SKIP_DIRS for part in rel.parts[:-1]):
        return False
    name = rel.name.lower()
    if name.endswith((".min.js", ".min.css")):
        return False
    return rel.suffix.lower() not in SKIP_EXT


class _Stream(io.RawIOBase):
    """A file-like view of an httpx byte stream, so tarfile can read it."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._buffer = b""

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        while not self._buffer:
            try:
                self._buffer = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(target), len(self._buffer))
        target[:n] = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return n


def _download(owner: str, repo: str, sha: str, headers: dict,
              dest: Path) -> None:
    """Stream the GitHub tarball at `sha` into `dest`, code files only."""
    _stream_tarball(f"{API}/repos/{owner}/{repo}/tarball/{sha}", headers,
                    dest)


def _stream_tarball(url: str, headers: dict, dest: Path) -> None:
    with httpx.stream(
        "GET", url,
        headers=headers, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT,
    ) as response:
        response.raise_for_status()
        raw = io.BufferedReader(_Stream(response.iter_bytes()), 1 << 20)
        with tarfile.open(fileobj=raw, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                # Both hosts wrap everything in one "<owner>-<repo>-<sha>/"
                # directory.
                parts = PurePosixPath(member.name).parts[1:]
                if not parts or any(p in ("..", "") for p in parts):
                    continue
                rel = PurePosixPath(*parts)
                if not _wanted(rel, member.size):
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                target = dest.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    shutil.copyfileobj(source, out)


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------

def _bitbucket_head_sha(workspace: str, repo: str, ref: str,
                        headers: dict) -> str:
    key = ("bitbucket:" + workspace, repo, ref)
    cached = _sha_cache.get(key)
    if cached and time.monotonic() - cached[0] < SHA_TTL:
        return cached[1]
    response = httpx.get(
        f"{BITBUCKET_API}/repositories/{workspace}/{repo}/commit/"
        f"{ref or 'HEAD'}",
        headers=headers, params={"fields": "hash"}, timeout=30,
    )
    response.raise_for_status()
    sha = str(response.json().get("hash") or "").strip()
    if not sha:
        raise RuntimeError(f"no commit for {repo}@{ref}")
    _sha_cache[key] = (time.monotonic(), sha)
    return sha


def _bitbucket_download(workspace: str, repo: str, sha: str, headers: dict,
                        dest: Path) -> None:
    """The archive at `sha`; failing that, a shallow read-only clone."""
    try:
        _stream_tarball(f"{BITBUCKET_WEB}/{workspace}/{repo}/get/{sha}.tar.gz",
                        headers, dest)
        return
    except Exception as archive_error:
        first = f"{type(archive_error).__name__}: {str(archive_error)[:120]}"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        _git_clone(workspace, repo, sha, headers, dest)
    except Exception as clone_error:
        raise RuntimeError(
            f"archive refused ({first}); clone failed "
            f"({type(clone_error).__name__}: {str(clone_error)[:120]})"
        ) from None


def _git_clone(workspace: str, repo: str, sha: str, headers: dict,
               dest: Path) -> None:
    """`git fetch --depth 1` of exactly `sha`, then copy the code out.

    The token goes in an http.extraHeader for this one command, never in a
    URL or on disk, so it cannot leak through a remote config or a log line.
    """
    if not shutil.which("git"):
        raise RuntimeError("git is not installed on this host")
    auth = headers.get("Authorization", "")
    work = Path(tempfile.mkdtemp(prefix="clone.", dir=dest.parent))
    try:
        base = ["git", "-c", f"http.extraHeader=Authorization: {auth}"]
        url = f"{BITBUCKET_WEB}/{workspace}/{repo}.git"
        steps = (
            ["git", "init", "-q", str(work)],
            base + ["-C", str(work), "fetch", "-q", "--depth", "1", url, sha],
            ["git", "-C", str(work), "checkout", "-q", "FETCH_HEAD"],
        )
        for step in steps:
            done = subprocess.run(step, capture_output=True, text=True,
                                  timeout=DOWNLOAD_TIMEOUT)
            if done.returncode:
                raise RuntimeError((done.stderr or "git failed").replace(
                    auth, "***").strip()[:160])
        for path in work.rglob("*"):
            if not path.is_file():
                continue
            rel = PurePosixPath(path.relative_to(work).as_posix())
            if not _wanted(rel, path.stat().st_size):
                continue
            target = dest.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _ensure_copy(owner: str, repo: str, sha: str, headers: dict,
                 download=None) -> Path:
    """The local copy of `repo` at `sha`, downloading it if needed.

    Several worker processes share one disk, so the first one to arrive
    downloads and the rest wait for its `.complete` marker rather than
    pulling the same tarball four times.
    """
    base = CACHE_DIR / f"{owner}__{repo}"
    final = base / sha
    if (final / _DONE).exists():
        return final

    base.mkdir(parents=True, exist_ok=True)
    lock = base / f"{sha}.lock"
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT + 30
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            if (final / _DONE).exists():
                return final
            try:
                stale = time.time() - lock.stat().st_mtime > DOWNLOAD_TIMEOUT
            except FileNotFoundError:
                continue
            if stale:
                lock.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError("another process is still downloading")
            time.sleep(1)

    try:
        work = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}.", dir=base))
        try:
            (download or _download)(owner, repo, sha, headers, work)
            (work / _DONE).write_text(sha)
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            work.rename(final)
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
        # Older commits are of no further use; only the live one is searched.
        for old in base.iterdir():
            if old.is_dir() and old.name != sha:
                shutil.rmtree(old, ignore_errors=True)
        return final
    finally:
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------

def _terms(query: str) -> tuple[str, list[str]]:
    """The phrase to look for first, and its words for the fallback."""
    phrase = query.strip()
    if len(phrase) >= 2 and phrase[0] == phrase[-1] and phrase[0] in "\"'":
        phrase = phrase[1:-1].strip()
    return phrase.lower(), [w for w in phrase.lower().split() if w]


def _grep(root: Path, phrase: str, words: list[str]) -> tuple[list, int]:
    """Files containing `phrase`; failing that, files with every word.

    Mirrors how GitHub's search treats a multi-word query (all terms, any
    order) while still preferring an exact match, which is what a pasted
    error message needs.
    """
    files = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.name != _DONE),
        key=lambda p: (not p.relative_to(root).as_posix().startswith("src/"),
                       p.relative_to(root).as_posix()),
    )
    attempts = [[phrase]] + ([words] if len(words) > 1 else [])
    for needles in attempts:
        items, total = [], 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lowered = text.lower()
            if "\x00" in text[:1024]:
                continue
            if not all(n in lowered for n in needles):
                continue
            total += 1
            if len(items) >= MAX_FILES:
                continue
            lines = []
            for number, line in enumerate(text.splitlines(), start=1):
                if any(n in line.lower() for n in needles):
                    lines.append({
                        "line": number,
                        "segments": [{"text": line.strip()[:MAX_LINE_CHARS]}],
                    })
                    if len(lines) >= MAX_LINES_PER_FILE:
                        break
            items.append({
                "path": path.relative_to(root).as_posix(),
                "content_matches": [{"lines": lines}],
            })
        if total:
            return items, total
    return [], 0


def search(owner: str, repo: str, query: str, headers: dict,
           ref: str = "") -> IntegrationResult:
    """Grep `repo` at the live head of `ref` (default branch when empty).

    `data` has the GitHub search shape (`items`, `total_count`,
    `incomplete_results`) so existing callers read it unchanged, each item
    also carrying `content_matches` lines in the Bitbucket shape, plus
    `mirror: {"ref", "sha"}` naming exactly what was searched.
    """
    started = time.monotonic()
    phrase, words = _terms(query)
    if not phrase:
        return _fail("give a search term")
    try:
        sha = _head_sha(owner, repo, ref, headers)
        root = _ensure_copy(owner, repo, sha, headers)
        items, total = _grep(root, phrase, words)
    except Exception as exc:  # any failure is "unknown", never "no match"
        return _fail(f"{type(exc).__name__}: {str(exc)[:200]}")
    return _result(repo, ref, sha, items, total, started)


def _result(repo: str, ref: str, sha: str, items: list, total: int,
            started: float) -> IntegrationResult:
    for item in items:
        item["repository"] = {"name": repo}
    return IntegrationResult(
        SYSTEM, "search_code", True,
        data={
            "items": items,
            "total_count": total,
            "incomplete_results": False,
            "mirror": {"ref": ref or "default branch", "sha": sha[:12]},
        },
        duration_ms=int((time.monotonic() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# Any host: the copy, and grep / find over it
# ---------------------------------------------------------------------------

def local_copy(host: str, owner: str, repo: str, ref: str,
               headers: dict) -> tuple[Path, str]:
    """(root, sha) of `repo` at the live head of `ref`. Raises on failure.

    A failure is remembered for FAILURE_TTL so an agent's next twenty calls
    do not each retry a refused credential for minutes.
    """
    key = (host, owner, repo)
    failed = _failures.get(key)
    if failed and time.monotonic() - failed[0] < FAILURE_TTL:
        raise RuntimeError(f"copy unavailable (recent failure: {failed[1]})")
    try:
        if host == "bitbucket":
            sha = _bitbucket_head_sha(owner, repo, ref, headers)
            root = _ensure_copy(owner, repo, sha, headers,
                                download=_bitbucket_download)
        else:
            sha = _head_sha(owner, repo, ref, headers)
            root = _ensure_copy(owner, repo, sha, headers)
    except Exception as exc:
        _failures[key] = (time.monotonic(),
                          f"{type(exc).__name__}: {str(exc)[:200]}")
        raise
    _failures.pop(key, None)
    return root, sha


def search_copy(host: str, owner: str, repo: str, query: str, headers: dict,
                ref: str = "") -> IntegrationResult:
    """`search` for either host, in the same result shape."""
    if host != "bitbucket":
        return search(owner, repo, query, headers, ref=ref)
    started = time.monotonic()
    phrase, words = _terms(query)
    if not phrase:
        return _fail("give a search term")
    try:
        root, sha = local_copy(host, owner, repo, ref, headers)
        items, total = _grep(root, phrase, words)
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {str(exc)[:200]}")
    return _result(repo, ref, sha, items, total, started)


def _files(root: Path, path_glob: str = "") -> list[tuple[str, Path]]:
    out = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name == _DONE:
            continue
        rel = path.relative_to(root).as_posix()
        if path_glob and not _glob_match(rel, path_glob):
            continue
        out.append((rel, path))
    out.sort(key=lambda pair: pair[0])
    return out


def _glob_match(rel: str, pattern: str) -> bool:
    """`src/**/*.js`, `*.py`, or a plain folder prefix like `sequence_app`."""
    pattern = pattern.strip().lstrip("./")
    if not any(ch in pattern for ch in "*?["):
        return rel == pattern or rel.startswith(pattern.rstrip("/") + "/")
    if "/" not in pattern:
        return fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern)
    # fnmatch's * already crosses "/", so ** needs no special case beyond
    # letting "dir/**/x" also match "dir/x".
    return (fnmatch.fnmatch(rel, pattern)
            or fnmatch.fnmatch(rel, pattern.replace("**/", "")))


def grep(root: Path, pattern: str, path_glob: str = "",
         ignore_case: bool = True, context: int = 0,
         max_matches: int = GREP_MAX_MATCHES) -> tuple[list[str], int, int]:
    """Regex search, like `rg`. (lines, matches, files with a match).

    An invalid regex is searched as plain text rather than refused, since
    an agent pasting an error message with brackets in it means the text.
    """
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error:
        rx = re.compile(re.escape(pattern), flags)
    context = max(0, min(int(context or 0), 5))
    out: list[str] = []
    matches = files = 0
    for rel, path in _files(root, path_glob):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "\x00" in text[:1024] or not rx.search(text):
            continue
        files += 1
        lines = text.splitlines()
        shown: set[int] = set()
        for index, line in enumerate(lines):
            if not rx.search(line):
                continue
            matches += 1
            if len(out) >= max_matches:
                continue
            lo, hi = max(0, index - context), min(len(lines), index + context
                                                  + 1)
            for n in range(lo, hi):
                if n in shown:
                    continue
                shown.add(n)
                mark = ":" if n == index else "-"
                out.append(f"{rel}{mark}{n + 1}{mark} "
                           f"{lines[n].strip()[:MAX_LINE_CHARS]}")
    return out, matches, files


def find(root: Path, pattern: str,
         max_results: int = FIND_MAX_RESULTS) -> tuple[list[str], int]:
    """Paths matching a glob (`**/*Reservation*.js`), or containing a plain
    word in their path. (paths, total)."""
    pattern = (pattern or "").strip()
    if not pattern:
        return [], 0
    if any(ch in pattern for ch in "*?["):
        hits = [rel for rel, _ in _files(root, pattern)]
    else:
        word = pattern.lower()
        hits = [rel for rel, _ in _files(root) if word in rel.lower()]
    return hits[:max_results], len(hits)
