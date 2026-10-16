"""
mcp_connector/kb_rules.py

The FAQ spec's rules, as a check every draft must pass before it publishes.

Errors block publishing. Warnings are shown to the person reviewing and do
not block, because each one has a legitimate exception (the "Volunteer
Database" is a real product name; "volunteer" in it is not a terminology
slip).

Every rule cites where it came from, so a rule nobody can explain can be
removed rather than obeyed forever. Source: `vome-react/instructions/
vome-faq-project-instructions.md` (the spec), sections as noted.
"""

from __future__ import annotations

import re

SUPPORT_EMAIL = "support@vomevolunteer.zohodesk.com"

# Built from the code point so this file holds no em dash itself.
EM_DASH = chr(0x2014)
_EM_DASH = re.compile(EM_DASH + "|&mdash;|&#8212;|&#x2014;", re.I)
_ENTITY = re.compile(r"&(?:[a-z]+|#\d+|#x[0-9a-f]+);", re.I)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BANNER = "KB_New_Editor_Highlights"
_BANNER_IN_P = re.compile(
    r"<p\b[^>]*>(?:(?!</p>).)*?" + _BANNER, re.I | re.S,
)
_ADJACENT_BANNERS = re.compile(
    r"</span>\s*<span\b[^>]*" + _BANNER, re.I | re.S,
)
_MARKDOWN = re.compile(r"(^|\n)\s*(#{1,6}\s|\*\*[^*]+\*\*|- \S)")
_TAG = re.compile(r"<[^>]+>")

# Spec section 8. (pattern, why) pairs; matched on visible text only.
_EN_TERMS = (
    (r"\bopportunity roles?\b", "use Opportunity, not Opportunity Role"),
    (r"\bvolunteers?\b", "use User or profile, not volunteer, unless it "
     "is part of a product name"),
)
_FR_TERMS = (
    (r"\bb[ée]n[ée]voles?\b", "utilisateur ou profil, pas bénévole"),
    (r"\battribu(?:er|é|ée|és|ées|tion|tions|ez|ons)\b",
     "affecter, pas attribuer"),
    (r"\b[ée]tiquettes? de profil\b", "tag de profil, pas étiquette"),
    (r"\boccasions?\b", "opportunité, pas occasion"),
    (r"\bd[ée]pistage\b", "sélection, pas dépistage"),
    (r"\bpr[ée]s[ée]lection\b", "sélection, pas présélection"),
    (r"\bpeupler\b", "pas peupler"),
    (r"\bbillets?\b", "ticket, pas billet"),
    (r"\bMaj\b", "quart, pas Maj"),
)


def visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html or "")).strip()


def check_locale(
    locale: str, fields: dict, new_permalink: bool = True,
) -> tuple[list[str], list[str]]:
    """`new_permalink` False for an update: the live permalink is kept and
    never sent, and some live ones carry accents (les-séries-de-quarts-sur-
    vome), so holding them to the slug rule would block every update."""
    errors: list[str] = []
    warnings: list[str] = []
    label = locale.upper()
    title = (fields or {}).get("title") or ""
    answer = (fields or {}).get("answer_html") or ""
    permalink = (fields or {}).get("permalink") or ""

    if not title.strip():
        errors.append(f"{label}: title is empty")
    if not answer.strip():
        errors.append(f"{label}: body is empty")
        return errors, warnings
    if new_permalink and not _SLUG.match(permalink):
        errors.append(
            f"{label}: permalink {permalink!r} must be lowercase words "
            "joined by hyphens"
        )

    # Spec section 9: no em dashes anywhere, either language.
    for name, value in (
        ("title", title), ("body", answer),
        ("SEO title", fields.get("seo_title") or ""),
        ("SEO description", fields.get("seo_description") or ""),
    ):
        if _EM_DASH.search(value):
            errors.append(f"{label}: em dash in the {name}")

    # Titles and SEO fields are plain text; entities render raw (FR titles)
    # or are rejected outright (SEO). Spec section 3, 2026-10-13.
    for name in ("title", "seo_title", "seo_description"):
        if _ENTITY.search(fields.get(name) or ""):
            errors.append(
                f"{label}: HTML entity in the {name.replace('_', ' ')}; "
                "use literal characters"
            )

    if "�" in answer or "&#65533;" in answer or "�" in title:
        errors.append(f"{label}: contains the replacement character (�)")

    # Bodies are raw HTML, never Markdown. Spec section 4 of /faq.
    if _MARKDOWN.search(answer) and "<" not in answer[:200]:
        errors.append(f"{label}: body looks like Markdown, not HTML")

    # A banner inside <p> renders as an empty box with the text below it.
    if _BANNER_IN_P.search(answer):
        errors.append(
            f"{label}: a banner sits inside a <p>; emit the <span> at top "
            "level"
        )
    banners = answer.count(_BANNER)
    if banners > 3:
        warnings.append(f"{label}: {banners} banners; two or three is plenty")
    if _ADJACENT_BANNERS.search(answer):
        warnings.append(f"{label}: two banners in a row")

    lowered = answer.lower()
    if "support@vomevolunteer.com" in lowered:
        errors.append(
            f"{label}: support@vomevolunteer.com does not reach the desk; "
            f"use {SUPPORT_EMAIL}"
        )
    if SUPPORT_EMAIL in lowered and "/newticket" not in lowered:
        errors.append(
            f"{label}: offers the support email without the submit-a-ticket "
            "link first"
        )
    if locale == "fr":
        if "/portal/en/" in lowered:
            errors.append("FR: links to an English portal URL")
    if re.search(r"french version|version (?:anglaise|française)",
                 visible_text(answer), re.I):
        errors.append(
            f"{label}: manual cross-language reference; the portal language "
            "switcher handles this"
        )

    text = visible_text(answer) + " " + title
    for pattern, why in (_FR_TERMS if locale == "fr" else _EN_TERMS):
        hits = re.findall(pattern, text, re.I)
        if hits:
            warnings.append(f"{label}: {why} ({len(hits)}x)")
    return errors, warnings


def check_draft(draft: dict) -> dict:
    """{errors, warnings} for a whole draft, both locales."""
    errors: list[str] = []
    warnings: list[str] = []
    for locale in ("en", "fr"):
        if not isinstance(draft.get(locale), dict):
            errors.append(f"{locale.upper()}: missing")
            continue
        e, w = check_locale(
            locale, draft[locale],
            new_permalink=draft.get("action") != "update",
        )
        errors += e
        warnings += w
    if draft.get("action") == "create" and not draft.get("category_id"):
        errors.append("no category chosen")
    if draft.get("action") == "update" and not draft.get("article_id"):
        errors.append("an update needs the article id")
    en_banners = (draft.get("en") or {}).get("answer_html", "").count(_BANNER)
    fr_banners = (draft.get("fr") or {}).get("answer_html", "").count(_BANNER)
    if en_banners != fr_banners:
        errors.append(
            f"EN has {en_banners} banners and FR has {fr_banners}; a banner "
            "added to one language goes in the other in the same pass"
        )
    return {"errors": errors, "warnings": warnings}
