"""
video_studio_jobs.py

The slow half of the video studio workflow: position the video and offer
concepts, write the script for the concept a person picks, revise it, build
the video. Each runs on its own thread and posts its own result back into
the Slack thread it belongs to.

THE MARKETING TEAM

    content_strategist  positioning brief: audience, what is new, one claim
    creative_director   three concepts, each its own angle, hook and format
    (a person picks one by number, matched in the handler)
    content_producer    the copywriter: the storyboard for that concept
    brand_editor        one pass for voice, truth and the format contract
    Vome Studio         validates, then the script goes up for review

Split from the handler because the handler's job is routing and money
discipline, and this one's job is talking to the agents and to Vome Studio.
Keeping them apart means the rule that matters (greenlight is explicit,
never inferred) stays readable in one short file.

HOW STUDIO IS REACHED

Through `vomeos.integrations.vome_studio` when it is configured, which is the
Phase 2 path and the one that will work in production. Until that endpoint
exists, the same operations run as Studio's CLI, which emits exactly the
payload the connector documents. `_studio_cli` is the interim, and it is
deliberately the only place that knows Studio is local.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import slack
import video_concepts
import video_threads

# Local checkout, used only when Studio is not reachable over HTTP. No
# default: a hardcoded laptop path is how the deployed agent ended up trying
# to write C:\Users\... on a Linux host.
STUDIO_DIR = os.environ.get("VOME_STUDIO_DIR", "")
STUDIO_PY = os.environ.get("VOME_STUDIO_PYTHON", sys.executable)

# A four-format render is minutes, not seconds.
BUILD_TIMEOUT = int(os.environ.get("VOME_STUDIO_BUILD_TIMEOUT", "1800"))

# A help-centre link in the brief names the exact article to read.
# Stops at "|" and ">": Slack sends a pasted link as <url|label>, and
# matching to the next space captured the label and a ">" with it.
FAQ_URL = re.compile(r"https?://[^\s|<>]*(?:kb|support|faq)[^\s|<>]*", re.I)


def _studio() -> "object | None":
    """The HTTP connector, if Studio is deployed and configured."""
    try:
        from vomeos.integrations.vome_studio import VomeStudio
    except ImportError:
        return None
    conn = VomeStudio()
    return conn if conn.configured() else None


def _local_studio() -> str:
    """The local checkout, if there is a usable one on THIS machine."""
    if not STUDIO_DIR:
        return ""
    if not os.path.isdir(os.path.join(STUDIO_DIR, "storyboards")):
        return ""
    return STUDIO_DIR


def _no_transport_error() -> dict:
    """Said once, in the thread, in terms someone can act on."""
    return {
        "ok": False,
        "error": (
            "I cannot reach Vome Studio from here. Set VOME_STUDIO_API_URL "
            "and VOME_STUDIO_API_KEY once Studio is deployed, or run this "
            "agent on a machine with the repo checked out and "
            "VOME_STUDIO_DIR pointing at it."
        ),
    }


def _post(thread_ts: str, message: str) -> None:
    try:
        slack.post_to_video_studio(message, thread_ts=thread_ts)
    except Exception as exc:
        print("[VIDEO] post failed on %s: %s" % (thread_ts, exc))


def _studio_cli(args: list[str], timeout: int = 120):
    """Run a Studio command. Returns (ok, stdout, stderr).

    The interim path until Studio has an HTTP endpoint. Everything about
    Studio being a local directory is confined here.
    """
    root = _local_studio()
    if not root:
        return False, "", ("no local Vome Studio checkout "
                           "(set VOME_STUDIO_DIR, or deploy Studio and set "
                           "VOME_STUDIO_API_URL)")
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        p = subprocess.run(
            [STUDIO_PY] + args, cwd=root, env=env,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "", "timed out after %ds" % timeout
    except OSError as exc:
        return False, "", "could not run Vome Studio: %s" % exc
    return p.returncode == 0, p.stdout, p.stderr


def _studio_json(args: list[str], timeout: int = 120):
    """Run a Studio command that prints JSON, and parse it."""
    ok, out, err = _studio_cli(args, timeout=timeout)
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return {"ok": False,
                "error": (err or out or "no output from Vome Studio")[-1500:]}


# ---------------------------------------------------------------------------
# Script: write, revise, post for review
# ---------------------------------------------------------------------------

def _post_script_for_review(thread_ts: str, note: str = "",
                            review: str = "") -> None:
    """Post the current script and park the thread on the human.

    `review` is the script as Studio's validate call formatted it. Deployed,
    that is the only way to get it: there is no checkout here to run
    review.py in. Locally the CLI is the fallback.
    """
    if review:
        ok, out, err = True, review, ""
    else:
        ok, out, err = _studio_cli(["scripts/review.py"])
    if not ok:
        _post(thread_ts,
              "I could not read the script back.\n```%s```" % (err or out)[-900:])
        return
    rounds = video_threads.bump_round(thread_ts)
    header = note + "\n\n" if note else ""
    footer = ("\n\n_Round %d. Nothing has been generated yet, so changes are "
              "free. Say *go build the video* when you are happy._" % rounds)
    _post(thread_ts, header + out.strip() + footer)
    video_threads.update(thread_ts,
                         state=video_threads.STATE_AWAITING_REVIEW)


def develop_concepts(thread_ts: str, brief: str, direction: str = "") -> None:
    """An idea, or direction on earlier concepts, becomes three concepts.

    First time: the strategist positions the video, then the creative
    director offers three concepts. With direction: the positioning stands
    and the creative director tries again, shown the concepts it is moving
    away from. Either way the concepts are posted and the thread waits for
    a person to pick one.
    """
    row = video_threads.get(thread_ts) or {}
    had_concepts = bool(video_concepts.load_concepts(row))
    result = run_concepts(brief, row, direction=direction)
    if not result.get("ok"):
        if direction and had_concepts:
            tail = ("The earlier concepts still stand: reply with 1, 2 or 3 "
                    "to pick one, or give direction again.")
            state = video_threads.STATE_AWAITING_CONCEPT
        else:
            tail = ("The thread is still open, so a nudge with more detail "
                    "is worth a try.")
            state = video_threads.STATE_IDEA
        error = str(result.get("error", "unknown"))[:900]
        _post(thread_ts, "I could not come up with concepts for this one."
              "\n```%s```\n%s" % (error, tail))
        video_threads.update(thread_ts, state=state)
        return
    video_threads.update(
        thread_ts,
        positioning_json=json.dumps(result["positioning"]),
        concepts_json=json.dumps(result["concepts"]),
        chosen_concept=None,
        feature_name=result.get("feature_name", ""),
        state=video_threads.STATE_AWAITING_CONCEPT,
    )
    _post(thread_ts, video_concepts.concepts_post(
        result["concepts"],
        feature_name=result.get("feature_name", ""),
        single_claim=str(result["positioning"].get("single_claim", "")),
        notes=str(result.get("notes", "")),
        catalogue=result.get("catalogue"),
        direction=bool(direction),
    ))


def draft_from_concept(thread_ts: str, number: int) -> None:
    """A person picked a concept: write its script and post it for review.

    The copywriter writes it in the concept's own format, from that format's
    contract, and the brand editor reads it once before Studio validates it.
    If Studio says the format is not available, the thread is told so and
    nothing is written in another format instead.
    """
    row = video_threads.get(thread_ts) or {}
    concepts = video_concepts.load_concepts(row)
    if not 1 <= number <= len(concepts):
        _post(thread_ts, "There is no concept %d in this thread. Reply with "
              "a number from 1 to %d, or give direction for new concepts."
              % (number, len(concepts) or 3))
        video_threads.update(thread_ts,
                             state=video_threads.STATE_AWAITING_CONCEPT)
        return
    concept = concepts[number - 1]
    positioning = video_concepts.load_positioning(row) or {}
    video_threads.update(thread_ts, chosen_concept=number,
                         state=video_threads.STATE_DRAFTING)
    row = dict(row, chosen_concept=number)

    result = run_content_producer(
        "Write the storyboard for concept %d, %s."
        % (number, concept.get("title", "")),
        row, concept=concept, positioning=positioning)
    if not result.get("ok"):
        if result.get("format_unavailable"):
            message = result["error"]
        else:
            message = ("I could not write the script for concept %d.\n```%s```"
                       % (number, str(result.get("error", "unknown"))[:900]))
        _post(thread_ts, message + "\nThe concepts still stand: reply with "
              "a number to try again or pick another, or give direction for "
              "new ones.")
        video_threads.update(thread_ts, chosen_concept=None,
                             state=video_threads.STATE_AWAITING_CONCEPT)
        return

    video_threads.update(thread_ts, storyboard=result.get("storyboard", ""),
                         feature_name=result.get("feature_name", ""))
    note = "Here is the first pass, written for concept %d, *%s* (%s). %s" % (
        number, concept.get("title", ""),
        video_concepts.format_title(concept.get("format", "explainer")),
        result.get("estimate", ""))
    changes = result.get("changes") or []
    if changes:
        note = note.rstrip() + "\nBrand edit:\n" + "\n".join(
            "• %s" % c for c in changes)
    _post_script_for_review(thread_ts, note.rstrip(),
                            review=result.get("review", ""))


def revise_script(thread_ts: str, note: str) -> None:
    """A note in the thread becomes an edit to the storyboard.

    Revising, never recreating. The storyboard for this feature already
    exists and carries wording a person approved; starting over throws that
    away along with every earlier round.
    """
    row = video_threads.get(thread_ts) or {}
    if not row.get("storyboard"):
        _post(thread_ts,
              "I do not have a script for this thread yet, so there is "
              "nothing to change. Post the feature and its FAQ link and I "
              "will draft one.")
        return
    result = run_content_producer(note, row, revise=True)
    if not result.get("ok"):
        _post(thread_ts,
              "I could not apply that change.\n```%s```"
              % str(result.get("error", "unknown"))[:900])
        return
    _post_script_for_review(thread_ts, result.get("summary", "Updated."),
                            review=result.get("review", ""))


def _faq_text(row: dict, brief: str) -> tuple[str, str]:
    """The FAQ this video is about, as (title, body).

    Prefer the article's own permalink from the link someone pasted. Falling
    back to full-text search is a real fallback, not an equivalent: searching
    for the feature name can rank a neighbouring article first, and writing a
    script from the wrong FAQ is an error nobody catches until it is public.
    """
    url = (row.get("faq_url") or "")
    if not url:
        m = FAQ_URL.search(brief or "")
        url = m.group(0) if m else ""

    # Rows saved before the pattern was fixed carry "|label>" on the end.
    url = re.split(r"[|<>]", url)[0]
    if url:
        permalink = url.rstrip("/").rsplit("/", 1)[-1]
        try:
            from database import get_kb_article_by_permalink
            article = get_kb_article_by_permalink(permalink)
        except Exception as exc:
            print("[VIDEO] KB lookup failed for %s: %s" % (permalink, exc))
            article = None
        if article and (article.get("body") or "").strip():
            return article.get("title", ""), article["body"]
        # Not in our copy, usually because it was published after the
        # nightly sync. Ask Zoho directly. Never fall back to a keyword
        # search of our copy: that found "How do the Vome API and webhooks
        # work?" for an email format article on the first live run.
        return _live_article(permalink) or ("", "")

    query = (row.get("feature_name") or brief or "").strip()
    if not query:
        return "", ""
    try:
        from database import search_kb_articles_db
        hits = search_kb_articles_db(query, language="en", limit=1,
                                     body_chars=20000)
    except Exception as exc:
        print("[VIDEO] KB search failed for %r: %s" % (query, exc))
        return "", ""
    if not hits:
        return "", ""
    return hits[0].get("title", ""), hits[0].get("body", "")


def _live_article(permalink: str) -> tuple[str, str] | None:
    """One article straight from Zoho Desk, for a link newer than our copy.

    Zoho has no lookup by permalink, and its text search is no substitute
    (ranking could hand back a neighbour). So: the 50 most recently modified
    published articles, newest first, and a match ONLY on the exact
    permalink. An article too new for the nightly copy is near the top of
    that list by definition. The body comes from the article itself, and only
    if it is published, because a draft's portal URL is a dead link outside
    Vome.
    """
    try:
        from agent import _zoho_desk_call, _unwrap_mcp_result, ZOHO_ORG_ID
        from kb_sync import _fetch_article_detail, _is_published
    except Exception as exc:
        print("[VIDEO] Zoho lookup unavailable: %s" % exc)
        return None
    try:
        raw = _unwrap_mcp_result(_zoho_desk_call("ZohoDesk_getArticles", {
            "query_params": {"orgId": str(ZOHO_ORG_ID),
                             "status": "Published",
                             "sortBy": "-modifiedTime", "limit": 50}}))
    except Exception as exc:
        print("[VIDEO] Zoho listing failed for %s: %s" % (permalink, exc))
        return None
    items = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    match = next((a for a in items if isinstance(a, dict)
                  and a.get("permalink") == permalink), None)
    if not match:
        print("[VIDEO] %s not found in Zoho either" % permalink)
        return None
    detail = _fetch_article_detail(str(match.get("id", "")))
    if not detail or not _is_published(detail.get("status")):
        return None
    body = re.sub(r"<[^>]+>", " ", detail.get("answer") or "")
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return None
    print("[VIDEO] read %s live from Zoho (not yet synced)" % permalink)
    return detail.get("title") or match.get("title", ""), body


def _slugify(value: str) -> str:
    """A storyboard id from a feature name.

    The id names the render folder and every output file, so "untitled" is
    never acceptable. The agent is told to set it; this is the backstop for
    when it does not.
    """
    out = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return out or "untitled"


def _read_storyboard(row: dict | str) -> dict | None:
    """The thread's storyboard: its saved copy, else the local checkout.

    The saved copy comes first because it is the one that exists when
    deployed. A bare slug is accepted for the local-only callers.
    """
    if isinstance(row, dict):
        raw = row.get("storyboard_json") or ""
        if raw:
            try:
                sb = json.loads(raw)
                if isinstance(sb, dict) and sb.get("scenes"):
                    return sb
            except ValueError:
                pass
        slug = row.get("storyboard") or ""
    else:
        slug = row or ""
    root = _local_studio()
    if not root or not slug:
        return None
    path = os.path.join(root, "storyboards", "%s.json" % slug)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_storyboard(sb: dict) -> str:
    """Persist locally. Over HTTP the storyboard is sent with the request, so
    there is nothing to write here and the slug is all the caller needs."""
    slug = sb.get("id") or _slugify(sb.get("feature_name", ""))
    sb["id"] = slug
    root = _local_studio()
    if root:
        path = os.path.join(root, "storyboards", "%s.json" % slug)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sb, f, indent=2)
    return slug


class FormatUnavailable(Exception):
    """Studio says it cannot give the contract for this format."""

    def __init__(self, fmt: str):
        super().__init__(fmt)
        self.format = fmt


def _contract(format: str = "explainer") -> dict | None:
    """Studio's storyboard contract for one format: schema, worked example,
    required cues.

    Raises FormatUnavailable when Studio answers that the format is unknown
    or not ready. The caller says so in the thread; it never asks for a
    different format instead.
    """
    fmt = format or "explainer"
    conn = _studio()
    if conn is not None:
        res = conn.contract(fmt)
        if res.ok and isinstance(res.data, dict):
            return res.data
        if res.status_code == 404 and fmt != "explainer":
            raise FormatUnavailable(fmt)
        return None
    if _local_studio():
        arg = "" if fmt == "explainer" else json.dumps(fmt)
        out = _studio_json(["-c", "import json, sys; sys.path.insert(0, "
                                  "'scripts'); import storyboard; "
                                  "print(json.dumps(storyboard.contract(%s)))"
                                  % arg])
        if out.get("example"):
            return out
        if fmt != "explainer":
            raise FormatUnavailable(fmt)
        return None
    return None


def _formats() -> tuple[list[dict] | None, str]:
    """Studio's format catalogue, as (formats, error).

    A Studio older than the catalogue answers /v1/formats with a 404. That
    Studio can only build explainers, so that is the catalogue; it is a
    fact about the service, not a fallback chosen here.
    """
    conn = _studio()
    if conn is not None:
        res = conn.formats()
        if res.ok:
            formats = video_concepts.parse_catalogue(res.data)
            if formats:
                return formats, ""
            return None, "Vome Studio returned no video formats."
        if res.status_code == 404:
            return [dict(video_concepts.EXPLAINER_ONLY)], ""
        return None, ("I could not get the list of video formats from Vome "
                      "Studio: %s" % (res.error or "no answer"))
    if _local_studio():
        out = _studio_json(["-c", "import json, sys; sys.path.insert(0, "
                                  "'scripts'); import storyboard; "
                                  "f = getattr(storyboard, 'formats', None); "
                                  "print(json.dumps({'formats': f() if f "
                                  "else None}))"])
        raw = out.get("formats") if isinstance(out, dict) else None
        if raw is None and out.get("ok") is not False:
            return [dict(video_concepts.EXPLAINER_ONLY)], ""
        formats = video_concepts.parse_catalogue(raw)
        if formats:
            return formats, ""
        return None, ("I could not read the video formats from the local "
                      "Vome Studio checkout: %s" % out.get("error", ""))
    return None, _no_transport_error()["error"]


def _claim_fresh_id(sb: dict, contract: dict, thread_ts: str) -> None:
    """Give a new draft an id no other video is using.

    Drafting the pilot's own feature, the agent chose `recruitment-workflows`
    and saving it overwrote the approved storyboard. A taken id gets today's
    date, and the reviewer is told why, so revising the existing video stays
    a deliberate choice made in its own thread.
    """
    slug = sb.get("id") or _slugify(sb.get("feature_name", ""))
    taken = set(video_threads.slugs_in_use(thread_ts))
    taken.add(((contract or {}).get("example") or {}).get("id", ""))
    folder = os.path.join(_local_studio() or "", "storyboards")
    if _local_studio() and os.path.isdir(folder):
        taken.update(n[:-5] for n in os.listdir(folder) if n.endswith(".json"))
    if slug not in taken:
        sb["id"] = slug
        return
    fresh = "%s-%s" % (slug, time.strftime("%Y%m%d"))
    n = 2
    while fresh in taken:
        fresh = "%s-%s-%d" % (slug, time.strftime("%Y%m%d"), n)
        n += 1
    note = ("A video called %s already exists, so this draft is saved as %s "
            "and cannot overwrite it. To change that video instead, reply in "
            "its own thread." % (slug, fresh))
    sb["review_notes"] = (note + " " + sb.get("review_notes", "")).strip()
    sb["id"] = fresh


def _remember_storyboard(thread_ts: str, sb: dict) -> None:
    """Keep the whole storyboard on the thread row.

    The thread is the one place both a revision and a greenlight can always
    reach. Studio's copy is wiped on every redeploy.
    """
    if thread_ts:
        video_threads.update(thread_ts, storyboard=sb.get("id", ""),
                             storyboard_json=json.dumps(sb))


def _no_faq_error(row: dict, message: str) -> dict:
    if row.get("faq_url") or FAQ_URL.search(message or ""):
        return {
            "ok": False,
            "error": ("I could not find that article, in my copy of the "
                      "help centre or in Zoho itself. Check the link, "
                      "and that the article is published rather than a "
                      "draft, then post it again in this thread."),
        }
    return {
        "ok": False,
        "error": ("I could not find the FAQ for this one. Paste the help "
                  "centre link and I will read it."),
    }


def _run(agent: str, context: dict, thread_ts: str):
    """One agent call, as (data, error). Blocked and error are both failures
    here, and the reason says which, because they are not the same thing."""
    try:
        from vomeos.runner import run_agent
    except ImportError as exc:
        return None, "Vome OS runner unavailable: %s" % exc
    result = run_agent(agent, context, subject_type="video_thread",
                       subject_id=thread_ts or "")
    if result.blocked:
        return None, "%s was blocked by a guard: %s" % (agent, result.why())
    if not result.ok:
        return None, "%s failed: %s" % (agent, result.why())
    if not isinstance(result.data, dict):
        return None, "%s returned nothing usable" % agent
    return result.data, ""


def run_concepts(brief: str, row: dict, direction: str = "") -> dict:
    """The strategist and the creative director, for one thread.

    Returns {"ok", "positioning", "concepts", "notes", "catalogue",
    "feature_name"} or {"ok": False, "error"}. Persisting and posting are the
    caller's.

    On direction the positioning already on the thread is kept. Direction is
    about how to tell the story; what the video claims was settled from the
    FAQ and does not move because a hook was not liked.
    """
    thread_ts = row.get("thread_ts", "")
    title, body = _faq_text(row, brief)
    if not body.strip():
        return _no_faq_error(row, brief)
    feature_name = title or row.get("feature_name") or ""

    catalogue, err = _formats()
    if err:
        return {"ok": False, "error": err}
    ready = video_concepts.ready_formats(catalogue)
    if not ready:
        return {"ok": False,
                "error": "Vome Studio reports no video format ready to build."}

    prior = video_concepts.summarise_prior(
        video_threads.recent_videos(thread_ts))
    prior_text = video_concepts.prior_videos_text(prior)

    positioning = video_concepts.load_positioning(row) if direction else None
    if not positioning:
        positioning, err = _run("content.content_strategist", {
            "feature_name": feature_name,
            "faq_text": body,
            "brief": brief,
            "prior_videos": prior_text,
        }, thread_ts)
        if err:
            return {"ok": False, "error": err}
        if not str(positioning.get("single_claim") or "").strip():
            return {"ok": False,
                    "error": "the positioning brief came back with no claim"}

    context = {
        "positioning": json.dumps(positioning, indent=2),
        "faq_text": body,
        # The whole catalogue, so a format that is not ready yet can be
        # mentioned as "later". Only ready ones pass check_concepts.
        "formats": json.dumps({"formats": catalogue}, indent=2),
        "prior_videos": prior_text,
        "brief": brief,
    }
    if direction:
        context["direction"] = direction
        context["previous_concepts"] = json.dumps(
            video_concepts.load_concepts(row), indent=2)
    data, err = _run("content.creative_director", context, thread_ts)
    if err:
        return {"ok": False, "error": err}
    concepts, err = video_concepts.check_concepts(
        data, [f["name"] for f in ready], [p["hook"] for p in prior])
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "positioning": positioning, "concepts": concepts,
            "notes": data.get("notes", ""), "catalogue": catalogue,
            "feature_name": feature_name}


def _set_format(sb: dict, fmt: str) -> None:
    """The format is the chosen concept's, never the model's.

    Explainer is the default and is left out, which is also what a Studio
    older than formats accepts: its schema has no `format` key at all.
    """
    if not fmt or fmt == "explainer":
        sb.pop("format", None)
    else:
        sb["format"] = fmt


def _structure(sb: dict) -> list:
    return [(s.get("id"), s.get("template")) for s in sb.get("scenes") or []
            if isinstance(s, dict)]


def _brand_edit(sb: dict, concept: dict, positioning: dict | None,
                contract: dict, thread_ts: str) -> tuple[dict, list[str]]:
    """One pass by the brand editor. Returns (storyboard, changes).

    The edit is accepted only on terms checked here, not trusted to the
    model: the id, format, voice, music and pacing are put back as they
    were, review notes are not lost, and an edit that changed the scene
    structure is set aside. If the editor fails, the copywriter's draft goes
    up unedited and the reviewer is told, because a person reads it next
    either way and silence would suggest it had been edited.
    """
    data, err = _run("content.brand_editor", {
        "storyboard": json.dumps(sb, indent=2),
        "concept": json.dumps(concept, indent=2),
        "positioning": json.dumps(positioning or {}, indent=2),
        "contract": json.dumps(contract, indent=2),
    }, thread_ts)
    unedited = "so this is the copywriter's draft as written."
    if err:
        return sb, ["The brand edit did not run (%s), %s" % (err[:200],
                                                             unedited)]
    edited = data.get("storyboard")
    if not isinstance(edited, dict) or not edited.get("scenes"):
        return sb, ["The brand edit came back without a storyboard, %s"
                    % unedited]
    if _structure(edited) != _structure(sb):
        return sb, ["The brand edit changed the scenes, so it was set aside "
                    "and %s" % unedited.replace("so ", "", 1)]
    if data.get("verdict") == "pass":
        return sb, []

    edited["id"] = sb.get("id", "")
    _set_format(edited, sb.get("format", ""))
    for key in ("voice", "music", "pacing"):
        if key in sb:
            edited[key] = sb[key]
        else:
            edited.pop(key, None)
    if sb.get("review_notes") and not edited.get("review_notes"):
        edited["review_notes"] = sb["review_notes"]
    changes = [str(c).strip() for c in data.get("changes") or []
               if str(c).strip()][:5]
    return edited, changes


def run_content_producer(message: str, row: dict, revise: bool = False,
                         concept: dict | None = None,
                         positioning: dict | None = None) -> dict:
    """Ask the content_producer agent for a storyboard, and write it out.

    Everything about talking to the agent is here: gathering the FAQ, calling
    the one OS run path, and persisting what comes back. The caller only has
    to branch on `ok`.

    A first draft is written for the concept a person picked, in that
    concept's format, and then read once by the brand editor. On a revision
    the existing storyboard goes in as context, with the thread's concept
    and positioning so the angle survives. That is what makes "make the hook
    a question" an edit rather than a rewrite, which matters because every
    earlier round of wording was approved by a person. Revisions do not go
    through the brand editor: they are a person's own changes.
    """
    title, body = _faq_text(row, message)
    if not body.strip() and not revise:
        return _no_faq_error(row, message)

    if concept is None:
        concept = video_concepts.chosen_concept(row)
    if positioning is None:
        positioning = video_concepts.load_positioning(row)

    context = {
        # The article's own title over the thread's label, which is just
        # the first line of the Slack message ("I have a new video idea...").
        "feature_name": title or row.get("feature_name") or "",
        "faq_text": body,
        "brief": message,
        # Empty for a thread that predates concepts. The charter says what
        # to do then: keep the existing storyboard's angle.
        "concept": json.dumps(concept, indent=2) if concept else "",
        "positioning": json.dumps(positioning, indent=2) if positioning
        else "",
    }
    fmt = (concept or {}).get("format") or "explainer"
    if revise:
        existing = _read_storyboard(row)
        if existing is None:
            return {"ok": False,
                    "error": "the storyboard file for this thread is missing"}
        context["existing_storyboard"] = json.dumps(existing, indent=2)
        context["change_requested"] = message
        # A revision stays in the format the script is already in. An
        # explainer carries no format key, so absent means explainer.
        fmt = existing.get("format") or "explainer"

    # The agent is shown the real format, from Studio, not told about it.
    # Told "match Studio's schema" with no schema, it guessed every field
    # name on its first live run.
    try:
        contract = _contract(fmt)
    except FormatUnavailable:
        return {"ok": False, "format_unavailable": fmt,
                "error": ("Vome Studio says the %s format is not available "
                          "right now, so I have not written this script. I "
                          "will not switch it to another format on my own."
                          % video_concepts.format_title(fmt))}
    if not contract or not contract.get("example"):
        return {"ok": False,
                "error": ("I could not get the storyboard format from Vome "
                          "Studio, so I did not guess at it.")}
    context["storyboard_contract"] = json.dumps(contract, indent=2)

    try:
        from vomeos.runner import run_agent
    except ImportError as exc:
        return {"ok": False, "error": "Vome OS runner unavailable: %s" % exc}

    result = run_agent(
        "content.content_producer",
        context,
        subject_type="video_thread",
        subject_id=row.get("thread_ts", ""),
    )

    # Three outcomes, and they are not the same thing. `blocked` means the
    # model answered and a guard rejected it, which is a human's problem, not
    # something to retry or quietly send anyway.
    if result.blocked:
        return {"ok": False,
                "error": "the draft was blocked by a guard: %s" % result.why()}
    if not result.ok:
        return {"ok": False, "error": result.why()}

    storyboard = result.data
    if not isinstance(storyboard, dict) or not storyboard.get("scenes"):
        return {"ok": False,
                "error": "the agent returned no storyboard"}

    _set_format(storyboard, fmt)

    # Keep the id stable across revisions. A new id would render into a new
    # folder and orphan every earlier version of this video.
    if revise and row.get("storyboard"):
        storyboard["id"] = row["storyboard"]
    elif not revise:
        _claim_fresh_id(storyboard, contract, row.get("thread_ts", ""))

    changes: list[str] = []
    if not revise and concept:
        storyboard, changes = _brand_edit(storyboard, concept, positioning,
                                          contract, row.get("thread_ts", ""))

    try:
        slug = _write_storyboard(storyboard)
    except OSError as exc:
        return {"ok": False, "error": "could not save the storyboard: %s" % exc}
    _remember_storyboard(row.get("thread_ts", ""), storyboard)

    # Studio validates it properly: em dashes, duplicate cues, unknown
    # templates, timings that should not be there. Catching it now costs a
    # message; catching it at build time costs credits.
    conn = _studio()
    if conn is not None:
        res = conn.validate(storyboard)
        check = res.data if res.ok else {"ok": False, "error": res.error}
    elif _local_studio():
        check = _studio_json(["scripts/produce.py", slug, "--dry-run"])
    else:
        return _no_transport_error()
    if not check.get("ok"):
        return {"ok": False,
                "error": "the draft did not validate:\n%s"
                         % str(check.get("error", ""))[:800]}

    estimate = "Estimated %ss." % round(check.get("estimated_runtime") or 0)
    return {
        "ok": True,
        "storyboard": slug,
        "feature_name": storyboard.get("feature_name", ""),
        "format": fmt,
        "estimated_runtime": check.get("estimated_runtime"),
        "estimate": estimate,
        "changes": changes,
        "review": check.get("review", ""),
        "summary": "%s %s" % ("Updated." if revise
                              else "Here is the first pass.", estimate),
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_video(thread_ts: str) -> None:
    """Greenlit: build it, then report the result into the thread.

    This is the expensive call. It only ever runs from an explicit greenlight
    matched in the handler, never from a model's reading of a message.
    """
    row = video_threads.get(thread_ts) or {}
    slug = row.get("storyboard") or ""

    conn = _studio()
    if conn is not None:
        storyboard = _read_storyboard(row)
        if storyboard is None:
            # Never send a stub. Studio would have nothing to build, and an
            # older Studio would overwrite the real script with it.
            report = {"ok": False,
                      "error": "I have lost the script for this thread, so "
                               "there is nothing to build. Post the feature "
                               "again and I will redraft it."}
        else:
            report = _build_over_http(conn, storyboard)
    elif _local_studio():
        args = ["scripts/produce.py", "--engine", "elevenlabs"]
        if slug:
            args.insert(1, slug)
        report = _studio_json(args, timeout=BUILD_TIMEOUT)
    else:
        report = _no_transport_error()

    _post(thread_ts, _delivery_post(report))
    video_threads.update(
        thread_ts,
        state=(video_threads.STATE_DELIVERED if report.get("ok")
               else video_threads.STATE_AWAITING_REVIEW),
        last_runtime=report.get("runtime"),
        render_folder=report.get("folder", ""),
    )


def _build_over_http(conn, storyboard: dict) -> dict:
    """Start a build and poll it.

    A render is minutes long, which is why the endpoint returns an id instead
    of holding the request open. Polling here rather than in the handler
    keeps the Slack thread responsive the whole time.
    """
    started = conn.build(storyboard, engine="elevenlabs")
    if not started.ok:
        return {"ok": False, "error": started.error or "Studio refused the build"}
    build_id = (started.data or {}).get("build_id")
    if not build_id:
        return {"ok": False, "error": "Studio did not return a build id"}

    deadline = time.time() + BUILD_TIMEOUT
    while time.time() < deadline:
        time.sleep(15)
        polled = conn.get_build(build_id)
        if not polled.ok:
            continue          # a blip while rendering is not a failed build
        rec = polled.data or {}
        if rec.get("status") != "running":
            return rec.get("report") or {
                "ok": False, "error": "build %s" % rec.get("status")}
    return {"ok": False,
            "error": "build did not finish within %ds" % BUILD_TIMEOUT}


def _delivery_post(report: dict) -> str:
    """The message that goes back to the thread.

    Studio formats this when there is a local checkout, so the wording lives
    next to the thing it describes. Over HTTP there is no checkout, so the
    same shape is built here from the report.
    """
    root = _local_studio()
    if root:
        tmp = os.path.join(root, ".last_build_report.json")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(report, f)
            ok, out, _ = _studio_cli(
                ["scripts/review.py", "--delivered", ".last_build_report.json"])
            if ok and out.strip():
                return out.strip()
        except OSError as exc:
            print("[VIDEO] could not write build report: %s" % exc)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    if not report.get("ok"):
        return ("*%s* could not be built.\n```%s```\nNothing was published "
                "and the storyboard is unchanged."
                % (report.get("feature_name", "This video"),
                   str(report.get("error", "unknown error"))[:1200]))

    lines = ["*%s* is built. :white_check_mark:"
             % report.get("feature_name", "Your video"), ""]
    if report.get("runtime"):
        lines.append("_%.0f seconds, %d formats._"
                     % (report["runtime"], len(report.get("outputs", []))))
    if report.get("folder"):
        lines.append("Folder: `vome-studio/%s`" % report["folder"])
    lines.append("")
    for o in report.get("outputs", []):
        lines.append("*%s*  `%s`" % (o["aspect"], o["path"].rsplit("/", 1)[-1]))
        lines.append("    %s  (%.1f MB)" % (o.get("where", ""),
                                            o.get("bytes", 0) / 1e6))
    for w in report.get("warnings", []):
        lines.append(":warning: %s" % w)
    return "\n".join(lines)
