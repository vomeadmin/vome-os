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


CONTRACT = {"schema": {}, "example": {"id": "x", "scenes": []},
            "required_cues": ["card_in"], "templates": ["hook"]}


@pytest.fixture(autouse=True)
def _contract(jobs, monkeypatch):
    """Studio's contract, so the agent wiring tests reach the agent. And no
    database: unit tests must never ask production which ids are taken."""
    monkeypatch.setattr(jobs, "_contract", lambda: dict(CONTRACT))
    monkeypatch.setattr(jobs.video_threads, "slugs_in_use",
                        lambda exclude="": set())


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


def test_a_link_that_misses_is_not_replaced_by_search(jobs, monkeypatch):
    """A new article is not in the nightly copy yet. Searching instead
    drafted from "How do the Vome API and webhooks work?" on the first live
    run, for an article about email formats."""
    import database
    monkeypatch.setattr(database, "get_kb_article_by_permalink",
                        lambda p: None, raising=False)
    monkeypatch.setattr(
        database, "search_kb_articles_db",
        lambda q, **k: pytest.fail("searched despite a pasted link"),
        raising=False)
    assert jobs._faq_text({"faq_url": ARTICLE_URL}, "") == ("", "")
    out = jobs.run_content_producer("video please", {"faq_url": ARTICLE_URL})
    assert out["ok"] is False and "nightly" in out["error"]


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


# ------------------------------------------- deployed: no checkout to lean on

class _Conn:
    """Stands in for the VomeStudio connector."""

    def __init__(self, validate=None):
        self.built = []
        self._validate = validate or {"ok": True, "estimated_runtime": 40,
                                      "review": "*Kiosk check-in*\nthe script"}

    def validate(self, sb):
        from types import SimpleNamespace
        return SimpleNamespace(ok=True, data=self._validate, error="")

    def build(self, sb, engine="elevenlabs"):
        from types import SimpleNamespace
        self.built.append(sb)
        return SimpleNamespace(ok=False, data=None, error="stop here")


def _use_http(jobs, monkeypatch, conn):
    monkeypatch.setattr(jobs, "_studio", lambda: conn)
    monkeypatch.setattr(jobs, "_local_studio", lambda: "")


def test_saved_copy_is_read_before_any_checkout(jobs, monkeypatch):
    """Deployed, the thread's own copy is the only one that exists."""
    import json
    monkeypatch.setattr(jobs, "_local_studio", lambda: "")
    row = {"storyboard": "kiosk-check-in",
           "storyboard_json": json.dumps(STORYBOARD)}
    assert jobs._read_storyboard(row) == STORYBOARD
    assert jobs._read_storyboard({"storyboard": "kiosk-check-in"}) is None


def test_draft_is_remembered_on_the_thread(jobs, monkeypatch):
    """Without this, the first revision on Railway said the storyboard file
    was missing, because the only copy was on Studio's wiped disk."""
    _use_http(jobs, monkeypatch, _Conn())
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    saved = {}
    monkeypatch.setattr(jobs.video_threads, "update",
                        lambda t, **f: saved.update(f, thread=t))
    _patch_agent(jobs, monkeypatch, _Result("ok", data=dict(STORYBOARD)))
    out = jobs.run_content_producer("brief", {"thread_ts": "123.4"})
    assert out["ok"] is True
    assert saved["thread"] == "123.4"
    assert '"scenes"' in saved["storyboard_json"]
    assert out["review"].startswith("*Kiosk check-in*")


def test_review_text_is_posted_without_a_checkout(jobs, monkeypatch):
    """review.py only runs where Studio is checked out. Deployed, the text
    comes back from validate and must be used as is."""
    posted = []
    monkeypatch.setattr(jobs, "_post", lambda t, m: posted.append(m))
    monkeypatch.setattr(jobs, "_studio_cli",
                        lambda *a, **k: (False, "", "must not run"))
    monkeypatch.setattr(jobs.video_threads, "bump_round", lambda t: 1)
    monkeypatch.setattr(jobs.video_threads, "update", lambda *a, **k: None)
    jobs._post_script_for_review("1.2", "First pass.", review="the script")
    assert "the script" in posted[0] and "must not run" not in posted[0]


def test_build_sends_the_whole_storyboard(jobs, monkeypatch):
    """It once sent {"id": slug}: a build with no words in it."""
    import json
    conn = _Conn()
    _use_http(jobs, monkeypatch, conn)
    monkeypatch.setattr(jobs.video_threads, "get", lambda t: {
        "thread_ts": t, "storyboard": "kiosk-check-in",
        "storyboard_json": json.dumps(STORYBOARD)})
    monkeypatch.setattr(jobs.video_threads, "update", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_post", lambda *a: None)
    jobs.build_video("1.2")
    assert conn.built == [STORYBOARD]


def test_build_without_a_storyboard_sends_nothing(jobs, monkeypatch):
    """Lost script: say so. Never send a stub that spends credits or
    overwrites the approved words."""
    conn = _Conn()
    _use_http(jobs, monkeypatch, conn)
    monkeypatch.setattr(jobs.video_threads, "get",
                        lambda t: {"thread_ts": t, "storyboard": "gone"})
    monkeypatch.setattr(jobs.video_threads, "update", lambda *a, **k: None)
    posted = []
    monkeypatch.setattr(jobs, "_post", lambda t, m: posted.append(m))
    jobs.build_video("1.2")
    assert conn.built == []
    assert posted and "lost the script" in posted[0]


# ------------------------------------------------------------ the contract

def test_agent_is_given_the_real_format(jobs, monkeypatch):
    """Told to "match Studio's schema" with no schema, the first live run
    guessed every field name. It must be handed the contract."""
    _use_http(jobs, monkeypatch, _Conn())
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    monkeypatch.setattr(jobs.video_threads, "update", lambda *a, **k: None)
    seen = {}

    def run_agent(name, context, **k):
        seen.update(context)
        return _Result("ok", data=dict(STORYBOARD))
    import vomeos.runner as runner
    monkeypatch.setattr(runner, "run_agent", run_agent)
    jobs.run_content_producer("brief", {"thread_ts": "1.2"})
    assert '"required_cues"' in seen["storyboard_contract"]


def test_no_contract_means_no_guessing(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_contract", lambda: None)
    monkeypatch.setattr(jobs, "_faq_text", lambda r, m: ("T", "body"))
    import vomeos.runner as runner
    monkeypatch.setattr(runner, "run_agent",
                        lambda *a, **k: pytest.fail("agent must not run"))
    out = jobs.run_content_producer("brief", {"feature_name": "Kiosk"})
    assert out["ok"] is False and "format" in out["error"]


# ---------------------------------------------------------- slack link markup

SLACK_MSG = ("I have a new video idea from a featrure we just went live with."
             "\n\n<https://support.vomevolunteer.com/portal/en/kb/articles/"
             "choosing-an-email-format-basic-designed-custom-html|"
             "support.vomevolunteer.com/portal/…/choosing-an-email-format>")


def test_slack_link_markup_is_not_part_of_the_url(jobs):
    """Slack sends <url|label>. The first live run stored the label and a
    trailing ">" as part of the URL, so the permalink lookup missed."""
    url = jobs.FAQ_URL.search(SLACK_MSG).group(0)
    assert url.endswith("choosing-an-email-format-basic-designed-custom-html")


def test_stored_url_with_markup_still_finds_the_article(jobs, monkeypatch):
    import database
    seen = []
    monkeypatch.setattr(database, "get_kb_article_by_permalink",
                        lambda p: seen.append(p) or {"title": "T", "body": "b"})
    bad = ("https://support.vomevolunteer.com/portal/en/kb/articles/"
           "choosing-an-email-format|support.vomevolunteer.com/x>")
    jobs._faq_text({"faq_url": bad}, "")
    assert seen == ["choosing-an-email-format"]


def test_new_draft_never_takes_an_existing_videos_id(jobs, monkeypatch):
    """Drafting the pilot's feature, the agent chose its id and the save
    overwrote the approved storyboard."""
    monkeypatch.setattr(jobs, "_local_studio", lambda: "")
    monkeypatch.setattr(jobs.video_threads, "slugs_in_use",
                        lambda exclude="": {"kiosk-check-in"})
    sb = dict(STORYBOARD)
    jobs._claim_fresh_id(sb, CONTRACT, "1.2")
    assert sb["id"].startswith("kiosk-check-in-") and sb["id"] != "kiosk-check-in"
    assert "already exists" in sb["review_notes"]

    pilot = dict(STORYBOARD, id="x")          # the contract example's id
    jobs._claim_fresh_id(pilot, CONTRACT, "1.2")
    assert pilot["id"] != "x"

    free = dict(STORYBOARD, id="brand-new")
    jobs._claim_fresh_id(free, CONTRACT, "1.2")
    assert free["id"] == "brand-new" and "review_notes" not in free
