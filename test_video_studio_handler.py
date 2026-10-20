"""Tests for the video studio Slack handler.

Almost all of this file is about one function. `classify` decides whether a
message is a new idea, a note on a script, or a greenlight, and a greenlight
spends ElevenLabs credits and six minutes of rendering. Getting it wrong in
the permissive direction means money spent on a script nobody approved, by
someone who has no idea they authorised anything.

So the cases that matter most are the near misses: messages that contain an
approving word inside a sentence that is refusing approval.
"""

import os

import pytest


@pytest.fixture(scope="module")
def handler():
    """Import the handler with a dummy Slack token.

    slack.py builds a WebClient at import time and reads SLACK_BOT_TOKEN
    directly, so a token has to exist for the import to succeed. A dummy one
    is enough: constructing the client makes no network call, and every test
    here either returns before posting or patches the post.

    An earlier version of this fixture installed a FAKE slack module into
    sys.modules instead. That works in isolation and breaks the suite: the
    fake outlives this module and the next test that does
    `from slack import post_to_engineering` gets an ImportError.
    """
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-not-real")
    import slack_video_studio_handler as h
    return h


AWAITING = "awaiting_review"


def test_new_idea_starts_a_thread(handler):
    assert handler.classify("Video on recruitment workflows?", None) == "new"


@pytest.mark.parametrize("text", [
    "go build the video",
    "Go build it",
    "build it",
    "ship it",
    "approve",
    "approved",
    "greenlight",
])
def test_explicit_greenlight(handler, text):
    assert handler.classify(text, AWAITING) == "greenlight"


@pytest.mark.parametrize("text", [
    "This looks great!",
    "I love it",
    "perfect!!",
    "nice work",
    "make the hook a question",
    "shorten the problem scene",
])
def test_enthusiasm_is_not_approval(handler, text):
    """The failure that costs money. Praise is not an instruction."""
    assert handler.classify(text, AWAITING) == "note"


@pytest.mark.parametrize("text", [
    "do not approve yet, the hook is weak",
    "don't approve this one",
    "don't build it yet",
    "do not build until Ron sees it",
    "hold off, Ron wants to see it",
    "wait, change the CTA first",
    "before you build, fix line 2",
    "not yet",
    "not approved",
])
def test_refusals_containing_approving_words(handler, text):
    """Every one of these contains 'approve' or 'build'. None is a greenlight."""
    assert handler.classify(text, AWAITING) == "note"


@pytest.mark.parametrize("state", [None, "idea"])
def test_cannot_greenlight_what_does_not_exist(handler, state):
    """Approving before a script exists means the person has not seen one."""
    assert handler.classify("go build it", state) == "new"


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_is_ignored(handler, text):
    assert handler.classify(text, AWAITING) == "ignore"


def test_case_and_punctuation_do_not_matter(handler):
    for text in ("GO BUILD THE VIDEO", "Go build the video.", "ship it!"):
        assert handler.classify(text, AWAITING) == "greenlight"


def test_feature_name_strips_links_and_mentions(handler):
    got = handler._guess_feature_name(
        "<@U123> can we do Recruitment workflows: "
        "https://support.vomevolunteer.com/portal/en/kb/articles/x"
    )
    assert "http" not in got
    assert "U123" not in got
    assert "Recruitment workflows" in got


def test_handler_ignores_other_channels(handler, monkeypatch):
    """A message in a different channel must never reach this workflow."""
    monkeypatch.setattr(handler.slack, "post_to_video_studio", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(handler.video_threads, "get", lambda t: called.append(t))
    handler.handle_video_studio_message(
        {"channel": "C_SOMETHING_ELSE", "text": "go build it", "ts": "1.0"})
    assert called == []


def test_handler_ignores_bot_messages(handler, monkeypatch):
    """Our own delivery post must not be read as a new video idea."""
    monkeypatch.setattr(handler.slack, "post_to_video_studio", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(handler.video_threads, "get", lambda t: called.append(t))
    handler.handle_video_studio_message(
        {"channel": "C0C9P2JMEEN", "bot_id": "B123",
         "text": "Recruitment workflows is built.", "ts": "1.0"})
    assert called == []


# ------------------------------------------------------------ lessons undo

@pytest.mark.parametrize("text", [
    "forget that", "Forget that.", "ok, forget that", "please forget that",
    "forget that lesson", "drop that lesson", "forget it", "forget those",
    "<@U123> forget that", "do not keep that"])
def test_forget_that_is_an_undo_in_any_state(handler, text):
    for state in (None, "awaiting_concept", "awaiting_review", "delivered"):
        assert handler.classify(text, state) == "forget"


@pytest.mark.parametrize("text", [
    "don't forget that the hook should say volunteer",
    "forget that line, use the first hook instead please",
    "I forgot that the shift block exists"])
def test_a_note_containing_the_words_is_not_an_undo(handler, text):
    assert handler.classify(text, "awaiting_review") != "forget"


# ----------------------------------- approval is the whole message, only

SAMMYS_NOTE = (
    "Here are a few notes\n\nIn the opener hook make it clear that we're "
    "sending volunteer emails or we're using volunteer email campaigns or "
    "something like that. Specify that our target audience is volunteer "
    "managers sending out volunteer communication. Vome offers a complete "
    "email integration module where they can integrate their domain, send "
    "and schedule emails from their personal account, and customise it to "
    "build complete newsletters. Then it's just simple: preview it, "
    "indicate who you're sending it to, and send. This is the concept to "
    "take forward... use that angle")


def test_notes_that_contain_make_it_are_notes(handler):
    """2026-10-17: these notes began 'make it clear that', the old matcher
    found 'make it' inside them, and the rejected script was built and paid
    for while the notes went unread."""
    assert handler.classify(SAMMYS_NOTE, "awaiting_review") == "note"
    assert not handler.is_greenlight(SAMMYS_NOTE)


@pytest.mark.parametrize("text", [
    "go build the video", "Go build the video.", "approve", "approved!",
    "ship it", "ok, go build it", "looks good, go build the video",
    "great, approve it please", "<@U123> go build the video",
    "build it now", "lgtm, ship it"])
def test_a_whole_message_approval_builds(handler, text):
    assert handler.classify(text, "awaiting_review") == "greenlight"


@pytest.mark.parametrize("text", [
    "make it shorter", "make it about volunteers", "build it from blocks",
    "approve the hook but change the ending",
    "I'd approve this if the hook said volunteer",
    "can you build it so the context comes first?",
    "ship it once the CTA says volunteer"])
def test_approval_words_inside_a_note_are_a_note(handler, text):
    assert handler.classify(text, "awaiting_review") == "note"
    assert handler.mentions_approval(text) or "make it" in text


def test_approval_while_the_script_is_being_rewritten_waits(handler):
    """Approving during a rewrite would build the version being replaced."""
    assert handler.classify("go build the video", "drafting") == \
        "script_pending"


# ------------------------------------------ scrap, start over, vague dislike

@pytest.mark.parametrize("text", [
    "scrap this", "Scrap it.", "kill this one", "discard this video",
    "ok, scrap the whole thing", "<@U1> scrap this", "cancel this please"])
def test_scrap_closes_the_thread(handler, text):
    for state in ("awaiting_concept", "awaiting_review", "delivered"):
        assert handler.classify(text, state) == "scrap"


@pytest.mark.parametrize("text", [
    "scrap the second line", "kill the music under the hook",
    "drop the shift block"])
def test_scrap_inside_a_note_is_a_note(handler, text):
    assert handler.classify(text, "awaiting_review") == "note"


@pytest.mark.parametrize("text,direction", [
    ("start over", None), ("Start over.", None), ("let's start fresh", None),
    ("start over, lead with the automation angle",
     "lead with the automation angle"),
    ("start again: make it about volunteer email campaigns",
     "make it about volunteer email campaigns"),
    ("new concepts please", None)])
def test_start_over_restarts_with_optional_direction(handler, text,
                                                     direction):
    assert handler.classify(text, "delivered") == "restart"
    m = handler.RESTART.match(text.strip().lower())
    got = (m.group("direction") or "").strip(" ,.:;-") or None
    assert got == (direction.lower() if direction else None)


@pytest.mark.parametrize("text", [
    "I don't like it", "don't like this", "I didn't like the video",
    "not good", "meh", "this isn't working"])
def test_a_vague_dislike_gets_the_options_not_a_guess(handler, text):
    assert handler.classify(text, "delivered") == "unclear"
    assert handler.classify(text, "awaiting_review") == "unclear"


def test_a_specific_dislike_is_a_note(handler):
    assert handler.classify("I don't like the hook, say volunteer emails",
                            "awaiting_review") == "note"


def test_nothing_pulls_back_a_running_build(handler):
    assert handler.classify("scrap this", "building") == "building"
    assert handler.classify("start over", "building") == "building"


def test_a_scrapped_thread_only_answers_start_over(handler):
    assert handler.classify("change the hook", "scrapped") == "scrapped"
    assert handler.classify("go build the video", "scrapped") == "scrapped"
    assert handler.classify("start over", "scrapped") == "restart"


# ------------------------------------------------ slackbot and reminders

@pytest.mark.parametrize("event", [
    {"user": "USLACKBOT", "text": "Reminder: post the email video."},
    {"user": "U1", "app_id": "A1", "text": "Weekly content check-in"},
    {"user": "U1", "bot_profile": {"name": "Workflow"}, "text": "Idea"},
    {"user": "U1", "text": "<@U01B9FQNSBU> reminder to let me know how to "
                           "get on this"},
    {"user": "U1", "text": "Reminder: send the newsletter video"},
])
def test_slackbot_and_reminders_are_never_a_video(handler, event):
    """2026-10-20: a reminder at the top level of the channel was taken as
    a new idea and the whole team ran on it at 3:58 AM."""
    assert handler.is_automated(event)


@pytest.mark.parametrize("text", [
    "New video idea: reminders that send themselves "
    "https://support.vomevolunteer.com/portal/en/kb/articles/x",
    "<@U2> can we make a video about shift reminders?",
])
def test_a_person_asking_about_reminders_is_still_a_person(handler, text):
    assert not handler.is_automated({"user": "U1", "text": text})


def test_an_automated_message_starts_nothing(handler, monkeypatch):
    started = []
    monkeypatch.setattr(handler.video_threads, "get", lambda t: None)
    monkeypatch.setattr(handler.video_threads, "start",
                        lambda *a, **k: started.append(a))
    handler.handle_video_studio_message({
        "channel": handler.slack.CHANNEL_VIDEO_STUDIO, "ts": "9.9",
        "user": "USLACKBOT", "text": "Reminder: check the video thread"})
    assert started == []
