"""
mcp_connector/knowledge.py

The caveats: what the team learned the hard way, ranked against a question.

Two sources, both of which hold the thing no UI string or help article does,
which is "it works, except when":

  * the Sprint Lessons Log, a ClickUp doc page Sam writes during sprints;
  * `knowledge_sections`, generated weekly from closed tickets and closed
    engineering tasks.

Sam's voice guide is deliberately excluded. It is a style guide for drafting
and carries no product facts, and the claude.ai project already holds its own
drafting instructions.

Ranking reuses `product_knowledge._rank`: heading-weighted token overlap,
damped by length. Same scorer, so the same query behaves the same way across
every source the connector returns.
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from mcp_connector import config

_CACHE_TTL = 900
_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}
_lock = threading.Lock()

_EXCLUDED_SECTIONS = ("sams_voice",)


def chunk_markdown(text: str, prefix: str = "") -> list[tuple[str, str]]:
    """Split markdown into (heading, paragraph) pairs.

    A paragraph inherits the nearest heading above it, so a short bullet
    like "only appears when a claim exists" keeps the context that says
    which feature it is about.
    """
    chunks: list[tuple[str, str]] = []
    heading = prefix
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            chunks.append((heading, body))
        buffer.clear()

    for line in (text or "").splitlines():
        match = re.match(r"^\s*#{1,6}\s+(.*)", line)
        if match:
            flush()
            title = match.group(1).strip()
            heading = f"{prefix}: {title}" if prefix else title
        elif not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()
    return chunks


def _cached(key: str, loader) -> list[tuple[str, str]]:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (now, value)
    return value


def _load_lessons() -> list[tuple[str, str]]:
    """Raises when ClickUp cannot be read, so the caller can say so."""
    token, team = config.clickup_token(), config.clickup_team_id()
    if not token or not team:
        raise RuntimeError("CLICKUP_API_TOKEN or CLICKUP_TEAM_ID not set")
    chunks: list[tuple[str, str]] = []
    for page_id in config.lessons_page_ids():
        response = httpx.get(
            f"https://api.clickup.com/api/v3/workspaces/{team}/docs/"
            f"{config.lessons_doc_id()}/pages/{page_id}",
            headers={"Authorization": token},
            params={"content_format": "text/md"},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        page = body[0] if isinstance(body, list) and body else body
        name = (page or {}).get("name") or "Sprint Lessons Log"
        chunks += chunk_markdown((page or {}).get("content") or "", name)
    return chunks


def _load_sections() -> list[tuple[str, str]]:
    import database

    # get_current_knowledge_sections returns [] with no database, which
    # would read as "searched, nothing there". It was not searched.
    if not database._database_url_is_usable(database.DATABASE_URL):
        raise RuntimeError("no database reachable")
    chunks: list[tuple[str, str]] = []
    for section in database.get_current_knowledge_sections():
        if section["section_key"] in _EXCLUDED_SECTIONS:
            continue
        chunks += chunk_markdown(section["content"], section["title"])
    return chunks


def search_caveats(question: str, limit: int = 4) -> dict:
    """Ranked caveats, with an honest report of which sources ran."""
    from product_knowledge import _rank

    sources: dict[str, str] = {}
    results: list[dict] = []

    for name, key, loader in (
        ("sprint_lessons_log", "lessons", _load_lessons),
        ("learned_from_closed_tickets", "sections", _load_sections),
    ):
        try:
            pool = _cached(key, loader)
        except Exception as exc:
            sources[name] = f"NOT searched ({exc})"
            continue
        if not pool:
            sources[name] = "searched, source is empty"
            continue
        sources[name] = f"searched {len(pool)} passages"
        for score, heading, body in _rank(question, pool, limit, 0.2):
            results.append({
                "source": name,
                "about": heading,
                "text": body[:1200],
                "score": round(score, 3),
            })

    results.sort(key=lambda row: row["score"], reverse=True)
    return {"sources": sources, "caveats": results[:limit]}
