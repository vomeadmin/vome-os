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
import subprocess
import sys

import slack
import video_threads

STUDIO_DIR = os.environ.get(
    "VOME_STUDIO_DIR",
    r"C:\Users\sfage\Projects\Vome\vome-studio",
)
STUDIO_PY = os.environ.get("VOME_STUDIO_PYTHON", sys.executable)

# A four-format render is minutes, not seconds.
BUILD_TIMEOUT = int(os.environ.get("VOME_STUDIO_BUILD_TIMEOUT", "1800"))


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
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        p = subprocess.run(
            [STUDIO_PY] + args, cwd=STUDIO_DIR, env=env,
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


def run_content_producer(message: str, row: dict, revise: bool = False) -> dict:
    """Ask the content_producer agent for a storyboard, and write it out.

    NOT YET WIRED. The agent exists (manifest, charter, answer key) and the
    OS runner exists, but connecting them is the last piece of this workflow.
    Reporting that plainly is better than a stub that silently produces
    nothing and leaves someone waiting in a thread.
    """
    return {
        "ok": False,
        "error": ("content_producer is not wired to the runner yet. The "
                  "storyboard has to be written or edited by hand in "
                  "vome-studio/storyboards/ for now; everything after that, "
                  "including review and build, works."),
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_video(thread_ts: str) -> None:
    """Greenlit: build it, then report the result into the thread.

    This is the expensive call. It only ever runs from an explicit greenlight
    matched in the handler, never from a model's reading of a message.
    """
    report = _studio_json(
        ["scripts/produce.py", "--engine", "elevenlabs"],
        timeout=BUILD_TIMEOUT,
    )

    # Studio formats its own delivery post, so the wording lives next to the
    # thing it describes rather than being duplicated here.
    tmp = os.path.join(STUDIO_DIR, ".last_build_report.json")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f)
        ok, out, err = _studio_cli(
            ["scripts/review.py", "--delivered", ".last_build_report.json"])
        message = out.strip() if ok and out.strip() else None
    except OSError as exc:
        message = None
        print("[VIDEO] could not write build report: %s" % exc)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not message:
        message = ("The build finished but I could not format the result.\n"
                   "```%s```" % json.dumps(report)[:1200])

    _post(thread_ts, message)
    video_threads.update(
        thread_ts,
        state=(video_threads.STATE_DELIVERED if report.get("ok")
               else video_threads.STATE_AWAITING_REVIEW),
        last_runtime=report.get("runtime"),
        render_folder=report.get("folder", ""),
    )
