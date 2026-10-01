"""
test_sentry_notify.py

Routing is the part of this pipeline a person experiences directly, so the
failures that matter are social rather than technical.

Tag nobody and the issue sits unclaimed because everyone assumes it is
somebody else's. Tag everybody and the channel is muted inside a week, which
is the same outcome as tagging nobody but harder to notice.
"""

import pytest

import sentry_notify as notify


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "SENTRY_SLACK_CHANNEL", "SLACK_USER_ONLYG",
        "SLACK_USER_SANJAY", "SLACK_USER_SIRAJ", "SAM_SLACK_USER_ID",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The channel
# ---------------------------------------------------------------------------

def test_everything_goes_to_eng_all():
    assert notify.channel() == "C0BR5RZ51B3"
    assert notify.route("prod-vome").channel == "C0BR5RZ51B3"
    assert notify.route("vome-2j").channel == "C0BR5RZ51B3"


def test_the_channel_can_be_moved_without_a_deploy(monkeypatch):
    monkeypatch.setenv("SENTRY_SLACK_CHANNEL", "C0THEROTHER")
    assert notify.route("prod-vome").channel == "C0THEROTHER"


# ---------------------------------------------------------------------------
# Who gets tagged
# ---------------------------------------------------------------------------

def test_backend_tags_onlyg():
    r = notify.route("prod-vome")
    assert r.owners == ("onlyg",)
    assert r.mentions() == "<@U0APZ7JUHRD>"


def test_the_database_service_is_backend_too():
    assert notify.route("prod-volunteer-database").owners == ("onlyg",)


def test_mobile_tags_sanjay():
    r = notify.route("vome-2j")
    assert r.owners == ("sanjay",)
    assert r.mentions() == "<@U0AQJ8YJF6Y>"


def test_web_tags_sanjay_too():
    # Sanjay covers frontend for both web and mobile.
    assert notify.route("prod-vome-web").owners == ("sanjay",)


def test_an_unknown_project_still_has_an_owner():
    # Never nobody. An issue in Slack with no owner is one everybody assumes
    # is somebody else's.
    r = notify.route("some-new-project")
    assert r.owners == ("onlyg",)
    assert "defaulted" in r.reason


# ---------------------------------------------------------------------------
# Infrastructure and configuration
# ---------------------------------------------------------------------------

def test_infra_tags_siraj_and_onlyg_and_copies_sam(monkeypatch):
    monkeypatch.setenv("SAM_SLACK_USER_ID", "U01B9FQNSBU")
    r = notify.route(
        "prod-vome", exception_type="OperationalError",
        culprit="django/db/backends/postgresql/base.py",
    )
    assert r.owners == ("siraj", "onlyg")
    assert r.cc == ("sam",)
    assert r.mentions() == "<@U0ATX30F3TL> <@U0APZ7JUHRD> cc <@U01B9FQNSBU>"


def test_aws_errors_are_infra():
    for etype, culprit in (
        ("ClientError", "botocore/client.py"),
        ("NoCredentialsError", "boto3/session.py"),
        ("S3UploadFailedError", "storages/backends/s3boto3.py"),
        ("EndpointConnectionError", "botocore/endpoint.py"),
    ):
        assert notify.looks_like_infra(etype, culprit), etype


def test_misconfiguration_is_infra():
    assert notify.looks_like_infra("ImproperlyConfigured", "vome/settings.py")


def test_a_bare_keyerror_is_not_infra():
    # The rule that keeps this routing usable. KeyError is the most common
    # exception in any Python codebase, and tagging three people on each one
    # would make the whole scheme worthless inside a week.
    assert not notify.looks_like_infra(
        "KeyError", "opportunity_app/views.py in reserve_shift"
    )


def test_a_keyerror_on_settings_is_infra():
    assert notify.looks_like_infra("KeyError", "vome/settings.py")


def test_ordinary_product_bugs_are_not_infra():
    for etype, culprit in (
        ("AttributeError", "opportunity_app/views.py in reserve_shift"),
        ("ValueError", "/api/organization/admin/invite-register/{token}/"),
        ("ValidationError", "/api/opportunity/v2/.../opportunity-shift/create/"),
        ("TooManyFieldsSent", "/api/email-integration/send-bulk-email/"),
    ):
        assert not notify.looks_like_infra(etype, culprit), etype


def test_the_agent_can_override_the_heuristic():
    # The triage agent sees the stack trace and knows more than a regex.
    plain = notify.route("prod-vome", exception_type="AttributeError")
    assert plain.owners == ("onlyg",)
    overridden = notify.route(
        "prod-vome", exception_type="AttributeError", infra=True
    )
    assert overridden.owners == ("siraj", "onlyg")


def test_the_agent_can_override_in_the_other_direction():
    assert notify.route(
        "prod-vome", exception_type="OperationalError", infra=False
    ).owners == ("onlyg",)


# ---------------------------------------------------------------------------
# Degraded configuration
# ---------------------------------------------------------------------------

def test_a_missing_id_falls_back_to_a_readable_name(monkeypatch):
    # Never silently drop the person the message was meant to reach. A
    # visible "Sam" that does not ping beats an empty string nobody notices.
    monkeypatch.setenv("SAM_SLACK_USER_ID", "")
    r = notify.route("prod-vome", infra=True)
    assert "Sam" in r.mentions()


def test_people_can_be_reassigned_without_a_deploy(monkeypatch):
    monkeypatch.setenv("SLACK_USER_ONLYG", "U0NEWPERSON")
    assert notify.route("prod-vome").mentions() == "<@U0NEWPERSON>"
