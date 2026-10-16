"""
mcp_connector/zoho_kb.py

The Zoho Desk knowledge base, through the Desk MCP endpoint.

WHY THIS ENDPOINT
-----------------
It is the only route that can write articles. The claude.ai Zoho Desk
connector exposes tickets and contacts with no KB tools at all, and the REST
token in `zoho_desk_api.py` returns 403 SCOPE_MISMATCH on /articles. The
endpoint in ZOHO_DESK_MCP_URL (already set on Railway, already used by the
ticket pipeline) exposes all 50 KB tools.

EVERY TRAP FROM THE FAQ SPEC LIVES HERE, ONCE
---------------------------------------------
`vome-react/instructions/vome-faq-project-instructions.md` records what has
bitten. This module is where each rule is enforced by code rather than
remembered:

  * orgId on every call, injected, never forgotten.
  * The payload key is `body`. `request_body` is accepted and silently
    dropped, after which Zoho blames `categoryId`.
  * Path variable names differ per tool (`id` vs `articleId`), and `locale`
    is a PATH variable. Inside an update body it silently discards the write.
  * Never call checkArticlePermalinkAvailability on a slug we mean to use:
    it reserves the slug and the article gets a date suffix.
  * A tool error returns `isError` with Zoho's message. That message is
    raised, not swallowed, because "None" told the last caller nothing.

The URL is a bearer credential (the path authenticates). It is read from the
environment and never logged.
"""

from __future__ import annotations

import json
import os
import threading
import time

import httpx

DEFAULT_ORG = "736165782"
# Vome Support Team. Set on the article and the translation, always.
DEFAULT_AUTHOR = "569440000000192003"
# "FAQs & Knowledge Base". Its two children are For administrators and
# For volunteers/users.
ROOT_CATEGORY = "569440000000295175"

_TREE_TTL = 900
_tree_cache: tuple[float, list[dict]] | None = None
_tree_lock = threading.Lock()


class ZohoKBError(RuntimeError):
    """Zoho refused or did not answer. The message is safe to show."""


def org_id() -> str:
    return os.environ.get("ZOHO_ORG_ID", "").strip() or DEFAULT_ORG


def author_id() -> str:
    return os.environ.get("MCP_KB_AUTHOR_ID", "").strip() or DEFAULT_AUTHOR


def configured() -> bool:
    return bool(os.environ.get("ZOHO_DESK_MCP_URL", "").strip())


def _parse(response: httpx.Response) -> dict:
    """JSON-RPC envelope from a JSON or an SSE reply."""
    text = response.content.decode("utf-8")
    if "text/event-stream" in response.headers.get("content-type", ""):
        data = [
            line[5:].strip() for line in text.splitlines()
            if line.startswith("data:")
        ]
        if not data:
            raise ZohoKBError("empty event stream from the Desk endpoint")
        text = data[-1]
    return json.loads(text)


def call(
    tool: str,
    *,
    path: dict | None = None,
    query: dict | None = None,
    body: dict | None = None,
    attempts: int = 3,
) -> dict | list:
    """One Desk tool call. Returns the decoded payload or raises."""
    url = os.environ.get("ZOHO_DESK_MCP_URL", "").strip()
    if not url:
        raise ZohoKBError("ZOHO_DESK_MCP_URL is not set on the server")

    arguments: dict = {"query_params": {"orgId": org_id(), **(query or {})}}
    if path:
        arguments["path_variables"] = {k: str(v) for k, v in path.items()}
    if body is not None:
        if "locale" in body and tool == "ZohoDesk_updateArticleTranslation":
            # Silently discards the whole write. See the module docstring.
            raise ZohoKBError("locale must not be in an update body")
        arguments["body"] = body

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.post(
                url, json=payload, timeout=60,
                headers={"Accept": "application/json, text/event-stream"},
            )
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}"
        else:
            if response.status_code == 200:
                envelope = _parse(response)
                if "error" in envelope:
                    raise ZohoKBError(f"{tool}: {envelope['error']}")
                result = envelope.get("result") or {}
                text = "".join(
                    block.get("text", "")
                    for block in result.get("content") or []
                    if block.get("type") == "text"
                )
                if result.get("isError"):
                    raise ZohoKBError(f"{tool}: {text[:500]}")
                try:
                    return json.loads(text) if text else {}
                except ValueError:
                    return {"text": text}
            last = f"HTTP {response.status_code}"
            if response.status_code < 500 and response.status_code != 429:
                break
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    raise ZohoKBError(f"{tool}: Desk endpoint did not answer ({last})")


# -- reads -------------------------------------------------------------------

def get_article(article_id: str) -> dict:
    return call("ZohoDesk_getArticle", path={"id": article_id})


def get_translation(article_id: str, locale: str) -> dict:
    # `id` is the ARTICLE id here, paired with locale, both path variables.
    return call(
        "ZohoDesk_getArticleTranslation",
        path={"id": article_id, "locale": locale},
    )


def list_translations(article_id: str) -> list[dict]:
    result = call(
        "ZohoDesk_getArticleTranslations",
        path={"articleId": article_id}, query={"limit": 50},
    )
    return result.get("data", []) if isinstance(result, dict) else []


def _flatten(node: dict, trail: list[str], out: list[dict]) -> None:
    names = {
        t.get("locale"): t.get("name")
        for t in node.get("translations") or []
    }
    name = names.get("en") or node.get("name") or ""
    children = node.get("children") or node.get("child") or []
    path = trail + [name]
    out.append({
        "id": str(node.get("id")),
        "name": name,
        "name_fr": names.get("fr") or "",
        "path": " > ".join(path[1:]) or name,
        "children": len(children),
        "is_leaf": not children,
        "depth": len(trail),
    })
    for child in children:
        _flatten(child, path, out)


def category_tree(force: bool = False) -> list[dict]:
    """Every category under the root, flattened, cached 15 minutes.

    Read live because the map in the FAQ spec has gone stale five times as
    sections were reorganised into children, and a parent with children
    rejects new articles.
    """
    global _tree_cache
    with _tree_lock:
        if (not force and _tree_cache
                and time.monotonic() - _tree_cache[0] < _TREE_TTL):
            return _tree_cache[1]
    tree = call(
        "ZohoDesk_getKBRootCategoryTree",
        path={"rootCategoryId": ROOT_CATEGORY},
    )
    flat: list[dict] = []
    if isinstance(tree, dict):
        _flatten(tree, [], flat)
    with _tree_lock:
        _tree_cache = (time.monotonic(), flat)
    return flat


def category(category_id: str, force: bool = False) -> dict | None:
    for node in category_tree(force=force):
        if node["id"] == str(category_id):
            return node
    return None


def leaf_categories() -> list[dict]:
    return [c for c in category_tree() if c["is_leaf"] and c["depth"] >= 2]


# -- writes ------------------------------------------------------------------

def _common(status: str) -> dict:
    return {
        "status": status,
        "permission": "ALL",
        "authorId": author_id(),
    }


def _seo(fields: dict) -> dict:
    # Plain text only. Entities here are rejected with INVALID_DATA.
    out = {}
    if fields.get("seo_title"):
        out["seoTitle"] = fields["seo_title"]
    if fields.get("seo_description"):
        out["seoDescription"] = fields["seo_description"]
    return out


def create_article(
    *, category_id: str, en: dict, status: str = "Published",
) -> dict:
    """The English base article. The permalink is passed straight in, never
    pre-checked."""
    return call("ZohoDesk_createArticle", body={
        "title": en["title"],
        "answer": en["answer_html"],
        "categoryId": str(category_id),
        "permalink": en["permalink"],
        **_common(status),
        **_seo(en),
    })


def create_translation(
    *, article_id: str, locale: str, fields: dict, status: str = "Published",
) -> dict:
    """A translation attached to the article. Never a second article."""
    return call(
        "ZohoDesk_createArticleTranslation",
        path={"articleId": article_id},
        body={
            "title": fields["title"],
            "answer": fields["answer_html"],
            "locale": locale,
            "permalink": fields["permalink"],
            **_common(status),
            **_seo(fields),
        },
    )


def update_article(
    *, article_id: str, en: dict, status: str = "Published",
) -> dict:
    return call(
        "ZohoDesk_updateArticle",
        path={"id": article_id},
        body={
            "title": en["title"],
            "answer": en["answer_html"],
            **_common(status),
            **_seo(en),
        },
    )


def update_translation(
    *, article_id: str, locale: str, fields: dict, status: str = "Published",
) -> dict:
    # locale goes in the PATH. In the body it silently voids the write.
    return call(
        "ZohoDesk_updateArticleTranslation",
        path={"articleId": article_id, "locale": locale},
        body={
            "title": fields["title"],
            "answer": fields["answer_html"],
            **_common(status),
            **_seo(fields),
        },
    )
