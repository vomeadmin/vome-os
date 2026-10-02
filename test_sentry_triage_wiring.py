"""
test_sentry_triage_wiring.py

The agent itself is scored by the answer key (`py -m vomeos.evaluate
engineering.sentry_triage`). What is tested here is everything around it: what
the handler does when the agent works, when a guard rejects its answer, when
the model is unreachable, and when Sentry has no stack trace to give.

The theme is that none of those may lose an issue. It is already claimed in
the ledger, so an issue that falls out of triage is an issue nobody will ever
look at again, and that is worse than any wrong verdict.
"""

import pytest

import sentry_event
import sentry_handler as handler
import sentry_ledger


class _Result:
    """Stand-in for vomeos.runner.AgentResult."""

    def __init__(self, status="ok", data=None, run_id="run-1", error=""):
        self.status = status
        self.data = data or {}
        self.run_id = run_id
        self.error = error
        self.agent = "engineering.sentry_triage"

    @property
    def ok(self):
        return self.status == "ok"

    def get(self, key, default=None):
        return self.data.get(key, default)

    def why(self):
        return self.error or self.status


@pytest.fixture
def wired(monkeypatch):
    updates = []
    monkeypatch.setattr(
        sentry_ledger, "update",
        lambda issue_id, **fields: updates.append((issue_id, fields)) or True,
    )
    monkeypatch.setattr(
        sentry_ledger, "record",
        lambda signal, **kw: sentry_ledger.Claim(True, signal.issue_id),
    )
    monkeypatch.setattr(sentry_event, "fetch_trace", lambda issue_id: "TRACE")
    monkeypatch.setattr(handler, "sentry_event", sentry_event)
    # Stop at the end of triage. Everything after it (code reading, the
    # analyst, Slack, the patch) is phases 3 to 5 and has its own tests; what
    # is under test here is the triage hop itself.
    monkeypatch.setattr(
        handler, "_investigate",
        lambda signal, message, triage: {"status": "analysed"},
    )
    return updates


def _message(issue_id="900"):
    return {
        "signal": {
            "issue_id": issue_id,
            "project": "prod-vome",
            "title": "AttributeError: 'NoneType' has no attribute 'site'",
            "culprit": "opportunity_app/views.py in reserve_shift",
            "exception_type": "AttributeError",
            "level": "error",
            "reason": "issue.created",
        },
        "repo": "vomedjango-restored-core-app",
        "repo_ref": "master",
        "stack": "backend",
    }


def _agent(monkeypatch, result, calls=None):
    import vomeos

    def fake(agent, context=None, **kw):
        if calls is not None:
            calls.append({"agent": agent, "context": context, "kw": kw})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(vomeos, "run_agent", fake, raising=False)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_verdict_is_recorded(monkeypatch, wired):
    _agent(monkeypatch, _Result(data={
        "verdict": "actionable", "severity": "s2", "infrastructure": False,
        "summary": "Reserving a shift fails when the role has no site.",
    }))
    result = handler.handle_sentry_issue(_message())
    assert result["verdict"] == "actionable"
    assert result["severity"] == "s2"

    issue_id, fields = wired[-1]
    assert issue_id == "900"
    assert fields["status"] == sentry_ledger.STATUS_TRIAGED
    assert fields["verdict"] == "actionable"
    assert fields["triage_summary"].startswith("Reserving a shift")
    assert fields["run_ids"] == ["run-1"]


def test_the_agent_is_given_the_stack_trace_and_the_repo(monkeypatch, wired):
    # Triage on a title alone is guessing. The trace is the evidence, and the
    # repo is a fact we already hold rather than something to ask a model.
    calls = []
    _agent(monkeypatch, _Result(data={"verdict": "noise", "severity": "s3"}),
           calls)
    handler.handle_sentry_issue(_message())
    context = calls[0]["context"]
    assert context["stack_trace"] == "TRACE"
    assert context["repo"] == "vomedjango-restored-core-app"
    assert context["exception_type"] == "AttributeError"
    assert calls[0]["kw"]["subject_type"] == "sentry_issue"
    assert calls[0]["kw"]["subject_id"] == "900"


def test_a_noise_verdict_is_recorded_too(monkeypatch, wired):
    # Noise is a judgement worth keeping. It is how the deterministic gate
    # list grows from evidence instead of from guessing.
    _agent(monkeypatch, _Result(data={
        "verdict": "noise", "severity": "s3", "summary": "Handset offline.",
    }))
    assert handler.handle_sentry_issue(_message())["verdict"] == "noise"
    assert wired[-1][1]["verdict"] == "noise"


def test_infrastructure_is_carried_through(monkeypatch, wired):
    # It decides who gets tagged in phase 3, so it has to survive the hop.
    _agent(monkeypatch, _Result(data={
        "verdict": "actionable", "severity": "s1", "infrastructure": True,
        "summary": "Database connections exhausted.",
    }))
    handler.handle_sentry_issue(_message())
    triage_write = [f for _, f in wired if "infrastructure" in f][-1]
    assert triage_write["infrastructure"] is True


def test_noise_never_reaches_the_analyst(monkeypatch, wired):
    # The whole economic argument for a cheap triage pass. Spending a
    # senior-tier call on something just called noise would undo it.
    reached = []
    monkeypatch.setattr(
        handler, "_investigate",
        lambda s, m, t: reached.append(s.issue_id) or {"status": "analysed"},
    )
    _agent(monkeypatch, _Result(data={
        "verdict": "noise", "severity": "s3", "summary": "Handset offline.",
    }))
    result = handler.handle_sentry_issue(_message())
    assert reached == []
    assert result["status"] == "triaged"


def test_an_actionable_verdict_does_reach_the_analyst(monkeypatch, wired):
    reached = []
    monkeypatch.setattr(
        handler, "_investigate",
        lambda s, m, t: reached.append(s.issue_id) or {"status": "analysed"},
    )
    _agent(monkeypatch, _Result(data={
        "verdict": "actionable", "severity": "s2", "summary": "Broken.",
    }))
    handler.handle_sentry_issue(_message())
    assert reached == ["900"]


# ---------------------------------------------------------------------------
# Nothing may lose the issue
# ---------------------------------------------------------------------------

def test_a_blocked_answer_leaves_the_issue_untriaged_not_dismissed(
    monkeypatch, wired
):
    # A guard rejected the output. That is a human's problem, not a verdict
    # of noise, and treating it as noise would silently bury a real bug.
    _agent(monkeypatch, _Result(status="blocked", error="json_shape failed"))
    result = handler.handle_sentry_issue(_message())
    assert result["status"] == "recorded"
    assert result["verdict"] == ""
    assert wired[-1][1]["status"] == sentry_ledger.STATUS_SEEN
    assert "verdict" not in wired[-1][1]


def test_a_model_outage_does_not_lose_the_issue(monkeypatch, wired):
    _agent(monkeypatch, RuntimeError("anthropic is down"))
    result = handler.handle_sentry_issue(_message())
    assert result["status"] == "recorded"
    assert result["acted"] is True
    assert result["verdict"] == ""


def test_an_error_status_leaves_no_verdict(monkeypatch, wired):
    _agent(monkeypatch, _Result(status="error", error="timeout"))
    assert handler.handle_sentry_issue(_message())["verdict"] == ""


def test_a_missing_stack_trace_still_gets_triaged(monkeypatch, wired):
    # Sentry slow, or the token expired. Degrade to a title-only verdict
    # rather than skipping the issue, and say so in the prompt so the agent
    # knows its evidence is thin.
    calls = []
    monkeypatch.setattr(sentry_event, "fetch_trace", lambda issue_id: "")
    _agent(monkeypatch, _Result(data={"verdict": "noise", "severity": "s3"}),
           calls)
    handler.handle_sentry_issue(_message())
    assert calls[0]["context"]["stack_trace"] == "(no stack trace available)"


def test_a_known_issue_is_never_triaged_twice(monkeypatch, wired):
    # The whole point of the ledger. A second delivery must cost nothing.
    calls = []
    monkeypatch.setattr(
        sentry_ledger, "record",
        lambda signal, **kw: sentry_ledger.Claim(False, signal.issue_id),
    )
    _agent(monkeypatch, _Result(data={"verdict": "actionable"}), calls)
    result = handler.handle_sentry_issue(_message())
    assert result["status"] == "known"
    assert result["acted"] is False
    assert calls == []


# ---------------------------------------------------------------------------
# Trace rendering
# ---------------------------------------------------------------------------

def test_the_api_entries_shape_is_understood():
    event = {
        "entries": [
            {"type": "request", "data": {}},
            {"type": "exception", "data": {"values": [{
                "type": "AttributeError",
                "value": "'NoneType' object has no attribute 'site'",
                "stacktrace": {"frames": [{
                    "filename": "opportunity_app/views.py",
                    "function": "reserve_shift",
                    "lineNo": 412,
                    "inApp": True,
                    "context": [[411, "    role = get_role()"],
                                [412, "    site = role.site.name"]],
                }]},
            }]}},
        ]
    }
    trace = sentry_event.render_trace(event)
    assert 'File "opportunity_app/views.py", line 412, in reserve_shift' in trace
    assert "site = role.site.name" in trace
    assert "AttributeError: 'NoneType' object has no attribute 'site'" in trace


def test_the_raw_exception_shape_is_understood():
    event = {"exception": {"values": [{
        "type": "ValueError", "value": "boom",
        "stacktrace": {"frames": [{"filename": "a.py", "function": "f",
                                   "lineNo": 1}]},
    }]}}
    assert "ValueError: boom" in sentry_event.render_trace(event)


def test_in_app_frames_come_first():
    # A traceback through six layers of Django and one line of ours is a bug
    # about the one line of ours.
    frames = [
        {"filename": "django/db/models/query.py", "function": "get",
         "lineNo": 637, "inApp": False},
        {"filename": "form_app/views.py", "function": "get_queryset",
         "lineNo": 701, "inApp": True},
    ]
    event = {"exception": {"values": [{
        "type": "DoesNotExist", "value": "x",
        "stacktrace": {"frames": frames},
    }]}}
    trace = sentry_event.render_trace(event)
    assert trace.index("form_app/views.py") < trace.index("django/db")


def test_an_event_with_no_exception_renders_nothing():
    # Must be empty, not a plausible-looking blank frame. The caller treats
    # "" as no evidence, and anything else would read as evidence.
    assert sentry_event.render_trace({}) == ""
    assert sentry_event.render_trace({"entries": []}) == ""


def test_a_runaway_trace_is_capped(monkeypatch):
    monkeypatch.setattr(sentry_event, "MAX_TRACE_CHARS", 200)
    frames = [{"filename": f"f{i}.py", "function": "recurse", "lineNo": i,
               "inApp": True} for i in range(500)]
    event = {"exception": {"values": [{
        "type": "RecursionError", "value": "maximum depth exceeded",
        "stacktrace": {"frames": frames},
    }]}}
    trace = sentry_event.render_trace(event)
    assert len(trace) < 400
    assert "truncated" in trace


def test_fetch_trace_is_silent_when_sentry_is_unconfigured(monkeypatch):
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("VOMEOS_SENTRY_AUTH_TOKEN", raising=False)
    assert sentry_event.fetch_trace("123") == ""
