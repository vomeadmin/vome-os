"""
slack_video_studio_handler.py

The video studio channel, end to end.

Someone posts a feature idea. The strategist positions it and the creative
director posts three concepts, numbered. A person picks one by number, or
replies with direction and gets three new ones. The copywriter
(content_producer) writes the storyboard for the pick, in its format, and
the brand editor reads it once. The script goes back into the thread for
review. It is refined there over as many rounds as it takes, costing
nothing. On an explicit greenlight, Vome Studio builds it and the result is
posted into the same thread.

A concept pick is matched the same way as a greenlight: by pattern, in
`video_concepts.match_concept_pick`, never inferred. "I like 2 but make it
shorter" is direction, not a pick.

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
import video_concepts
import video_threads

# Where the work actually happens. Studio is a separate repo and will be a
# separate service; until Phase 2 gives it an HTTP endpoint it is reached
# through its CLI, which emits exactly the payload the connector documents.
STUDIO_DIR = os.environ.get(
    "VOME_STUDIO_DIR",
    r"C:\Users\sfage\Projects\Vome\vome-studio",
)

# Matched on the WHOLE message, lowercased, after a little politeness is
# trimmed ("ok, go build the video please"). Deliberately a short list of
# unambiguous instructions. Anything else is a note on the script, which is
# the safe direction to be wrong in: the cost of asking again is one
# message, the cost of building early is real money.
#
# It used to match these phrases anywhere in a message, and "make it" was
# on the list. On 2026-10-17 Sammy posted three paragraphs of notes that
# began "In the opener hook make it clear that...", and the bot built the
# rejected script, spent the credits, and never read the notes.
GREENLIGHT = (
    "go build the video",
    "go build it",
    "go build",
    "build the video",
    "build it",
    "ship it",
    "approve",
    "approved",
    "approve it",
    "approve the script",
    "greenlight",
    "green light",
)

# Politeness around an approval, trimmed before the whole-message match.
_LEAD = re.compile(
    r"^(?:(?:ok|okay|great|perfect|yes|yep|lgtm|looks good|love it|"
    r"all good|good to go|thanks|thank you|please|let'?s)[,.!]*\s+)+")
_TAIL = re.compile(r"(?:[,.!]*\s+(?:please|thanks|thank you|now))+[.!]*$")


def is_greenlight(text: str) -> bool:
    """True only when the whole message is an approval."""
    body = re.sub(r"<[^>]+>", " ", text or "")        # Slack mentions
    body = re.sub(r"\s+", " ", body).strip().lower()
    body = _LEAD.sub("", body)
    body = _TAIL.sub("", body)
    return body.strip(" ,.!") in GREENLIGHT


def mentions_approval(text: str) -> bool:
    """An approval word inside a longer message: it is a note, and the
    person is told so, so nobody wonders why nothing was built."""
    body = " %s " % re.sub(r"[^a-z' ]+", " ", (text or "").lower())
    return any(" %s " % p in body for p in GREENLIGHT)

# Phrases that LOOK like approval and are not. Checked first, because
# "approve" appears inside "do not approve yet" and matching that as a
# greenlight would be the worst possible failure of this file.
NOT_GREENLIGHT = (
    "don't build", "do not build", "not yet", "hold off", "wait",
    "before you build", "don't approve", "do not approve", "not approved",
)

# Undo the lesson this thread just taught. The whole message must be the
# instruction, optionally with "please" or a mention, so a note that merely
# contains the words is still a note.
FORGET = re.compile(
    r"^(?:<[^>]+>\s*)?(?:ok,?\s+|okay,?\s+|please\s+)?"
    r"(?:forget that|forget it|forget those|drop that lesson|"
    r"undo that lesson|scratch that lesson|don't keep that|do not keep that)"
    r"(?:\s+lesson)?(?:,?\s+please)?[.!]*$")

# Stops at "|" and ">": Slack sends a pasted link as <url|label>, and
# matching to the next space captured the label and a ">" with it.
FAQ_URL = re.compile(r"https?://[^\s|<>]*(?:kb|support|faq)[^\s|<>]*", re.I)


def classify(text: str, thread_state: str | None) -> str:
    """What this message is asking for.

    new | pick | direction | busy | pick_first | note | greenlight | forget
    | script_pending | ignore

    `thread_state` is what we already know about this thread, and it matters:
    the same words mean different things at different points. "Go build it"
    before a script exists is not a greenlight, it is someone who has not
    seen a script yet.

    While concepts await a choice, a message is a pick only if
    `match_concept_pick` says so. A bare greenlight there gets a nudge to
    pick first, because there is no script to build and regenerating
    concepts from "go build it" would help nobody. Everything else is
    direction for new concepts, including praise that names a number.
    """
    body = (text or "").strip().lower()
    if not body:
        return "ignore"

    # Undoing a lesson, in any state. Matched on the whole message so "don't
    # forget that the hook..." is still a note, not an undo.
    if FORGET.match(body):
        return "forget"

    if thread_state == video_threads.STATE_AWAITING_CONCEPT:
        if video_concepts.match_concept_pick(text) is not None:
            return "pick"
        if is_greenlight(text):
            return "pick_first"
        return "direction"

    if thread_state == video_threads.STATE_CONCEPTING:
        return "busy"

    if any(p in body for p in NOT_GREENLIGHT):
        return "note"

    if is_greenlight(text):
        # Approving something that does not exist yet.
        if thread_state in (None, video_threads.STATE_IDEA):
            return "new"
        # A script is being rewritten: the approval would build the version
        # being replaced. The person approves the one they have read.
        if thread_state == video_threads.STATE_DRAFTING:
            return "script_pending"
        return "greenlight"

    # No thread yet, or a thread where nothing came of the first attempt
    # (concepts failed, so it is still an idea): a nudge starts it again.
    if thread_state in (None, video_threads.STATE_IDEA):
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

    if intent == "forget":
        import video_lessons
        try:
            dropped = video_lessons.retire_latest(thread_ts)
        except Exception as exc:
            print("[VIDEO] could not retire lessons in %s: %s"
                  % (thread_ts, exc))
            dropped = None
        if dropped is None:
            _ack(thread_ts, "I could not reach the lessons just now, so "
                 "nothing changed. Try again in a minute.")
        elif dropped:
            _ack(thread_ts, "Dropped, no future video will follow it:\n%s"
                 % "\n".join("• %s" % r for r in dropped))
        else:
            _ack(thread_ts, "There is no lesson from this thread to drop.")
        return

    if intent == "new":
        faq = FAQ_URL.search(text)
        video_threads.start(
            thread_ts, channel,
            feature_name=_guess_feature_name(text),
            faq_url=faq.group(0) if faq else "",
        )
        if row and faq:
            # A retry in a thread whose first attempt found nothing: the
            # link posted now is the one to read.
            video_threads.update(thread_ts, faq_url=faq.group(0))
        video_threads.update(thread_ts, state=video_threads.STATE_CONCEPTING)
        _ack(thread_ts,
             "On it. Reading the FAQ and working out three ways to tell "
             "this one, back shortly with concepts to pick from. Nothing "
             "gets built until you say so.")
        threading.Thread(
            target=_develop_concepts, args=(thread_ts, text), daemon=True
        ).start()
        return

    if intent == "busy":
        _ack(thread_ts,
             "Still working on the concepts for this one. Once they are "
             "posted, reply with a number to pick one, or with direction.")
        return

    if intent == "pick_first":
        _ack(thread_ts,
             "There is no script to build yet. Reply with 1, 2 or 3 to pick "
             "a concept first, and I will write it up for review.")
        return

    if intent == "pick":
        number = video_concepts.match_concept_pick(text)
        concepts = video_concepts.load_concepts(row or {})
        title = (concepts[number - 1].get("title", "")
                 if 1 <= number <= len(concepts) else "")
        video_threads.update(thread_ts, state=video_threads.STATE_DRAFTING)
        _ack(thread_ts,
             "Going with concept %d%s. Writing the script now, back shortly "
             "with it for review." % (number, ", *%s*" % title if title
                                      else ""))
        threading.Thread(
            target=_draft_from_concept, args=(thread_ts, number), daemon=True
        ).start()
        return

    if intent == "direction":
        video_threads.update(thread_ts, state=video_threads.STATE_CONCEPTING)
        _ack(thread_ts,
             "Taking that as direction. Back shortly with three new "
             "concepts.")
        threading.Thread(
            target=_develop_concepts, args=(thread_ts, text, text),
            daemon=True,
        ).start()
        return

    if intent == "script_pending":
        _ack(thread_ts,
             "Hold on, I am still updating the script. Approve it once you "
             "have read the new version, and nothing is built before then.")
        return

    if intent == "note":
        if mentions_approval(text):
            # Approval words inside notes: say plainly which way it went.
            _ack(thread_ts,
                 "I read this as notes, not approval, so nothing is being "
                 "built. Updating the script now. When you are happy with it, "
                 "reply with just: go build the video")
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
# The slow paths. Each runs on its own thread and posts its own result.
# They are thin on purpose: the work belongs to the agents and to Studio, and
# this module's job is routing and money discipline.
# ---------------------------------------------------------------------------

def _develop_concepts(thread_ts: str, brief: str, direction: str = "") -> None:
    from video_studio_jobs import develop_concepts
    develop_concepts(thread_ts, brief, direction=direction)


def _draft_from_concept(thread_ts: str, number: int) -> None:
    from video_studio_jobs import draft_from_concept
    draft_from_concept(thread_ts, number)


def _revise_script(thread_ts: str, note: str) -> None:
    from video_studio_jobs import revise_script
    revise_script(thread_ts, note)


def _build_video(thread_ts: str) -> None:
    from video_studio_jobs import build_video
    build_video(thread_ts)
