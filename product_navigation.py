"""
product_navigation.py

Where a thing lives in the product: the page, the view, the path to it.

WHY THIS EXISTS
---------------
I argued twice that source code could not answer user-education questions,
because it says what the system computes rather than what a user clicks. That
was wrong about the frontend, and the reason is structural rather than
incidental.

`vome-react` is navigable by design:

  * `src/routers/*.js` map a URL to a component:
        <PrivateRoute path="/database" component={MyVolunteers} />
  * components live at semantically named paths:
        src/views/org/dashboard/myvolunteers/MyVolunteers/MyVolunteerUserList.js
  * every visible label is a translation key, because the codebase forbids
    literal UI text.

So a question can be traced end to end. "How do I export volunteers" finds the
export control in `MyVolunteerUserList.js`, which is rendered by
`MyVolunteers`, which is mounted at `/database`. That is the answer, derived
rather than remembered, and it is current because it came from the branch.

The same file also carries this hint:

    "If you are looking to only export [the filtered set], please select
    checkbox"

which is a procedural caveat of exactly the kind I claimed code did not hold.
It does. Inline hints, empty states and tooltips are written for users and
they sit right next to the control they describe.

WHAT THIS IS AND IS NOT
-----------------------
It is a map: concept to file to route. It gives an agent the page to name and
the exact wording of the controls on it.

It is not a guarantee the customer can see any of it. Master is not
necessarily deployed, routes are permission-gated (`Authorization(Component,
[5])`), and features are plan-gated. Treat a hit as "this exists in the
product" and never as "this is live for you", and never put a file path, a
component name or a translation key in a message to a customer.

CONFIGURATION
-------------
    VOME_REACT_PATH   path to the vome-react src/translations directory, or
                      the repository root. Defaults to a sibling checkout.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VOME = _HERE.parent
DEFAULT_REACT = _VOME / "web-app" / "vome-react"

# <PrivateRoute path="/database" component={MyVolunteers} />, and the
# multi-line form where path and component sit on separate lines.
_ROUTE_PATH = re.compile(r'path=\{?["\']([^"\']+)["\']')
_ROUTE_COMPONENT = re.compile(r'component=\{(?:\w+\()?(\w+)')

# import MyVolunteers from "../views/org/dashboard/.../MyVolunteer";
_IMPORT = re.compile(
    r'import\s+(\w+)\s+from\s+["\']([^"\']+)["\']'
)

_STOPWORDS = frozenset(
    """
    a an and are as at be by can cannot could do does for from get has have
    how in into is it its me my not of on or our so that the their them then
    there these this to up us was we what when where which who why will with
    you your would able want need please thanks issue problem help page view
    """.split()
)


def react_root() -> Path | None:
    raw = os.environ.get("VOME_REACT_PATH", "")
    candidates = []
    if raw:
        path = Path(raw)
        # Accept either the repo root or the translations directory.
        candidates += [path, path.parent.parent, path.parent]
    candidates.append(DEFAULT_REACT)
    for candidate in candidates:
        if (candidate / "src" / "routers").is_dir():
            return candidate
    return None


def available() -> bool:
    return react_root() is not None


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def _split_identifier(name: str) -> str:
    """MyVolunteerUserList -> my volunteer user list."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name)


@dataclass(frozen=True)
class Route:
    path: str
    component: str
    file: str = ""


@lru_cache(maxsize=1)
def _routes() -> list[Route]:
    """Every URL the app mounts, paired with the component behind it."""
    root = react_root()
    if root is None:
        return []

    routes: list[Route] = []
    for router in sorted((root / "src" / "routers").glob("*.js")):
        try:
            text = router.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        imports = {
            name: target for name, target in _IMPORT.findall(text)
        }

        # Routes can span lines, so work on <...> blocks rather than lines.
        for block in re.findall(r"<[A-Za-z]*Route\b[^>]*>", text, re.DOTALL):
            path_match = _ROUTE_PATH.search(block)
            comp_match = _ROUTE_COMPONENT.search(block)
            if not path_match or not comp_match:
                continue
            component = comp_match.group(1)
            routes.append(Route(
                path=path_match.group(1),
                component=component,
                file=imports.get(component, ""),
            ))
    return routes


def _route_rank(path: str) -> tuple[int, int]:
    """Sort key preferring the page a person can navigate to.

    `/org-schedule` is a page. `/org-schedule/shift/:id` is a deep link to one
    record, and telling a customer to visit it is useless because they cannot
    type an id. Parameterised and deeper routes therefore lose.
    """
    return (path.count(":"), path.count("/"))


@lru_cache(maxsize=1)
def _component_index() -> dict[str, str]:
    """Component file stem -> the most navigable route rendering it."""
    candidates: dict[str, list[str]] = {}
    for route in _routes():
        names = [route.component]
        if route.file:
            names.append(Path(route.file).stem)
        for name in names:
            candidates.setdefault(name, []).append(route.path)
    return {
        name: sorted(paths, key=_route_rank)[0]
        for name, paths in candidates.items()
    }


def route_for_file(file_path: str) -> str:
    """Best-guess route for a component file.

    Walks up the path: an inner component such as MyVolunteerUserList is not
    itself routed, but the directory it sits in usually names the routed
    component (MyVolunteers), so the ancestry finds it.
    """
    index = _component_index()
    parts = Path(file_path).with_suffix("").parts
    for name in reversed(parts):
        if name in index:
            return index[name]
        for known, path in index.items():
            if known.lower().startswith(name.lower()[:12]) and len(name) > 6:
                return path
    return ""


def find_pages(query: str, limit: int = 4) -> list[dict]:
    """Component files whose name and location match the question."""
    root = react_root()
    if root is None:
        return []

    wanted = _tokens(query)
    if not wanted:
        return []

    scored: list[tuple[float, dict]] = []
    views = root / "src" / "views"
    for path in views.rglob("*.js"):
        relative = path.relative_to(root).as_posix()
        # The directory path is semantic: views/org/dashboard/myvolunteers/...
        haystack = _split_identifier(relative.replace("/", " "))
        have = _tokens(haystack)
        if not have:
            continue
        hit = len(wanted & have) / len(wanted)
        if hit < 0.34:
            continue
        route = route_for_file(relative)
        scored.append((hit + (0.3 if route else 0), {
            "file": relative,
            "route": route,
            "score": round(hit, 3),
        }))

    scored.sort(key=lambda row: row[0], reverse=True)
    return [row[1] for row in scored[:limit]]


def describe_navigation(query: str, limit: int = 4) -> str:
    """A context block naming where in the product this lives."""
    if not available():
        return (
            "Product navigation was NOT searched (no vome-react checkout "
            "found). Set VOME_REACT_PATH to enable it."
        )
    pages = find_pages(query, limit=limit)
    if not pages:
        return (
            f"No product page matched (searched {len(_routes())} routes). "
            "Weak evidence only."
        )
    lines = [
        "Where this lives in the product (derived from the frontend routes "
        "and component tree; routes are permission and plan gated, so this "
        "shows what EXISTS, not what a given customer can see):"
    ]
    for page in pages:
        where = page["route"] or "(not directly routed)"
        lines.append(f"  {where}  <- {page['file']}")
    return "\n".join(lines)
