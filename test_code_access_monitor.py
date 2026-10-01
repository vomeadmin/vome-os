"""
test_code_access_monitor.py

This monitor exists because an expired token is silent, so its own failure
modes are the interesting ones.

Post on every healthy pass and it gets muted, which makes it worthless on the
one morning it has something true to say. Complain about a token we have not
created yet and it gets muted just as fast. Stay quiet when a credential is
actually dead and it is worse than not existing, because it reads as proof
that everything is fine.
"""

from datetime import datetime, timedelta, timezone

import pytest

import code_access_monitor as mon


def _at(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def _patch(monkeypatch, bitbucket, github, sentry):
    posts = []
    monkeypatch.setattr(mon, "check_bitbucket", lambda: bitbucket)
    monkeypatch.setattr(mon, "check_github", lambda: [github])
    monkeypatch.setattr(mon, "check_sentry", lambda: sentry)
    monkeypatch.setattr(mon, "_post", lambda text: posts.append(text) or True)
    return posts


# ---------------------------------------------------------------------------
# When it should say nothing
# ---------------------------------------------------------------------------

def test_silent_when_everything_is_healthy(monkeypatch):
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.OK, expires_at=_at(300)),
        mon.Check("github", mon.OK, expires_at=_at(200)),
        mon.Check("sentry", mon.OK),
    )
    result = mon.check_code_access()
    assert result["status"] == "healthy"
    assert result["posted"] is False
    assert posts == []


def test_silent_when_tokens_are_not_configured_yet(monkeypatch):
    # Phase 1 does not read code. A monitor that complains every morning
    # about a thing you have not built is one you learn to ignore before it
    # ever tells you something true.
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.SKIPPED, "no token set"),
        mon.Check("github", mon.SKIPPED, "no token set"),
        mon.Check("sentry", mon.OK),
    )
    result = mon.check_code_access()
    assert result["status"] == "healthy"
    assert posts == []


# ---------------------------------------------------------------------------
# When it must speak
# ---------------------------------------------------------------------------

def test_posts_when_a_credential_is_broken(monkeypatch):
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.FAILING, "cannot read repo: HTTP 401"),
        mon.Check("github", mon.SKIPPED),
        mon.Check("sentry", mon.OK),
    )
    result = mon.check_code_access()
    assert result["status"] == "failing"
    assert result["posted"] is True
    assert "BROKEN" in posts[0]
    assert "401" in posts[0]


def test_posts_when_a_token_is_near_expiry(monkeypatch):
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.WARNING, "expires in 12 day(s)"),
        mon.Check("github", mon.OK),
        mon.Check("sentry", mon.OK),
    )
    result = mon.check_code_access()
    assert result["status"] == "expiring"
    assert "EXPIRING" in posts[0]


def test_a_broken_credential_outranks_an_expiring_one(monkeypatch):
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.WARNING, "expires in 3 day(s)"),
        mon.Check("github", mon.FAILING, "HTTP 404"),
        mon.Check("sentry", mon.OK),
    )
    assert mon.check_code_access()["status"] == "failing"
    assert "not working" in posts[0]


def test_the_message_tags_sam_and_copies_onlyg(monkeypatch):
    # Sam holds these credentials, so he is the only one who can rotate
    # them. OnlyG is copied because a dead read token is why the analyst has
    # gone quiet and he notices that first.
    monkeypatch.setenv("SAM_SLACK_USER_ID", "U01B9FQNSBU")
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.FAILING, "HTTP 401"),
        mon.Check("github", mon.OK),
        mon.Check("sentry", mon.OK),
    )
    mon.check_code_access()
    assert "<@U01B9FQNSBU>" in posts[0]
    assert "cc <@U0APZ7JUHRD>" in posts[0]


def test_the_message_says_what_breaks_as_a_result(monkeypatch):
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.FAILING, "HTTP 401"),
        mon.Check("github", mon.OK),
        mon.Check("sentry", mon.OK),
    )
    mon.check_code_access()
    assert "cannot read code" in posts[0]


def test_healthy_credentials_are_still_listed_in_a_problem_report(monkeypatch):
    # Context matters when you are deciding how bad it is.
    posts = _patch(
        monkeypatch,
        mon.Check("bitbucket", mon.FAILING, "HTTP 401"),
        mon.Check("github", mon.OK),
        mon.Check("sentry", mon.OK),
    )
    mon.check_code_access()
    assert "Still fine: github, sentry" in posts[0]


# ---------------------------------------------------------------------------
# Expiry arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "2027-12-31 00:00:00 +0000",   # GitHub's header format
        "2027-12-31 00:00:00 UTC",
        "2027-12-31T00:00:00+0000",
        "2027-12-31",                  # what a human types in Railway
    ],
)
def test_the_date_formats_these_apis_actually_send(raw):
    parsed = mon._parse_date(raw)
    assert parsed is not None
    assert parsed.year == 2027 and parsed.month == 12 and parsed.day == 31


def test_an_unparseable_or_absent_date_is_unknown_not_zero():
    # "We cannot tell" and "it expires today" must not look the same.
    assert mon._parse_date("") is None
    assert mon._parse_date("next tuesday") is None
    assert mon._days_until(None) is None


def test_expiry_inside_the_window_warns():
    check = mon._expiry_verdict("bitbucket", _at(10), True, "")
    assert check.status == mon.WARNING
    # Floored, so 10 days out reports 9. Under-reporting days remaining is
    # the right direction to be wrong in.
    assert check.days_left == 9
    assert "day(s)" in check.detail


def test_expiry_outside_the_window_is_quiet():
    assert mon._expiry_verdict("bitbucket", _at(200), True, "").status == mon.OK


def test_unknown_expiry_does_not_warn_but_says_so():
    # Nagging daily about an unknown date would get the monitor muted.
    check = mon._expiry_verdict("bitbucket", None, True, "reading ok")
    assert check.status == mon.OK
    assert check.days_left is None


def test_a_broken_credential_is_failing_regardless_of_expiry():
    check = mon._expiry_verdict("github", _at(300), False, "HTTP 401")
    assert check.status == mon.FAILING
