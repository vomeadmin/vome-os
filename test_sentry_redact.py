"""
test_sentry_redact.py

The redactor's job is to be right about two opposite failures, and both of
them are silent.

Leaking: a customer's email, a session cookie or an Authorization header ends
up in a Postgres row, a model prompt and a Slack channel, and nobody notices
because the pipeline worked.

Over-redacting: the stack trace is eaten along with the secrets, and the
pipeline still looks like it is working while producing diagnoses from
nothing.

So the cases below are paired. For every "this must be removed" there is a
"this must survive".
"""

import json

import sentry_redact as red


def _event():
    """A Sentry event with the shapes that actually carry secrets."""
    return {
        "event_id": "abc123",
        "platform": "python",
        "request": {
            "url": "https://app.vome.com/api/shifts/",
            "method": "POST",
            "headers": {
                "Authorization": "Bearer sk_live_abcdefghijklmnop",
                "Cookie": "sessionid=xyz; csrftoken=abc",
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
                "X-Internal-Debug": "whatever",
            },
            "cookies": {"sessionid": "xyz"},
            "data": {"email": "donor@example.org", "note": "hello"},
        },
        "user": {
            "id": "4821",
            "email": "admin@bigcharity.org",
            "username": "bigadmin",
            "ip_address": "203.0.113.9",
        },
        "exception": {
            "values": [
                {
                    "type": "AttributeError",
                    "value": "'NoneType' object has no attribute 'site'",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "opportunity_app/views.py",
                                "function": "reserve_shift",
                                "lineno": 412,
                                "vars": {
                                    "shift_id": "b3f1",
                                    "password": "hunter2",
                                    "user_email": "admin@bigcharity.org",
                                },
                            }
                        ]
                    },
                }
            ]
        },
        "tags": [["environment", "production"], ["release", "1.42.0"]],
    }


def _blob(payload) -> str:
    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Secrets must not survive
# ---------------------------------------------------------------------------

def test_authorization_and_cookies_are_dropped():
    clean = red.redact(_event())
    blob = _blob(clean)
    assert "sk_live_abcdefghijklmnop" not in blob
    assert "sessionid=xyz" not in blob
    assert clean["request"]["headers"]["Authorization"] == red.REDACTED
    assert clean["request"]["headers"]["Cookie"] == red.REDACTED
    assert clean["request"]["cookies"] == red.REDACTED


def test_unlisted_headers_are_dropped_by_allowlist():
    # The keys in a headers container are chosen by whoever sent the request,
    # so anything unrecognised is assumed sensitive rather than assumed safe.
    clean = red.redact(_event())
    assert clean["request"]["headers"]["X-Internal-Debug"] == red.REDACTED


def test_useful_headers_survive_the_allowlist():
    clean = red.redact(_event())
    headers = clean["request"]["headers"]
    assert headers["User-Agent"] == "Mozilla/5.0"
    assert headers["Content-Type"] == "application/json"


def test_emails_are_removed_everywhere_they_appear():
    clean = red.redact(_event())
    blob = _blob(clean)
    assert "admin@bigcharity.org" not in blob
    assert "donor@example.org" not in blob


def test_password_in_frame_vars_is_dropped_but_other_vars_survive():
    # Frame locals are the most useful thing in an event and the most likely
    # place for a credential to be sitting in plain sight.
    clean = red.redact(_event())
    frame = clean["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["vars"]["password"] == red.REDACTED
    assert frame["vars"]["shift_id"] == "b3f1"


def test_user_identifiers_are_hashed_not_stored():
    clean = red.redact(_event())
    user = clean["user"]
    assert user["email"].startswith("u_")
    assert user["username"].startswith("u_")
    assert user["ip_address"].startswith("u_")
    assert "203.0.113.9" not in _blob(clean)


def test_hash_is_stable_so_the_same_person_is_recognisable():
    # "How many distinct users are affected" has to stay answerable without
    # holding who they are.
    first = red.pseudonymize("admin@bigcharity.org")
    second = red.pseudonymize("admin@bigcharity.org")
    assert first == second
    assert first != red.pseudonymize("someone.else@bigcharity.org")


# ---------------------------------------------------------------------------
# Diagnosis must survive
# ---------------------------------------------------------------------------

def test_the_stack_trace_survives_intact():
    # A redactor that eats the trace protects nothing and breaks everything.
    clean = red.redact(_event())
    frame = clean["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["filename"] == "opportunity_app/views.py"
    assert frame["function"] == "reserve_shift"
    assert frame["lineno"] == 412
    assert clean["exception"]["values"][0]["type"] == "AttributeError"


def test_release_and_environment_tags_survive():
    clean = red.redact(_event())
    assert ["environment", "production"] in [
        list(t) for t in clean["tags"]
    ]
    assert ["release", "1.42.0"] in [list(t) for t in clean["tags"]]


def _mobile_event():
    """A real React Native App context, as Sentry renders it."""
    return {
        "contexts": {
            "app": {
                "app_identifier": "com.vomeinc.vomemobile",
                "app_name": "Vome",
                "app_version": "3.6.5",
                "app_build": "168",
                "build_type": "app store",
                "device_app_hash": "6118a481db630853605db1535d7ab43e3b944801",
                "app_memory": 284639232,
                "in_foreground": True,
            },
            "device": {
                "model": "iPhone15,2",
                "name": "Sam's iPhone",
                "device_unique_identifier": "BF294B8F-C410-364C-AB0A-99C32348",
            },
        }
    }


def test_mobile_device_identifiers_are_hashed():
    # A device id is persistent and singles out one person's handset, which
    # makes it personal data even though it contains no name. It needs its own
    # rule because the value patterns deliberately leave long hex alone.
    clean = red.redact(_mobile_event())
    blob = _blob(clean)
    assert "6118a481db630853605db1535d7ab43e3b944801" not in blob
    assert "BF294B8F-C410-364C-AB0A-99C32348" not in blob
    assert clean["contexts"]["app"]["device_app_hash"].startswith("u_")


def test_the_device_name_is_hashed_because_it_carries_a_person():
    clean = red.redact(_mobile_event())
    assert "Sam's iPhone" not in _blob(clean)


def test_mobile_diagnostic_context_survives():
    # Version, build and device model are how you tell "every 3.6.5 on an
    # iPhone 15" from "one person's handset". None of it is personal.
    clean = red.redact(_mobile_event())
    app = clean["contexts"]["app"]
    assert app["app_version"] == "3.6.5"
    assert app["app_build"] == "168"
    assert app["app_identifier"] == "com.vomeinc.vomemobile"
    assert app["in_foreground"] is True
    assert clean["contexts"]["device"]["model"] == "iPhone15,2"


def test_commit_shas_are_not_eaten():
    # A 40 character hex string is a git SHA far more often than a secret,
    # and it is what "what shipped just before this" is answered with.
    sha = "9c565cda1f2b3c4d5e6f708192a3b4c5d6e7f809"
    assert red.redact_text(f"deployed at {sha}") == f"deployed at {sha}"


# ---------------------------------------------------------------------------
# Value patterns
# ---------------------------------------------------------------------------

def test_jwt_is_scrubbed():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij"
    assert token not in red.redact_text(f"auth failed for {token}")


def test_stripe_and_aws_keys_are_scrubbed():
    text = "sk_live_51H0abcdefghij and AKIAIOSFODNN7EXAMPLE"
    cleaned = red.redact_text(text)
    assert "sk_live_51H0abcdefghij" not in cleaned
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned


def test_real_card_numbers_are_scrubbed():
    for card in (
        "4111 1111 1111 1111",   # Visa
        "5555555555554444",      # Mastercard
        "378282246310005",       # Amex
        "6011111111111117",      # Discover
    ):
        assert "[card]" in red.redact_text(f"paid with {card}"), card


def test_a_sentry_issue_id_is_not_mistaken_for_a_card():
    # REGRESSION. This exact id reached the queue as "[card]" because the
    # issuer-prefix pattern is really a "starts with 4 or 5" pattern, and a
    # Sentry issue id is a 16 digit number that often does.
    #
    # The issue id is the ledger's primary key. Corrupting it collapses every
    # issue into one row, so the first claims it and every issue after is
    # marked "already known" and dropped, silently. Luhn is what tells the
    # two apart.
    assert red.redact_text("4506427274952704") == "4506427274952704"
    assert red.redact("4506427274952704") == "4506427274952704"


def test_long_ids_that_fail_luhn_survive():
    for value in (
        "1234567890123456",
        "4506427274952704",
        "5012345678901234",
        "4000000000000001",
    ):
        assert value in red.redact_text(f"shift {value}"), value


def test_identifier_keys_are_never_pattern_scrubbed():
    # The second line of defence. Even if a pattern matched, a value under an
    # identifier key is truncated and never rewritten.
    clean = red.redact(
        {"id": "4506427274952704", "issue_id": "5555555555554444"}
    )
    assert clean["id"] == "4506427274952704"
    assert clean["issue_id"] == "5555555555554444"


def test_named_secret_assignment_is_scrubbed():
    cleaned = red.redact_text('api_key="abc123def456ghi"')
    assert "abc123def456ghi" not in cleaned


# ---------------------------------------------------------------------------
# Structural safety
# ---------------------------------------------------------------------------

def test_input_is_never_mutated():
    # The caller keeps the raw body for signature checking and logging. A
    # redactor that mutates in place would be a very quiet bug.
    original = _event()
    copy = json.loads(json.dumps(original))
    red.redact(original)
    assert original == copy


def test_long_strings_are_truncated():
    long_value = "x" * (red.MAX_STRING + 500)
    assert len(red.redact_text(long_value)) < red.MAX_STRING + 100


def test_deep_nesting_terminates():
    payload = current = {}
    for _ in range(red.MAX_DEPTH + 20):
        current["next"] = {}
        current = current["next"]
    assert red.redact(payload)  # does not recurse forever


def test_long_lists_are_capped():
    payload = {"breadcrumbs": list(range(red.MAX_ITEMS + 100))}
    clean = red.redact(payload)
    assert len(clean["breadcrumbs"]) == red.MAX_ITEMS + 1  # plus the marker


def test_non_dict_payloads_do_not_raise():
    assert red.redact("admin@bigcharity.org") == "[email]"
    assert red.redact(None) is None
    assert red.redact([{"email": "a@b.co"}])[0]["email"].startswith("u_")
