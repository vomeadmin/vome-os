"""
test_workflow_items.py

The second round of the agent roadmap (2026-10-17):

  1. closing the loop after a send (Zoho status, Agent Fixes closure)
  2. retiring the paths that competed with shadow triage
  3. code checks in triage (the kernel tool loop)
  4. ticket bugs proposed as pull requests, promoted when they merge
  5. measuring how often drafts go out unedited

The cases that matter are the ones that would quietly hurt a customer or a
teammate: a closure email Zoho sends on its own, a task moved to `on prod`
(which emails the customer a second time), a code tool reaching an agent that
writes to customers, or an agent-opened PR being treated as merged.
"""

import inspect
import os
import types

# Dummy creds so the older handlers' module-level clients can be imported.
# No network calls happen at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS"):
    os.environ.setdefault(_k, "test-dummy")

import pytest  # noqa: E402

import sprint_store  # noqa: E402
import sprint_thread  # noqa: E402
import ticket_fix  # noqa: E402
from vomeos import config, store  # noqa: E402

TEST_DB = os.environ.get("SPRINT_TEST_DATABASE_URL", "")


def _item(**over):
    item = {
        "id": 5, "number": 2, "ticket_id": "T1", "ticket_number": "9415",
        "category": "clear_bug", "status": "open", "draft_status": "ok",
        "draft": "Hi D,\n\nx\n\nSam | Vome team\nsupport.vomevolunteer.com",
        "recipients": {"to": "d@x.org", "cc": []}, "last_thread_id": "L",
    }
    item.update(over)
    return item


# ---------------------------------------------------------------------------
# 1. Closing the loop
# ---------------------------------------------------------------------------

@pytest.fixture
def after(monkeypatch):
    import shadow_triage
    import zoho_actions

    class A:
        statuses = []
        closed = []
        live = False

    monkeypatch.setattr(zoho_actions, "set_status",
                        lambda tid, s: A.statuses.append((tid, s)))
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": A.live, "task_id": "P1"})
    monkeypatch.setattr(sprint_thread, "_close_agent_fix",
                        lambda item: A.closed.append(item["id"]) or "closed")
    return A


def test_a_resolution_on_a_live_fix_closes_quietly(after):
    after.live = True
    note = sprint_thread._after_send(_item())
    assert after.statuses == [("T1", "Closed")]
    assert after.closed == [5]
    assert "no closure email" in note


def test_an_answer_closes(after):
    sprint_thread._after_send(_item(category="user_education"))
    assert after.statuses == [("T1", "Closed")] and after.closed == []


def test_a_question_waits_for_the_customer(after):
    sprint_thread._after_send(_item(category="vague_bug"))
    assert after.statuses == [("T1", "Awaiting Client Response")]


def test_an_acknowledgment_leaves_the_work_open(after):
    note = sprint_thread._after_send(_item(category="clear_bug"))
    assert after.statuses == [] and "left as it is" in note


def test_a_zoho_failure_is_reported_not_raised(monkeypatch, after):
    import zoho_actions

    def boom(tid, s):
        raise RuntimeError("Zoho 500")

    monkeypatch.setattr(zoho_actions, "set_status", boom)
    note = sprint_thread._after_send(_item(category="user_education"))
    assert "NOT set" in note and "500" in note


def test_closure_is_always_silent():
    import zoho_actions

    source = inspect.getsource(zoho_actions.set_status)
    assert '"disableClosureNotification": "true"' in source


def test_agent_fix_closes_child_then_parent(monkeypatch):
    import release_watcher as rw

    tasks = [{"id": "P1", "parent": None, "name": "fix"},
             {"id": "C1", "parent": "P1", "name": "[#9415] MLH"},
             {"id": "C2", "parent": "P1", "name": "[#9436] MLH"}]
    moved = []
    monkeypatch.setattr(rw, "_list_on_dev_tasks", lambda: tasks)
    monkeypatch.setattr(sprint_thread, "_clickup_status",
                        lambda tid, s: moved.append((tid, s)))
    note = sprint_thread._close_agent_fix(_item())
    assert moved == [("C1", "Closed")] and "1 client(s) to go" in note
    tasks.pop(2)
    moved.clear()
    sprint_thread._close_agent_fix(_item())
    assert moved == [("C1", "Closed"), ("P1", "Closed")]


def test_nothing_is_ever_moved_to_on_prod():
    """on prod fires the on-prod handler, a second email to the customer."""
    for module in (sprint_thread, ticket_fix):
        source = inspect.getsource(module)
        assert '"status": "on prod"' not in source
        assert "'on prod')" not in source


# ---------------------------------------------------------------------------
# 2. Retired paths
# ---------------------------------------------------------------------------

def test_engineer_triggered_replies_sign_as_sam():
    import clickup_user_education_handler as edu
    import clickup_waiting_client_handler as wait
    import on_prod_handler as prod

    for module in (prod, edu, wait):
        source = inspect.getsource(module)
        assert '"vic", language)' not in source
        assert 'signature("vic")' not in source


def test_completeness_gate_is_gone():
    import agent

    assert not hasattr(agent, "_email_completeness_gate")
    assert "ENABLE_EMAIL_COMPLETENESS_GATE" not in inspect.getsource(agent)


# ---------------------------------------------------------------------------
# 3. Code tools and the tool loop
# ---------------------------------------------------------------------------

def test_repo_map_defaults_to_the_live_branches(monkeypatch):
    from vomeos import tools

    monkeypatch.delenv("VOMEOS_CODE_REPO_MAP", raising=False)
    repos = tools.repo_map()
    assert repos["core"].branch == "master"
    assert repos["core"].slug == "vomedjango-restored-core-app"
    assert repos["web"].host == "github" and repos["web"].owner == "vomeadmin"


def test_only_granted_tools_are_defined():
    from vomeos import tools

    names = [d["name"] for d in tools.definitions(["read_code_file"])]
    assert names == ["read_code_file"]


def test_unknown_repo_is_a_message_not_a_crash():
    from vomeos import tools

    assert "Unknown repo" in tools.execute("read_code_file",
                                           {"repo": "chats", "path": "x"})


def test_read_returns_numbered_window(monkeypatch):
    from vomeos import tools
    from vomeos.integrations.base import IntegrationResult

    body = "\n".join(f"line {n}" for n in range(1, 500))

    class Fake:
        def read_file(self, repo, path, ref="", max_bytes=0, **kw):
            assert ref == "master"
            return IntegrationResult("bitbucket", "read_file", True,
                                     data=body)

    monkeypatch.setattr(tools, "_client", lambda repo: Fake())
    out = tools.execute("read_code_file", {"repo": "core", "path": "a.py",
                                           "start_line": 100,
                                           "end_line": 102})
    assert "100 | line 100" in out and "102 | line 102" in out
    assert "103 |" not in out


def test_the_drafter_has_no_code_tools_and_triage_does():
    from vomeos.registry import get_agent

    assert get_agent("customer_success.reply_drafter").job.tools == ()
    assert get_agent("customer_success.sprint_partner").job.tools == ()
    triage = get_agent("customer_success.triage_director")
    assert "read_code_file" in triage.job.tools
    assert triage.job.max_tool_calls > 0


def _block(kind, **kw):
    return types.SimpleNamespace(type=kind, **kw)


def _resp(blocks, stop="end_turn"):
    return types.SimpleNamespace(
        content=blocks, stop_reason=stop,
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))


def test_tool_loop_runs_tools_then_answers(monkeypatch):
    from vomeos import runner, tools
    from vomeos.registry import get_agent

    manifest = get_agent("customer_success.triage_director")
    script = [
        _resp([_block("tool_use", id="u1", name="search_code",
                      input={"repo": "core", "query": "x"})], "tool_use"),
        _resp([_block("text", text='{"category": "clear_bug"}')]),
    ]
    sent = []
    monkeypatch.setattr(runner, "_request",
                        lambda m, kw: (sent.append(kw) or script.pop(0), 1))
    monkeypatch.setattr(tools, "execute", lambda n, a: "found it")
    text, *_ = runner._call_with_tools(manifest, "sys", "user", "model")
    assert text == '{"category": "clear_bug"}'
    results = sent[1]["messages"][-1]["content"]
    assert results[0]["type"] == "tool_result"
    assert results[0]["content"] == "found it"


def test_tool_budget_forces_an_answer_and_nudges_once(monkeypatch):
    from vomeos import runner, tools
    from vomeos.registry import get_agent

    manifest = get_agent("customer_success.triage_director")
    budget = manifest.job.max_tool_calls
    use = _resp([_block("tool_use", id="u", name="search_code",
                        input={"repo": "core", "query": "x"})], "tool_use")
    script = [use] * budget + [
        _resp([_block("thinking", thinking="...")]),   # the #9477 case
        _resp([_block("text", text='{"category": "vague_bug"}')]),
    ]
    sent = []
    monkeypatch.setattr(runner, "_request",
                        lambda m, kw: (sent.append(kw) or script.pop(0), 1))
    monkeypatch.setattr(tools, "execute", lambda n, a: "r")
    text, *_ = runner._call_with_tools(manifest, "s", "u", "m")
    assert text == '{"category": "vague_bug"}'
    assert sent[budget]["tool_choice"] == {"type": "none"}
    assert sent[-1]["messages"][-1]["content"] == runner._ANSWER_NOW


# ---------------------------------------------------------------------------
# 4. Ticket fixes
# ---------------------------------------------------------------------------

GOOD = {"code_check": {"verdict": "bug", "confidence": "high",
                       "fix_risk": "low", "fix": "exclude self",
                       "where": "core:form_app/s.py:120",
                       "finding": "compares with its own row"},
        "live_question": "Rename does not save"}


@pytest.mark.parametrize("change,reason", [
    ({"verdict": "works_as_designed"}, "verdict"),
    ({"verdict": "design_question"}, "Sam's decision"),
    ({"confidence": "low"}, "confidence"),
    ({"fix_risk": "medium"}, "risk"),
    ({"fix": ""}, "fix"),
    # #9415's card: a path with commentary is not a location to fix at.
    ({"where": "web:src/views/org/orgSequences (exact render path not "
               "located)"}, "not exact"),
])
def test_the_ladder_stops_early(change, reason):
    card = {"code_check": {**GOOD["code_check"], **change}}
    ok, why = ticket_fix.should_attempt(card)
    assert not ok and reason in why


def test_a_small_certain_bug_qualifies():
    assert ticket_fix.should_attempt(GOOD)[0] is True


def test_medium_confidence_is_enough_for_a_low_risk_bug():
    # Sam, 2026-10-18: #9500 and #9415 were real bugs at medium.
    card = {"code_check": {**GOOD["code_check"], "confidence": "medium"}}
    assert ticket_fix.should_attempt(card)[0] is True


def test_sam_can_ask_for_a_design_question_fix_but_not_a_risky_one():
    question = {"code_check": {**GOOD["code_check"],
                               "verdict": "design_question",
                               "confidence": "low"}}
    assert ticket_fix.should_attempt(question)[0] is False
    assert ticket_fix.should_attempt(question, sam_approved=True)[0] is True
    risky = {"code_check": {**question["code_check"], "fix_risk": "medium"}}
    assert ticket_fix.should_attempt(risky, sam_approved=True)[0] is False


def test_sams_fix_may_retry_and_skips_the_daily_cap(monkeypatch):
    import sentry_code
    import sentry_fix

    monkeypatch.setenv("TICKET_FIX_ENABLED", "true")
    monkeypatch.setattr(ticket_fix, "_attempted", lambda t: True)
    monkeypatch.setattr(ticket_fix, "_opened_today", lambda: 99)
    monkeypatch.setattr(ticket_fix, "_record", lambda *a, **k: None)
    monkeypatch.setattr(sentry_code, "gather",
                        lambda *a, **k: {"files": "src"})
    monkeypatch.setattr(sentry_fix, "propose",
                        lambda a, c: {"attempted": True, "diff": "d"})
    monkeypatch.setattr(sentry_fix, "open_pull_request",
                        lambda i, p, c: {"opened": True,
                                         "url": "https://bb/pr/8"})
    monkeypatch.setattr(ticket_fix, "_file_tasks", lambda *a: ("P", "C"))
    out = ticket_fix.propose_for_ticket("T1", "9500", "x", GOOD, "u",
                                        sam_approved=True)
    assert out["outcome"] == "pr_opened"


def test_parse_where():
    assert ticket_fix.parse_where("core:form_app/s.py:120-140") == (
        "core", "form_app/s.py", 120)
    assert ticket_fix.parse_where("nonsense") == ("", "", 0)


def test_propose_opens_a_pr_and_files_tasks(monkeypatch):
    import sentry_code
    import sentry_fix

    filed = []
    monkeypatch.setenv("TICKET_FIX_ENABLED", "true")
    monkeypatch.setattr(ticket_fix, "_attempted", lambda t: False)
    monkeypatch.setattr(ticket_fix, "_opened_today", lambda: 0)
    monkeypatch.setattr(ticket_fix, "_record", lambda *a, **k: None)
    monkeypatch.setattr(sentry_code, "gather",
                        lambda *a, **k: {"files": "src"})
    monkeypatch.setattr(sentry_fix, "propose",
                        lambda a, c: {"attempted": True, "diff": "d"})
    monkeypatch.setattr(sentry_fix, "open_pull_request",
                        lambda i, p, c: {"opened": True,
                                         "url": "https://bb/pr/7"})
    monkeypatch.setattr(ticket_fix, "_file_tasks",
                        lambda *a: filed.append(a) or ("P9", "C9"))
    out = ticket_fix.propose_for_ticket("T1", "9500", "Sarasota", GOOD,
                                        "https://zoho/9500")
    assert out["outcome"] == "pr_opened" and out["pr_url"] == \
        "https://bb/pr/7"
    assert filed and filed[0][0] == "9500"


def test_one_attempt_per_ticket(monkeypatch):
    monkeypatch.setattr(ticket_fix, "_attempted", lambda t: True)
    out = ticket_fix.propose_for_ticket("T1", "9500", "x", GOOD, "u")
    assert out["reason"] == "already attempted"


def test_merged_pr_fills_fix_commit_and_moves_to_on_dev(monkeypatch):
    calls = []
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: ("merged", "core", "abc12345"))
    monkeypatch.setattr(ticket_fix, "_clickup",
                        lambda m, p, b=None: calls.append((m, p, b)) or {})
    tasks = [
        {"id": "P1", "parent": None, "status": {"status": "in progress"},
         "description": "Fix PR: https://bitbucket.org/v/r/pull-requests/7",
         "custom_fields": []},
        {"id": "C1", "parent": "P1", "status": {"status": "in progress"}},
        {"id": "P2", "parent": None, "status": {"status": "in progress"},
         "description": "no pr here", "custom_fields": []},
    ]
    changed = ticket_fix.promote_merged(tasks)
    assert changed == ["P1 merged as core@abc12345"]
    assert ("POST", f"/task/P1/field/{ticket_fix.FIELD_FIX_COMMIT}",
            {"value": "core@abc12345"}) in calls
    assert ("PUT", "/task/P1", {"status": "on dev"}) in calls
    assert ("PUT", "/task/C1", {"status": "on dev"}) in calls


def test_an_open_pr_is_not_promoted(monkeypatch):
    calls = []
    monkeypatch.setattr(ticket_fix, "_pr_state",
                        lambda url: ("open", "web", ""))
    monkeypatch.setattr(ticket_fix, "_clickup",
                        lambda *a, **k: calls.append(a) or {})
    ticket_fix.promote_merged([
        {"id": "P1", "parent": None, "status": {"status": "in progress"},
         "description": "Fix PR: https://github.com/o/r/pull/3",
         "custom_fields": []}])
    assert calls == []


# ---------------------------------------------------------------------------
# 5. Measurement, against a real Postgres
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    if not TEST_DB:
        pytest.skip("SPRINT_TEST_DATABASE_URL not set")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB)
    store.reset()
    for table in (sprint_store.ITEMS, sprint_store.DAYS, sprint_store.EDITS):
        store.execute(f"DROP TABLE IF EXISTS {table}")
    store.reset()
    assert sprint_store.ready()
    yield
    store.reset()


def _upsert(ticket_id, category="user_education", draft="agent words"):
    return sprint_store.upsert_ticket(
        ticket_id=ticket_id, ticket_number=ticket_id[-4:], category=category,
        account="A", subject="s", summary="q", important=False, card={},
        draft=draft, draft_status="ok", draft_why="", recipients={},
        last_thread_id="T")


def test_unedited_and_edited_sends_are_counted(db):
    import autonomy_report

    a, b, c = _upsert("t-0001"), _upsert("t-0002"), _upsert("t-0003")
    sprint_store.record_sent(a["id"], "agent words")
    sprint_store.set_draft(b["id"], "Sam's words", "ok", kind="edit")
    sprint_store.record_sent(b["id"], "Sam's words")
    sprint_store.set_draft(c["id"], "second try", "ok", kind="redraft")
    row = [r for r in autonomy_report.category_stats()
           if r["category"] == "user_education"][0]
    assert (row["drafted"], row["sent"], row["unedited"], row["edited"],
            row["redrafts"]) == (3, 2, 1, 1, 1)
    assert row["unedited_rate"] == 0.5 and row["eligible"] is False
    edits = autonomy_report.recent_edits()
    assert {e["kind"] for e in edits} == {"edit", "redraft"}
    assert any(e["before_text"] == "agent words" and
               e["after_text"] == "Sam's words" for e in edits)


def test_a_new_customer_message_resets_the_original(db):
    item = _upsert("t-0101", draft="first")
    sprint_store.set_draft(item["id"], "edited", "ok")
    again = _upsert("t-0101", draft="fresh agent draft")
    assert again["original_draft"] == "fresh agent draft"
    assert again["edits"] == 0


def test_eligibility_needs_volume_and_rate(monkeypatch):
    import autonomy_report

    monkeypatch.setattr(autonomy_report, "MIN_SENT", 2)
    text = autonomy_report.render(
        [{"category": "misdirected", "drafted": 3, "sent": 2, "unedited": 2,
          "edited": 0, "redrafts": 0, "unedited_rate": 1.0,
          "eligible": True}], [])
    assert "qualifies" in text and chr(0x2014) not in text


def test_the_tool_loop_caches_and_counts_cache_tokens(monkeypatch):
    """Caching (2026-10-19): the system prompt and the growing conversation
    are cache breakpoints, and cache reads count as input the model read,
    reported separately so a cost report can price them."""
    from vomeos import runner, tools
    from vomeos.registry import get_agent

    def resp(blocks, stop, read, write):
        return types.SimpleNamespace(
            content=blocks, stop_reason=stop,
            usage=types.SimpleNamespace(
                input_tokens=100, output_tokens=5,
                cache_read_input_tokens=read,
                cache_creation_input_tokens=write))

    manifest = get_agent("customer_success.triage_director")
    script = [
        resp([_block("tool_use", id="u1", name="search_code",
                     input={"repo": "core", "query": "x"})], "tool_use",
             0, 3000),
        resp([_block("text", text='{"category": "clear_bug"}')], "end_turn",
             3000, 400),
    ]
    sent = []
    monkeypatch.setattr(runner, "_request",
                        lambda m, kw: (sent.append(kw) or script.pop(0), 1))
    monkeypatch.setattr(tools, "execute", lambda n, a: "found it")
    text, total_in, out, _, read, write = runner._call_with_tools(
        manifest, "the charter", "user", "model")
    assert all(kw["cache_control"] == {"type": "ephemeral"} for kw in sent)
    assert sent[0]["system"] == [{"type": "text", "text": "the charter",
                                  "cache_control": {"type": "ephemeral"}}]
    assert (total_in, out, read, write) == (6600, 10, 3000, 3400)
