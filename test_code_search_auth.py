"""
test_code_search_auth.py

Bitbucket has three credential shapes in circulation and they do not
authenticate the same way. Sending the wrong header returns a bare 401 with no
hint about which of the two it wanted, so the cost of getting this wrong is
an afternoon rather than an exception.

Also pins the thing that is not allowed to change: these connectors can read
and cannot write. That is enforced by the absence of any write code, so the
test asserts the absence.
"""

import base64

from vomeos.integrations import code_search


def _clear(monkeypatch):
    for name in (
        "VOMEOS_BITBUCKET_TOKEN", "VOMEOS_BITBUCKET_EMAIL",
        "VOMEOS_BITBUCKET_WORKSPACE", "VOMEOS_GITHUB_TOKEN",
        "VOMEOS_GITHUB_OWNER", "VOMEOS_CODE_REPOS",
        "VOMEOS_GITHUB_TOKEN_VOMEADMIN", "VOMEOS_GITHUB_TOKEN_SAMFAGEN15",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Bitbucket auth shape
# ---------------------------------------------------------------------------

def test_bearer_is_the_default(monkeypatch):
    # What a Workspace Access Token and a SCOPED Atlassian API token want.
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_BITBUCKET_TOKEN", "tok123")
    assert code_search.Bitbucket().headers()["Authorization"] == "Bearer tok123"


def test_an_email_switches_to_basic(monkeypatch):
    # What an UNSCOPED Atlassian API token and the old app passwords want.
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_BITBUCKET_TOKEN", "tok123")
    monkeypatch.setenv("VOMEOS_BITBUCKET_EMAIL", "sam@vomevolunteer.co")
    header = code_search.Bitbucket().headers()["Authorization"]
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "sam@vomevolunteer.co:tok123"


def test_github_is_always_bearer(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN", "ghp_x")
    assert code_search.GitHub().headers()["Authorization"] == "Bearer ghp_x"


# ---------------------------------------------------------------------------
# One token per GitHub account
# ---------------------------------------------------------------------------

def test_each_owner_gets_its_own_token(monkeypatch):
    # A fine-grained token is bound to one account at creation and cannot be
    # changed, so two accounts means two tokens. vome-react is under the
    # vomeadmin org, VomeApp under a personal account.
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN_VOMEADMIN", "tok_react")
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN_SAMFAGEN15", "tok_app")
    client = code_search.GitHub()
    assert client.token_for("vomeadmin") == "tok_react"
    assert client.token_for("samfagen15") == "tok_app"
    assert client.headers("vomeadmin")["Authorization"] == "Bearer tok_react"
    assert client.headers("samfagen15")["Authorization"] == "Bearer tok_app"


def test_the_env_name_is_derived_from_the_owner():
    assert code_search.GitHub.token_env_name("samfagen15") == (
        "VOMEOS_GITHUB_TOKEN_SAMFAGEN15"
    )
    # Hyphens and dots are legal in GitHub names and illegal in env names.
    assert code_search.GitHub.token_env_name("vome-admin.co") == (
        "VOMEOS_GITHUB_TOKEN_VOME_ADMIN_CO"
    )


def test_a_single_shared_token_still_works(monkeypatch):
    # The one-account setup must not need per-owner variables.
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN", "shared")
    client = code_search.GitHub()
    assert client.token_for("anyone") == "shared"


def test_the_owner_specific_token_wins_over_the_shared_one(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN", "shared")
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN_VOMEADMIN", "specific")
    assert code_search.GitHub().token_for("vomeadmin") == "specific"


def test_configured_answers_two_different_questions(monkeypatch):
    # With an owner: can we reach that account. Without: is GitHub usable at
    # all. The second is what Connector._call asks as a guard, and it must
    # not answer no just because no default owner is set.
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN_SAMFAGEN15", "tok")
    client = code_search.GitHub()
    assert client.configured() is True
    assert client.configured("samfagen15") is True
    assert client.configured("vomeadmin") is False


def test_owners_are_discovered_from_the_environment(monkeypatch):
    # Adding a third account should be a Railway variable, not a code change.
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN_VOMEADMIN", "a")
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN_SAMFAGEN15", "b")
    assert code_search.github_owners_configured() == (
        "samfagen15", "vomeadmin"
    )


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------

def test_a_repo_outside_the_allowlist_is_refused(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_BITBUCKET_TOKEN", "tok")
    monkeypatch.setenv("VOMEOS_BITBUCKET_WORKSPACE", "vomedjango")
    monkeypatch.setenv("VOMEOS_CODE_REPOS", "vomedjango-restored-core-app")
    result = code_search.Bitbucket().read_file("some-other-repo", "x.py")
    assert not result.ok
    assert "not in VOMEOS_CODE_REPOS" in result.error


def test_the_allowed_repo_is_not_refused(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("VOMEOS_BITBUCKET_TOKEN", "tok")
    monkeypatch.setenv("VOMEOS_BITBUCKET_WORKSPACE", "vomedjango")
    monkeypatch.setenv("VOMEOS_CODE_REPOS", "vomedjango-restored-core-app")
    # Fails on the network, not on the allowlist. The distinction is the test.
    result = code_search.Bitbucket().read_file(
        "vomedjango-restored-core-app", "manage.py"
    )
    assert "not in VOMEOS_CODE_REPOS" not in (result.error or "")


# ---------------------------------------------------------------------------
# No writes. Structural, not conventional.
# ---------------------------------------------------------------------------

def test_neither_connector_can_write():
    # The access control is the absence of the code, not an instruction in a
    # charter that a model might reason around. If a write method is ever
    # added here, this test is where that decision gets noticed.
    import inspect

    source = inspect.getsource(code_search)
    for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in source, f"{verb} appeared in code_search.py"


def test_github_methods_take_a_per_call_owner():
    # vome-react is under vomeadmin, VomeApp under samfagen15. A single
    # owner cannot reach both, and the failure is a 404 that reads like
    # "the file does not exist".
    import inspect

    for name in ("read_file", "recent_commits", "search_code"):
        sig = inspect.signature(getattr(code_search.GitHub, name))
        assert "owner" in sig.parameters, name


def test_commit_history_can_be_asked_for_a_branch():
    # dev and prod are the same repo at different refs. Bitbucket's bare
    # /commits answers for the main branch and looks plausible doing it.
    import inspect

    for cls in (code_search.Bitbucket, code_search.GitHub):
        sig = inspect.signature(cls.recent_commits)
        assert "ref" in sig.parameters, cls.__name__
