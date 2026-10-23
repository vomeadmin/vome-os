"""
test_deep_triage.py

The 2026-10-23 triage reliability work: grep and find over a full copy of
the code (Bitbucket too), read_task, the deep pass that re-checks an unsure
card, and the nightly reconcile of In Progress tickets against ClickUp. No
network and no model calls.
"""

import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from vomeos.integrations import code_mirror, code_search


# ---------------------------------------------------------------------------
# grep / find over a copy
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    files = {
        "sequence_app/views.py": "def remove_step(request):\n"
                                 "    step.is_active = False\n"
                                 "    step.save()\n",
        "sequence_app/models.py": "class SequenceStep:\n    is_active = 1\n",
        "opportunity_app/kiosk.py": "def kiosk_shifts():\n"
                                    "    return shifts.filter(hour__lt=17)\n",
        "src/views/org/Kiosk/KioskList.js": "export const KioskList = 1;\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_grep_is_a_regex_with_line_numbers(tree):
    lines, matches, files = code_mirror.grep(tree, r"is_active\s*=")
    assert matches == 2 and files == 2
    assert "sequence_app/views.py:2: step.is_active = False" in lines


def test_grep_path_filter_and_context(tree):
    lines, matches, _ = code_mirror.grep(tree, "save",
                                         path_glob="sequence_app", context=1)
    assert matches == 1
    assert any(line.startswith("sequence_app/views.py-2-") for line in lines)


def test_grep_bad_regex_is_searched_as_text(tree):
    _, matches, _ = code_mirror.grep(tree, "filter(hour__lt=17")
    assert matches == 1


def test_find_by_glob_and_by_word(tree):
    assert code_mirror.find(tree, "**/*Kiosk*.js")[0] == [
        "src/views/org/Kiosk/KioskList.js"]
    paths, total = code_mirror.find(tree, "kiosk")
    assert total == 2


# ---------------------------------------------------------------------------
# Bitbucket copies
# ---------------------------------------------------------------------------

def _tarball(files: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, text in files.items():
            body = text.encode()
            info = tarfile.TarInfo(f"vomedjango-core-abc/{path}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


class _Resp:
    def __init__(self, body=None, data=None):
        self.body, self.data = body, data
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self.body


@pytest.fixture
def bitbucket(monkeypatch, tmp_path):
    state = {"urls": []}
    monkeypatch.setattr(code_mirror, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(code_mirror, "_sha_cache", {})
    monkeypatch.setattr(code_mirror, "_failures", {})
    monkeypatch.setattr(httpx, "get", lambda url, **k: _Resp(
        data={"hash": "c" * 40}))

    def stream(method, url, **kwargs):
        state["urls"].append(url)
        body = _tarball({"core_app/a.py": "reservation_limit = 3\n"})
        return _Resp(body=body)

    monkeypatch.setattr(httpx, "stream", stream)
    return state


def test_bitbucket_copy_comes_from_the_live_commit_archive(bitbucket):
    root, sha = code_mirror.local_copy("bitbucket", "vomedjango", "core",
                                       "master", {})
    assert sha == "c" * 40
    assert bitbucket["urls"] == [
        f"https://bitbucket.org/vomedjango/core/get/{'c' * 40}.tar.gz"]
    assert code_mirror.grep(root, "reservation_limit")[1] == 1


def test_a_failed_copy_is_not_retried_for_ten_minutes(monkeypatch, bitbucket):
    calls = []

    def refused(*a, **k):
        calls.append(1)
        raise httpx.HTTPStatusError("403", request=None, response=None)

    monkeypatch.setattr(httpx, "stream", refused)
    monkeypatch.setattr(code_mirror.shutil, "which", lambda name: None)
    for _ in range(3):
        with pytest.raises(Exception):
            code_mirror.local_copy("bitbucket", "vomedjango", "core",
                                   "master", {})
    assert len(calls) == 1


def test_bitbucket_search_greps_the_live_branch_first(monkeypatch, bitbucket):
    monkeypatch.setenv("VOMEOS_BITBUCKET_WORKSPACE", "vomedjango")
    monkeypatch.setenv("VOMEOS_BITBUCKET_TOKEN", "t")
    result = code_search.Bitbucket().search_code("reservation_limit",
                                                 repo="core", ref="master")
    assert result.ok and result.data["mirror"]["ref"] == "master"
    assert result.data["items"][0]["path"] == "core_app/a.py"


# ---------------------------------------------------------------------------
# The agent tools
# ---------------------------------------------------------------------------

def test_chats_and_integrations_are_readable_repos():
    from vomeos import tools

    repos = tools.repo_map()
    assert repos["chats"].slug == "vomedjango-chats-app"
    assert repos["integrations"].branch == "main"


def test_grep_code_tool_names_the_commit(monkeypatch, tree):
    from vomeos import tools

    monkeypatch.setattr(tools, "_copy", lambda repo: (tree, "d" * 40))
    out = tools.execute("grep_code", {"repo": "core",
                                      "pattern": "def remove_step"})
    assert "core@master (dddddddddddd)" in out
    assert "sequence_app/views.py:1:" in out


def test_grep_failure_is_not_no_match(monkeypatch):
    from vomeos import tools

    def broken(repo):
        raise RuntimeError("archive refused")

    monkeypatch.setattr(tools, "_copy", broken)
    out = tools.execute("grep_code", {"repo": "core", "pattern": "x"})
    assert "NOT 'no match'" in out


def test_read_task_has_no_repo_or_branch():
    from vomeos import tools

    [definition] = tools.definitions(["read_task"])
    assert set(definition["input_schema"]["properties"]) == {"task"}


def test_task_id_from_a_url_or_an_id():
    from vomeos.integrations import clickup_read

    assert clickup_read.task_id("https://app.clickup.com/t/868mhc1wk") == \
        "868mhc1wk"
    assert clickup_read.task_id("868mkh025") == "868mkh025"


def test_both_triage_agents_pass_the_onboarding_gate():
    from vomeos import onboarding
    from vomeos.registry import get_agent

    for name in ("customer_success.triage_director",
                 "customer_success.triage_investigator"):
        blocking = [f for f in onboarding.check_agent(get_agent(name))
                    if f.severity == onboarding.BLOCKING]
        assert not blocking, blocking


# ---------------------------------------------------------------------------
# When the deep pass runs, and what it costs
# ---------------------------------------------------------------------------

def _card(**kw):
    base = {"category": "user_education", "confidence": "high",
            "lane": "answer",
            "code_check": {"verdict": "works_as_designed"}}
    base.update(kw)
    return base


def test_a_sure_cheap_card_goes_straight_through():
    import deep_triage

    assert deep_triage.reason_for(_card(), arr=420) == ""


@pytest.mark.parametrize("card,arr,want", [
    (_card(confidence="medium"), 0, "confidence medium"),
    (_card(code_check={"verdict": "unclear"}), 0, "code check unclear"),
    (_card(lane="engineer"), 0, "lane engineer"),
    (_card(), 7000, "important client"),
])
def test_unsure_or_costly_cards_get_the_deep_pass(card, arr, want):
    import deep_triage

    assert deep_triage.reason_for(card, arr=arr) == want


def test_nothing_owed_never_gets_the_deep_pass():
    import deep_triage

    assert deep_triage.reason_for(_card(lane="housekeeping",
                                        confidence="low")) == ""
    assert deep_triage.reason_for(_card(category="awaiting_client",
                                        confidence="low")) == ""


def test_switch_off(monkeypatch):
    import deep_triage

    monkeypatch.setenv("DEEP_TRIAGE_ENABLED", "false")
    assert deep_triage.reason_for(_card(confidence="low")) == ""


def test_cost_from_tokens():
    import deep_triage

    result = SimpleNamespace(model="claude-opus-5-5", input_tokens=1_000_000,
                             cache_read_tokens=800_000,
                             cache_write_tokens=100_000,
                             output_tokens=50_000)
    # 100k plain at $4, 800k read at $0.20, 100k write at $5, 50k out at $20
    assert deep_triage.usd(result) == pytest.approx(0.4 + 0.16 + 0.5 + 1.0)


def test_deep_card_replaces_the_first_and_keeps_its_summary(monkeypatch):
    import deep_triage
    import vomeos

    monkeypatch.setattr(deep_triage, "_deep_today", lambda: 0)
    deep = {"category": "user_education", "confidence": "high",
            "lane": "answer", "code_check": {"verdict": "works_as_designed"},
            "review": {"verdict": "corrected",
                       "changes": ["lane: engineer -> answer, because ..."]}}
    seen = {}

    def fake_run(agent, context, **kw):
        seen["agent"], seen["first"] = agent, json.loads(context["first_card"])
        return SimpleNamespace(ok=True, data=dict(deep), tool_calls=[{}] * 7,
                               model="claude-opus-5-5", input_tokens=0,
                               output_tokens=0, cache_read_tokens=0,
                               cache_write_tokens=0, why=lambda: "")

    monkeypatch.setattr(vomeos, "run_agent", fake_run)
    first = _card(category="clear_bug", confidence="medium", lane="engineer",
                  code_check={"verdict": "unclear"})
    card = deep_triage.deepen({"thread": "..."}, first, [], ticket_id="1",
                              ticket_number="9586", arr=0, first_result=None)
    assert seen["agent"] == deep_triage.AGENT
    assert seen["first"]["lane"] == "engineer"
    assert card["lane"] == "answer" and "review" not in card
    assert card["first_pass"] == {"category": "clear_bug", "lane": "engineer",
                                  "confidence": "medium",
                                  "code_check": "unclear"}
    assert card["deep"]["verdict"] == "corrected"
    assert card["deep"]["calls"] == 7


def test_a_failed_deep_pass_keeps_the_first_card(monkeypatch):
    import deep_triage
    import vomeos

    monkeypatch.setattr(deep_triage, "_deep_today", lambda: 0)
    monkeypatch.setattr(vomeos, "run_agent", lambda *a, **k: SimpleNamespace(
        ok=False, data=None, tool_calls=[], model="", why=lambda: "timeout"))
    first = _card(confidence="low")
    card = deep_triage.deepen({}, first, [], ticket_id="1",
                              ticket_number="1", arr=0, first_result=None)
    assert card["category"] == first["category"]
    assert card["deep"]["status"] == "error"


def test_a_reply_on_a_freshly_checked_ticket_reuses_the_investigation(
        monkeypatch):
    import deep_triage
    import vomeos

    def never(*a, **k):
        raise AssertionError("the deep pass ran again")

    monkeypatch.setattr(vomeos, "run_agent", never)
    previous = _card(confidence="high", code_check={
        "verdict": "works_as_designed", "where": "core:a.py:1"},
        deep={"status": "ok", "verdict": "corrected"})
    first = _card(confidence="medium", code_check={"verdict": "unclear"})
    card = deep_triage.deepen(
        {}, first, [], ticket_id="1", ticket_number="1", arr=0,
        first_result=None, previous=previous,
        previous_at=datetime.now(timezone.utc) - timedelta(hours=5))
    assert card["code_check"]["where"] == "core:a.py:1"
    assert card["deep"]["reason"].startswith("reused")


def test_a_changed_call_or_an_old_check_runs_the_deep_pass_again():
    import deep_triage

    previous = _card(deep={"status": "ok"})
    fresh = datetime.now(timezone.utc) - timedelta(hours=5)
    assert not deep_triage.recently_checked(
        previous, _card(lane="engineer"), fresh)
    assert not deep_triage.recently_checked(
        previous, _card(), datetime.now(timezone.utc) - timedelta(days=5))
    assert deep_triage.recently_checked(previous, _card(), fresh)


def test_past_the_daily_cap_the_first_card_stands(monkeypatch):
    import deep_triage

    monkeypatch.setattr(deep_triage, "_deep_today", lambda: 999)
    card = deep_triage.deepen({}, _card(confidence="low"), [], ticket_id="1",
                              ticket_number="1", arr=0, first_result=None)
    assert "daily cap" in card["deep"]["why"]


# ---------------------------------------------------------------------------
# Reconciling In Progress tickets
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def _task(status, days_ago=1, assignees=True, **kw):
    updated = int((NOW - timedelta(days=days_ago)).timestamp() * 1000)
    return {"id": kw.get("id", "t1"), "name": kw.get("name", ""),
            "status": {"status": status,
                       "type": "closed" if status == "closed" else "custom"},
            "assignees": [{"username": "OnlyGod"}] if assignees else [],
            "date_updated": str(updated), "custom_fields": [],
            "parent": kw.get("parent")}


@pytest.mark.parametrize("task,want", [
    (_task("in progress"), "engineer_active"),
    (_task("queued", days_ago=40), "engineer_stalled"),
    (_task("queued", assignees=False), "unassigned"),
    (_task("on prod"), "shipped"),
    (_task("closed"), "task_closed"),
    ({}, "no_task"),
])
def test_states(task, want):
    import ticket_reconciler

    assert ticket_reconciler.classify(task, {}, now=NOW) == want


def test_a_named_open_status_is_open_whatever_its_type():
    import ticket_reconciler

    task = _task("awaiting client response")
    task["status"]["type"] = "done"
    assert ticket_reconciler.classify(task, {}, now=NOW) == "engineer_active"


def test_an_agent_fix_outranks_the_engineer_task(monkeypatch):
    import ticket_reconciler

    monkeypatch.setattr(ticket_reconciler, "_release_check",
                        lambda t: "waiting")
    assert ticket_reconciler.classify(_task("queued"), _task("on dev"),
                                      now=NOW) == "fix_on_dev"


def test_assess_links_by_ticket_number_in_the_task_name(monkeypatch):
    import ticket_reconciler

    monkeypatch.setattr(ticket_reconciler, "_release_check", lambda t: "")
    rows = [{"id": "5694", "ticketNumber": "9188", "status": "In Progress",
             "subject": "Waitlist", "customerResponseTime": "2026-09-22"}]
    pq = [_task("closed", name="Wellspring waitlist delayed #9188")]
    [record] = ticket_reconciler.assess(rows, pq, [])
    assert record["state"] == "task_closed"
    assert record["task_id"] == "t1"


def test_retriage_waits_a_week_unless_the_task_moved():
    import ticket_reconciler as tr

    record = {"state": "task_closed", "task_status": "closed"}
    recent = {"retriaged_at": datetime.now(timezone.utc),
              "retriage_task_status": "closed"}
    assert not tr.wants_retriage(record, recent)
    assert tr.wants_retriage(record, {**recent,
                                      "retriage_task_status": "queued"})
    assert tr.wants_retriage(record, {})
    assert not tr.wants_retriage({"state": "engineer_active",
                                  "task_status": "queued"}, {})


def test_an_ignored_status_filter_stops_the_run(monkeypatch):
    import agent
    import ticket_reconciler

    monkeypatch.setattr(agent, "_zoho_desk_call", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(agent, "_unwrap_mcp_result", lambda r: {
        "data": {"data": [{"status": "Closed"}]}})
    with pytest.raises(RuntimeError, match="ignored"):
        ticket_reconciler.zoho_tickets("In Progress")
