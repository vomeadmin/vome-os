"""
product_strings.py

The product's own words, from the translation files. Source of truth for what
a thing is called and, surprisingly often, what it does.

WHY THIS EXISTS
---------------
I argued that source code was the wrong place to answer product questions,
because code says what the system computes rather than what a user clicks.
That was right about backend code and wrong about this.

`vome-react/src/translations/postlogin.js` holds **27,078 UI strings**, of
which **1,701 are explanatory** (`_exp`, `_desc`, `_tooltip`, `_help`,
`_hint`). They read like this:

    chat_all_members_desc: "One announcement channel for every active member
    of your database. Archived and offline profiles are excluded
    automatically."

That is a product answer, written for a user, in the product's own voice, and
it is on master. Every label a customer can see is in here by policy, because
the codebase forbids literal UI text. So this is the authoritative answer to
two questions the help centre answers badly:

  * **Does this feature exist?** The help centre can be silent about a real
    feature. The strings cannot: if a user can see it, it is in here.
  * **What is it actually called?** Telling a customer to click "Export" when
    the button says "Export to Excel" wastes a round trip.

WHAT IT STILL DOES NOT GIVE YOU
-------------------------------
The sequence and the caveats. "Opportunities, filter by Pending hour claims,
click the pill, Select All, Approve All" is an order of operations, and
"the Actions option only appears when at least one hour claim exists" is a
caveat learned from a ticket. Neither is in the strings. Those live in the
help centre and the Sprint Lessons Log.

So: strings for vocabulary and existence, help centre for procedure, lessons
for caveats. Three sources, three jobs.

A WARNING ABOUT master
----------------------
What is on master is not necessarily what the customer is running. A string
that exists here may be behind an unreleased branch or a plan gate. Treat a
hit as "this exists in the product" and never as "this is live for you", and
never quote a key name to a customer.

CONFIGURATION
-------------
    VOME_REACT_PATH   path to the vome-react checkout
    VOME_APP_PATH     path to the VomeApp (mobile) checkout

Both default to sibling directories of this repository. When neither is
present the module reports that it did not look, rather than reporting that
nothing was found.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VOME = _HERE.parent

DEFAULT_WEB = _VOME / "web-app" / "vome-react" / "src" / "translations"
DEFAULT_MOBILE = _VOME / "mobile-app" / "VomeApp" / "src" / "translations"

# key: "value" at any indentation. Values may contain escaped quotes.
_ENTRY = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"((?:[^"\\]|\\.)*)"')

# Where the French block begins. Everything after it is a translation of
# something already captured, so parsing stops there.
_FR_BLOCK = re.compile(r'^\s{0,8}fr\s*:\s*\{')

_EXPLANATORY = ("_exp", "_desc", "_tooltip", "_help", "_hint", "_info")

_STOPWORDS = frozenset(
    """
    a an and are as at be by can cannot could do does for from get has have
    how in into is it its me my not of on or our so that the their them then
    there these this to up us was we what when where which who why will with
    you your would able want need please thanks issue problem help
    """.split()
)


def _tokens(text: str) -> set[str]:
    # Split snake_case keys as well as prose.
    words = re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def _sources() -> list[tuple[str, Path]]:
    web = Path(os.environ.get("VOME_REACT_PATH", str(DEFAULT_WEB)))
    mobile = Path(os.environ.get("VOME_APP_PATH", str(DEFAULT_MOBILE)))
    found = []
    for label, directory in (("web", web), ("mobile", mobile)):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.js")):
            found.append((label, path))
    return found


def available() -> bool:
    return bool(_sources())


@lru_cache(maxsize=1)
def _strings() -> list[tuple[str, str, str]]:
    """Every English UI string as (surface, key, value).

    Parsed line by line rather than evaluated: the files are JavaScript
    modules, not JSON, and importing a 3MB module to read strings out of it
    is neither safe nor necessary.
    """
    entries: list[tuple[str, str, str]] = []
    for label, path in _sources():
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if _FR_BLOCK.match(line):
                        break  # French mirrors English; stop here
                    match = _ENTRY.match(line)
                    if match:
                        key, value = match.group(1), match.group(2)
                        if len(value) >= 3:
                            entries.append((label, key, value))
        except OSError as exc:
            print(f"[STRINGS] could not read {path.name}: {exc}")
    return entries


def search_strings(
    query: str, limit: int = 6, threshold: float = 0.34
) -> list[dict]:
    """UI strings matching a question, explanatory ones ranked first."""
    wanted = _tokens(query)
    if not wanted:
        return []

    scored: list[tuple[float, dict]] = []
    for surface, key, value in _strings():
        have = _tokens(f"{key} {value}")
        if not have:
            continue
        hit = len(wanted & have) / len(wanted)
        if hit < threshold:
            continue
        # An explanation is worth more than a bare button label, and a long
        # one is worth more than a fragment.
        explanatory = key.endswith(_EXPLANATORY)
        score = hit + (0.25 if explanatory else 0) + min(len(value) / 400, 0.2)
        scored.append((score, {
            "surface": surface,
            "key": key,
            "text": value,
            "explanatory": explanatory,
            "score": round(hit, 3),
        }))

    scored.sort(key=lambda row: row[0], reverse=True)
    return [row[1] for row in scored[:limit]]


def describe_strings(query: str, limit: int = 6) -> str:
    """A context block for an agent, or an honest statement of not looking."""
    if not available():
        return (
            "Product UI strings were NOT searched (no vome-react or VomeApp "
            "checkout found). Whether a feature exists is UNKNOWN from this "
            "source. Set VOME_REACT_PATH to enable it."
        )
    hits = search_strings(query, limit=limit)
    if not hits:
        return (
            "No matching UI string found (search ran over "
            f"{len(_strings())} strings). Weak evidence that no such "
            "feature is exposed in the interface."
        )
    lines = [
        "UI strings from the product (these prove a feature EXISTS and give "
        "its exact wording, but not the steps to reach it, and master is not "
        "always what a customer is running):"
    ]
    for hit in hits:
        mark = "explanation" if hit["explanatory"] else "label"
        lines.append(f"  [{hit['surface']} {mark}] {hit['text']}")
    return "\n".join(lines)
