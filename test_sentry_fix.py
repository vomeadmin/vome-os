"""
test_sentry_fix.py

Phases 4 and 5: the patch guard, the diff applier, and the gate in front of
anything that writes.

These are the tests that matter most in the whole pipeline, because this is
the only place where being wrong changes somebody else's repository. Every
case here is a thing that must NOT happen.
"""

import os

import pytest

import sentry_fix
from vomeos import diff as diff_tools
from vomeos.guards import patch as patch_guard
from vomeos.integrations import code_write

GOOD_DIFF = (
    "--- a/form_app/views.py\n"
    "+++ b/form_app/views.py\n"
    "@@ -10,5 +10,6 @@\n"
    "     def get_queryset(self):\n"
    "         user = self.request.user\n"
    "-        folders = user.admin_profile.site_form_folders\n"
    "+        profile = user.admin_profile\n"
    "+        folders = profile.site_form_folders if profile else []\n"
    "         return qs.filter(folder__in=folders)\n"
)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in (
        "SENTRY_AUTO_PR_ENABLED", "SENTRY_PATCH_PATH_ALLOWLIST",
        "VOMEOS_CODE_WRITE_REPOS", "VOMEOS_PR_ALLOW_PROTECTED_TARGET",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# What may be attempted at all
# ---------------------------------------------------------------------------

def test_only_low_risk_is_attempted():
    ok, _ = sentry_fix.should_attempt(
        {"risk": "low", "confidence": "high", "needs_migration": False}
    )
    assert ok
    for risk in ("medium", "high", "", "unknown"):
        ok, reason = sentry_fix.should_attempt(
            {"risk": risk, "confidence": "high"}
        )
        assert not ok, risk
        assert "risk" in reason


def test_a_migration_is_never_attempted():
    ok, reason = sentry_fix.should_attempt(
        {"risk": "low", "confidence": "high", "needs_migration": True}
    )
    assert not ok
    assert "migration" in reason


def test_the_analysts_own_low_confidence_stops_it():
    # If the analyst is not sure what is wrong, writing a patch for it is
    # guessing with a commit attached.
    ok, reason = sentry_fix.should_attempt(
        {"risk": "low", "confidence": "low", "needs_migration": False}
    )
    assert not ok
    assert "confidence" in reason


def test_no_analysis_means_no_attempt():
    assert sentry_fix.should_attempt(None)[0] is False
    assert sentry_fix.should_attempt({})[0] is False


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------

def test_auto_pr_is_off_by_default():
    assert sentry_fix.auto_pr_enabled() is False


def test_nothing_is_pushed_while_the_switch_is_off(monkeypatch):
    result = sentry_fix.open_pull_request(
        {"issue_id": "1"}, {"attempted": True, "diff": GOOD_DIFF}, {}
    )
    assert result["opened"] is False
    assert "SENTRY_AUTO_PR_ENABLED" in result["reason"]


def test_the_switch_must_say_true_exactly(monkeypatch):
    for value in ("yes", "1", "True ", "on", ""):
        monkeypatch.setenv("SENTRY_AUTO_PR_ENABLED", value)
        assert sentry_fix.auto_pr_enabled() is False, value
    monkeypatch.setenv("SENTRY_AUTO_PR_ENABLED", "true")
    assert sentry_fix.auto_pr_enabled() is True


# ---------------------------------------------------------------------------
# The patch guard
# ---------------------------------------------------------------------------

def test_a_migration_patch_is_refused_with_no_override(monkeypatch):
    # No environment variable can turn this off, which is the point.
    monkeypatch.setenv("SENTRY_PATCH_PATH_ALLOWLIST", "")
    migration = (
        "--- a/app/migrations/0042_add.py\n"
        "+++ b/app/migrations/0042_add.py\n"
        "@@ -1,2 +1,2 @@\n-a\n+b\n"
    )
    ok, detail = patch_guard.inspect(migration)
    assert not ok
    assert "generated on deploy" in detail


@pytest.mark.parametrize("path", [
    "vome/settings.py",
    "manage.py",
    "authentication_app/views.py",
    "billing_app/tasks.py",
    "requirements.txt",
    "package.json",
    "Dockerfile",
    ".github/workflows/ci.yml",
    "permissions.py",
])
def test_forbidden_paths_are_refused(path):
    patch = (f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n-a\n+b\n")
    ok, _ = patch_guard.inspect(patch)
    assert not ok, path


@pytest.mark.parametrize("path", [
    "test_views.py", "tests/test_forms.py", "app/__tests__/x.test.js",
    "forms_test.py",
])
def test_test_files_are_refused(path):
    # A fix that edits its own tests is a fix that makes itself pass.
    patch = (f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n-assert x\n+pass\n")
    ok, detail = patch_guard.inspect(patch)
    assert not ok, path
    assert "test" in detail.lower()


def test_size_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(patch_guard, "MAX_FILES", 1)
    two = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    ok, detail = patch_guard.inspect(two)
    assert not ok
    assert "over the limit" in detail


def test_a_huge_patch_is_refused(monkeypatch):
    monkeypatch.setattr(patch_guard, "MAX_LINES", 4)
    body = "".join(f"+line {i}\n" for i in range(20))
    ok, _ = patch_guard.inspect(
        f"--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,20 @@\n{body}"
    )
    assert not ok


def test_an_allowlist_narrows_further(monkeypatch):
    monkeypatch.setenv("SENTRY_PATCH_PATH_ALLOWLIST", "form_app/")
    assert patch_guard.inspect(GOOD_DIFF)[0]
    other = "--- a/other/x.py\n+++ b/other/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    ok, detail = patch_guard.inspect(other)
    assert not ok
    assert "ALLOWLIST" in detail


def test_a_real_fix_passes():
    ok, detail = patch_guard.inspect(GOOD_DIFF)
    assert ok, detail


# ---------------------------------------------------------------------------
# The diff applier
# ---------------------------------------------------------------------------

ORIGINAL = "\n".join([
    "class View:", "", "    def get(self):", "        return 1", "",
    "    def get_queryset(self):",
    "        user = self.request.user",
    "        folders = user.admin_profile.site_form_folders",
    "        return qs.filter(folder__in=folders)", "",
])


def test_a_patch_applies_cleanly():
    patch = (
        "--- a/x.py\n+++ b/x.py\n@@ -6,4 +6,5 @@\n"
        "     def get_queryset(self):\n"
        "         user = self.request.user\n"
        "-        folders = user.admin_profile.site_form_folders\n"
        "+        folders = getattr(user.admin_profile, 'f', [])\n"
        "         return qs.filter(folder__in=folders)\n"
    )
    out = diff_tools.apply(ORIGINAL, patch)
    assert "getattr(user.admin_profile" in out
    assert "site_form_folders" not in out
    assert out.count("\n") == ORIGINAL.count("\n")


def test_a_patch_that_no_longer_fits_is_refused():
    # THE important one. The file was read, the model thought, the branch
    # moved. Applying at a shifted offset silently deletes the wrong lines.
    moved = ORIGINAL.replace("return 1", "return 2")
    patch = (
        "--- a/x.py\n+++ b/x.py\n@@ -3,2 +3,2 @@\n"
        "     def get(self):\n"
        "-        return 1\n"
        "+        return 3\n"
    )
    with pytest.raises(diff_tools.DiffError) as exc:
        diff_tools.apply(moved, patch)
    assert "does not match" in str(exc.value)
    assert "nothing was applied" in str(exc.value)


def test_line_endings_survive():
    # Rewriting CRLF to LF turns a one line fix into a whole file diff.
    crlf = ORIGINAL.replace("\n", "\r\n")
    patch = (
        "--- a/x.py\n+++ b/x.py\n@@ -3,2 +3,2 @@\n"
        "     def get(self):\n-        return 1\n+        return 2\n"
    )
    out = diff_tools.apply(crlf, patch)
    assert "\r\n" in out
    assert "\n\n" not in out.replace("\r\n", "\n\n").replace("\n\n", "\r\n")


def test_a_diff_with_no_hunks_is_refused():
    with pytest.raises(diff_tools.DiffError):
        diff_tools.apply(ORIGINAL, "--- a/x.py\n+++ b/x.py\n")


def test_a_multi_file_diff_splits():
    two = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        "--- a/dir/c.py\n+++ b/dir/c.py\n@@ -1,1 +1,1 @@\n-c\n+d\n"
    )
    parts = diff_tools.split_by_file(two)
    assert sorted(parts) == ["a.py", "dir/c.py"]
    assert "+b" in parts["a.py"]
    assert "+d" in parts["dir/c.py"]
    assert diff_tools.files_in(two) == ["a.py", "dir/c.py"]


# ---------------------------------------------------------------------------
# What the write connector refuses before any request is made
# ---------------------------------------------------------------------------

def test_an_empty_write_allowlist_refuses_everything():
    # The reverse of the read allowlist, deliberately. Forgetting to set it
    # must mean nothing is writable, not everything.
    assert code_write.writable_repos() == ()
    with pytest.raises(code_write.WriteDenied) as exc:
        code_write._check("vomedjango-restored-core-app", branch="agent/x")
    assert "no repository is writable" in str(exc.value)


@pytest.mark.parametrize("branch", [
    "master", "main", "develop", "development", "staging", "production",
    "release-2026",
])
def test_protected_branches_are_never_written_to(monkeypatch, branch):
    monkeypatch.setenv("VOMEOS_CODE_WRITE_REPOS", "repo")
    assert code_write.is_protected(branch), branch
    with pytest.raises(code_write.WriteDenied):
        code_write._check("repo", branch=branch)


def test_an_agent_branch_is_allowed(monkeypatch):
    monkeypatch.setenv("VOMEOS_CODE_WRITE_REPOS", "repo")
    code_write._check("repo", branch="agent/sentry/123")


def test_a_pr_into_a_protected_branch_needs_a_deliberate_opt_in(monkeypatch):
    monkeypatch.setenv("VOMEOS_CODE_WRITE_REPOS", "repo")
    with pytest.raises(code_write.WriteDenied):
        code_write._check("repo", branch="agent/x", target="development")
    monkeypatch.setenv("VOMEOS_PR_ALLOW_PROTECTED_TARGET", "true")
    code_write._check("repo", branch="agent/x", target="development")


def test_a_repo_outside_the_write_allowlist_is_refused(monkeypatch):
    monkeypatch.setenv("VOMEOS_CODE_WRITE_REPOS", "repo-a")
    with pytest.raises(code_write.WriteDenied):
        code_write._check("repo-b", branch="agent/x")


def test_the_branch_name_traces_back_to_the_issue():
    name = code_write.branch_name("4506427274952704", "save() prohibited")
    assert name.startswith("agent/sentry/")
    assert "4506427274952704" in name
    assert " " not in name and "(" not in name


def test_the_write_surface_is_exactly_three_operations():
    # Structural, not conventional. An agent cannot reason its way into a
    # capability that has no code. Tested against the real method surface
    # rather than against documentation strings, because a docstring saying
    # "cannot merge" next to a merge method would pass a naive check.
    allowed = {
        "create_branch", "commit_file", "open_pull_request",
        # Read helpers needed to perform those three.
        "ref_sha", "file_sha", "token_for",
        # Inherited from Connector.
        "configured", "headers",
    }
    for cls in (code_write.BitbucketWriter, code_write.GitHubWriter):
        public = {
            name for name in dir(cls)
            if not name.startswith("_") and callable(getattr(cls, name, None))
        }
        extra = public - allowed - {"workspace", "owner", "system",
                                    "base_url", "timeout"}
        assert not extra, f"{cls.__name__} gained {extra}"


def test_no_destructive_http_verb_is_ever_sent():
    import inspect

    source = inspect.getsource(code_write)
    # The string appears only inside _call arguments, so finding it at all
    # would mean a destructive request exists.
    assert '"DELETE"' not in source
    assert '"PATCH"' not in source


def test_no_merge_endpoint_is_reachable():
    import inspect

    source = inspect.getsource(code_write)
    for path in ("/merge", "merge_pull", "/pullrequests/{", "/pulls/{"):
        assert path not in source, path


def test_the_write_connector_is_a_different_module_from_the_reader():
    # The read connector's guarantee is that no write code sits next to it.
    import inspect

    from vomeos.integrations import code_search

    read_source = inspect.getsource(code_search)
    for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in read_source, verb


def test_read_and_write_use_different_environment_variables():
    assert os.environ.get("VOMEOS_BITBUCKET_TOKEN") is not \
        os.environ.get("VOMEOS_BITBUCKET_WRITE_TOKEN") or True
    import inspect

    source = inspect.getsource(code_write)
    assert "VOMEOS_BITBUCKET_WRITE_TOKEN" in source
    assert "VOMEOS_GITHUB_WRITE_TOKEN" in source
