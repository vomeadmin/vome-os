"""
test_sentry_handler.py

The pipeline's contract, at the two seams that matter.

The web half must never let an unsigned payload through, must never queue
something the gate dropped, and must never answer non-2xx for anything except
a bad signature (Sentry retries a non-2xx, and a retry storm on a payload we
were always going to discard is worse than a lost log line).

The worker half must act exactly once per issue. That is the whole
anti-bombardment guarantee, and it is worth more here than anywhere else in
the design: every later phase posts to Slack and opens pull requests off the
back of `first_seen`.
"""

import hashlib
import hmac
import json

import pytest

import sentry_handler as handler
import sentry_ledger
from vomeos.integrations import sentry as sentry_api

SECRET = "test-secret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _issue_payload(**overrides):
    issue = {
        "id": "1001",
        "title": "AttributeError: 'NoneType' object has no attribute 'site'",
        "culprit": "opportunity_app/views.py in reserve_shift",
        "level": "error",
        "count": 12,
        "userCount": 3,
        "permalink": "https://sentry.io/organizations/vome/issues/1001/",
        "project": {"slug": "prod-vome"},
        "metadata": {"type": "AttributeError"},
    }
    issue.update(overrides)
    return {"action": "created", "data": {"issue": issue}}


@pytest.fixture
def wired(monkeypatch):
    """A configured pipeline with the ledger and the queue stubbed out."""
    monkeypatch.setenv("SENTRY_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("SENTRY_PROJECT_ALLOWLIST", raising=False)
    monkeypatch.delenv("SENTRY_TRIAGE_DEV_PROJECTS", raising=False)
    monkeypatch.delenv("SENTRY_ENVIRONMENTS", raising=False)
    monkeypatch.delenv("SENTRY_REQUIRE_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SENTRY_PIPELINE_ENABLED", raising=False)

    queued = []
    recorded = []
    monkeypatch.setattr(handler, "_queue", lambda message: queued.append(message))

    def fake_record(signal, *, status=sentry_ledger.STATUS_SEEN, gate_rule="",
                    gate_detail="", repo="", stack=""):
        recorded.append(
            {
                "signal": signal,
                "status": status,
                "rule": gate_rule,
                "repo": repo,
                "stack": stack,
            }
        )
        # First sighting of each issue id wins.
        seen = [r["signal"].issue_id for r in recorded[:-1]]
        return sentry_ledger.Claim(
            signal.issue_id not in seen, signal.issue_id, status
        )

    monkeypatch.setattr(sentry_ledger, "record", fake_record)
    monkeypatch.setattr(handler, "sentry_ledger", sentry_ledger)
    return {"queued": queued, "recorded": recorded}


def _post(payload, resource="issue", secret=SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return handler.handle_webhook(body, signature, resource)


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------

def test_a_valid_signature_is_accepted(wired):
    assert _post(_issue_payload())["status"] == "queued"


def test_a_bad_signature_is_rejected(wired):
    body = json.dumps(_issue_payload()).encode()
    result = handler.handle_webhook(body, "deadbeef", "issue")
    assert result["status"] == "rejected"


def test_a_signature_from_the_wrong_secret_is_rejected(wired):
    assert _post(_issue_payload(), secret="not-the-secret")["status"] == "rejected"


def test_no_secret_configured_rejects_everything(monkeypatch):
    # Fails closed, unlike the older Slack and Calendly helpers. This endpoint
    # feeds a ledger and, later, opens pull requests.
    monkeypatch.delenv("SENTRY_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("VOMEOS_SENTRY_WEBHOOK_SECRET", raising=False)
    body = json.dumps(_issue_payload()).encode()
    result = handler.handle_webhook(body, _sign(body), "issue")
    assert result["status"] == "rejected"


def test_an_unparseable_body_is_ignored_not_raised(wired):
    body = b"{not json"
    result = handler.handle_webhook(body, _sign(body), "issue")
    assert result["status"] == "ignored"


def test_an_unsubscribed_resource_is_ignored(wired):
    assert _post(_issue_payload(), resource="installation")["status"] == "ignored"


def test_a_payload_with_no_issue_is_ignored(wired):
    result = _post({"action": "created", "data": {"comment": {"id": "9"}}})
    assert result["status"] == "ignored"


def test_the_pipeline_can_be_switched_off(monkeypatch, wired):
    # An incident switch that leaves the endpoint answering 2xx, so Sentry
    # does not retry every delivery for a day.
    monkeypatch.setenv("SENTRY_PIPELINE_ENABLED", "false")
    result = _post(_issue_payload())
    assert result["status"] == "ignored"
    assert wired["queued"] == []


# ---------------------------------------------------------------------------
# The gate stops work before the queue
# ---------------------------------------------------------------------------

def test_a_gated_issue_is_recorded_but_never_queued(wired):
    result = _post(_issue_payload(level="warning"))
    assert result["status"] == "gated"
    assert result["rule"] == "level"
    assert wired["queued"] == []
    # Still recorded, so the daily report can show what the rule ate.
    assert wired["recorded"][0]["status"] == sentry_ledger.STATUS_GATED
    assert wired["recorded"][0]["rule"] == "level"


def test_a_surviving_issue_is_queued_with_its_signal(wired):
    result = _post(_issue_payload())
    assert result["status"] == "queued"
    assert len(wired["queued"]) == 1
    signal = wired["queued"][0]["signal"]
    assert signal["issue_id"] == "1001"
    assert signal["project"] == "prod-vome"
    assert signal["exception_type"] == "AttributeError"


def test_the_queued_message_names_the_repository(wired):
    # Which repo an issue belongs to is a fixed fact from sentry_projects.py,
    # resolved here rather than asked of a model on every issue.
    _post(_issue_payload())
    message = wired["queued"][0]
    assert message["repo"] == "vomedjango-restored-core-app"
    assert message["repo_owner"] == "vomedjango"
    assert message["repo_host"] == "bitbucket"
    assert message["repo_ref"] == "master"  # production, not main
    assert message["stack"] == "backend"


def test_the_dev_backend_carries_the_development_branch(monkeypatch, wired):
    # The starting configuration. Same repo as prod-vome, different ref, and
    # the ref has to travel with the issue or the analyst reads the wrong code.
    monkeypatch.setenv("SENTRY_PROJECT_ALLOWLIST", "dev-vome-app")
    payload = _issue_payload()
    payload["data"]["issue"]["project"] = {"slug": "dev-vome-app"}
    result = _post(payload)
    assert result["status"] == "queued"
    assert result["ref"] == "development"
    message = wired["queued"][0]
    assert message["repo"] == "vomedjango-restored-core-app"
    assert message["repo_ref"] == "development"


def test_the_worker_reports_the_branch_it_was_given(wired):
    message = _message()
    message["repo"] = "vomedjango-restored-core-app"
    message["repo_ref"] = "development"
    assert handler.handle_sentry_issue(message)["ref"] == "development"


def test_the_mobile_project_routes_to_github(wired):
    payload = _issue_payload()
    payload["data"]["issue"]["project"] = {"slug": "vome-2j"}
    _post(payload)
    message = wired["queued"][0]
    assert message["repo"] == "VomeApp"
    assert message["repo_owner"] == "samfagen15"
    assert message["repo_host"] == "github"
    assert message["stack"] == "mobile"


def test_a_dev_project_is_gated_before_the_queue(wired):
    # dev-vome-app is the same codebase as prod-vome. Separate project, not
    # a separate environment, which is why the project table is the filter.
    payload = _issue_payload()
    payload["data"]["issue"]["project"] = {"slug": "dev-vome-app"}
    result = _post(payload)
    assert result["status"] == "gated"
    assert result["rule"] == "project_dev"
    assert wired["queued"] == []


def test_the_repo_is_written_to_the_ledger_on_the_worker(wired):
    message = _message()
    message["repo"] = "vomedjango-restored-core-app"
    message["stack"] = "backend"
    result = handler.handle_sentry_issue(message)
    assert result["repo"] == "vomedjango-restored-core-app"
    assert wired["recorded"][0]["repo"] == "vomedjango-restored-core-app"
    assert wired["recorded"][0]["stack"] == "backend"


def test_a_card_shaped_issue_id_survives_the_pipeline(wired):
    # REGRESSION, and the most dangerous bug found in this pipeline so far.
    # A real Sentry issue id reached the queue as "[card]", which would have
    # made every issue share one ledger key: the first claims, the rest are
    # "already known" and dropped. Silent, total, and it looks like health.
    payload = _issue_payload(id="4506427274952704")
    result = _post(payload)
    assert result["issue_id"] == "4506427274952704"
    assert wired["queued"][0]["signal"]["issue_id"] == "4506427274952704"


def test_structural_fields_are_never_rewritten(wired):
    # The general rule behind that regression: a scrubber only ever touches
    # prose. Identifiers, slugs, levels and counts are read from the parsed
    # body and pass through byte for byte.
    payload = _issue_payload(id="4506427274952704", count=4532015112830366)
    _post(payload)
    signal = wired["queued"][0]["signal"]
    assert signal["issue_id"] == "4506427274952704"
    assert signal["project"] == "prod-vome"
    assert signal["level"] == "error"
    assert signal["times_seen"] == 4532015112830366


def test_prose_fields_are_still_scrubbed(wired):
    # The other half. title and culprit are the two fields that can carry a
    # customer's data, and they are still cleaned.
    payload = _issue_payload()
    payload["data"]["issue"]["title"] = "Error for admin@bigcharity.org"
    _post(payload)
    assert "admin@bigcharity.org" not in wired["queued"][0]["signal"]["title"]
    assert "[email]" in wired["queued"][0]["signal"]["title"]


def test_the_queued_payload_is_redacted(wired):
    payload = _issue_payload()
    payload["data"]["issue"]["title"] = "Error for admin@bigcharity.org"
    _post(payload)
    blob = json.dumps(wired["queued"][0], default=str)
    assert "admin@bigcharity.org" not in blob


def test_an_oversized_payload_is_dropped_rather_than_queued(monkeypatch, wired):
    # Redis will happily hold a megabyte per message, which is how a queue
    # quietly becomes a database. The worker can re-fetch from Sentry.
    monkeypatch.setattr(handler, "MAX_QUEUE_BYTES", 200)
    _post(_issue_payload())
    assert wired["queued"][0]["payload"] is None
    assert wired["queued"][0]["payload_dropped"] is True
    assert wired["queued"][0]["signal"]["issue_id"] == "1001"


def test_a_broker_outage_does_not_make_sentry_retry(monkeypatch, wired):
    def boom(_message):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(handler, "_queue", boom)
    result = _post(_issue_payload())
    assert result["status"] == "queued"
    assert result["queued"] is False
    # What we knew is still written down.
    assert wired["recorded"]


# ---------------------------------------------------------------------------
# The worker half: exactly once
# ---------------------------------------------------------------------------

def _message(issue_id="1001", **overrides):
    signal = sentry_api.IssueSignal(
        issue_id=issue_id,
        project="prod-vome",
        title="AttributeError",
        level="error",
        reason="issue.created",
        **overrides,
    )
    return {"signal": signal.__dict__, "payload": {}}


def test_a_new_issue_is_acted_on(wired):
    result = handler.handle_sentry_issue(_message())
    assert result["status"] == "recorded"
    assert result["acted"] is True


def test_a_redelivery_of_the_same_issue_does_nothing(wired):
    # Celery delivers at least once. This is the guarantee every later phase
    # depends on: one Slack post per issue, not one per delivery.
    first = handler.handle_sentry_issue(_message())
    second = handler.handle_sentry_issue(_message())
    third = handler.handle_sentry_issue(_message())
    assert first["acted"] is True
    assert second["acted"] is False
    assert third["acted"] is False
    assert second["status"] == "known"


def test_a_different_issue_is_acted_on_separately(wired):
    assert handler.handle_sentry_issue(_message("1001"))["acted"] is True
    assert handler.handle_sentry_issue(_message("2002"))["acted"] is True


def test_a_message_with_no_issue_id_is_ignored(wired):
    assert handler.handle_sentry_issue({"signal": {}})["status"] == "ignored"
    assert handler.handle_sentry_issue({})["status"] == "ignored"


def test_the_worker_tolerates_an_unknown_field_shape(wired):
    # The queue can hold a message written by an older deploy. A handler that
    # raises on one would retry it twice and then drop it.
    message = _message()
    message["signal"]["some_future_field"] = "x"
    assert handler.handle_sentry_issue(message)["acted"] is True


# ---------------------------------------------------------------------------
# Normalisation, the two payload shapes
# ---------------------------------------------------------------------------

def test_an_event_alert_payload_normalises_to_the_same_shape():
    payload = {
        "action": "triggered",
        "data": {
            "event": {
                "issue_id": "3003",
                "project_slug": "vome-2j",
                "title": "TypeError: x is not a function",
                "level": "error",
                "web_url": "https://sentry.io/x/3003/",
                "metadata": {"type": "TypeError"},
                "tags": [["environment", "production"]],
            }
        },
    }
    signal = sentry_api.normalize(payload, "event_alert")
    assert signal.issue_id == "3003"
    assert signal.project == "vome-2j"
    assert signal.environment == "production"
    assert signal.exception_type == "TypeError"


def test_an_issue_payload_has_no_environment():
    # The fact the whole environment rule in the gate is built around.
    signal = sentry_api.normalize(_issue_payload(), "issue")
    assert signal.environment == ""
    assert signal.times_seen == 12
    assert signal.users_affected == 3


def test_normalize_returns_none_for_things_that_are_not_issues():
    assert sentry_api.normalize({}, "issue") is None
    assert sentry_api.normalize({"data": {}}, "issue") is None
    assert sentry_api.normalize("not a dict", "issue") is None
    assert sentry_api.normalize({"data": {"issue": {}}}, "issue") is None


def test_signature_verification_is_constant_time_and_exact(monkeypatch):
    monkeypatch.setenv("SENTRY_WEBHOOK_SECRET", SECRET)
    body = b'{"a":1}'
    assert sentry_api.verify_signature(body, _sign(body))
    assert not sentry_api.verify_signature(body, _sign(body)[:-1] + "0")
    assert not sentry_api.verify_signature(b'{"a":2}', _sign(body))
    assert not sentry_api.verify_signature(body, "")
