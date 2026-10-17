"""
slack_video_studio_handler.py

The video studio channel, end to end.

Someone posts a feature idea. The content_producer writes a storyboard. The
script goes back into the thread for review. It is refined there over as many
rounds as it takes, costing nothing. On an explicit greenlight, Vome Studio
builds it and the result is posted into the same thread.

THE ONE RULE THIS FILE EXISTS TO ENFORCE

Everything before greenlight is free. Everything after it spends ElevenLabs
credits and about six minutes of rendering. So greenlight is matched
deterministically, by phrase, and never inferred by a model. "This looks
great" is enthusiasm. "Go build it" is an instruction. A classifier that
confuses the two burns money on a script nobody approved, and the person who
wrote the enthusiastic message has no idea they authorised anything.

For the same reason, intent is decided here in Python before any model is
called, and the model is only asked to do the thing it is actually good at:
writing and revising the script.
"""

from __future__ import annotations

import os
import re
import threading

import slack
import video_threads

# Where the work actually happens. Studio is a separate repo and will be a
# separate service; until Phase 2 gives it an HTTP endpoint it is reached
# through its CLI, which emits exactly the payload the connector documents.
STUDIO_DIR = os.environ.get(
    "VOME_STUDIO_DIR",
    r"C:\Users\sfage\Projects\Vome\vome-studio",
)

# Matched on the whole message, lowercased. Deliberately a short list of
# unambiguous instructions. Anything not on it is treated as a note on the
# script, which is the safe direction to be wrong in: the cost of asking
# again is one message, the cost of building early is real money.
GREENLIGHT = (
    "go build the video",
    "go build it",
    "build the video",
    "build it",
    "ship it",
    "approve",
    "approved",
    "greenlight",
    "green light",
    "make it",
)

# Phrases that LOOK like approval and are not. Checked first, because
# "approve" appears inside "do not approve yet" and matching that as a
# greenlight would be the worst possible failure of this file.
NOT_GREENLIGHT = (
    "don't build", "do not build", "not yet", "hold off", "wait",
    "before you build", "don't approve", "do not approve", "not approved",
)

# Stops at "|" and ">": Slack sends a pasted link as <url|label>, and
# matching to the next space captured the label and a ">" with it.
FAQ_URL = re.compile(r"https?://[^\s|<>]*(?:kb|support|faq)[^\s|<>]*", re.I)


def classify(text: str, thread_state: str | None) -> str:
    """What this message is asking for: new | note | greenlight | ignore.

    `thread_state` is what we already know about this thread, and it matters:
    the same words mean different things at different points. "Go build it"
    before a script exists is not a greenlight, it is someone who has not
    seen a script yet.
    """
    body = (text or "").strip().lower()
    if not body:
        return "ignore"

    if any(p in body for p in NOT_GREENLIGHT):
        return "note"

    is_greenlight = any(
        body == p or body.startswith(p + " ") or body.startswith(p + "!")
        or body.startswith(p + ".") or (" " + p + " ") in (" " + body + " ")
        for p in GREENLIGHT
    )
    if is_greenlight:
        # Approving something that does not exist yet.
        if thread_state in (None, video_threads.STATE_IDEA):
            return "new"
        return "greenlight"

    if thread_state is None:
        return "new"
    return "note"


def _ack(thread_ts: str, message: str) -> None:
    try:
        slack.post_to_video_studio(message, thread_ts=thread_ts)
    except Exception as exc:  # a failed post must not kill the build
        print("[VIDEO] could not post to thread %s: %s" % (thread_ts, exc))


def handle_video_studio_message(event: dict) -> None:
    """Route one message in the video studio channel.

    Returns immediately. Anything slow runs on a daemon thread, because Slack
    retries an event we do not answer within three seconds and a retried
    build would be a second set of credits spent on the same script.
    """
    channel = event.get("channel", "")
    if channel and channel != slack.CHANNEL_VIDEO_STUDIO:
        return
    if event.get("bot_id") or event.get("subtype"):
        return

    text = event.get("text", "") or ""
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or ts
    user = event.get("user", "")

    row = video_threads.get(thread_ts)
    intent = classify(text, (row or {}).get("state"))
    print("[VIDEO] %s from %s: %s" % (intent, user, text[:90]))

    if intent == "ignore":
        return

    if intent == "new":
        faq = FAQ_URL.search(text)
        video_threads.start(
            thread_ts, channel,
            feature_name=_guess_feature_name(text),
            faq_url=faq.group(0) if faq else "",
        )
        _ack(thread_ts,
             "On it. Reading the FAQ and drafting a script, back shortly "
             "with it for review. Nothing gets built until you say so.")
        threading.Thread(
            target=_draft_script, args=(thread_ts, text), daemon=True
        ).start()
        return

    if intent == "note":
        video_threads.update(thread_ts, state=video_threads.STATE_DRAFTING)
        threading.Thread(
            target=_revise_script, args=(thread_ts, text), daemon=True
        ).start()
        return

    if intent == "greenlight":
        if (row or {}).get("state") == video_threads.STATE_BUILDING:
            _ack(thread_ts, "Already building this one, hold tight.")
            return
        video_threads.update(thread_ts, state=video_threads.STATE_BUILDING)
        _ack(thread_ts,
             "Noted, working on it. Generating the voice and rendering four "
             "formats, roughly six minutes. I will post the video here when "
             "it is done.")
        threading.Thread(
            target=_build_video, args=(thread_ts,), daemon=True
        ).start()
        return


def _guess_feature_name(text: str) -> str:
    """A readable name from the first line, for the thread row.

    Only a label. The content_producer decides the real feature name from the
    FAQ, and the storyboard id is what everything downstream keys on.
    """
    first = (text or "").strip().splitlines()[0] if text.strip() else ""
    first = FAQ_URL.sub("", first)
    first = re.sub(r"<[^>]+>", "", first)        # Slack mentions and links
    return first.strip(" :-.,").strip()[:120]


# ---------------------------------------------------------------------------
# The three slow paths. Each runs on its own thread and posts its own result.
# They are thin on purpose: the work belongs to the agent and to Studio, and
# this module's job is routing and money discipline.
# ---------------------------------------------------------------------------

def _draft_script(thread_ts: str, brief: str) -> None:
    from video_studio_jobs import draft_script
    draft_script(thread_ts, brief)


def _revise_script(thread_ts: str, note: str) -> None:
    from video_studio_jobs import revise_script
    revise_script(thread_ts, note)


def _build_video(thread_ts: str) -> None:
    from video_studio_jobs import build_video
    build_video(thread_ts)
