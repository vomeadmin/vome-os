"""
video_marketing.py

Everything the video marketing team knows about how Vome sells itself.

Sammy, 2026-10-18: "All the context should live within the marketing team."
Until now the team read help centre articles only: how to click, never why
it matters, who it is for, or how Vome talks about it. Three sources, each
for a different question:

  * The Product Marketing Playbook in ClickUp: positioning, the two ICPs,
    the prospect's own words, the messaging framework and brand voice.
    Read live, cached for an hour, so an edit in ClickUp reaches the next
    video with no deploy. The last good copy is kept if ClickUp is down.
  * The website: knowledge_book/feature_catalog.md, the landing page's own
    copy, flattened by scripts/sync_landing_strings.py. How Vome describes
    each area of the product in public.
  * The product's labels (product_strings): the exact words on screen.

The help centre stays the source of facts about what a feature does.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / "knowledge_book" / "feature_catalog.md"

# The Product Marketing Strategy Playbook v2 and the pages that bear on a
# video. Sales tooling (channels, objection handling, Clay setup, metrics)
# is left out: it is about selling, not about what to say on screen.
PLAYBOOK_DOC = os.environ.get("VIDEO_PLAYBOOK_DOC", "3dztm-3371")
PLAYBOOK_PAGES = tuple(p.strip() for p in os.environ.get(
    "VIDEO_PLAYBOOK_PAGES",
    "3dztm-1971,3dztm-2251,3dztm-1991,3dztm-2011,3dztm-2031,3dztm-2071,"
    "3dztm-2111,3dztm-2191").split(",") if p.strip())
# Competitive landscape: for deciding what to claim, not for the script.
STRATEGY_ONLY_PAGES = tuple(p.strip() for p in os.environ.get(
    "VIDEO_PLAYBOOK_STRATEGY_PAGES", "3dztm-2051").split(",") if p.strip())

CACHE_SECONDS = 3600
PAGE_CHARS = 14000          # one page, at most, in a prompt
WEBSITE_CHARS = 9000        # the website sections for one topic

# The playbook names real customers, prospects and revenue. It is context
# for judgement, never material for a public video.
PLAYBOOK_RULE = (
    "How to use this playbook: it is Vome's own positioning, audience and "
    "voice, for judgement about what to say and to whom. Never put in a "
    "video any customer or prospect name, any quote attributed to a person, "
    "any revenue, deal or customer-count figure, or a competitor's name. "
    "Prospect language here shows the words coordinators use; echo the "
    "feeling, not the person.")

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}


def _workspace() -> str:
    return (os.environ.get("CLICKUP_TEAM_ID", "") or "3604276").strip()


def _fetch_page(page_id: str) -> tuple[str, str]:
    token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("CLICKUP_API_TOKEN not set")
    response = httpx.get(
        "https://api.clickup.com/api/v3/workspaces/%s/docs/%s/pages/%s"
        % (_workspace(), PLAYBOOK_DOC, page_id),
        headers={"Authorization": token},
        params={"content_format": "text/md"},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    page = body[0] if isinstance(body, list) and body else body
    return ((page or {}).get("name") or page_id,
            ((page or {}).get("content") or "").strip())


def _page(page_id: str) -> str:
    """One playbook page as markdown, cached; the last good copy on error."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(page_id)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        name, content = _fetch_page(page_id)
        text = "## %s\n\n%s" % (name, content[:PAGE_CHARS])
        with _lock:
            _cache[page_id] = (now, text)
        return text
    except Exception as exc:
        print("[VIDEO] playbook page %s unavailable: %s" % (page_id, exc))
        return hit[1] if hit else ""


def playbook(strategy: bool = False) -> str:
    """The playbook pages for a video, with the rule on how to use them."""
    pages = PLAYBOOK_PAGES + (STRATEGY_ONLY_PAGES if strategy else ())
    texts = [t for t in (_page(p) for p in pages) if t]
    if not texts:
        return ("(the Product Marketing Playbook could not be read from "
                "ClickUp; position from the help centre and the website "
                "copy, and say so in your notes)")
    return PLAYBOOK_RULE + "\n\n" + "\n\n---\n\n".join(texts)


# ------------------------------------------------------------------ website

def _catalog_sections() -> list[tuple[str, str]]:
    try:
        text = CATALOG.read_text(encoding="utf-8")
    except OSError:
        return []
    sections, heading, buf = [], "", []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading and buf:
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = line[3:].strip(), []
        elif heading:
            buf.append(line)
    if heading and buf:
        sections.append((heading, "\n".join(buf).strip()))
    return sections


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())}


def website(topic: str, limit: int = 3) -> str:
    """How the website describes the product area a video is about.

    Ranked by plain overlap with the topic, at the level of the website's
    own short title-and-description pairs, so a long page contributes only
    the lines about this area rather than everything it says.
    """
    wanted = _words(topic)
    if not wanted:
        return "(no topic to look up)"
    scored = []
    for page, body in _catalog_sections():
        lines = [l[2:].strip() for l in body.splitlines()
                 if l.startswith("- ")]
        # Title and description alternate on the website.
        for i in range(0, len(lines), 2):
            pair = lines[i:i + 2]
            hit = len(wanted & _words(" ".join(pair)))
            if hit:
                scored.append((hit, page, pair))
    if not scored:
        return "(the website copy has nothing on this area)"
    scored.sort(key=lambda s: -s[0])
    out, used, pages = [], 0, []
    for hit, page, pair in scored:
        block = "- %s" % "\n  ".join(pair)
        if used + len(block) > WEBSITE_CHARS:
            break
        if page not in pages:
            if len(pages) >= limit + 2:
                continue
            pages.append(page)
        out.append("[%s] %s" % (page, block))
        used += len(block)
    return ("How the website describes this area (vomevolunteer.com, the "
            "landing page's own copy):\n" + "\n".join(out))


def labels(topic: str, limit: int = 12) -> str:
    """The product's own on-screen wording for this area."""
    try:
        import product_strings
        if not product_strings.available():
            return "(product labels not available here)"
        hits = product_strings.search_strings(topic, limit=limit)
    except Exception as exc:
        return "(product labels unavailable: %s)" % exc
    if not hits:
        return "(no matching product labels)"
    return "\n".join('- "%s"' % (h.get("value") or h.get("text") or "")
                     for h in hits if (h.get("value") or h.get("text")))
