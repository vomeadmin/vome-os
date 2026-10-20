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

# Close the thread. The whole message, so a note mentioning "scrap the
# second line" is still a note.
SCRAP = re.compile(
    r"^(?:<[^>]+>\s*)?(?:ok,?\s+|okay,?\s+|please\s+|let'?s\s+)?"
    r"(?:scrap|kill|discard|abandon|bin|drop|cancel)\s+"
    r"(?:it|this|this one|that|that one|this video|the video|this thread|"
    r"the whole thing|all of it)(?:,?\s+please)?[.!]*$")

# Fresh research and three new concepts, same feature. Anything after the
# phrase is direction: "start over, lead with automations".
RESTART = re.compile(
    r"^(?:<[^>]+>\s*)?(?:ok,?\s+|okay,?\s+|please\s+|let'?s\s+)?"
    r"(?:start (?:over|again|fresh|from scratch)|restart|from scratch|"
    r"new concepts|back to (?:the )?concepts)"
    r"(?:\s*(?:[,.:;!-]+\s*|\s+)(?:please[,.]?\s*)?(?:with\s+|and\s+)?"
    r"(?P<direction>.*))?$", re.S)

# A dislike with nothing to act on. Guessing a revision from it helps
# nobody, so the person is shown their three options instead.
VAGUE_DISLIKE = re.compile(
    r"^(?:<[^>]+>\s*)?(?:hmm+,?\s+|honestly,?\s+)?"
    r"(?:i\s+)?(?:don'?t|do not|didn'?t|did not)\s+(?:really\s+)?"
    r"(?:like|love)\s+(?:it|this|that|this one|the video|the script)"
    r"(?:\s+(?:much|at all|very much))?[.!]*$"
    r"|^(?:not (?:good|great|it|loving it)|meh|nope|no|this is bad|"
    r"this isn'?t (?:it|working|good)|it'?s not working)[.!]*$")

# Go ahead with no help centre article, on what the thread already says.
# The whole message, like a greenlight: "continue with the hook idea" is
# more detail, not consent.
CONTINUE = re.compile(
    r"^(?:<[^>]+>\s*)?(?:ok,?\s+|okay,?\s+|yes,?\s+|yep,?\s+|sure,?\s+)?"
    r"(?:continue|carry on|go ahead|proceed|yes|yep|"
    r"go with (?:this|that|what'?s here)|that'?s (?:all|everything))"
    r"(?:\s+with (?:this|that|what'?s here|only this|"
    r"(?:only )?the context(?: (?:here|provided))?))?"
    r"(?:,?\s+please)?[.!]*$")

SLACKBOT_USER = "USLACKBOT"

# A reminder's text, after any leading mentions: "Reminder: ...", "reminder
# to ...". Slack's own reminders, and anyone's "reminder for @x" nudges,
# are about people, not videos.
REMINDER = re.compile(r"^(?:<[^>]+>[\s,:]*)*reminder\b", re.I)


def is_automated(event: dict) -> bool:
    """True for anything that is not a person asking for a video."""
    if (event.get("user") or "") == SLACKBOT_USER:
        return True
    # Workflow Builder steps and app posts can arrive without a bot_id.
    if event.get("bot_profile") or event.get("app_id"):
        return True
    return bool(REMINDER.match((event.get("text") or "").strip()))


# Stops at "|" and ">": Slack sends a pasted link as <url|label>, and
# matching to the next space captured the label and a ">" with it.
FAQ_URL = re.compile(r"https?://[^\s|<>]*(?:kb|support|faq)[^\s|<>]*", re.I)


def classify(text: str, thread_state: str | None) -> str:
    """What this message is asking for.

    new | context | context_link | continue | need_context | pick
    | direction | busy | pick_first | note | greenlight | forget
    | script_pending | scrap | restart | unclear | scrapped | building
    | ignore

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

    if thread_state == video_threads.STATE_SCRAPPED:
        return "restart" if RESTART.match(body) else "scrapped"

    # A build is spending credits and cannot be pulled back; anything that
    # would change the script waits for it.
    if thread_state == video_threads.STATE_BUILDING and (
            SCRAP.match(body) or RESTART.match(body)):
        return "building"

    if SCRAP.match(body):
        return "scrap"
    if RESTART.match(body) and thread_state not in (
            None, video_threads.STATE_IDEA, video_threads.STATE_CONCEPTING,
            video_threads.STATE_AWAITING_CONTEXT):
        return "restart"
    if VAGUE_DISLIKE.match(body) and thread_state in (
            video_threads.STATE_AWAITING_REVIEW,
            video_threads.STATE_DELIVERED,
            video_threads.STATE_AWAITING_CONCEPT):
        return "unclear"

    # Waiting for a help centre link. Nothing has been spent: a link starts
    # the team, "continue" starts it on the thread alone, and anything else
    # is more detail about the feature.
    if thread_state == video_threads.STATE_AWAITING_CONTEXT:
        if FAQ_URL.search(text or ""):
            return "context_link"
        if CONTINUE.match(body):
            return "continue"
        if is_greenlight(text):
            return "need_context"
        return "context"

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
    # Slackbot and reminders are never a video request. On 2026-10-20 a
    # reminder ("@Sammy reminder to let me know how to get on this") arrived
    # at the top level, carried no bot_id, was taken as a new idea, and the
    # whole team ran on it at 3:58 AM.
    if is_automated(event):
        print("[VIDEO] ignoring automated message: %s"
              % (event.get("text") or "")[:80])
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
        started_by = (row or {}).get("started_by") or user
        if not faq and not (row or {}).get("faq_url"):
            # No article to work from. Ask before any model runs: the team
            # costs about $2 a round, and guessing the article is how a
            # reminder became concepts about sequence steps.
            video_threads.update(
                thread_ts, state=video_threads.STATE_AWAITING_CONTEXT,
                brief=_add_to_brief(row, text), started_by=started_by)
            _ack(thread_ts, _ask_for_context(started_by))
            return
        video_threads.update(thread_ts, state=video_threads.STATE_CONCEPTING,
                             brief=text if not (row or {}).get("brief")
                             else row["brief"],
                             started_by=started_by)
        _ack(thread_ts,
             "On it. Reading the FAQ and working out three ways to tell "
             "this one, back shortly with concepts to pick from. Nothing "
             "gets built until you say so.")
        threading.Thread(
            target=_develop_concepts, args=(thread_ts, text), daemon=True
        ).start()
        return

    if intent == "context":
        video_threads.update(thread_ts, brief=_add_to_brief(row, text))
        _ack(thread_ts,
             "Added to the brief. Do you want to continue to build a video "
             "with the context provided so far? Reply *continue*, or keep "
             "adding detail, or paste the help centre link.")
        return

    if intent == "need_context":
        _ack(thread_ts,
             "Nothing to build yet. Paste the help centre link, tell me more "
             "about the feature, or reply *continue* to start the concepts "
             "with only what is in this thread.")
        return

    if intent in ("context_link", "continue"):
        if intent == "context_link":
            brief = _add_to_brief(row, text)
            video_threads.update(thread_ts,
                                 faq_url=FAQ_URL.search(text).group(0),
                                 brief_only=False)
            lead = "Reading the article and working"
        else:
            brief = (row or {}).get("brief") or text
            video_threads.update(thread_ts, brief_only=True)
            lead = ("Going ahead with only the context in this thread. "
                    "Anything the team had to assume will be flagged on the "
                    "script. Working")
        video_threads.update(thread_ts, state=video_threads.STATE_CONCEPTING,
                             brief=brief)
        _ack(thread_ts,
             "%s out three ways to tell this one, back shortly with concepts "
             "to pick from. Nothing gets built until you say so."
             % lead)
        threading.Thread(
            target=_develop_concepts, args=(thread_ts, brief), daemon=True
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

    if intent == "scrap":
        video_threads.update(thread_ts, state=video_threads.STATE_SCRAPPED)
        _ack(thread_ts,
             "Scrapped. Nothing more happens in this thread, and it will not "
             "count as an earlier video, so its hook is free to use again. "
             "Any files already made stay in storage. Start a new thread for "
             "a fresh video, or reply *start over* to bring this one back "
             "with new concepts.")
        return

    if intent == "scrapped":
        _ack(thread_ts,
             "This thread was scrapped. Reply *start over* to bring it back "
             "with new concepts, or start a new thread.")
        return

    if intent == "building":
        _ack(thread_ts,
             "The build is already running and cannot be pulled back. Once "
             "it is posted, reply *start over* or *scrap this*.")
        return

    if intent == "unclear":
        _ack(thread_ts,
             "Happy to change it. Three ways to go:\n"
             "• Tell me what to change (the angle, a line, the hook) and I "
             "will rework this script.\n"
             "• Reply *start over* (optionally with direction, like "
             "_start over, lead with automations_) for fresh research and "
             "three new concepts.\n"
             "• Reply *scrap this* to close the thread.")
        return

    if intent == "restart":
        m = RESTART.match((text or "").strip().lower()) or None
        direction = ""
        if m and m.group("direction"):
            # The original casing, from the same offset.
            start = len(text.strip()) - len(m.group("direction"))
            direction = text.strip()[start:].strip(" ,.:;-")
        video_threads.update(thread_ts, state=video_threads.STATE_CONCEPTING)
        _ack(thread_ts,
             "Starting over: fresh research and three new concepts%s. The "
             "previous script%s is set aside." % (
                 ", taking your direction" if direction else "",
                 " and video" if (row or {}).get("render_folder") else ""))
        threading.Thread(
            target=_restart, args=(thread_ts, direction), daemon=True
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


def _add_to_brief(row: dict | None, text: str) -> str:
    """The thread's brief with one more message on the end."""
    brief = ((row or {}).get("brief") or "").strip()
    return (brief + "\n\n" + text.strip()) if brief else text.strip()


def _ask_for_context(user: str) -> str:
    """The question for a thread with no help centre link."""
    who = "<@%s> " % user if user else ""
    return (
        "%sBefore the team starts: a help centre article makes for a much "
        "stronger video. Paste the link to the article for this feature if "
        "there is one.\n\n"
        "If there is no article yet, tell me as much as you can about the "
        "feature: what it does, who it is for, where it lives in Vome, and "
        "what is new.\n\n"
        "Or, do you want to continue to build a video with only the context "
        "provided here? Reply *continue*. Nothing runs until you answer."
        % who)


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


def _restart(thread_ts: str, direction: str) -> None:
    from video_studio_jobs import restart_concepts
    restart_concepts(thread_ts, direction)


def _revise_script(thread_ts: str, note: str) -> None:
    from video_studio_jobs import revise_script
    revise_script(thread_ts, note)


def _build_video(thread_ts: str) -> None:
    from video_studio_jobs import build_video
    build_video(thread_ts)
