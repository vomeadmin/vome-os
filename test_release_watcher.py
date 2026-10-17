"""
test_release_watcher.py

The watcher exists to be right about one thing: whether a client can be told
their fix is live. So the cases that matter are the ones where it could lie in
either direction: calling a fix live when one of its repos has not shipped,
calling it live off a green build the site is not serving, reading a failed API
call as "not merged", or telling Slack the same news every 15 minutes.
"""

from datetime import datetime, timedelta, timezone

import pytest

import release_watcher as rw
from vomeos.integrations.base import IntegrationResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _History:
    """A linear master history. `branch` resolves to the head commit."""

    def __init__(self, commits, head=None, broken=False):
        self.commits = list(commits)
        self.head = head or self.commits[-1]
        self.broken = broken

    def resolve(self, ref):
        if ref == "master":
            return self.head
        for c in self.commits:
            if c.startswith(ref) or ref.startswith(c):
                return c
        return None

    def contains(self, sha, head):
        a, b = self.resolve(sha), self.resolve(head)
        if a is None or b is None:
            return None
        return self.commits.index(a) <= self.commits.index(b)


class FakeGitHub:
    def __init__(self, history, runs=(), runs_error=""):
        self.history = history
        self.runs = list(runs)
        self.runs_error = runs_error

    def compare(self, repo, base, head, owner=""):
        if self.history.broken:
            return IntegrationResult("github", "compare", False,
                                     error="HTTP 401: bad credentials")
        answer = self.history.contains(base, head)
        if answer is None:
            return IntegrationResult("github", "compare", False,
                                     error="HTTP 404: no such commit")
        status = "ahead" if answer else "diverged"
        return IntegrationResult("github", "compare", True,
                                 data={"status": status})

    def workflow_runs(self, repo, workflow, branch="", limit=10, owner=""):
        if self.runs_error:
            return IntegrationResult("github", "workflow_runs", False,
                                     error=self.runs_error)
        return IntegrationResult(
            "github", "workflow_runs", True,
            data={"workflow_runs": self.runs},
        )


class FakeBitbucket:
    workspace = "vomedjango"

    def __init__(self, history, runs=()):
        self.history = history
        self.runs = list(runs)

    def merge_base(self, repo, a, b):
        answer = self.history.contains(a, b)
        if answer is None:
            return IntegrationResult("bitbucket", "merge_base", False,
                                     error="HTTP 404")
        base = self.history.resolve(a) if answer else "0000000000"
        return IntegrationResult("bitbucket", "merge_base", True,
                                 data={"hash": base + "f" * 30})

    def pipelines(self, repo, branch, limit=10):
        return IntegrationResult("bitbucket", "pipelines", True,
                                 data={"values": self.runs})


def gh_run(run_id, head, status="completed", conclusion="success"):
    return {"id": run_id, "head_sha": head, "status": status,
            "conclusion": conclusion,
            "html_url": f"https://github.com/run/{run_id}"}


def bb_run(number, head, name="COMPLETED", result="SUCCESSFUL"):
    return {"build_number": number,
            "state": {"name": name, "result": {"name": result}},
            "target": {"commit": {"hash": head}}}


class MemoryLedger:
    """Same contract as rw.Ledger, in a dict."""

    def __init__(self, available=True):
        self.available = available
        self.pairs = {}
        self.fixes = {}

    def record_pair(self, task_id, check):
        if not self.available:
            return {}
        key = (task_id, check.alias, check.sha)
        row = self.pairs.setdefault(key, {"merged_at": None,
                                          "deployed_at": None})
        now = rw._now()
        if check.state in (rw.BUILDING, rw.DEPLOYED, rw.BUILD_FAILED):
            row["merged_at"] = row["merged_at"] or now
        if check.state == rw.DEPLOYED:
            row["deployed_at"] = row["deployed_at"] or now
        row["state"] = check.state
        return dict(row)

    def last_alerted(self, task_id, dry_run):
        column = "dry" if dry_run else "real"
        return self.fixes.get(task_id, {}).get(column)

    def record_fix(self, task_id, name, fix_commit, state, *, alerted,
                   dry_run):
        row = self.fixes.setdefault(task_id, {})
        row["state"] = state
        if alerted:
            row["dry" if dry_run else "real"] = state


def _options():
    return [
        {"id": rw.RELEASE_CHECK_OPTIONS["waiting"], "orderindex": 0},
        {"id": rw.RELEASE_CHECK_OPTIONS["failed"], "orderindex": 1},
        {"id": rw.RELEASE_CHECK_OPTIONS["live"], "orderindex": 2},
    ]


def parent(task_id, fix_commit, release_check=0):
    return {
        "id": task_id, "name": f"[#9500] fix {task_id}", "parent": None,
        "url": f"https://app.clickup.com/t/{task_id}",
        "custom_fields": [
            {"id": rw.FIELD_FIX_COMMIT, "value": fix_commit},
            {"id": rw.FIELD_RELEASE_CHECK, "value": release_check,
             "type_config": {"options": _options()}},
        ],
    }


def child(task_id, parent_id, number):
    return {
        "id": task_id, "name": f"[#{number}] Client ticket",
        "parent": parent_id, "url": f"https://app.clickup.com/t/{task_id}",
        "custom_fields": [
            {"id": rw.FIELD_ZOHO_TICKET_LINK,
             "value": f"https://desk.zoho.com/x/{number}"},
        ],
    }


@pytest.fixture
def world(monkeypatch):
    """Wire the watcher to fakes. Returns a mutable namespace."""

    class W:
        tasks = []
        posts = []
        writes = []
        stamp = ""
        github = FakeGitHub(_History(["a1a1a11", "a2a2a22", "a3a3a33"]))
        bitbucket = FakeBitbucket(_History(["c1c1c11", "c2c2c22", "c3c3c33"]))
        ledger = MemoryLedger()

    monkeypatch.setattr(rw, "CLICKUP_API_TOKEN", "tok")
    monkeypatch.delenv("RELEASE_WATCHER_ENABLED", raising=False)
    monkeypatch.setattr(rw, "_list_on_dev_tasks", lambda: W.tasks)
    monkeypatch.setattr(rw, "_set_release_check",
                        lambda t, v: W.writes.append((t, v)))
    monkeypatch.setattr(rw, "_post",
                        lambda text: W.posts.append(text) or True)
    monkeypatch.setattr(rw, "_live_stamp", lambda url: W.stamp)
    monkeypatch.setattr(rw, "GitHub", lambda: W.github)
    monkeypatch.setattr(rw, "Bitbucket", lambda: W.bitbucket)
    monkeypatch.setattr(rw, "Ledger", lambda **k: W.ledger)
    return W


def _state(summary, task_id):
    return next(f for f in summary["fixes"] if f["task_id"] == task_id)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_the_real_field_format():
    pairs, errors = rw.parse_fix_commit("core@068fed0d; web@311a422d5")
    assert pairs == [("core", "068fed0d"), ("web", "311a422d5")]
    assert errors == []


def test_aliases_and_separators():
    pairs, errors = rw.parse_fix_commit("backend@abcdef1, frontend@1234567")
    assert pairs == [("core", "abcdef1"), ("web", "1234567")]
    assert errors == []


def test_a_bad_token_is_an_error_not_a_skip():
    """A mistyped web commit must not let the fix go live on core alone."""
    pairs, errors = rw.parse_fix_commit("core@068fed0d; web@zzz")
    assert pairs == [("core", "068fed0d")]
    assert len(errors) == 1


def test_unknown_repo_is_an_error():
    pairs, errors = rw.parse_fix_commit("chats@abcdef1")
    assert pairs == [] and "unknown repo" in errors[0]


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def test_not_merged_is_waiting_and_silent(world):
    world.github.history.head = "a2a2a22"
    world.tasks = [parent("p1", "web@a3a3a33")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_WAITING
    assert _state(summary, "p1")["pairs"][0]["state"] == rw.PENDING_MERGE
    assert world.posts == [] and world.writes == []


def test_merged_but_build_older_is_building(world):
    world.github.runs = [gh_run(1, "a2a2a22")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    summary = rw.run_release_watcher(dry_run=False)
    pair = _state(summary, "p1")["pairs"][0]
    assert pair["state"] == rw.BUILDING
    assert world.posts == []


def test_web_fix_goes_live_and_names_the_clients(world):
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.stamp = "a3a3a33"
    world.tasks = [
        parent("p1", "web@a2a2a22"),
        child("k1", "p1", "9415"),
    ]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_LIVE
    assert world.writes == [("p1", "live")]
    assert len(world.posts) == 1
    post = world.posts[0]
    assert "Fix live" in post and "1 client to notify" in post
    assert "Zoho #9415" in post
    assert "Nothing has been sent" in post
    assert chr(0x2014) not in post  # no em dashes


def test_green_build_the_site_is_not_serving_is_not_live(world):
    """CI green, CloudFront still on the old build: the client can't see it."""
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.stamp = "a1a1a11"
    world.tasks = [parent("p1", "web@a3a3a33")]
    summary = rw.run_release_watcher(dry_run=False)
    pair = _state(summary, "p1")["pairs"][0]
    assert pair["state"] == rw.BUILDING
    assert "still serves a1a1a11" in pair["detail"]
    assert world.writes == []


def test_unreadable_stamp_does_not_block(world):
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.stamp = ""
    world.tasks = [parent("p1", "web@a3a3a33")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_LIVE


def test_multi_repo_fix_waits_for_every_commit(world):
    """#9500: core is live, web is not. Not live."""
    world.bitbucket.runs = [bb_run(40, "c3c3c33")]
    world.github.history.head = "a1a1a11"
    world.tasks = [parent("p1", "core@c2c2c22; web@a2a2a22")]
    summary = rw.run_release_watcher(dry_run=False)
    fix = _state(summary, "p1")
    assert fix["state"] == rw.FIX_WAITING
    states = {p["repo"]: p["state"] for p in fix["pairs"]}
    assert states == {"core": rw.DEPLOYED, "web": rw.PENDING_MERGE}
    assert world.writes == [] and world.posts == []


def test_multi_repo_fix_goes_live_when_both_ship(world):
    world.bitbucket.runs = [bb_run(40, "c3c3c33")]
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.stamp = "a3a3a33"
    world.tasks = [parent("p1", "core@c2c2c22; web@a2a2a22")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_LIVE


def test_failed_build_is_failed(world):
    world.bitbucket.runs = [bb_run(41, "c3c3c33", result="FAILED"),
                            bb_run(40, "c1c1c11")]
    world.tasks = [parent("p1", "core@c2c2c22")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_FAILED
    assert world.writes == [("p1", "failed")]
    assert "pipelines/results/41" in world.posts[0]


def test_a_retry_after_a_failure_is_building_not_failed(world):
    world.bitbucket.runs = [
        bb_run(42, "c3c3c33", name="IN_PROGRESS", result=""),
        bb_run(41, "c3c3c33", result="FAILED"),
    ]
    world.tasks = [parent("p1", "core@c2c2c22")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["pairs"][0]["state"] == rw.BUILDING
    assert world.posts == []


def test_an_older_success_still_counts_when_a_newer_build_runs(world):
    world.github.runs = [gh_run(8, "a3a3a33", status="in_progress",
                                conclusion=None),
                         gh_run(7, "a2a2a22")]
    world.tasks = [parent("p1", "web@a2a2a22")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_LIVE


def test_merged_two_hours_without_a_deploy_is_failed(world):
    world.github.runs = [gh_run(7, "a2a2a22")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    rw.run_release_watcher(dry_run=False)
    key = ("p1", "web", "a3a3a33")
    world.ledger.pairs[key]["merged_at"] = (
        rw._now() - timedelta(minutes=125)
    )
    summary = rw.run_release_watcher(dry_run=False)
    fix = _state(summary, "p1")
    assert fix["state"] == rw.FIX_FAILED
    assert "2h 5m" in fix["pairs"][0]["detail"]


def test_an_api_failure_is_unknown_not_waiting(world):
    """A dead token must never read as 'not merged yet'."""
    world.github.history.broken = True
    world.tasks = [parent("p1", "web@a3a3a33", release_check=1)]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_UNKNOWN
    # Nothing written: Failed stays Failed, because we do not know.
    assert world.writes == []
    assert "could not run" in world.posts[0]
    assert "401" in world.posts[0]


def test_unreadable_build_list_is_unknown(world):
    world.github.runs_error = "HTTP 403: Resource not accessible"
    world.tasks = [parent("p1", "web@a3a3a33")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_UNKNOWN


def test_empty_or_bad_fix_commit_is_unknown(world):
    world.tasks = [parent("p1", "web@a3a3a33; mystery@abcdef1")]
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_UNKNOWN


def test_live_is_announced_once(world):
    """Every 15 minutes, forever, would be muted within a day."""
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    rw.run_release_watcher(dry_run=False)
    world.tasks = [parent("p1", "web@a3a3a33", release_check=2)]
    rw.run_release_watcher(dry_run=False)
    rw.run_release_watcher(dry_run=False)
    assert len(world.posts) == 1
    assert world.writes == [("p1", "live")]


def test_a_failed_slack_post_is_retried(monkeypatch, world):
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    monkeypatch.setattr(rw, "_post", lambda text: False)
    rw.run_release_watcher(dry_run=False)
    monkeypatch.setattr(rw, "_post",
                        lambda text: world.posts.append(text) or True)
    rw.run_release_watcher(dry_run=False)
    assert len(world.posts) == 1


def test_failed_then_recovered_goes_live_and_is_announced(world):
    world.bitbucket.runs = [bb_run(41, "c3c3c33", result="FAILED")]
    world.tasks = [parent("p1", "core@c2c2c22")]
    rw.run_release_watcher(dry_run=False)
    world.bitbucket.runs = [bb_run(42, "c3c3c33"),
                            bb_run(41, "c3c3c33", result="FAILED")]
    world.tasks = [parent("p1", "core@c2c2c22", release_check=1)]
    rw.run_release_watcher(dry_run=False)
    assert world.writes == [("p1", "failed"), ("p1", "live")]
    assert len(world.posts) == 2 and "Fix live" in world.posts[1]


def test_recovering_build_resets_failed_to_waiting(world):
    world.bitbucket.runs = [
        bb_run(42, "c3c3c33", name="IN_PROGRESS", result=""),
        bb_run(41, "c3c3c33", result="FAILED"),
    ]
    world.tasks = [parent("p1", "core@c2c2c22", release_check=1)]
    rw.run_release_watcher(dry_run=False)
    assert world.writes == [("p1", "waiting")]


def test_dry_run_writes_nothing_and_says_so(world):
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    rw.run_release_watcher(dry_run=True)
    rw.run_release_watcher(dry_run=True)
    assert world.writes == []
    assert len(world.posts) == 1
    assert world.posts[0].startswith("[dry run")
    assert "would be set to Live" in world.posts[0]


def test_leaving_dry_run_announces_for_real(world):
    """The dry run's memory must not swallow the real announcement."""
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    rw.run_release_watcher(dry_run=True)
    rw.run_release_watcher(dry_run=False)
    assert len(world.posts) == 2
    assert not world.posts[1].startswith("[dry run")
    assert world.writes == [("p1", "live")]


def test_the_scheduled_job_runs_live(world):
    """No switch to forget: the job writes Release Check and posts."""
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    summary = rw.run_release_watcher()
    assert summary["dry_run"] is False
    assert world.writes == [("p1", "live")]
    assert not world.posts[0].startswith("[dry run")


def test_switched_off(monkeypatch, world):
    monkeypatch.setenv("RELEASE_WATCHER_ENABLED", "false")
    world.tasks = [parent("p1", "web@a3a3a33")]
    assert rw.run_release_watcher()["status"] == "disabled"


def test_children_and_parents_without_fix_commit_are_skipped(world):
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [
        parent("p1", "web@a3a3a33"),
        parent("p2", ""),
        child("k1", "p1", "9415"),
    ]
    summary = rw.run_release_watcher(dry_run=False)
    assert [f["task_id"] for f in summary["fixes"]] == ["p1"]
    assert _state(summary, "p1")["children"] == ["k1"]


def test_a_fix_with_no_clients_says_so(world):
    """The kiosk fix was soft-closed with no reply owed."""
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    rw.run_release_watcher(dry_run=False)
    assert "nobody to notify" in world.posts[0]


def test_unreachable_clickup_is_reported_not_empty(monkeypatch, world):
    def boom():
        raise RuntimeError("ClickUp 503")

    monkeypatch.setattr(rw, "_list_on_dev_tasks", boom)
    summary = rw.run_release_watcher(dry_run=False)
    assert summary["status"] == "unreachable"


def test_without_a_ledger_it_posts_only_when_clickup_changes(world):
    world.ledger = MemoryLedger(available=False)
    world.github.runs = [gh_run(7, "a3a3a33")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    rw.run_release_watcher(dry_run=False)
    world.tasks = [parent("p1", "web@a3a3a33", release_check=2)]
    rw.run_release_watcher(dry_run=False)
    assert len(world.posts) == 1


def test_never_moves_task_status():
    """The on-prod handler auto-sends. Only the Release Check field moves."""
    import inspect

    source = inspect.getsource(rw)
    # Exactly one ClickUp write in the module, and it is the field endpoint.
    assert source.count('method="POST"') == 1
    assert "method=\"PUT\"" not in source
    writer = inspect.getsource(rw._set_release_check)
    assert 'method="POST"' in writer
    assert "/field/{FIELD_RELEASE_CHECK}" in writer


def test_release_check_reads_orderindex():
    task = parent("p1", "web@a3a3a33", release_check=2)
    assert rw._release_check_value(task) == "live"
    task = parent("p1", "web@a3a3a33", release_check=None)
    assert rw._release_check_value(task) == ""


def test_live_stamp_parsing(monkeypatch):
    class Resp:
        text = ('<head><script>window.__VOME_BUILD__={"sha":"c3c8052",'
                '"time":"2026-10-17T00:08:42.216Z"};</script></head>')

        def raise_for_status(self):
            return None

    monkeypatch.setattr(rw.httpx, "get", lambda *a, **k: Resp())
    assert rw._live_stamp("https://x/index.html") == "c3c8052"


def test_merged_at_naive_timestamp_is_utc(world):
    """Postgres hands back naive UTC. It must not shift by the box's zone."""
    world.github.runs = [gh_run(7, "a2a2a22")]
    world.tasks = [parent("p1", "web@a3a3a33")]
    rw.run_release_watcher(dry_run=False)
    key = ("p1", "web", "a3a3a33")
    world.ledger.pairs[key]["merged_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=30)
    ).replace(tzinfo=None)
    summary = rw.run_release_watcher(dry_run=False)
    assert _state(summary, "p1")["state"] == rw.FIX_WAITING


@pytest.mark.parametrize("name", ["VOMEOS-PIPELINES",
                                  "VOMEOS_BITBUCKET_PIPELINES_TOKEN"])
def test_pipelines_use_the_pipelines_token(monkeypatch, name):
    """The code-search token was never given the pipelines scope."""
    from vomeos.integrations import code_search

    monkeypatch.setenv("VOMEOS_BITBUCKET_TOKEN", "code-token")
    monkeypatch.setenv("VOMEOS_BITBUCKET_WORKSPACE", "vomedjango")
    monkeypatch.delenv("VOMEOS_BITBUCKET_EMAIL", raising=False)
    for other in code_search.PIPELINES_TOKEN_VARS:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "pipes-token")
    seen = {}

    def fake_call(self, operation, method, path, **kwargs):
        seen["auth"] = kwargs["headers"]["Authorization"]
        seen["path"] = path
        return IntegrationResult("bitbucket", operation, True, data={})

    monkeypatch.setattr(code_search.Bitbucket, "_call", fake_call)
    code_search.Bitbucket().pipelines("vomedjango-restored-core-app", "master")
    assert seen["auth"] == "Bearer pipes-token"
    assert seen["path"].endswith("/pipelines/")


def test_pipelines_fall_back_to_the_main_token(monkeypatch):
    from vomeos.integrations import code_search

    monkeypatch.setenv("VOMEOS_BITBUCKET_TOKEN", "code-token")
    monkeypatch.setenv("VOMEOS_BITBUCKET_WORKSPACE", "vomedjango")
    monkeypatch.delenv("VOMEOS_BITBUCKET_EMAIL", raising=False)
    for other in code_search.PIPELINES_TOKEN_VARS:
        monkeypatch.delenv(other, raising=False)
    seen = {}

    def fake_call(self, operation, method, path, **kwargs):
        seen["auth"] = kwargs["headers"]["Authorization"]
        return IntegrationResult("bitbucket", operation, True, data={})

    monkeypatch.setattr(code_search.Bitbucket, "_call", fake_call)
    code_search.Bitbucket().pipelines("vomedjango-restored-core-app", "master")
    assert seen["auth"] == "Bearer code-token"
