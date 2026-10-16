"""Tests for the video studio jobs: FAQ lookup and the agent wiring.

Two things here are worth guarding.

The FAQ lookup must prefer the article's own permalink over a keyword search.
Searching for a feature name can rank a neighbouring article first, and
writing a video script from the wrong FAQ is an error nobody catches until
the video is public.

And `run_content_producer` must branch correctly on the three run outcomes.
A `blocked` result means a guard rejected the draft; treating that as a
success would publish the thing the guard existed to stop.
"""

import os

import pytest


@pytest.fixture(scope="module")
def jobs():
    """Import the jobs module with a dummy Slack token. See the note in
    test_video_studio_handler.py: faking the slack module here poisons every
    later test in the session."""
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-not-real")
    import video_studio_jobs as j
    return j


ARTICLE_URL = ("https://support.vomevolunteer.com/portal/en/kb/articles/"
               "recruitment-workflows-on-vome")


# ---------------------------------------------------------------- FAQ lookup

def test_permalink_is_preferred_over_search(jobs, monkeypatch):
    """The link names the article. Search must not get a say."""
    import database
    monkeypatch.setattr(
        database, "get_kb_article_by_permalink",
        lambda p: {"title": "Recruitment workflows", "body": "IF-THEN rules."}
        if p == "recruitment-workflows-on-vome" else None,
        raising=False)

    def _must_not_run(*a, **k):
        raise AssertionError("search ran even though a permalink was given")

    monkeypatch.setattr(database, "search_kb_articles_db", _must_not_run,
                        raising=False)

    title, body = jobs._faq_text({"faq_url": ARTICLE_URL}, "")
    assert title == "Recruitment workflows"
    assert "IF-THEN" in body


def test_url_is_found_in_a_free_text_brief(jobs, monkeypatch):
    """Nobody fills in a form; they paste a link into a sentence."""
    import database
    monkeypatch.setattr(database, "get_kb_article_by_permalink",
                        lambda p: {"title": "T", "body": "B"}, raising=False)
    title, body = jobs._faq_text(
        {}, "can we do a video on this %s please" % ARTICLE_URL)
    assert (title, body) == ("T", "B")


def test_falls_back_to_search_without_a_link(jobs, monkeypatch):
    import database
    monkeypatch.setattr(database, "get_kb_article_by_permalink",
                        lambda p: None, raising=False)
    monkeypatch.setattr(
        database, "search_kb_articles_db",
        lambda q, **k: [{"title": "Kiosk", "body": "PIN check-in."}],
        raising=False)
    title, body = jobs._faq_text({"feature_name": "Kiosk check-in"}, "")
    assert title == "Kiosk"


def test_no_faq_is_reported_not_guessed(jobs, monkeypatch):
    """Rather than draft a script from nothing, ask for the link."""
    monkeypatch.setattr(jobs, "_faq_text", lambda row, msg: ("", ""))
    out = jobs.run_content_producer("something vague", {})
    assert out["ok"] is False
    assert "link" in out["error"].lower()


# ------------------------------------------------------- the agent wiring

class _Result:
    """Stands in for vomeos.runner.AgentResult."""

    def __init__(self, status="ok", data=None, reason="nope"):
        self.status = status
        self.data = data
        self._reason = reason

    @property
    def ok(self):
        return self.status == "ok"

    @property
    def blocked(self):
        return self.status == "blocked"

    def why(self):
        return self._reason


def _patch_agent(jobs, monkeypatch, result):
    import vomeos.runner as runner
    monkeypatch.setattr(runner, "run_agent", lambda *a, **k: result)


STORYBOARD = {"id": "kiosk-check-in", "feature_name": "Kiosk check-in",
              "scenes": [{"id": "hook", "template": "hook", "lines": []}]}


def test_blocked_is_not_treated_as_success(jobs, monkeypatch):
    """A guard rejected the draft. Publishing it anyway defeats the guard."""
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    _patch_agent(jobs, monkeypatch,
                 _Result("blocked", reason="json_shape: not an object"))
    out = jobs.run_content_producer("brief", {"feature_name": "Kiosk"})
    assert out["ok"] is False
    assert "blocked" in out["error"]


def test_error_is_reported(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    _patch_agent(jobs, monkeypatch, _Result("error", reason="overloaded"))
    out = jobs.run_content_producer("brief", {"feature_name": "Kiosk"})
    assert out["ok"] is False and "overloaded" in out["error"]


def test_empty_storyboard_is_rejected(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    _patch_agent(jobs, monkeypatch, _Result("ok", data={"id": "x"}))
    out = jobs.run_content_producer("brief", {"feature_name": "Kiosk"})
    assert out["ok"] is False


def _use_local(jobs, monkeypatch, tmp_path):
    """Pretend there is a local Studio checkout, and no HTTP endpoint."""
    monkeypatch.setattr(jobs, "_studio", lambda: None)
    monkeypatch.setattr(jobs, "_local_studio", lambda: str(tmp_path))


def test_validation_failure_stops_before_the_build(jobs, monkeypatch, tmp_path):
    """Studio's validator is the real gate. A failure here costs a message;
    the same failure at build time costs credits."""
    _use_local(jobs, monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    monkeypatch.setattr(jobs, "_write_storyboard", lambda sb: sb["id"])
    monkeypatch.setattr(jobs, "_studio_json",
                        lambda *a, **k: {"ok": False,
                                         "error": "em dash in caption"})
    _patch_agent(jobs, monkeypatch, _Result("ok", data=dict(STORYBOARD)))
    out = jobs.run_content_producer("brief", {"feature_name": "Kiosk"})
    assert out["ok"] is False
    assert "em dash" in out["error"]


def test_revision_keeps_the_storyboard_id(jobs, monkeypatch, tmp_path):
    """A new id would render into a new folder and orphan every earlier
    version of this video."""
    _use_local(jobs, monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    monkeypatch.setattr(jobs, "_read_storyboard", lambda slug: dict(STORYBOARD))
    written = {}
    monkeypatch.setattr(jobs, "_write_storyboard",
                        lambda sb: written.setdefault("id", sb["id"]))
    monkeypatch.setattr(jobs, "_studio_json",
                        lambda *a, **k: {"ok": True, "estimated_runtime": 42})
    renamed = dict(STORYBOARD, id="something-else")
    _patch_agent(jobs, monkeypatch, _Result("ok", data=renamed))

    out = jobs.run_content_producer(
        "make the hook a question",
        {"storyboard": "recruitment-workflows", "feature_name": "RW"},
        revise=True)
    assert out["ok"] is True
    assert written["id"] == "recruitment-workflows"


def test_revision_without_a_storyboard_is_refused(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    monkeypatch.setattr(jobs, "_read_storyboard", lambda slug: None)
    out = jobs.run_content_producer("change it", {"storyboard": "gone"},
                                    revise=True)
    assert out["ok"] is False


# ------------------------------------------------------------- transport

def test_no_transport_is_reported_not_guessed(jobs, monkeypatch):
    """The first real Slack message failed with a Windows path on a Linux
    host: the deployed agent fell through to a local checkout that only
    exists on one laptop. It must say so instead."""
    monkeypatch.setattr(jobs, "_studio", lambda: None)
    monkeypatch.setattr(jobs, "_local_studio", lambda: "")
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    monkeypatch.setattr(jobs, "_write_storyboard", lambda sb: sb["id"])
    _patch_agent(jobs, monkeypatch, _Result("ok", data=dict(STORYBOARD)))
    out = jobs.run_content_producer("brief", {"feature_name": "Kiosk"})
    assert out["ok"] is False
    assert "VOME_STUDIO_API_URL" in out["error"]


def test_missing_id_is_derived_not_untitled(jobs):
    """"untitled" names the render folder and every output file. The first
    real run produced exactly that."""
    sb = {"feature_name": "Recruitment Workflows", "scenes": []}
    monkey = jobs._write_storyboard(sb)
    assert monkey == "recruitment-workflows"
    assert sb["id"] == "recruitment-workflows"


def test_slugify(jobs):
    assert jobs._slugify("Kiosk check-in!") == "kiosk-check-in"
    assert jobs._slugify("") == "untitled"
