"""
video_studio_jobs.py

The slow half of the video studio workflow: write a script, revise it, build
the video. Each runs on its own thread and posts its own result back into the
Slack thread it belongs to.

Split from the handler because the handler's job is routing and money
discipline, and this one's job is talking to the content_producer agent and
to Vome Studio. Keeping them apart means the rule that matters (greenlight is
explicit, never inferred) stays readable in one short file.

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
import video_threads

# Local checkout, used only when Studio is not reachable over HTTP. No
# default: a hardcoded laptop path is how the deployed agent ended up trying
# to write C:\Users\... on a Linux host.
STUDIO_DIR = os.environ.get("VOME_STUDIO_DIR", "")
STUDIO_PY = os.environ.get("VOME_STUDIO_PYTHON", sys.executable)

# A four-format render is minutes, not seconds.
BUILD_TIMEOUT = int(os.environ.get("VOME_STUDIO_BUILD_TIMEOUT", "1800"))

# A help-centre link in the brief names the exact article to read.
FAQ_URL = re.compile(r"https?://\S*(?:kb|support|faq)\S*", re.I)


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

def _post_script_for_review(thread_ts: str, note: str = "") -> None:
    """Post the current script and park the thread on the human."""
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


def draft_script(thread_ts: str, brief: str) -> None:
    """First pass: an idea becomes a storyboard, then a script in the thread.

    The agent that writes it is content_producer. Wiring the runner in is the
    remaining piece: until then this reports honestly rather than pretending
    it wrote something.
    """
    row = video_threads.get(thread_ts) or {}
    result = run_content_producer(brief, row)
    if not result.get("ok"):
        _post(thread_ts,
              "I could not draft this one.\n```%s```\nThe thread is still "
              "open, so a nudge with more detail is worth a try."
              % str(result.get("error", "unknown"))[:900])
        return
    video_threads.update(thread_ts, storyboard=result.get("storyboard", ""),
                         feature_name=result.get("feature_name", ""))
    _post_script_for_review(thread_ts, "Here is the first pass.")


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
    _post_script_for_review(thread_ts, result.get("summary", "Updated."))


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


def _slugify(value: str) -> str:
    """A storyboard id from a feature name.

    The id names the render folder and every output file, so "untitled" is
    never acceptable. The agent is told to set it; this is the backstop for
    when it does not.
    """
    out = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return out or "untitled"


def _read_storyboard(slug: str) -> dict | None:
    root = _local_studio()
    if not root:
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


def run_content_producer(message: str, row: dict, revise: bool = False) -> dict:
    """Ask the content_producer agent for a storyboard, and write it out.

    Everything about talking to the agent is here: gathering the FAQ, calling
    the one OS run path, and persisting what comes back. The caller only has
    to branch on `ok`.

    On a revision the existing storyboard goes in as context. That is what
    makes "make the hook a question" an edit rather than a rewrite, which
    matters because every earlier round of wording was approved by a person.
    """
    title, body = _faq_text(row, message)
    if not body.strip() and not revise:
        return {
            "ok": False,
            "error": ("I could not find the FAQ for this one. Paste the help "
                      "centre link and I will read it."),
        }

    context = {
        "feature_name": row.get("feature_name") or title or "",
        "faq_text": body,
        "brief": message,
    }
    if revise:
        existing = _read_storyboard(row.get("storyboard") or "")
        if existing is None:
            return {"ok": False,
                    "error": "the storyboard file for this thread is missing"}
        context["existing_storyboard"] = json.dumps(existing, indent=2)
        context["change_requested"] = message

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

    # Keep the id stable across revisions. A new id would render into a new
    # folder and orphan every earlier version of this video.
    if revise and row.get("storyboard"):
        storyboard["id"] = row["storyboard"]

    try:
        slug = _write_storyboard(storyboard)
    except OSError as exc:
        return {"ok": False, "error": "could not save the storyboard: %s" % exc}

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

    return {
        "ok": True,
        "storyboard": slug,
        "feature_name": storyboard.get("feature_name", ""),
        "estimated_runtime": check.get("estimated_runtime"),
        "summary": ("Updated." if revise else "Here is the first pass.")
                   + " Estimated %ss."
                   % round(check.get("estimated_runtime") or 0),
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
        report = _build_over_http(conn, slug)
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


def _build_over_http(conn, slug: str) -> dict:
    """Start a build and poll it.

    A render is minutes long, which is why the endpoint returns an id instead
    of holding the request open. Polling here rather than in the handler
    keeps the Slack thread responsive the whole time.
    """
    storyboard = _read_storyboard(slug) or {"id": slug}
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
