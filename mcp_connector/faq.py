"""
mcp_connector/faq.py

`/faq` as a hand-off: a person describes the article, the kb_writer agent
drafts it in English and French, a person reviews, and publishing is code.

THE THREE STEPS, AND WHO OWNS EACH
----------------------------------
1. run_faq          Starts a job. Gathers the facts deterministically (the
                    existing article, related articles, UI labels in both
                    languages, help centre and lessons, the live category
                    tree), then hands them to `knowledge.kb_writer`. Runs in
                    the background because two full articles take minutes,
                    longer than a tool call should hold a connection open.
2. faq_job_status   The draft, the rule check, the agent's questions.
3. publish_faq_draft Deterministic. Re-checks the rules, writes English then
                    French, reads both back from Zoho, repairs status or
                    permission once if Zoho dropped them, refreshes the help
                    centre index, and reports the portal URLs Zoho returned.

The agent never touches Zoho. Code that cannot be argued with does the
writes, which is the same split as everywhere else in Vome OS.

A FAILED PUBLISH NEVER DUPLICATES
---------------------------------
If English is created and French then fails, the job is rewritten as an
update of the article that now exists, so pressing publish again finishes
the job instead of creating a second article.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from urllib.parse import urlparse

from mcp_connector import kb_rules, store, zoho_kb
from mcp_connector.feature_requests import RequestError

AGENT = "knowledge.kb_writer"
STALE_AFTER = 900  # seconds a job may sit in a working state

WORKING = ("queued", "gathering", "drafting")


# ---------------------------------------------------------------------------
# finding articles
# ---------------------------------------------------------------------------

def resolve_article(ref: str) -> str:
    """An article id from an id, a portal URL or a permalink."""
    ref = (ref or "").strip()
    if not ref:
        raise RequestError("give an article id, portal URL or permalink")
    if ref.isdigit():
        return ref
    permalink = ref
    if "://" in ref:
        parts = [p for p in urlparse(ref).path.split("/") if p]
        if "articles" not in parts or parts[-1] == "articles":
            raise RequestError(f"{ref} is not a help centre article URL")
        permalink = parts[-1]
    import database

    row = None
    if database._database_url_is_usable(database.DATABASE_URL):
        row = database.get_kb_article_by_permalink(permalink)
    if not row:
        raise RequestError(
            f"no article with permalink {permalink!r} in the help centre "
            "index; pass the numeric article id instead"
        )
    return str(row["zoho_article_id"]).split(":")[0]


def read_article(ref: str) -> dict:
    """Both languages of one article, as Zoho holds them now."""
    article_id = resolve_article(ref)
    en = zoho_kb.get_article(article_id)
    node = zoho_kb.category(str(en.get("categoryId") or ""))
    locales = {
        (t.get("locale") or "").lower()
        for t in zoho_kb.list_translations(article_id)
    }
    fr = zoho_kb.get_translation(article_id, "fr") if "fr" in locales else None

    def view(record: dict | None) -> dict | None:
        if not record:
            return None
        return {
            "title": record.get("title"),
            "permalink": record.get("permalink"),
            "portal_url": record.get("portalUrl"),
            "status": record.get("status"),
            "permission": record.get("permission"),
            "answer_html": record.get("answer") or "",
        }

    return {
        "article_id": article_id,
        "category_id": str(en.get("categoryId") or ""),
        "category": node["path"] if node else None,
        "en": view(en),
        "fr": view(fr),
        "has_french": fr is not None,
    }


def list_categories() -> list[dict]:
    return [
        {"category_id": c["id"], "path": c["path"], "french": c["name_fr"]}
        for c in zoho_kb.leaf_categories()
    ]


# ---------------------------------------------------------------------------
# what the agent is given
# ---------------------------------------------------------------------------

def queries(request: dict) -> list[str]:
    """Short search queries: the request, then each sentence of the notes.

    The UI string matcher scores a string by the share of the QUERY's words
    it contains, so one 400-character query (request plus notes) needs a
    third of forty words in a single label and matches nothing. That is how
    the first run reported "no matching UI strings" for a feature whose
    "Admin Login with SSO" button is plainly in the product.
    """
    out = [(request.get("request") or "").strip()]
    notes = request.get("developer_notes") or ""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", notes):
        words = sentence.split()
        if len(words) >= 3:
            out.append(" ".join(words[:16]))
    return [q for q in out if q][:10]


def _label_pairs(searches: list[str], limit: int = 40) -> str:
    """English and French UI strings for the topic, side by side.

    The spec's rule: French labels are looked up in the product, never
    translated. product_strings only parses English, so the French side is
    read here from the `fr:` block of the same files.
    """
    import product_strings
    from mcp_connector import product_source

    product_source.ensure_fresh()
    if not product_strings.available():
        return "(UI strings unavailable: frontend source not loaded)"
    hits: list[dict] = []
    seen: set[str] = set()
    for query in searches:
        for hit in product_strings.search_strings(query, limit=8):
            if hit["key"] not in seen:
                seen.add(hit["key"])
                hits.append(hit)
    hits = hits[:limit]
    if not hits:
        return "(no matching UI strings)"
    french = _french_strings()
    lines = []
    for hit in hits:
        fr = french.get(hit["key"], "")
        lines.append(
            f"- EN: {hit['text']}\n  FR: {fr or '(no French string)'}"
        )
    return "\n".join(lines)


_french_cache: tuple[str, dict[str, str]] | None = None


def _french_strings() -> dict[str, str]:
    global _french_cache
    import os

    import product_strings

    marker = os.environ.get("VOME_REACT_PATH", "")
    if _french_cache and _french_cache[0] == marker:
        return _french_cache[1]
    out: dict[str, str] = {}
    for surface, path in product_strings._sources():
        if surface != "web":
            continue
        in_french = False
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if product_strings._FR_BLOCK.match(line):
                        in_french = True
                        continue
                    if not in_french:
                        continue
                    match = product_strings._ENTRY.match(line)
                    if match:
                        value = match.group(2)
                        if value is None:
                            value = match.group(3)
                        out.setdefault(match.group(1), value)
        except OSError:
            continue
    _french_cache = (marker, out)
    return out


def _related(topic: str) -> str:
    import database

    if not database._database_url_is_usable(database.DATABASE_URL):
        return "(help centre index unavailable: NOT searched)"
    rows = database.search_kb_articles_db(
        topic, language="en", limit=6, body_chars=700,
    )
    if not rows:
        return "(no related articles found; search ran)"
    blocks = []
    for row in rows:
        blocks.append(
            f"- article_id {str(row.get('id') or '').split(':')[0]}: "
            f"{row['title']}\n  {row['url']}\n  {row.get('body', '')}"
        )
    return "\n".join(blocks)


def _facts(topic: str) -> str:
    """Help centre steps, Setup Guide, in-app explanations and caveats.

    `topic` must be short (the request), for the same reason as queries().
    """
    from mcp_connector import tools

    found = tools.lookup_feature(topic)
    lines = []
    for hit in found.get("setup_guide_and_catalog", []):
        lines.append(f"[{hit['source']}: {hit['section']}] {hit['text']}")
    for hit in found.get("ui_text", []):
        if hit["kind"] == "explanation":
            lines.append(f"[in-app explanation] {hit['text']}")
    for caveat in found.get("caveats", []):
        lines.append(f"[{caveat['source']}: {caveat['about']}] "
                     f"{caveat['text']}")
    screens = tools.where_in_app(topic).get("pages", [])[:2]
    for screen in screens:
        labels = "; ".join(
            t["text"] for t in screen["text_on_this_screen"][:10]
        )
        lines.append(
            f"[screen: {screen['area'] or 'app'}] labels: {labels}"
        )
    lines.append("Sources: " + json.dumps(found.get("sources", {})))
    return "\n".join(lines)[:9000]


def _categories_block() -> str:
    return "\n".join(
        f"{c['category_id']} | {c['path']}" for c in list_categories()
    )


def build_context(request: dict) -> dict:
    searches = queries(request)
    # Full-text search ORs terms with a minimum match, so a longer query is
    # fine for related articles; the token matchers get the short one.
    topic = " ".join(
        x for x in (request.get("request"), request.get("developer_notes"))
        if x
    )[:400]
    short = searches[0] if searches else topic
    existing = "(none: decide from related_articles)"
    if request.get("article"):
        article = read_article(request["article"])
        existing = json.dumps(article, ensure_ascii=False)

    revision = "(none)"
    if request.get("previous_draft"):
        revision = json.dumps({
            "previous_draft": request["previous_draft"],
            "feedback": request.get("feedback") or "",
        }, ensure_ascii=False)

    return {
        "request": request.get("request") or "",
        "developer_notes": request.get("developer_notes") or "(none)",
        "existing_article": existing,
        "related_articles": _related(topic),
        "product_facts": _facts(short),
        "ui_labels": _label_pairs(searches),
        "categories": _categories_block(),
        "revision": revision,
    }


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def start(request: dict, caller: str) -> str:
    if not (request.get("request") or "").strip():
        raise RequestError("say what the article should cover")
    if not zoho_kb.configured():
        raise RequestError("ZOHO_DESK_MCP_URL is not set on the server")
    job_id = secrets.token_hex(6)
    store.create_faq_job(job_id, caller, request)
    threading.Thread(
        target=_run, args=(job_id,), name=f"faq-{job_id}", daemon=True,
    ).start()
    return job_id


def _run(job_id: str) -> None:
    from vomeos.runner import run_agent

    job = store.get_faq_job(job_id)
    request = job["request"]
    try:
        store.update_faq_job(job_id, status="gathering")
        context = build_context(request)
        store.update_faq_job(job_id, status="drafting")
        result = run_agent(
            AGENT, context, subject_type="faq_job", subject_id=job_id,
        )
    except Exception as exc:
        store.update_faq_job(
            job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
        )
        return

    if result.status == "error" or not result.data:
        store.update_faq_job(
            job_id, status="failed", run_id=result.run_id,
            error=result.error or result.why() or "no draft returned",
        )
        return

    draft = normalise(result.data, request)
    if draft.get("action") == "needs_input":
        store.update_faq_job(
            job_id, status="needs_input", draft=draft, run_id=result.run_id,
        )
        return
    lint = full_check(draft)
    store.update_faq_job(
        job_id, status="ready" if not lint["errors"] else "needs_revision",
        draft=draft, lint=lint, run_id=result.run_id,
    )


def normalise(data: dict, request: dict) -> dict:
    """Pin what must not drift on an update: the id and the permalinks."""
    draft = dict(data)
    if request.get("article") and draft.get("action") != "needs_input":
        draft["action"] = "update"
        draft["article_id"] = resolve_article(request["article"])
    if draft.get("action") == "update" and draft.get("article_id"):
        try:
            current = read_article(draft["article_id"])
        except Exception:
            current = None
        if current:
            for locale in ("en", "fr"):
                live = current.get(locale)
                if live and isinstance(draft.get(locale), dict):
                    draft[locale]["permalink"] = live["permalink"]
            draft["category_id"] = current["category_id"]
    return draft


def full_check(draft: dict) -> dict:
    lint = kb_rules.check_draft(draft)
    if draft.get("action") == "create" and draft.get("category_id"):
        try:
            node = zoho_kb.category(draft["category_id"])
        except zoho_kb.ZohoKBError as exc:
            lint["warnings"].append(f"could not check the category: {exc}")
        else:
            if node is None:
                lint["errors"].append(
                    f"category {draft['category_id']} does not exist"
                )
            elif not node["is_leaf"]:
                lint["errors"].append(
                    f"category {node['path']} has child sections and will "
                    "reject new articles; choose one of its children"
                )
    return lint


def view(job: dict) -> dict:
    status = job["status"]
    if status in WORKING and time.time() - job["updated_at"] > STALE_AFTER:
        status = "interrupted"
    out = {
        "job_id": job["id"],
        "status": status,
        "requested_by": job["created_by"],
        "request": job["request"].get("request"),
    }
    if status in WORKING:
        out["next_step"] = (
            "Still working. Drafting two articles takes two to four "
            "minutes. Check again shortly."
        )
        return out
    if status == "interrupted":
        out["next_step"] = "The server restarted mid-job. Run it again."
        return out
    if job.get("error"):
        out["error"] = job["error"]
    draft = job.get("draft") or {}
    if draft:
        out.update({
            "action": draft.get("action"),
            "article_id": draft.get("article_id"),
            "category_id": draft.get("category_id"),
            "plan": draft.get("plan"),
            "questions": draft.get("questions") or [],
            "contradictions": draft.get("contradictions") or [],
            "reviewer_notes": draft.get("reviewer_notes"),
            "en": draft.get("en"),
            "fr": draft.get("fr"),
        })
        if draft.get("category_id"):
            try:
                node = zoho_kb.category(draft["category_id"])
                out["category"] = node["path"] if node else None
            except zoho_kb.ZohoKBError:
                pass
    if job.get("lint"):
        out["rule_check"] = job["lint"]
    if job.get("result"):
        out["published"] = job["result"]
    out["next_step"] = {
        "needs_input": "Answer the questions, then run_faq again with the "
                       "answers in developer_notes.",
        "ready": "Show the draft for review. To change it, run_faq with "
                 "revise_job_id and feedback, or save_faq_draft with edits. "
                 "To publish, publish_faq_draft.",
        "needs_revision": "The rule check failed. Fix with run_faq "
                          "(revise_job_id + feedback) or save_faq_draft.",
        "failed": "Drafting failed. Run it again.",
        "publish_failed": "Publishing failed and nothing duplicated. Fix "
                          "the cause, then publish_faq_draft again.",
        "published": "Done. Report the portal URLs exactly as shown.",
        "published_with_issues": "Live, but the read-back found problems. "
                                 "Fix them with save_faq_draft and publish.",
        "saved_as_draft": "Saved in Zoho as a Draft, not public. Publish "
                          "again without as_draft when ready.",
    }.get(status, "")
    return out


def revise(job_id: str, feedback: str, caller: str) -> str:
    job = store.get_faq_job(job_id)
    if not job:
        raise RequestError(f"no FAQ job {job_id}")
    if not job.get("draft"):
        raise RequestError("that job has no draft to revise")
    request = dict(job["request"])
    request["previous_draft"] = job["draft"]
    request["feedback"] = feedback
    request["revises"] = job_id
    draft = job["draft"]
    if draft.get("action") == "update" and draft.get("article_id"):
        request["article"] = draft["article_id"]
    return start(request, caller)


def save(fields: dict, caller: str) -> dict:
    """A draft written or edited by a person (or their Claude), put through
    the same rule check and the same publish path as an agent draft."""
    action = "update" if fields.get("article_id") else "create"
    draft = {
        "action": action,
        "article_id": (
            resolve_article(fields["article_id"])
            if fields.get("article_id") else None
        ),
        "category_id": fields.get("category_id") or None,
        "plan": fields.get("plan") or "unknown",
        "questions": [],
        "contradictions": [],
        "reviewer_notes": fields.get("note") or "",
        "en": fields["en"],
        "fr": fields["fr"],
    }
    draft = normalise(draft, {})
    lint = full_check(draft)
    job_id = secrets.token_hex(6)
    store.create_faq_job(
        job_id, caller,
        {"request": fields.get("note") or "saved draft",
         "saved_from": fields.get("based_on_job")},
        status="ready" if not lint["errors"] else "needs_revision",
        draft=draft, lint=lint,
    )
    return view(store.get_faq_job(job_id))


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------

def _verify(article_id: str, draft: dict, status: str) -> dict:
    en = zoho_kb.get_article(article_id)
    locales = {
        (t.get("locale") or "").lower(): t
        for t in zoho_kb.list_translations(article_id)
    }
    fr = zoho_kb.get_translation(article_id, "fr") if "fr" in locales else {}
    problems: list[str] = []
    fixable: dict[str, bool] = {"en": False, "fr": False}

    for locale, record in (("en", en), ("fr", fr)):
        label = locale.upper()
        if not record:
            problems.append(f"{label}: translation missing after publish")
            continue
        if record.get("status") != status:
            problems.append(f"{label}: status is {record.get('status')}")
            fixable[locale] = True
        if record.get("permission") != "ALL":
            problems.append(
                f"{label}: permission is {record.get('permission')}"
            )
            fixable[locale] = True
        stored = (record.get("answer") or "") + (record.get("title") or "")
        if "�" in stored or "&#65533;" in stored:
            problems.append(f"{label}: stored text has replacement characters")
            fixable[locale] = True
        sent = draft[locale]["answer_html"].count("KB_New_Editor_Highlights")
        kept = (record.get("answer") or "").count("KB_New_Editor_Highlights")
        if sent != kept:
            problems.append(f"{label}: sent {sent} banners, Zoho kept {kept}")
        if kb_rules._BANNER_IN_P.search(record.get("answer") or ""):
            problems.append(f"{label}: Zoho moved a banner inside a <p>")
        if status == "Published" and not record.get("portalUrl"):
            problems.append(f"{label}: no portalUrl returned")

    if (draft.get("action") == "create"
            and str(en.get("categoryId")) != str(draft.get("category_id"))):
        problems.append(
            f"category is {en.get('categoryId')}, expected "
            f"{draft.get('category_id')}"
        )

    def summary(record: dict) -> dict:
        return {
            "title": record.get("title"),
            "status": record.get("status"),
            "permission": record.get("permission"),
            "portal_url": record.get("portalUrl"),
        }

    return {
        "article_id": article_id,
        "en": summary(en) if en else None,
        "fr": summary(fr) if fr else None,
        "problems": problems,
        "fixable": fixable,
        "records": {"en": en, "fr": fr},
    }


def _write(article_id: str | None, draft: dict, status: str) -> str:
    """Write both languages. Returns the article id."""
    if draft["action"] == "create" and not article_id:
        created = zoho_kb.create_article(
            category_id=draft["category_id"], en=draft["en"], status=status,
        )
        article_id = str(created.get("id") or "")
        if not article_id:
            raise zoho_kb.ZohoKBError(
                "createArticle returned no id: " + json.dumps(created)[:300]
            )
    else:
        zoho_kb.update_article(
            article_id=article_id, en=draft["en"], status=status,
        )
    return article_id


def _write_french(article_id: str, draft: dict, status: str) -> None:
    locales = {
        (t.get("locale") or "").lower()
        for t in zoho_kb.list_translations(article_id)
    }
    if "fr" in locales:
        zoho_kb.update_translation(
            article_id=article_id, locale="fr", fields=draft["fr"],
            status=status,
        )
    else:
        zoho_kb.create_translation(
            article_id=article_id, locale="fr", fields=draft["fr"],
            status=status,
        )


def _refresh_index(verification: dict, category_name: str) -> bool:
    """Put the published article in the help centre index now, rather than
    after the nightly sync, so lookup_feature finds it today."""
    try:
        import database

        if not database._database_url_is_usable(database.DATABASE_URL):
            return False
        article_id = verification["article_id"]
        for locale, record in verification["records"].items():
            if not record or record.get("status") != "Published":
                continue
            database.upsert_kb_article({
                "id": article_id if locale == "en" else f"{article_id}:fr",
                "title": record.get("title") or "",
                "content": kb_rules.visible_text(record.get("answer") or ""),
                "url": record.get("portalUrl") or "",
                "permalink": record.get("permalink") or "",
                "category": category_name,
                "language": locale,
                "status": record.get("status") or "",
                "modifiedTime": record.get("modifiedTime") or "",
                "createdTime": record.get("createdTime") or "",
            })
        return True
    except Exception as exc:
        print(f"[FAQ] index refresh failed: {exc}")
        return False


def publish(job_id: str, caller: str, as_draft: bool = False) -> dict:
    job = store.get_faq_job(job_id)
    if not job:
        raise RequestError(f"no FAQ job {job_id}")
    if job["status"] not in ("ready", "publish_failed", "saved_as_draft"):
        raise RequestError(
            f"job {job_id} is {job['status']}; only a ready draft publishes"
        )
    draft = job["draft"]
    lint = full_check(draft)
    if lint["errors"]:
        store.update_faq_job(job_id, status="needs_revision", lint=lint)
        raise RequestError(
            "the rule check failed, nothing was published: "
            + "; ".join(lint["errors"])
        )
    if not store.update_faq_job(
        job_id, expect_status=job["status"], status="publishing",
    ):
        raise RequestError("someone else is publishing this job right now")

    status = "Draft" if as_draft else "Published"
    article_id = draft.get("article_id")
    try:
        article_id = _write(article_id, draft, status)
        if draft["action"] == "create":
            # From here on a retry must update, never create again.
            draft = {**draft, "action": "update", "article_id": article_id}
            store.update_faq_job(job_id, draft=draft)
        _write_french(article_id, draft, status)
        verification = _verify(article_id, draft, status)

        if verification["problems"] and any(
            verification["fixable"].values()
        ):
            # One repair pass. Zoho has dropped status and permission on
            # update before, and the replacement character is intermittent
            # transport corruption that a resend clears.
            if verification["fixable"]["en"]:
                zoho_kb.update_article(
                    article_id=article_id, en=draft["en"], status=status,
                )
            if verification["fixable"]["fr"]:
                zoho_kb.update_translation(
                    article_id=article_id, locale="fr", fields=draft["fr"],
                    status=status,
                )
            verification = _verify(article_id, draft, status)
    except zoho_kb.ZohoKBError as exc:
        store.update_faq_job(
            job_id, status="publish_failed", draft=draft, error=str(exc),
            result={"article_id": article_id} if article_id else None,
        )
        raise RequestError(
            f"Zoho refused the publish: {exc}"
            + (f" (article {article_id} exists; publishing again will "
               "update it, not duplicate it)" if article_id else "")
        ) from exc

    node = zoho_kb.category(
        str(verification["records"]["en"].get("categoryId") or "")
    )
    indexed = (
        not as_draft
        and _refresh_index(verification, node["path"] if node else "")
    )
    final = (
        "saved_as_draft" if as_draft
        else "published_with_issues" if verification["problems"]
        else "published"
    )
    result = {
        "article_id": article_id,
        "status": status,
        "category": node["path"] if node else None,
        "en": verification["en"],
        "fr": verification["fr"],
        "problems": verification["problems"],
        "indexed_for_search": indexed,
        "published_by": caller,
        "rule_warnings": lint["warnings"],
    }
    store.update_faq_job(job_id, status=final, result=result, error=None)
    return result
