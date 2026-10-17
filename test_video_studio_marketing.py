"""Tests for the video marketing team wiring.

What is guarded here, in order of how much it would hurt:

1. A format Studio cannot build is never offered, and a format Studio says
   is unavailable is reported in the thread, never silently swapped for
   another one.
2. The copywriter is given the chosen concept and positioning, and the
   contract for the concept's own format.
3. The brand editor's edit is accepted only on checked terms: it never
   changes the id or the format, and an edit that restructures the scenes
   is set aside rather than trusted.
4. The thread moves through the right states, so a pick, direction and a
   greenlight each mean what they should.

No test here reaches the database, Slack, Studio or a model. `run_agent` is
replaced by a fake that answers per agent and records what it was given.
"""

import json
import os
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def jobs():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-not-real")
    import video_studio_jobs as j
    return j


@pytest.fixture(scope="module")
def handler():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-not-real")
    import slack_video_studio_handler as h
    return h


# ------------------------------------------------------------------ fakes

CATALOGUE = [
    {"name": "explainer", "title": "Explainer", "seconds": "40-50",
     "best_for": "rules", "description": "d", "ready": True},
    {"name": "reveal", "title": "Reveal", "seconds": "20-30",
     "best_for": "looks", "description": "d", "ready": True},
    {"name": "trio", "title": "Trio", "seconds": "30-40",
     "best_for": "choices", "description": "d", "ready": False},
]

SCOUT = {"parent_capability": "Sending email to volunteers from Vome",
         "is_new": True, "new_evidence": "just went live",
         "queries": ["send emails from your own domain",
                     "automated emails"],
         "related_features": []}

POSITIONING = {"audience": "Admins who already email users from Vome.",
               "whats_new": "How emails look.",
               "job_to_be_done": "Send a considered update.",
               "single_claim": "Your Vome emails can now look like your "
                               "organization.",
               "proof_points": ["Designed needs no code."],
               "must_not_say": ["prices"], "tone_notes": "Premium."}


def _concept(n, fmt="explainer"):
    return {"title": "Concept %d" % n, "format": fmt, "angle": "angle %d" % n,
            "hook": "Volunteer hook %d." % n, "beats": ["a", "b", "c"],
            "why_it_works": "w", "purpose": "p", "seconds": 40}


CONCEPTS = [_concept(1, "reveal"), _concept(2), _concept(3)]

EXPLAINER_CONTRACT = {"format": "explainer", "schema": {},
                      "example": {"id": "pilot", "scenes": []},
                      "required_cues": ["card_in"], "templates": ["hook"]}
REVEAL_CONTRACT = {"format": "reveal", "schema": {},
                   "example": {"id": "reveal-pilot", "format": "reveal",
                               "scenes": []},
                   "required_cues": ["cut"], "templates": ["before"]}

STORYBOARD = {"id": "email-formats", "feature_name": "Email formats",
              "voice": {"name": "Maya"}, "pacing": {"gap": 0.5},
              "scenes": [{"id": "hook", "template": "hook",
                          "lines": [{"vo": "Hook 2."}]},
                         {"id": "cta", "template": "cta",
                          "lines": [{"vo": "Try it in Vome."}]}]}


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


class Agents:
    """A fake run_agent that answers per agent and remembers its calls."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, name, context, **kwargs):
        self.calls.append((name, dict(context)))
        answer = self.answers.get(name.split(".")[-1])
        if answer is None:
            raise AssertionError("unexpected agent call: %s" % name)
        if callable(answer):
            answer = answer(context)
        if isinstance(answer, _Result):
            return answer
        return _Result("ok", data=json.loads(json.dumps(answer)))

    def names(self):
        return [n.split(".")[-1] for n, _ in self.calls]

    def context(self, short):
        return next(c for n, c in self.calls if n.endswith("." + short))


def _install(monkeypatch, agents):
    import vomeos.runner as runner
    monkeypatch.setattr(runner, "run_agent", agents)


class Studio:
    """Stands in for the VomeStudio connector."""

    def __init__(self, formats=None, formats_status=200, contracts=None,
                 validate=None):
        self._formats = formats if formats is not None else CATALOGUE
        self._formats_status = formats_status
        self._contracts = contracts if contracts is not None else {
            "explainer": EXPLAINER_CONTRACT, "reveal": REVEAL_CONTRACT}
        self._validate = validate or {"ok": True, "estimated_runtime": 24.6,
                                      "review": "*Email formats*\nscript"}
        self.contract_requests = []
        self.validated = []

    def formats(self):
        if self._formats_status != 200:
            return SimpleNamespace(ok=False, data=None,
                                   status_code=self._formats_status,
                                   error="HTTP %d" % self._formats_status)
        return SimpleNamespace(ok=True, data={"formats": self._formats},
                               status_code=200, error="")

    def contract(self, format="explainer"):
        self.contract_requests.append(format)
        if format in self._contracts:
            return SimpleNamespace(ok=True, data=self._contracts[format],
                                   status_code=200, error="")
        return SimpleNamespace(ok=False, data=None, status_code=404,
                               error="HTTP 404: unknown format")

    def validate(self, sb):
        self.validated.append(json.loads(json.dumps(sb)))
        return SimpleNamespace(ok=True, data=self._validate, error="")


@pytest.fixture
def env(jobs, monkeypatch):
    """HTTP Studio, a found FAQ, no database, and a record of every update
    and post."""
    state = SimpleNamespace(updates=[], posts=[], studio=Studio(), row={})
    monkeypatch.setattr(jobs, "_studio", lambda: state.studio)
    monkeypatch.setattr(jobs, "_local_studio", lambda: "")
    state.research = []
    state.captured = []
    state.real_capture = jobs.capture_lessons
    monkeypatch.setattr(jobs.video_lessons, 'render_for',
                        lambda role: '- [1] Say volunteer.')
    monkeypatch.setattr(jobs, 'capture_lessons',
                        lambda t, m, stage, row=None:
                        state.captured.append((m, stage)))
    monkeypatch.setattr(
        jobs, "_related_articles",
        lambda queries, exclude_title="": state.research.append(
            list(queries)) or "### Send from your own domain\nIt works.")
    monkeypatch.setattr(jobs, "_faq_text",
                        lambda row, msg: ("Choosing an email format",
                                          "Basic, Designed or Custom HTML."))
    monkeypatch.setattr(jobs.video_threads, "get", lambda t: dict(state.row))
    monkeypatch.setattr(jobs.video_threads, "update",
                        lambda t, **f: state.updates.append(f))
    monkeypatch.setattr(jobs.video_threads, "slugs_in_use",
                        lambda exclude="": set())
    monkeypatch.setattr(jobs.video_threads, "recent_videos",
                        lambda exclude="", limit=8: [])
    monkeypatch.setattr(jobs.video_threads, "bump_round", lambda t: 1)
    monkeypatch.setattr(jobs, "_post", lambda t, m: state.posts.append(m))
    return state


def _merged(updates):
    out = {}
    for u in updates:
        out.update(u)
    return out


# -------------------------------------------------------------- connector

def test_connector_formats_and_contract_paths(monkeypatch):
    """formats() and contract(format) are GETs with no body. The explainer
    contract carries no query string, so an older Studio answers it the
    same way it always has."""
    from vomeos.integrations import base
    from vomeos.integrations.vome_studio import VomeStudio
    monkeypatch.setenv("VOME_STUDIO_API_URL", "https://studio.example")
    monkeypatch.setenv("VOME_STUDIO_API_KEY", "k")
    seen = []

    class Resp:
        status_code = 200
        headers = {}
        text = "{}"

        def json(self):
            return {"formats": []}

    def request(method, url, **kw):
        seen.append((method, url, kw.get("params"), kw.get("json")))
        return Resp()

    monkeypatch.setattr(base.httpx, "request", request)
    conn = VomeStudio(base_url="https://studio.example")
    assert conn.formats().ok
    conn.contract()
    conn.contract("explainer")
    conn.contract("reveal")
    assert seen == [
        ("GET", "https://studio.example/v1/formats", None, None),
        ("GET", "https://studio.example/v1/contract", None, None),
        ("GET", "https://studio.example/v1/contract", None, None),
        ("GET", "https://studio.example/v1/contract", {"format": "reveal"},
         None),
    ]


def test_connector_reports_a_404_rather_than_raising(monkeypatch):
    from vomeos.integrations import base
    from vomeos.integrations.vome_studio import VomeStudio
    monkeypatch.setenv("VOME_STUDIO_API_KEY", "k")

    class Resp:
        status_code = 404
        headers = {}
        text = "unknown format"

    monkeypatch.setattr(base.httpx, "request", lambda *a, **k: Resp())
    res = VomeStudio(base_url="https://studio.example").contract("story")
    assert res.ok is False and res.status_code == 404


def test_connector_unconfigured_makes_no_call(monkeypatch):
    from vomeos.integrations import base
    from vomeos.integrations.vome_studio import VomeStudio
    monkeypatch.delenv("VOME_STUDIO_API_KEY", raising=False)
    monkeypatch.setattr(base.httpx, "request",
                        lambda *a, **k: pytest.fail("called out"))
    res = VomeStudio(base_url="https://studio.example").formats()
    assert res.ok is False and "not configured" in res.error


# ------------------------------------------------------- formats, contract

def test_contract_is_asked_for_the_named_format(jobs, env):
    assert jobs._contract("reveal") == REVEAL_CONTRACT
    assert jobs._contract() == EXPLAINER_CONTRACT
    assert env.studio.contract_requests == ["reveal", "explainer"]


def test_unavailable_format_raises_and_asks_for_nothing_else(jobs, env):
    with pytest.raises(jobs.FormatUnavailable) as exc:
        jobs._contract("story")
    assert exc.value.format == "story"
    assert env.studio.contract_requests == ["story"]


def test_formats_from_studio(jobs, env):
    formats, err = jobs._formats()
    assert err == "" and [f["name"] for f in formats] == [
        "explainer", "reveal", "trio"]


def test_studio_without_a_catalogue_can_only_build_explainers(jobs, env):
    env.studio = Studio(formats_status=404)
    formats, err = jobs._formats()
    assert err == "" and [f["name"] for f in formats] == ["explainer"]


def test_studio_failing_to_list_formats_is_reported(jobs, env):
    env.studio = Studio(formats_status=500)
    formats, err = jobs._formats()
    assert formats is None and "formats" in err


# --------------------------------------------------------- the concepts

def test_strategist_then_director_with_the_right_context(jobs, env,
                                                         monkeypatch):
    monkeypatch.setattr(jobs.video_threads, "recent_videos",
                        lambda exclude="", limit=8: [{
                            "feature_name": "Recruitment workflows",
                            "storyboard_json": json.dumps({"scenes": [
                                {"lines": [{"vo": "Still sending?"}]}]}),
                            "concepts_json": "", "chosen_concept": None}])
    agents = Agents(feature_scout=SCOUT, content_strategist=POSITIONING,
                    creative_director={"concepts": CONCEPTS, "notes": "n"})
    _install(monkeypatch, agents)
    out = jobs.run_concepts("video on this", {"thread_ts": "1.2"})
    assert out["ok"] is True, out
    assert agents.names() == ["feature_scout", "content_strategist",
                              "creative_director"]
    assert set(agents.context("feature_scout")) == {
        "feature_name", "faq_text", "brief"}
    # The scout's queries are what gets researched, and the strategist
    # sees what came back: the bigger picture, with its sources.
    assert env.research == [SCOUT["queries"]]
    s = agents.context("content_strategist")
    assert set(s) == {"feature_name", "faq_text", "brief", "prior_videos",
                      "scout", "related_articles", "house_lessons"}
    # Every writing role is given what the team has been taught.
    assert s["house_lessons"] == "- [1] Say volunteer."
    assert agents.context("creative_director")["house_lessons"]
    assert "house_lessons" not in agents.context("feature_scout")
    assert json.loads(s["scout"]) == SCOUT
    assert "own domain" in s["related_articles"]
    assert "Recruitment workflows | explainer" in s["prior_videos"]
    d = agents.context("creative_director")
    for key in ("positioning", "faq_text", "formats", "prior_videos"):
        assert key in d
    assert json.loads(d["positioning"]) == POSITIONING
    # The whole catalogue, so a not-ready format can be named as "later".
    assert "trio" in d["formats"]
    assert "direction" not in d and "previous_concepts" not in d
    assert out["concepts"] == CONCEPTS


def test_a_concept_in_a_format_that_is_not_ready_is_refused(jobs, env,
                                                            monkeypatch):
    bad = [_concept(1, "trio"), _concept(2), _concept(3, "reveal")]
    _install(monkeypatch, Agents(feature_scout=SCOUT, content_strategist=POSITIONING,
                                 creative_director={"concepts": bad}))
    out = jobs.run_concepts("brief", {"thread_ts": "1.2"})
    assert out["ok"] is False and "trio" in out["error"]


def test_a_refused_set_is_retried_once_with_the_reason(jobs, env,
                                                      monkeypatch):
    """Two hooks opening alike is fixable, so the director gets one retry
    with the reason rather than the person getting no concepts."""
    alike = [dict(_concept(1, "reveal"),
                  hook="Vome sends email to your volunteers A"),
             dict(_concept(2), hook="Vome sends email to your volunteers B"),
             _concept(3)]
    answers = iter([{"concepts": alike, "notes": "n"},
                    {"concepts": CONCEPTS, "notes": "n"}])
    agents = Agents(feature_scout=SCOUT, content_strategist=POSITIONING,
                    creative_director=lambda ctx: next(answers))
    _install(monkeypatch, agents)
    out = jobs.run_concepts("brief", {"thread_ts": "1.2"})
    assert out["ok"] is True and out["concepts"] == CONCEPTS
    second = [c for n, c in agents.calls if n.endswith("creative_director")][1]
    assert "same words" in second["check_failed"]


def test_no_ready_format_means_no_agents(jobs, env, monkeypatch):
    env.studio = Studio(formats=[dict(CATALOGUE[2])])
    _install(monkeypatch, Agents())
    out = jobs.run_concepts("brief", {"thread_ts": "1.2"})
    assert out["ok"] is False and "no video format" in out["error"]


def test_blocked_strategist_stops_the_director(jobs, env, monkeypatch):
    agents = Agents(feature_scout=SCOUT, content_strategist=_Result("blocked",
                                               reason="json_shape: no"))
    _install(monkeypatch, agents)
    out = jobs.run_concepts("brief", {"thread_ts": "1.2"})
    assert out["ok"] is False and "blocked" in out["error"]
    assert agents.names() == ["feature_scout", "content_strategist"]


def test_direction_reaches_the_strategist_and_the_director(
        jobs, env, monkeypatch):
    """The first real direction changed the angle ("an automations angle"),
    and with the positioning held fixed all three new concepts stayed boxed
    into the old claim. Direction now reaches the scout's research, the
    strategist (with its previous positioning) and the director."""
    moved = dict(POSITIONING, single_claim="Vome automates email campaigns.")
    agents = Agents(feature_scout=SCOUT, content_strategist=moved,
                    creative_director={"concepts": [
                        _concept(4, "reveal"), _concept(5), _concept(6)]})
    _install(monkeypatch, agents)
    row = {"thread_ts": "1.2", "positioning_json": json.dumps(POSITIONING),
           "concepts_json": json.dumps(CONCEPTS)}
    out = jobs.run_concepts("video please", row,
                            direction="an automations angle")
    assert out["ok"] is True
    assert agents.names() == ["feature_scout", "content_strategist",
                              "creative_director"]
    assert "an automations angle" in agents.context("feature_scout")["brief"]
    s = agents.context("content_strategist")
    assert s["direction"] == "an automations angle"
    assert json.loads(s["previous_positioning"]) == POSITIONING
    d = agents.context("creative_director")
    assert d["direction"] == "an automations angle"
    assert json.loads(d["previous_concepts"]) == CONCEPTS
    assert json.loads(d["positioning"]) == moved
    assert out["positioning"] == moved


def test_develop_concepts_posts_and_waits_for_a_pick(jobs, env, monkeypatch):
    _install(monkeypatch, Agents(
        feature_scout=SCOUT, content_strategist=POSITIONING,
        creative_director={"concepts": CONCEPTS, "notes": "Trio later."}))
    env.row = {"thread_ts": "1.2"}
    jobs.develop_concepts("1.2", "video please")
    saved = _merged(env.updates)
    assert saved["state"] == jobs.video_threads.STATE_AWAITING_CONCEPT
    assert json.loads(saved["concepts_json"]) == CONCEPTS
    assert json.loads(saved["positioning_json"]) == POSITIONING
    assert saved["chosen_concept"] is None
    assert len(env.posts) == 1
    post = env.posts[0]
    assert post.startswith("Three ways to tell *Choosing an email format*.")
    assert "*1. Concept 1*" in post and "_Reveal, about 40 seconds" in post
    assert post.endswith("Reply with 1, 2 or 3 to pick one. Anything else I "
                         "will treat as direction and come back with new "
                         "concepts.")


def test_failed_first_concepts_leave_the_thread_open(jobs, env, monkeypatch):
    _install(monkeypatch, Agents(
        feature_scout=SCOUT, content_strategist=_Result("error", reason="overloaded")))
    env.row = {"thread_ts": "1.2"}
    jobs.develop_concepts("1.2", "video please")
    assert _merged(env.updates)["state"] == jobs.video_threads.STATE_IDEA
    assert "overloaded" in env.posts[0] and "nudge" in env.posts[0]


def test_failed_direction_keeps_the_earlier_concepts(jobs, env, monkeypatch):
    _install(monkeypatch, Agents(
        feature_scout=SCOUT, content_strategist=POSITIONING,
        creative_director=_Result("error", reason="timeout")))
    env.row = {"thread_ts": "1.2", "positioning_json": json.dumps(POSITIONING),
               "concepts_json": json.dumps(CONCEPTS)}
    jobs.develop_concepts("1.2", "shorter", direction="shorter")
    saved = _merged(env.updates)
    assert saved == {"state": jobs.video_threads.STATE_AWAITING_CONCEPT}
    assert "earlier concepts still stand" in env.posts[0]


# --------------------------------------------- copywriter and brand editor

def _edit(sb, verdict="revised", changes=("Sharpened the hook.",), **kw):
    edited = json.loads(json.dumps(sb))
    edited["scenes"][0]["lines"][0]["vo"] = "This is last week's email."
    edited.update(kw)
    return {"verdict": verdict, "changes": list(changes),
            "storyboard": edited}


def test_copywriter_writes_the_concept_in_its_format(jobs, env, monkeypatch):
    agents = Agents(content_producer=STORYBOARD,
                    brand_editor=lambda ctx: _edit(json.loads(
                        ctx["storyboard"])))
    _install(monkeypatch, agents)
    concept = _concept(1, "reveal")
    out = jobs.run_content_producer("write it", {"thread_ts": "1.2"},
                                    concept=concept, positioning=POSITIONING)
    assert out["ok"] is True, out
    assert env.studio.contract_requests == ["reveal"]
    c = agents.context("content_producer")
    assert json.loads(c["concept"]) == concept
    assert json.loads(c["positioning"]) == POSITIONING
    assert json.loads(c["storyboard_contract"]) == REVEAL_CONTRACT
    e = agents.context("brand_editor")
    assert set(e) == {"storyboard", "concept", "positioning", "contract", "house_lessons"}
    assert json.loads(e["storyboard"])["format"] == "reveal"
    # What Studio validated is the edited storyboard, in the concept's format.
    sent = env.studio.validated[0]
    assert sent["format"] == "reveal"
    assert sent["scenes"][0]["lines"][0]["vo"] == "This is last week's email."
    assert out["changes"] == ["Sharpened the hook."]
    assert agents.names() == ["content_producer", "brand_editor"]


def test_explainer_storyboards_carry_no_format_key(jobs, env, monkeypatch):
    """An older Studio's schema has no format key and would reject one."""
    _install(monkeypatch, Agents(
        content_producer=dict(STORYBOARD, format="explainer"),
        brand_editor=lambda ctx: {"verdict": "pass", "changes": [],
                                  "storyboard": json.loads(ctx["storyboard"])}))
    out = jobs.run_content_producer("x", {"thread_ts": "1.2"},
                                    concept=_concept(2),
                                    positioning=POSITIONING)
    assert out["ok"] and "format" not in env.studio.validated[0]
    assert env.studio.contract_requests == ["explainer"]


def test_the_model_does_not_choose_the_format(jobs, env, monkeypatch):
    """The copywriter wrote explainer for a reveal concept. The concept
    decides, and Studio validates it against the reveal contract."""
    _install(monkeypatch, Agents(
        content_producer=dict(STORYBOARD, format="explainer"),
        brand_editor=lambda ctx: {"verdict": "pass", "changes": [],
                                  "storyboard": json.loads(ctx["storyboard"])}))
    jobs.run_content_producer("x", {"thread_ts": "1.2"},
                              concept=_concept(1, "reveal"),
                              positioning=POSITIONING)
    assert env.studio.validated[0]["format"] == "reveal"


def test_editor_never_changes_the_id_format_or_voice(jobs, env, monkeypatch):
    sneaky = lambda ctx: _edit(json.loads(ctx["storyboard"]),  # noqa: E731
                               id="something-else", format="explainer",
                               voice={"name": "Other"}, music="loud.wav")
    _install(monkeypatch, Agents(content_producer=STORYBOARD,
                                 brand_editor=sneaky))
    jobs.run_content_producer("x", {"thread_ts": "1.2"},
                              concept=_concept(1, "reveal"),
                              positioning=POSITIONING)
    sent = env.studio.validated[0]
    assert sent["id"] == "email-formats"
    assert sent["format"] == "reveal"
    assert sent["voice"] == {"name": "Maya"}
    assert "music" not in sent
    assert sent["scenes"][0]["lines"][0]["vo"] == "This is last week's email."


def test_editor_that_restructures_the_scenes_is_set_aside(jobs, env,
                                                          monkeypatch):
    def restructure(ctx):
        sb = json.loads(ctx["storyboard"])
        sb["scenes"].append({"id": "bonus", "template": "hook", "lines": []})
        return {"verdict": "revised", "changes": ["Added a scene."],
                "storyboard": sb}
    _install(monkeypatch, Agents(content_producer=STORYBOARD,
                                 brand_editor=restructure))
    out = jobs.run_content_producer("x", {"thread_ts": "1.2"},
                                    concept=_concept(2),
                                    positioning=POSITIONING)
    sent = env.studio.validated[0]
    assert [s["id"] for s in sent["scenes"]] == ["hook", "cta"]
    assert sent["scenes"][0]["lines"][0]["vo"] == "Hook 2."
    assert "set aside" in out["changes"][0]


def test_editor_failure_is_said_not_hidden(jobs, env, monkeypatch):
    _install(monkeypatch, Agents(
        content_producer=STORYBOARD,
        brand_editor=_Result("blocked", reason="json_shape: not JSON")))
    out = jobs.run_content_producer("x", {"thread_ts": "1.2"},
                                    concept=_concept(2),
                                    positioning=POSITIONING)
    assert out["ok"] is True
    assert env.studio.validated[0]["scenes"][0]["lines"][0]["vo"] == "Hook 2."
    assert "did not run" in out["changes"][0]
    assert "copywriter's draft" in out["changes"][0]


def test_editor_keeps_the_review_notes(jobs, env, monkeypatch):
    sb = dict(STORYBOARD, review_notes="TEMPLATE GAP: needs a new screen.")
    _install(monkeypatch, Agents(
        content_producer=sb,
        brand_editor=lambda ctx: _edit(
            {k: v for k, v in json.loads(ctx["storyboard"]).items()
             if k != "review_notes"})))
    jobs.run_content_producer("x", {"thread_ts": "1.2"},
                              concept=_concept(2), positioning=POSITIONING)
    assert env.studio.validated[0]["review_notes"].startswith("TEMPLATE GAP")


def test_format_unavailable_is_reported_never_swapped(jobs, env, monkeypatch):
    agents = Agents()
    _install(monkeypatch, agents)
    out = jobs.run_content_producer("x", {"thread_ts": "1.2"},
                                    concept=_concept(1, "story"),
                                    positioning=POSITIONING)
    assert out["ok"] is False and out["format_unavailable"] == "story"
    assert "Story format is not available" in out["error"]
    assert "not switch" in out["error"]
    assert env.studio.contract_requests == ["story"]
    assert agents.calls == []


def test_revision_keeps_concept_and_format_and_skips_the_editor(
        jobs, env, monkeypatch):
    agents = Agents(content_producer=dict(STORYBOARD, format="reveal"))
    _install(monkeypatch, agents)
    row = {"thread_ts": "1.2", "storyboard": "email-formats",
           "storyboard_json": json.dumps(dict(STORYBOARD, format="reveal")),
           "concepts_json": json.dumps(CONCEPTS), "chosen_concept": 1,
           "positioning_json": json.dumps(POSITIONING)}
    out = jobs.run_content_producer("make the hook shorter", row,
                                    revise=True)
    assert out["ok"] is True
    assert agents.names() == ["content_producer"]
    c = agents.context("content_producer")
    assert json.loads(c["concept"]) == CONCEPTS[0]
    assert json.loads(c["positioning"]) == POSITIONING
    assert "existing_storyboard" in c
    assert env.studio.contract_requests == ["reveal"]


def test_revision_of_a_thread_that_predates_concepts(jobs, env, monkeypatch):
    agents = Agents(content_producer=STORYBOARD)
    _install(monkeypatch, agents)
    row = {"thread_ts": "1.2", "storyboard": "email-formats",
           "storyboard_json": json.dumps(STORYBOARD)}
    out = jobs.run_content_producer("shorter", row, revise=True)
    assert out["ok"] is True
    c = agents.context("content_producer")
    assert c["concept"] == "" and c["positioning"] == ""
    assert env.studio.contract_requests == ["explainer"]


# ------------------------------------------------------- picking a concept

def _concept_row():
    return {"thread_ts": "1.2", "concepts_json": json.dumps(CONCEPTS),
            "positioning_json": json.dumps(POSITIONING),
            "state": "awaiting_concept"}


def test_draft_from_concept_posts_the_script(jobs, env, monkeypatch):
    env.row = _concept_row()
    agents = Agents(content_producer=STORYBOARD,
                    brand_editor=lambda ctx: _edit(json.loads(
                        ctx["storyboard"]), changes=["Cut 'free'."]))
    _install(monkeypatch, agents)
    jobs.draft_from_concept("1.2", 1)
    saved = _merged(env.updates)
    assert saved["chosen_concept"] == 1
    assert saved["state"] == jobs.video_threads.STATE_AWAITING_REVIEW
    assert saved["storyboard"] == "email-formats"
    post = env.posts[-1]
    assert post.startswith("Here is the first pass, written for concept 1, "
                           "*Concept 1* (Reveal). Estimated 25s.")
    assert "Brand edit:\n• Cut 'free'." in post
    assert "*Email formats*\nscript" in post
    assert "go build the video" in post


def test_unavailable_format_goes_back_to_the_concepts(jobs, env, monkeypatch):
    env.row = dict(_concept_row(), concepts_json=json.dumps(
        [_concept(1, "story"), _concept(2), _concept(3)]))
    _install(monkeypatch, Agents())
    jobs.draft_from_concept("1.2", 1)
    saved = _merged(env.updates)
    assert saved["state"] == jobs.video_threads.STATE_AWAITING_CONCEPT
    assert saved["chosen_concept"] is None
    assert "Story format is not available" in env.posts[-1]
    assert "concepts still stand" in env.posts[-1]
    assert env.studio.contract_requests == ["story"]


def test_failed_draft_goes_back_to_the_concepts(jobs, env, monkeypatch):
    env.row = _concept_row()
    _install(monkeypatch, Agents(
        content_producer=_Result("error", reason="overloaded")))
    jobs.draft_from_concept("1.2", 2)
    saved = _merged(env.updates)
    assert saved["state"] == jobs.video_threads.STATE_AWAITING_CONCEPT
    assert "concept 2" in env.posts[-1] and "overloaded" in env.posts[-1]


def test_a_number_with_no_concept_is_refused(jobs, env, monkeypatch):
    env.row = {"thread_ts": "1.2", "concepts_json": ""}
    _install(monkeypatch, Agents())
    jobs.draft_from_concept("1.2", 2)
    assert "no concept 2" in env.posts[-1]


# ------------------------------------------------------------- the handler

AWAITING_CONCEPT = "awaiting_concept"


@pytest.mark.parametrize("text", ["2", "go with 1", "option 3", "#2",
                                  "the second one"])
def test_a_pick_while_concepts_wait(handler, text):
    assert handler.classify(text, AWAITING_CONCEPT) == "pick"


@pytest.mark.parametrize("text", [
    "I like 2 but make it shorter",
    "more emotional please",
    "none of these land, try something about the volunteers",
    "wait, go with 2",
    "2?",
    "make it about retention",
])
def test_anything_else_while_concepts_wait_is_direction(handler, text):
    assert handler.classify(text, AWAITING_CONCEPT) == "direction"


@pytest.mark.parametrize("text", ["go build it", "Approve.", "ship it!",
                                  "ok, go build the video"])
def test_a_greenlight_before_a_pick_asks_for_a_pick(handler, text):
    """There is no script. Nothing is built, and the concepts are not
    regenerated from "go build it"."""
    assert handler.classify(text, AWAITING_CONCEPT) == "pick_first"


def test_messages_while_concepting_are_held(handler):
    assert handler.classify("2", "concepting") == "busy"
    assert handler.classify("go build it", "concepting") == "busy"


def test_a_nudge_after_failed_concepts_starts_again(handler):
    assert handler.classify("try again with more detail", "idea") == "new"


@pytest.mark.parametrize("text", ["2", "go with 1"])
def test_a_number_during_review_is_a_note_not_a_pick(handler, text):
    assert handler.classify(text, "awaiting_review") == "note"


def test_review_and_greenlight_paths_are_unchanged(handler):
    assert handler.classify("go build the video", "awaiting_review") == \
        "greenlight"
    assert handler.classify("make the hook a question",
                            "awaiting_review") == "note"
    assert handler.classify("This looks great!", "awaiting_review") == "note"


class _SyncThread:
    started = []

    def __init__(self, target=None, args=(), daemon=None, kwargs=None):
        self.target, self.args = target, args

    def start(self):
        _SyncThread.started.append((self.target.__name__, self.args))


@pytest.fixture
def routed(handler, monkeypatch):
    _SyncThread.started = []
    acks, updates = [], []
    monkeypatch.setattr(handler.threading, "Thread", _SyncThread)
    monkeypatch.setattr(handler, "_ack", lambda t, m: acks.append(m))
    monkeypatch.setattr(handler.video_threads, "update",
                        lambda t, **f: updates.append(f))
    monkeypatch.setattr(handler.video_threads, "start",
                        lambda *a, **k: {})
    return SimpleNamespace(acks=acks, updates=updates,
                           started=_SyncThread.started)


def _event(handler, text, thread="1.2"):
    return {"channel": handler.slack.CHANNEL_VIDEO_STUDIO, "text": text,
            "ts": "9.9", "thread_ts": thread, "user": "U1"}


def test_a_new_idea_goes_to_concepts(handler, routed, monkeypatch):
    monkeypatch.setattr(handler.video_threads, "get", lambda t: None)
    handler.handle_video_studio_message(
        {"channel": handler.slack.CHANNEL_VIDEO_STUDIO, "ts": "5.5",
         "text": "Video idea https://support.vomevolunteer.com/kb/x"})
    assert routed.started == [("_develop_concepts", (
        "5.5", "Video idea https://support.vomevolunteer.com/kb/x"))]
    assert {"state": "concepting"} in routed.updates
    assert "three ways" in routed.acks[0]


def test_a_pick_starts_the_copywriter(handler, routed, monkeypatch):
    monkeypatch.setattr(handler.video_threads, "get", lambda t: {
        "state": AWAITING_CONCEPT, "concepts_json": json.dumps(CONCEPTS)})
    handler.handle_video_studio_message(_event(handler, "go with 2"))
    assert routed.started == [("_draft_from_concept", ("1.2", 2))]
    assert {"state": "drafting"} in routed.updates
    assert routed.acks[0].startswith("Going with concept 2, *Concept 2*.")


def test_direction_regenerates_concepts(handler, routed, monkeypatch):
    monkeypatch.setattr(handler.video_threads, "get", lambda t: {
        "state": AWAITING_CONCEPT, "concepts_json": json.dumps(CONCEPTS)})
    note = "I like 2 but make it shorter"
    handler.handle_video_studio_message(_event(handler, note))
    assert routed.started == [("_develop_concepts", ("1.2", note, note))]
    assert {"state": "concepting"} in routed.updates


def test_a_greenlight_before_a_pick_starts_nothing(handler, routed,
                                                   monkeypatch):
    monkeypatch.setattr(handler.video_threads, "get",
                        lambda t: {"state": AWAITING_CONCEPT})
    handler.handle_video_studio_message(_event(handler, "go build it"))
    assert routed.started == [] and routed.updates == []
    assert "pick a concept first" in routed.acks[0]



# ------------------------------------------------------------ house lessons

def test_direction_and_notes_are_captured_as_lessons(jobs, env,
                                                     monkeypatch):
    """Sammy, 2026-10-17: feedback must improve every later video, at every
    stage. Direction on concepts and notes on a script both go to the
    lesson keeper, judged against what the person was reacting to."""
    _install(monkeypatch, Agents(
        feature_scout=SCOUT, content_strategist=POSITIONING,
        creative_director={"concepts": CONCEPTS, "notes": "n"}))
    env.row = {"thread_ts": "1.2", "positioning_json": json.dumps(POSITIONING),
               "concepts_json": json.dumps(CONCEPTS)}
    jobs.develop_concepts("1.2", "brief", direction="always say volunteer")
    assert env.captured == [("always say volunteer", "concepts")]


def test_capture_keeps_standing_taste_and_says_so(jobs, env, monkeypatch):
    kept = []
    monkeypatch.setattr(jobs.video_lessons, "active",
                        lambda role="": [{"id": 4, "rule": "Old rule."}])
    monkeypatch.setattr(
        jobs.video_lessons, "add",
        lambda lessons, t, q, stage, retire_ids=None: kept.append(
            (lessons, stage, retire_ids)) or [
            {"id": 9, "rule": l["rule"]} for l in lessons])
    agents = Agents(lesson_keeper={
        "lessons": [{"rule": "Every hook says volunteer.",
                     "applies_to": ["all"]}],
        "replaces": [4, 77], "note": "kept one"})
    _install(monkeypatch, agents)
    env.real_capture("1.2", "say volunteer always",
                                     "script", row={})
    ctx = agents.context("lesson_keeper")
    assert ctx["message"] == "say volunteer always"
    assert "[4] Old rule." in ctx["current_lessons"]
    # Only a lesson that exists can be replaced.
    assert kept[0][2] == [4]
    assert "Noted for every future video" in env.posts[-1]
    assert "forget that" in env.posts[-1]


def test_a_one_off_edit_keeps_nothing_and_says_nothing(jobs, env,
                                                      monkeypatch):
    monkeypatch.setattr(jobs.video_lessons, "active", lambda role="": [])
    monkeypatch.setattr(jobs.video_lessons, "add",
                        lambda *a, **k: pytest.fail("kept a one-off"))
    _install(monkeypatch, Agents(lesson_keeper={
        "lessons": [], "replaces": [], "note": "one-off"}))
    env.real_capture("1.2", "make line 3 shorter",
                                     "script", row={})
    assert env.posts == []
