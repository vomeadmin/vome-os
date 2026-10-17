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
