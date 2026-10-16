"""
mcp_connector/tools.py

What the connector can answer, as plain synchronous functions.

Kept free of MCP so each one can be called from a test or a shell exactly as
claude.ai will call it. `server.py` wraps them: it runs each in a thread,
records who called it, and turns a RequestError into a readable refusal.

THE RULE EVERY RESULT CARRIES
-----------------------------
Every result has `sources`: for each place it looked, either what it searched
or "NOT searched" and why. A source that did not run must never read as a
source that found nothing. That distinction is the whole difference between
"this feature does not exist" and "we did not check", and confusing the two is
how a customer gets told something false.
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from pathlib import Path

from mcp_connector import (
    config,
    feature_requests,
    knowledge,
    product_source,
    repos,
)

INTERNAL = (
    "INTERNAL CONTEXT. Never put a file path, component or function name, "
    "translation key, branch, commit or ClickUp task id in a message to a "
    "customer. Describe the product in the words the customer sees."
)

_T_CALL = re.compile(r"""\bt\(\s*["']([A-Za-z0-9_]+)["']""")


def _db_usable() -> bool:
    import database

    return database._database_url_is_usable(database.DATABASE_URL)


def _tokens(text: str) -> set[str]:
    from product_strings import _tokens as strings_tokens

    return strings_tokens(text)


# ---------------------------------------------------------------------------
# lookup_feature
# ---------------------------------------------------------------------------

def lookup_feature(question: str, language: str = "en") -> dict:
    """Does it exist, what is it called, how does it work, what are the
    catches. Help centre first, then the product's own words, then caveats.
    """
    import product_knowledge
    import product_strings

    question = (question or "").strip()[:500]
    language = language if language in ("en", "fr") else "en"
    source_state = product_source.ensure_fresh()
    sources: dict[str, str] = {}
    result: dict = {"question": question}

    # 1. Help centre: the published steps, and a link a customer can open.
    if _db_usable():
        try:
            from kb_sync import search_kb_articles

            articles = search_kb_articles(
                question, n_results=3, language=language, body_chars=1800,
            )
            result["help_centre"] = [
                {
                    "title": a["title"],
                    "url": a["url"],
                    "excerpt": a["body"],
                    "days_since_updated": a.get("days_stale"),
                }
                for a in articles
            ]
            sources["help_centre"] = f"searched ({language})"
        except Exception as exc:
            sources["help_centre"] = f"NOT searched ({exc})"
    else:
        sources["help_centre"] = "NOT searched (no database on this server)"

    # 2. Setup Guide and feature catalog, on disk.
    guide = product_knowledge.search_product_knowledge(question, limit=3)
    result["setup_guide_and_catalog"] = [
        {"source": h["source"], "section": h["title"],
         "text": h["body"][:1200]}
        for h in guide
    ]
    sources["setup_guide_and_catalog"] = "searched"

    # 3. The product's own words. Existence and exact naming.
    if product_strings.available():
        hits = product_strings.search_strings(question, limit=10)
        result["ui_text"] = [
            {
                "surface": h["surface"],
                "kind": "explanation" if h["explanatory"] else "label",
                "text": h["text"],
                "internal_key": h["key"],
            }
            for h in hits
        ]
        sources["ui_text"] = (
            f"searched {len(product_strings._strings())} strings on branch "
            f"{source_state.get('web_branch') or 'local checkout'}"
        )
    else:
        sources["ui_text"] = (
            "NOT searched (frontend source unavailable: "
            f"{source_state.get('error') or 'no checkout'})"
        )

    # 4. Caveats learned the hard way.
    caveats = knowledge.search_caveats(question)
    result["caveats"] = caveats["caveats"]
    sources.update(caveats["sources"])

    result["sources"] = sources
    result["how_to_use_this"] = (
        "Help centre and Setup Guide give the steps and are safe to link. UI "
        "text proves a feature exists and gives its exact name, but shows "
        "what is on the live branch, not what a given customer's plan or "
        "role can see. Caveats are internal lessons: use them to avoid a "
        "wrong answer, do not quote them. If nothing here supports an "
        "answer, say so and use check_existing_work or the code tools "
        "rather than guessing. " + INTERNAL
    )
    return result


# ---------------------------------------------------------------------------
# where_in_app
# ---------------------------------------------------------------------------

def _labels_in_file(relative: str, question: str, limit: int = 12) -> list:
    """The English text of every t('key') a component renders, best first."""
    import product_navigation
    import product_strings

    root = product_navigation.react_root()
    if root is None:
        return []
    try:
        source = (root / relative).read_text(encoding="utf-8",
                                             errors="replace")
    except OSError:
        return []
    keys = set(_T_CALL.findall(source))
    if not keys:
        return []
    english = {
        key: value for surface, key, value in product_strings._strings()
        if surface == "web" and key in keys
    }
    wanted = _tokens(question)
    ranked = sorted(
        english.items(),
        key=lambda kv: (
            -len(wanted & _tokens(f"{kv[0]} {kv[1]}")),
            not kv[0].endswith(product_strings._EXPLANATORY),
        ),
    )
    return [
        {
            "text": value,
            "kind": (
                "explanation" if key.endswith(product_strings._EXPLANATORY)
                else "label"
            ),
        }
        for key, value in ranked[:limit]
    ]


@lru_cache(maxsize=2)
def _key_usage(root: str, stamp: float) -> dict[str, tuple[str, ...]]:
    """Translation key -> the view files that render it.

    `stamp` is the source's fetch time, so a refresh builds a new index.
    """
    usage: dict[str, set[str]] = {}
    base = Path(root)
    for path in (base / "src" / "views").rglob("*.js"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(base).as_posix()
        for key in set(_T_CALL.findall(text)):
            usage.setdefault(key, set()).add(relative)
    return {key: tuple(sorted(files)) for key, files in usage.items()}


# Screens that describe features rather than hold them: the marketing site,
# pricing comparisons and the profile-completion upsell. They match almost
# every question, because describing every feature is their job, and they are
# never where a customer goes to do anything.
_NOT_A_WORKPLACE = (
    "src/views/landingPage/",
    "src/views/org/profileCompletion/",
    "src/views/me/profileCompletion/",
    "src/views/welcome/",
    "src/views/thank-you/",
)


def _singular(question: str) -> str:
    """'approve hour claims' -> 'approve hour claim'. Crude, and enough:
    the string search matches whole words, so a plural in the question
    otherwise misses every singular label."""
    words = []
    for word in question.split():
        lower = word.lower()
        if lower.endswith("ies") and len(lower) > 4:
            word = word[:-3] + "y"
        elif (
            lower.endswith("s") and not lower.endswith("ss")
            and len(lower) > 3
        ):
            word = word[:-1]
        words.append(word)
    return " ".join(words)


def _rank_screens(question: str, limit: int = 4) -> list[dict]:
    """Candidate screens, found two ways and scored together.

    By the words on the screen: the UI strings that match the question,
    traced to the components that render them. This is what finds "Approve
    all" on the hour-request popup, whose file name says nothing about
    approving.

    By the file's own path: `product_navigation.find_pages`, which wins when
    the directory names the feature (myvolunteers, kiosk, sequences).
    """
    import product_navigation
    import product_strings

    root = product_navigation.react_root()
    index = _key_usage(
        str(root), float(product_source._state["fetched_at"] or 0)
    )

    best: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    hits = product_strings.search_strings(question, limit=30)
    variant = _singular(question)
    if variant != question:
        seen = {h["key"] for h in hits}
        hits += [
            h for h in product_strings.search_strings(variant, limit=30)
            if h["key"] not in seen
        ]
    for hit in hits:
        if hit["surface"] != "web":
            continue
        for file in index.get(hit["key"], ())[:8]:
            if file.startswith(_NOT_A_WORKPLACE):
                continue
            best[file] = max(best.get(file, 0), hit["score"])
            matched.setdefault(file, []).append(hit["text"])

    # The best single match decides, and each further matching string adds a
    # little. Summing outright let a file with many loosely related
    # sentences outrank the one control that answers the question.
    scores = {
        file: top + 0.1 * (len(matched[file]) - 1)
        for file, top in best.items()
    }
    for page in product_navigation.find_pages(question, limit=6):
        if page["file"].startswith(_NOT_A_WORKPLACE):
            continue
        scores[page["file"]] = scores.get(page["file"], 0) + page["score"]

    by_screen: dict[str, dict] = {}
    for file, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        route = product_navigation.route_for_file(file)
        screen = route or file
        if screen in by_screen:
            continue
        by_screen[screen] = {
            "file": file, "route": route, "score": score,
            "matched": matched.get(file, [])[:4],
        }
        if len(by_screen) >= limit:
            break
    return list(by_screen.values())


def where_in_app(question: str) -> dict:
    """Which page, and the exact wording of the controls on it."""
    import product_navigation

    question = (question or "").strip()[:500]
    source_state = product_source.ensure_fresh()
    if not product_navigation.available():
        return {
            "question": question,
            "pages": [],
            "sources": {
                "navigation": (
                    "NOT searched (frontend source unavailable: "
                    f"{source_state.get('error') or 'no checkout'})"
                ),
            },
        }

    out = []
    for screen in _rank_screens(question):
        out.append({
            "route": screen["route"] or None,
            "area": _describe_area(screen["file"]),
            "internal_file": screen["file"],
            "matched_text": screen["matched"],
            "text_on_this_screen": _labels_in_file(screen["file"], question),
        })
    return {
        "question": question,
        "pages": out,
        "sources": {
            "navigation": (
                f"searched {len(product_navigation._routes())} routes on "
                f"branch {source_state.get('web_branch') or 'local checkout'}"
            ),
        },
        "how_to_use_this": (
            "Name the page by what the customer sees (the menu or page name "
            "and the labels in text_on_this_screen), never by the route or "
            "file. Routes are role and plan gated, so this is what EXISTS, "
            "not necessarily what this customer can see. " + INTERNAL
        ),
    }


def _describe_area(relative: str) -> str:
    """views/org/dashboard/myvolunteers/X.js -> 'org > dashboard > ...'."""
    parts = Path(relative).with_suffix("").parts
    if parts[:2] == ("src", "views"):
        parts = parts[2:]
    return " > ".join(parts[:-1]) if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# check_existing_work
# ---------------------------------------------------------------------------

def check_existing_work(query: str) -> dict:
    """Is this already logged, in flight, shipped or declined?"""
    import clickup_search

    query = (query or "").strip()[:300]
    if not config.clickup_token():
        return {
            "query": query, "ran": False, "tasks": [],
            "sources": {"clickup": "NOT searched (no ClickUp token)"},
            "how_to_use_this": (
                "The search did not run. Existing work is UNKNOWN, not none."
            ),
        }
    try:
        hits = clickup_search.search_tasks_by_text(query, limit=8)
    except Exception as exc:
        return {
            "query": query, "ran": False, "tasks": [],
            "sources": {"clickup": f"NOT searched ({exc})"},
            "how_to_use_this": (
                "The search failed. Existing work is UNKNOWN, not none."
            ),
        }
    return {
        "query": query,
        "ran": True,
        "tasks": [
            {
                "task_id": t["id"],
                "name": t["name"],
                "list": t["list"],
                "status": t["status"],
                "shipped": t["shipped"],
                "url": t["url"],
                "match": t["score"],
            }
            for t in hits
        ],
        "sources": {
            "clickup": "searched " + ", ".join(clickup_search.LISTS),
        },
        "how_to_use_this": (
            "Matching is on task names, so read each candidate before "
            "relying on it. 'shipped' true means the status is on prod, "
            "closed, done or complete; anything else is work in flight, not "
            "a fix, and must not be described to a customer as fixed or "
            "coming. A task in Declined or Sleeping is a decision, not a "
            "plan. Only say 'on the roadmap' when Sam has confirmed it. "
            + INTERNAL
        ),
    }


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------

def _connector_for(repo: repos.Repo):
    from vomeos.integrations.code_search import Bitbucket, GitHub

    allowlist = config.code_repos()
    if repo.host == "bitbucket":
        return Bitbucket(allowlist=allowlist)
    return GitHub(allowlist=allowlist)


def _bitbucket_hits(data) -> list[dict]:
    hits = []
    values = data.get("values", []) if isinstance(data, dict) else []
    for value in values:
        file = value.get("file") or {}
        repo_name = (
            ((file.get("commit") or {}).get("repository") or {})
            .get("full_name") or ""
        )
        lines = []
        for match in value.get("content_matches") or []:
            for line in match.get("lines") or []:
                text = "".join(
                    s.get("text", "") for s in line.get("segments") or []
                )
                lines.append({"line": line.get("line"), "text": text[:300]})
        hits.append({
            "repo": repo_name.split("/")[-1] or None,
            "path": file.get("path"),
            "lines": lines[:6],
        })
    return hits


def _github_hits(data) -> list[dict]:
    hits = []
    items = data.get("items", []) if isinstance(data, dict) else []
    for item in items:
        hits.append({
            "repo": (item.get("repository") or {}).get("name"),
            "path": item.get("path"),
            "lines": [],
        })
    return hits


def search_code(query: str, repo: str = "") -> dict:
    """Where a string, symbol or setting appears in the code."""
    query = (query or "").strip()[:200]
    if not query:
        return {"error": "give a search term"}

    targets: list[repos.Repo] = []
    if repo:
        found = repos.resolve(repo)
        if found is None:
            return {
                "error": f"unknown repository {repo!r}",
                "repositories": [r.slug for r in repos.all_repos()],
            }
        targets = [found]
    else:
        targets = list(repos.all_repos())

    hits: list[dict] = []
    sources: dict[str, str] = {}
    searched_bitbucket = False
    for target in targets:
        connector = _connector_for(target)
        if target.host == "bitbucket":
            if not repo and searched_bitbucket:
                continue  # one workspace-wide search covers every repo
            searched_bitbucket = True
            result = connector.search_code(query, repo=target.slug if repo
                                           else "")
            label = target.slug if repo else "bitbucket (all backend repos)"
            parsed = _bitbucket_hits(result.data) if result.ok else []
        else:
            result = connector.search_code(
                query, repo=target.slug, owner=target.owner,
            )
            label = target.slug
            parsed = _github_hits(result.data) if result.ok else []
        sources[label] = (
            f"searched, {len(parsed)} files" if result.ok
            else f"NOT searched ({result.error[:150]})"
        )
        hits += parsed

    # A workspace-wide Bitbucket search reaches every repo in the workspace,
    # not only the ones this connector may read. Drop the rest.
    permitted = {slug.lower() for slug in config.code_repos()}
    hits = [h for h in hits if (h["repo"] or "").lower() in permitted]
    return {
        "query": query,
        "files": hits[:30],
        "sources": sources,
        "how_to_use_this": (
            "Code search indexes each repository's DEFAULT branch, which is "
            "not always what production runs. Before concluding anything, "
            "open the file with read_code_file, which reads the live branch. "
            "Backend code is for diagnosis and for checking how a feature "
            "really behaves; explain the result to a customer in product "
            "terms only. " + INTERNAL
        ),
    }


def read_code_file(
    repo: str, path: str, ref: str = "", start_line: int = 0,
    end_line: int = 0,
) -> dict:
    """One file (or directory listing) from a repository's live branch."""
    found = repos.resolve(repo)
    if found is None:
        return {
            "error": f"unknown repository {repo!r}",
            "repositories": [r.slug for r in repos.all_repos()],
        }
    path = (path or "").strip().lstrip("/")
    if not path or ".." in path.split("/"):
        return {"error": "give a path inside the repository"}

    ref = (ref or "").strip() or found.live_ref
    connector = _connector_for(found)
    if found.host == "bitbucket":
        result = connector.read_file(found.slug, path, ref=ref)
    else:
        result = connector.read_file(
            found.slug, path, ref=ref, owner=found.owner,
        )
    if not result.ok:
        return {
            "repo": found.slug, "path": path, "branch": ref,
            "error": result.error[:300],
        }

    data = result.data
    # A directory: Bitbucket pages {"values": [...]}, GitHub returns a list.
    entries = None
    if isinstance(data, list):
        entries = [
            {"path": e.get("path"), "type": e.get("type")} for e in data
        ]
    elif isinstance(data, dict) and "values" in data:
        entries = [
            {"path": e.get("path"), "type": e.get("type")}
            for e in data.get("values", [])
        ]
    if entries is not None:
        return {
            "repo": found.slug, "path": path, "branch": ref,
            "directory": entries[:200], "how_to_use_this": INTERNAL,
        }

    if isinstance(data, dict) and data.get("encoding") == "base64":
        text = base64.b64decode(data.get("content") or "").decode(
            "utf-8", errors="replace"
        )
    elif isinstance(data, str):
        text = data
    elif isinstance(data, dict) and data.get("encoding") == "none":
        return {"repo": found.slug, "path": path, "branch": ref,
                "error": "file is over 1MB, too large for GitHub's contents "
                         "API; search_code for the part you need instead"}
    else:
        return {"repo": found.slug, "path": path, "branch": ref,
                "error": "not a text file"}

    lines = text.splitlines()
    total = len(lines)
    if start_line or end_line:
        first = max(int(start_line or 1), 1)
        last = min(int(end_line or total), total)
        lines = lines[first - 1:last]
        offset = first
    else:
        offset = 1
    numbered = "\n".join(
        f"{offset + i}: {line}" for i, line in enumerate(lines)
    )
    from vomeos.integrations.code_search import MAX_FILE_BYTES

    truncated = len(numbered) > MAX_FILE_BYTES
    return {
        "repo": found.slug,
        "path": path,
        "branch": ref,
        "is_live_branch": ref == found.live_ref,
        "total_lines": total,
        "content": numbered[:MAX_FILE_BYTES],
        "truncated": truncated,
        "how_to_use_this": (
            ("Truncated: request a line range with start_line and end_line. "
             if truncated else "") + INTERNAL
        ),
    }


def list_repositories() -> dict:
    permitted = set(config.code_repos())
    return {
        "repositories": [
            {"name": r.slug, "what": r.what, "live_branch": r.live_ref,
             "why_this_branch": r.evidence, "host": r.host,
             "readable": r.slug in permitted}
            for r in repos.all_repos()
        ],
        "aliases": repos.ALIASES,
    }


# ---------------------------------------------------------------------------
# feature requests
# ---------------------------------------------------------------------------

def log_feature_request(
    *,
    title: str,
    problem: str,
    requested_by: str,
    logged_by: str,
    ticket_url: str = "",
    platform: str = "",
    module: str = "",
    workaround: str = "",
    confirm_new: bool = False,
) -> dict:
    """Dedupe first. Create only when nothing plausible exists, or when the
    caller has looked at the candidates and confirmed this is new."""
    try:
        candidates = feature_requests.find_duplicates(title, problem)
    except Exception as exc:
        raise feature_requests.RequestError(
            f"could not check ClickUp for an existing request ({exc}); "
            "nothing was created"
        ) from exc

    if candidates and not confirm_new:
        return {
            "created": False,
            "possible_duplicates": [
                {
                    "task_id": c["id"], "name": c["name"], "list": c["list"],
                    "status": c["status"], "shipped": c["shipped"],
                    "url": c["url"],
                }
                for c in candidates
            ],
            "next_step": (
                "Nothing was created. If one of these is the same request, "
                "call add_request_to_existing with its task_id. If none is, "
                "call log_feature_request again with confirm_new=true."
            ),
        }

    task = feature_requests.create_request(
        title=title, problem=problem, requested_by=requested_by,
        logged_by=logged_by, ticket_url=ticket_url, platform=platform,
        module=module, workaround=workaround,
    )
    return {
        "created": True,
        "task": task,
        "triage": (
            "Untriaged, no priority. Sam decides priority and placement "
            "with triage_feature_request."
        ),
        "what_the_customer_may_be_told": (
            "That the request has been logged with the product team. Not "
            "that it is planned, scheduled or coming."
        ),
    }


def add_request_to_existing(
    *, task_id: str, requested_by: str, logged_by: str, note: str = "",
    ticket_url: str = "",
) -> dict:
    task = feature_requests.add_plus_one(
        task_id=task_id, requested_by=requested_by, logged_by=logged_by,
        note=note, ticket_url=ticket_url,
    )
    return {
        "added": True,
        "task": task,
        "what_the_customer_may_be_told": (
            "That their request has been added to an existing one with the "
            "product team. If status is a shipped state, check with Sam "
            "before saying it is available."
        ),
    }


def triage_feature_request(
    *, task_id: str, decision: str, priority: str, assign_to_me: bool,
    note: str, caller: str,
) -> dict:
    if not config.can_triage(caller):
        raise feature_requests.RequestError(
            f"{caller} cannot triage feature requests. Ask Sam."
        )
    assignee = config.clickup_user_for(caller) if assign_to_me else None
    if assign_to_me and assignee is None:
        raise feature_requests.RequestError(
            f"no ClickUp user mapped for {caller}; set MCP_CLICKUP_USERS"
        )
    return feature_requests.triage(
        task_id=task_id, decision=decision, priority=priority,
        assign_to=assignee, note=note, triaged_by=caller,
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def connector_status(caller: str) -> dict:
    from vomeos.integrations.code_search import describe as code_describe

    from mcp_connector import store

    return {
        "signed_in_as": caller,
        "can_triage_feature_requests": config.can_triage(caller),
        "help_centre": (
            "available" if _db_usable() else "unavailable (no database)"
        ),
        "frontend_source": product_source.status(),
        "clickup": "configured" if config.clickup_token() else "not set",
        "lessons_log": (
            "configured" if config.clickup_token() and config.clickup_team_id()
            else "not configured (CLICKUP_TEAM_ID missing)"
        ),
        "code": code_describe(),
        "sign_ins_survive_restart": store.durable(),
    }
