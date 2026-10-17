"""
video_concepts.py

The deterministic half of the concept stage in the video studio channel.

Before a script is written, the strategist positions the video and the
creative director offers three concepts, each with its own angle, hook and
format. A person picks one by number. Everything in this module is pure:
no Slack, no database, no model, no network. That keeps the two rules that
matter here readable and testable on their own:

1. A pick is matched by pattern, never inferred. "2" and "go with option 2"
   are picks. "I like 2 but make it shorter" is not: it is direction, and
   the creative director comes back with new concepts. The same discipline
   as the greenlight, for the same reason. Treating praise as a choice
   commits the thread to a concept nobody chose.

2. A concept may only name a format Studio reports as ready. That is checked
   here, against Studio's own catalogue, rather than trusted to the model.
"""

from __future__ import annotations

import json
import re

PICK_LINE = ("Reply with 1, 2 or 3 to pick one. Anything else I will treat "
             "as direction and come back with new concepts.")

# What a Studio older than the format catalogue can build. Used only when
# Studio answers /v1/formats with a 404, which is that Studio saying it has
# no catalogue, not a guess about what it can do.
EXPLAINER_ONLY = {
    "name": "explainer", "title": "Explainer", "seconds": "40-50",
    "best_for": "a feature built on a rule, a setting or a workflow",
    "description": "Hook, the old way, the feature working on screen, the "
                   "outcome, close.",
    "ready": True,
}

_FORMAT_NAME = re.compile(r"^[a-z0-9_-]{1,40}$")

# ---------------------------------------------------------------------------
# The pick
# ---------------------------------------------------------------------------

_WORDS = {"1": 1, "2": 2, "3": 3, "one": 1, "two": 2, "three": 3,
          "first": 1, "second": 2, "third": 3, "1st": 1, "2nd": 2, "3rd": 3}

# The verbs that turn a number into an instruction: "go with 2", "pick 3".
_VERB = (r"(?:(?:let'?s|lets|we'?ll|i'?ll|i'?d|we'?d|we|i)\s+)?"
         r"(?:go\s+with|go\s+for|pick|choose|take|use|do|want|like\s+to\s+go"
         r"\s+with)")
# Not "no": "no 2" is as likely to be a refusal as "number 2".
_NOUN = r"(?:option|concept|number|idea)"
_NUM = r"(?:#\s*)?([123]|one|two|three)"
_ORD = (r"(?:the\s+)?(first|second|third|1st|2nd|3rd)"
        r"(?:\s+(?:one|option|concept|idea))?")

_PICK_PATTERNS = (
    # "2", "2.", "#2", "(2)", "one"
    re.compile(r"^\(?%s\)?$" % _NUM),
    # "option 3", "number 1", "concept #2"
    re.compile(r"^%s\s*%s$" % (_NOUN, _NUM)),
    # "go with 2", "let's go with option 3", "pick #1"
    re.compile(r"^%s\s+(?:%s\s*)?%s$" % (_VERB, _NOUN, _NUM)),
    # "the second one", "go with the third option"
    re.compile(r"^(?:%s\s+)?%s$" % (_VERB, _ORD)),
)

# Courtesy around a pick does not change it. Anything else does.
_LEAD = re.compile(r"^(?:ok(?:ay)?|yes|yep|sure|great)\s*[,.!]?\s+")
_TAIL = re.compile(r"[\s,.!]*(?:please|pls|thanks|thank\s+you|thx)?[\s.!]*$")


def match_concept_pick(text: str) -> int | None:
    """The concept number a message picks, or None if it is not a pick.

    Matched on the whole message, so a number inside a sentence is never a
    pick: "I like 2 but make it shorter" is direction, and so is "2 and 3
    are both good", "not 1", "maybe 2" and "2?". A question is never a pick.
    Slack mentions, a leading "ok" and a trailing "please" or "thanks" are
    ignored; nothing else is.
    """
    body = re.sub(r"<[^>]+>", " ", text or "")       # Slack mentions
    body = re.sub(r"\s+", " ", body).strip().lower()
    if not body or "?" in body:
        return None
    body = _LEAD.sub("", body)
    body = _TAIL.sub("", body).strip()
    if not body:
        return None
    for pattern in _PICK_PATTERNS:
        m = pattern.match(body)
        if m:
            return _WORDS.get(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Studio's catalogue
# ---------------------------------------------------------------------------

def parse_catalogue(data) -> list[dict]:
    """The format list from Studio's /v1/formats payload.

    Accepts the documented `{"formats": [...]}` or a bare list, and drops
    anything that is not a format with a usable name.
    """
    items = data.get("formats") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [f for f in items
            if isinstance(f, dict)
            and _FORMAT_NAME.match(str(f.get("name", "")))]


def ready_formats(catalogue: list[dict]) -> list[dict]:
    """Only the formats Studio can build today."""
    return [f for f in catalogue if f.get("ready") is True]


def format_title(name: str, catalogue: list[dict] | None = None) -> str:
    for f in catalogue or []:
        if f.get("name") == name and f.get("title"):
            return str(f["title"])
    return (name or "explainer").replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# Earlier videos
# ---------------------------------------------------------------------------

_TAG = re.compile(r"\[[^\]]*\]")


def _plain(text: str, cap: int = 120) -> str:
    text = _TAG.sub("", str(text or "")).replace("*", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= cap else text[:cap - 3].rstrip() + "..."


def _loads(raw, kind):
    if isinstance(raw, kind):
        return raw
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, kind) else None


def chosen_concept(row: dict) -> dict | None:
    """The concept a thread picked, or None if it has not, or predates them."""
    concepts = _loads(row.get("concepts_json"), list) or []
    n = row.get("chosen_concept")
    if not isinstance(n, int) or not 1 <= n <= len(concepts):
        return None
    c = concepts[n - 1]
    return c if isinstance(c, dict) else None


def load_concepts(row: dict) -> list[dict]:
    return [c for c in (_loads(row.get("concepts_json"), list) or [])
            if isinstance(c, dict)]


def load_positioning(row: dict) -> dict | None:
    return _loads(row.get("positioning_json"), dict)


def summarise_prior(rows: list[dict]) -> list[dict]:
    """One short record per earlier video: feature, format, angle, hook.

    The concept is the source when the thread has one. A video made before
    concepts existed still has a first line, and that is its hook.
    """
    out = []
    for row in rows or []:
        sb = _loads(row.get("storyboard_json"), dict) or {}
        concept = chosen_concept(row) or {}
        hook = concept.get("hook", "")
        if not hook:
            try:
                hook = sb["scenes"][0]["lines"][0]["vo"]
            except (KeyError, IndexError, TypeError):
                hook = ""
        out.append({
            "feature": _plain(row.get("feature_name")
                              or sb.get("feature_name") or sb.get("id"), 80),
            "format": _plain(concept.get("format") or sb.get("format")
                             or "explainer", 30),
            "title": _plain(concept.get("title", ""), 80),
            "angle": _plain(concept.get("angle", "")),
            "hook": _plain(hook),
        })
    return out


def prior_videos_text(summary: list[dict]) -> str:
    """The short text list the strategist and creative director read."""
    if not summary:
        return "(no earlier videos)"
    lines = []
    for v in summary:
        parts = [v["feature"] or "untitled", v["format"]]
        if v.get("title"):
            parts.append("concept: %s" % v["title"])
        if v.get("angle"):
            parts.append("angle: %s" % v["angle"])
        if v.get("hook"):
            parts.append("hook: %s" % v["hook"])
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Checking what the creative director returned
# ---------------------------------------------------------------------------

def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _blank(c) -> bool:
    """True for a concept with no content at all: every value empty."""
    if not isinstance(c, dict):
        return False
    return not any(
        (str(v).strip() if not isinstance(v, (list, int, float)) else
         ([x for x in v if str(x).strip()] if isinstance(v, list) else v))
        for v in c.values())


def check_concepts(data, ready_names: list[str],
                   prior_hooks: list[str] | None = None
                   ) -> tuple[list[dict], str]:
    """(concepts, "") if they can be posted, else ([], the reason).

    Deterministic backstops for the charter: exactly three, each complete,
    each in a ready format, no two with the same hook, more than one format
    when more than one is ready, and no hook lifted from an earlier video.
    """
    concepts = data.get("concepts") if isinstance(data, dict) else None
    # A wholly blank entry carries nothing, so dropping it loses nothing. The
    # first live run returned three good concepts and a fourth with every
    # field empty, and the whole post was refused over it.
    if isinstance(concepts, list):
        concepts = [c for c in concepts if not _blank(c)]
    if not isinstance(concepts, list) or len(concepts) != 3:
        return [], ("the creative director did not return exactly three "
                    "concepts")
    ready = set(ready_names)
    for i, c in enumerate(concepts, start=1):
        if not isinstance(c, dict):
            return [], "concept %d is not an object" % i
        missing = [k for k in ("title", "format", "hook") if not
                   str(c.get(k) or "").strip()]
        if missing:
            return [], "concept %d has no %s" % (i, ", ".join(missing))
        beats = c.get("beats")
        if not isinstance(beats, list) or not [b for b in beats
                                               if str(b).strip()]:
            return [], "concept %d has no beats" % i
        if c["format"] not in ready:
            return [], ("concept %d uses the %s format, which Vome Studio "
                        "cannot build yet" % (i, c["format"]))
    hooks = [_norm(c["hook"]) for c in concepts]
    if len(set(hooks)) != len(hooks):
        return [], "two concepts share a hook"
    # Plain hooks drifted into one formula: all three opened "Vome sends
    # email to your users, and...". Three concepts are three ways in.
    openings = [" ".join(h.split()[:4]) for h in hooks]
    if len(set(openings)) != len(openings):
        return [], ("two hooks open with the same words (%r); three "
                    "concepts need three different first lines"
                    % next(o for o in openings if openings.count(o) > 1))
    # Variety is the default, not a rule: a ready format that cannot show
    # the feature would make a broken concept. The first real run used one
    # format because the explainer cannot draw an email at all, and said so.
    # One format is allowed only with that reason given.
    if (len(ready) > 1 and len({c["format"] for c in concepts}) < 2
            and not str((data or {}).get("notes") or "").strip()):
        return [], ("all three concepts use one format although %d are "
                    "ready, and the notes do not say why" % len(ready))
    taken = {_norm(h) for h in prior_hooks or [] if _norm(h)}
    for i, h in enumerate(hooks, start=1):
        if h in taken:
            return [], "concept %d reuses the hook of an earlier video" % i
    return concepts, ""


# ---------------------------------------------------------------------------
# The post
# ---------------------------------------------------------------------------

def _esc(text) -> str:
    """Slack treats &, < and > as markup."""
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _seconds(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return "about %d seconds" % round(value)
    return "%s seconds" % _esc(value)


def concepts_post(concepts: list[dict], feature_name: str = "",
                  single_claim: str = "", notes: str = "",
                  catalogue: list[dict] | None = None,
                  direction: bool = False) -> str:
    """The Slack message that offers the three concepts."""
    if direction:
        head = "Three new concepts, taking your direction."
    elif feature_name:
        head = "Three ways to tell *%s*." % _esc(feature_name)
    else:
        head = "Three ways to tell this one."
    lines = [head]
    if single_claim:
        lines.append("Each one proves the same claim: _%s_"
                     % _esc(single_claim))
    for n, c in enumerate(concepts, start=1):
        lines.append("")
        lines.append("*%d. %s*" % (n, _esc(c.get("title"))))
        meta = [format_title(c.get("format", ""), catalogue)]
        if _seconds(c.get("seconds")):
            meta.append(_seconds(c.get("seconds")))
        if c.get("purpose"):
            # The line ends in its own period; a purpose that brings one
            # printed "reassurance.." in the first real post.
            meta.append("for %s" % _esc(str(c["purpose"]).rstrip(" .")))
        lines.append("_%s._" % ", ".join(meta))
        lines.append("Hook: \"%s\"" % _esc(c.get("hook")))
        for beat in c.get("beats") or []:
            if str(beat).strip():
                lines.append("• %s" % _esc(beat))
        if c.get("why_it_works"):
            lines.append("Why it works: %s" % _esc(c["why_it_works"]))
    if notes:
        lines.append("")
        lines.append("_%s_" % _esc(notes))
    lines.append("")
    lines.append(PICK_LINE)
    return "\n".join(lines)
