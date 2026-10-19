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

ENVIRONMENT
-----------
    VOMEOS_CODE_MIRROR_DIR       where copies live (default: the temp dir)
    VOMEOS_CODE_MIRROR_TIMEOUT   seconds allowed for one download (300)
    VOMEOS_CODE_MIRROR_SHA_TTL   seconds a branch-head lookup is reused (60)
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

import httpx

from vomeos.integrations.base import IntegrationResult

SYSTEM = "github-mirror"
API = "https://api.github.com"

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
    """Stream the tarball at `sha` into `dest`, code files only."""
    with httpx.stream(
        "GET", f"{API}/repos/{owner}/{repo}/tarball/{sha}",
        headers=headers, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT,
    ) as response:
        response.raise_for_status()
        raw = io.BufferedReader(_Stream(response.iter_bytes()), 1 << 20)
        with tarfile.open(fileobj=raw, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                # GitHub wraps everything in one "<owner>-<repo>-<sha>/" dir.
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


def _ensure_copy(owner: str, repo: str, sha: str, headers: dict) -> Path:
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
            _download(owner, repo, sha, headers, work)
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
