"""
The support sprint from Claude.ai (sprint_tools.py, the Vome OS connector).

Sam, 2026-10-18: sprints run in Claude.ai; bugs go through the agents
first; everything that writes is his alone and goes through the same
guarded paths as the sprint thread.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

import sprint_batch
import sprint_store
import sprint_thread
import sprint_tools
from mcp_connector.feature_requests import RequestError
from vomeos import config, store

TEST_DB = os.environ.get("SPRINT_TEST_DATABASE_URL", "")
SAM = "s.fagen@vomevolunteer.com"
NOW = datetime(2026, 10, 19, 14, 0)
DRAFT = ("Hi Ana,\n\nYou can export them from the opportunity's People "
         "tab.\n\nSam | Vome team\nsupport.vomevolunteer.com")


def _item(n=4, **over):
    item = {
        "id": 100 + n, "number": n, "ticket_id": f"5694000{n}",
        "ticket_number": f"96{n:02d}", "status": "open",
        "category": "user_education", "account": "Mustard Seed (Pro)",
        "important": False, "draft": DRAFT, "draft_status": "ok",
        "draft_why": "", "summary": "s", "last_thread_id": "TH1",
        "recipients": {"to": "ana@ms.org", "cc": [], "first_name": "Ana",
                       "received_at": "2026-10-18T10:00:00Z"},
        "card": {"live_question": "How do I export?", "lane": "answer",
                 "code_check": {"verdict": "works_as_designed",
                                "confidence": "high"}},
        "arrived_at": NOW, "updated_at": NOW,
    }
    item.update(over)
    return item


@pytest.fixture
def sam(monkeypatch):
    from mcp_connector import config as mcp_config

    monkeypatch.setattr(mcp_config, "can_triage", lambda who: who == SAM)


@pytest.fixture
def one(monkeypatch):
    item = _item()
    monkeypatch.setattr(sprint_tools, "find",
                        lambda t: (item["ticket_id"], item))
    return item


# ---------------------------------------------------------------------------
# Writes are Sam's alone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda: sprint_tools.send_ticket_reply("9604", "send it", "ron@vome.co"),
    lambda: sprint_tools.route_ticket("9604", "onlyg", "ron@vome.co"),
    lambda: sprint_tools.close_ticket_quietly("9604", "ron@vome.co"),
    lambda: sprint_tools.propose_fix("9604", "ron@vome.co"),
    lambda: sprint_tools.log_ticket_request("9604", "ron@vome.co"),
])
def test_only_sam_can_act(sam, call):
    with pytest.raises(RequestError, match="Only Sam"):
        call()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def test_a_send_needs_sams_own_words(sam, one):
    with pytest.raises(RequestError, match="sam_said"):
        sprint_tools.send_ticket_reply("9604", "looks fine", SAM)


def test_send_uses_the_one_guarded_send_path(sam, one, monkeypatch):
    sent = []
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda item: sent.append(item["id"])
                        or "*4.* sent to ana@ms.org.")
    out = sprint_tools.send_ticket_reply("9604", "yes send it", SAM)
    assert sent == [104] and out["sent"] is True


def test_an_edited_reply_is_guarded_and_teaches(sam, one, monkeypatch):
    stored, learned = [], []
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda item, body: ("ok", ""))
    monkeypatch.setattr(sprint_store, "set_draft",
                        lambda i, t, s, w="": stored.append(t))
    monkeypatch.setattr(sprint_store, "get_item",
                        lambda i: {**one, "draft": stored[-1]})
    monkeypatch.setattr(sprint_thread, "_learn",
                        lambda item, b, a, said: learned.append(a) or
                        "\n:brain: Learned L2")
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda item: f"sent to x with {item['draft'][:8]}")
    out = sprint_tools.send_ticket_reply(
        "9604", "send this instead", SAM, text="Use the People tab.")
    assert stored[0].startswith("Hi Ana,") and "Sam | Vome team" in stored[0]
    assert learned and out["lesson"] == ":brain: Learned L2"


def test_an_edit_a_guard_blocks_is_not_sent(sam, one, monkeypatch):
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda item, body: ("blocked", "no_confirmation"))
    monkeypatch.setattr(sprint_store, "set_draft", lambda *a, **k: None)
    monkeypatch.setattr(sprint_thread, "_learn", lambda *a: "")
    sent = []
    monkeypatch.setattr(sprint_thread, "_send", lambda i: sent.append(i))
    with pytest.raises(RequestError, match="held by a guard"):
        sprint_tools.send_ticket_reply("9604", "send", SAM,
                                       text="We fixed it.")
    assert sent == []


# ---------------------------------------------------------------------------
# The agents' first pass
# ---------------------------------------------------------------------------

def test_a_current_card_is_returned_without_triaging_again(one, monkeypatch):
    import zoho_actions

    queued = []
    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda t: {"id": "TH1"})
    monkeypatch.setattr(sprint_tools, "fix_status",
                        lambda t: {"summary": "No agent fix."})
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda *a, **k: queued.append(a))
    out = sprint_tools.ticket_first_pass("9604")
    assert out["state"] == "ready" and out["draft"] == DRAFT
    assert out["code_check"]["verdict"] == "works_as_designed"
    assert queued == []


def test_a_newer_customer_message_triages_again(one, monkeypatch):
    import shadow_triage
    import zoho_actions

    queued = []
    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda t: {"id": "TH2"})
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda key, payload, **k: queued.append(
                            (key, payload)))
    out = sprint_tools.ticket_first_pass("9604")
    assert out["state"] == "triaging"
    assert queued == [(shadow_triage.HANDLER_KEY,
                       {"ticket_id": one["ticket_id"], "reason": "claude_ai",
                        "refresh": True})]


def test_the_worker_refreshes_and_skips_the_cap_when_sam_asks(monkeypatch):
    import shadow_triage

    calls = []
    monkeypatch.setattr(shadow_triage, "enabled", lambda: True)
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(shadow_triage, "_auto_reply", lambda tid: False)
    monkeypatch.setattr(shadow_triage, "_handle",
                        lambda tid, reason, **k: calls.append(k) or {})
    shadow_triage.handle({"ticket_id": "1", "reason": "claude_ai",
                          "refresh": True})
    shadow_triage.handle({"ticket_id": "1", "reason": "reply"})
    assert calls == [{"refresh": True, "ignore_cap": True},
                     {"refresh": False, "ignore_cap": False}]


def _conversation(text, author_type="END_USER"):
    return {"data": [{"summary": text, "isPublic": True,
                      "author": {"type": author_type,
                                 "email": "allie@womenscommunity.org"}}]}


@pytest.mark.parametrize("text,skipped", [
    ("I will be out of the office Oct 20-21, I will get back to you as "
     "soon as I can.", True),
    ("Thanks, but I still can't see the completion date.", False),
])
def test_an_out_of_office_is_never_triaged_as_a_reply(monkeypatch, text,
                                                     skipped):
    """#9526: an out-of-office right after Sam's answer was triaged as a
    new message and routed to OnlyG again."""
    import agent
    import shadow_triage

    calls = []
    monkeypatch.setattr(shadow_triage, "enabled", lambda: True)
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(agent, "fetch_ticket_conversations",
                        lambda tid: _conversation(text))
    monkeypatch.setattr(shadow_triage, "_handle",
                        lambda tid, reason, **k: calls.append(reason) or {})
    out = shadow_triage.handle({"ticket_id": "1", "reason": "reply"})
    if skipped:
        assert out == {"status": "skipped", "reason": "auto-reply"}
        assert calls == []
    else:
        assert calls == ["reply"]


def test_sam_asking_triages_even_an_out_of_office(monkeypatch):
    import agent
    import shadow_triage

    calls = []
    monkeypatch.setattr(shadow_triage, "enabled", lambda: True)
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(agent, "fetch_ticket_conversations",
                        lambda tid: _conversation("I am out of the office"))
    monkeypatch.setattr(shadow_triage, "_handle",
                        lambda tid, reason, **k: calls.append(reason) or {})
    shadow_triage.handle({"ticket_id": "1", "reason": "claude_ai",
                          "refresh": True})
    assert calls == ["claude_ai"]


# ---------------------------------------------------------------------------
# Where a fix stands
# ---------------------------------------------------------------------------

@pytest.fixture
def stages(monkeypatch, one):
    import shadow_triage

    state = {"ledger": {}, "release": {"live": False, "task_id": "",
                                       "text": ""}, "resolution": {}}
    monkeypatch.setattr(sprint_tools, "_ledger", lambda t: state["ledger"])
    monkeypatch.setattr(sprint_tools, "_resolution",
                        lambda t: state["resolution"])
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: state["release"])
    return state


def test_fix_stage_messages(stages, one):
    assert "propose_fix asks the agent" in \
        sprint_tools.fix_status("9604")["summary"]
    stages["ledger"] = {"outcome": "pr_opened", "pr_url": "https://bb/pr/1"}
    assert "Sam reviews and merges it" in \
        sprint_tools.fix_status("9604")["summary"]
    stages["release"] = {"live": False, "task_id": "868a",
                         "text": "NOT LIVE (core@1; web@2)"}
    summary = sprint_tools.fix_status("9604")["summary"]
    assert "OnlyG merges core to production and Sam deploys web" in summary
    stages["release"] = {"live": True, "task_id": "868a", "text": "LIVE"}
    stages["resolution"] = {"outcome": "drafted"}
    assert "resolution reply is drafted" in \
        sprint_tools.fix_status("9604")["summary"]
    one["status"] = sprint_store.SENT
    assert "client has been told" in \
        sprint_tools.fix_status("9604")["summary"]


def test_propose_fix_runs_on_the_worker_with_sams_approval(
        sam, one, monkeypatch):
    import ticket_fix

    queued = []
    monkeypatch.setattr("vomeos.worker.enqueue_event",
                        lambda key, payload, **k: queued.append(key))
    assert sprint_tools.propose_fix("9604", SAM)["state"] == \
        "writing the fix"
    assert queued == [sprint_tools.PROPOSE_FIX_KEY]

    calls, stored = [], []
    monkeypatch.setattr(sprint_batch, "_lease", lambda n, m: True)
    monkeypatch.setattr(sprint_store, "by_ticket", lambda t: one)
    monkeypatch.setattr(ticket_fix, "propose_for_ticket",
                        lambda *a, **k: calls.append(k) or
                        {"outcome": "pr_opened", "pr_url": "https://bb/9"})
    monkeypatch.setattr(sprint_store, "set_fix_pr",
                        lambda i, u: stored.append(u))
    sprint_tools.handle_propose_fix({"ticket_id": one["ticket_id"]})
    assert calls == [{"sam_approved": True}]
    assert stored == ["https://bb/9"]


def test_route_needs_a_first_pass(sam, monkeypatch):
    monkeypatch.setattr(sprint_tools, "find", lambda t: ("569", {}))
    with pytest.raises(RequestError, match="ticket_first_pass"):
        sprint_tools.route_ticket("9604", "onlyg", SAM)


def test_the_engineer_summary_carries_the_code_check():
    item = _item(card={"live_question": "Push does not open",
                       "escalation_reason": "mobile_unreadable",
                       "code_check": {"verdict": "unclear",
                                      "confidence": "low",
                                      "finding": "payload has a link key",
                                      "where": "core:notifications/push.py:40",
                                      "fix": "", "fix_risk": "medium"}})
    text = sprint_thread._task_description(item)
    assert "Likely cause: payload has a link key" in text
    assert "Where: core:notifications/push.py:40" in text
    assert "mobile_unreadable" in text


# ---------------------------------------------------------------------------
# The queue and the follow-ups
# ---------------------------------------------------------------------------

def test_queue_groups_like_the_sprint(monkeypatch):
    items = [_item(1), _item(2, draft="", draft_status="none",
                             category="needs_human",
                             card={"live_question": "q"}),
             _item(3, draft="", draft_status="none",
                   card={"lane": "housekeeping"})]
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_store, "working_set", lambda: items)
    monkeypatch.setattr(sprint_batch, "_queue",
                        lambda pages=None: [{"id": "56940001"},
                                            {"id": "NEW"}])
    monkeypatch.setattr(store, "query", lambda *a, **k: [])
    out = sprint_tools.sprint_queue()
    assert out["counts"]["ready"] == 1 and out["counts"]["decide"] == 1
    assert out["counts"]["quiet"] == 1
    assert out["ready_to_send"][0]["draft"] == DRAFT
    assert out["untriaged_open_tickets"] == 1


def test_production_owner():
    assert sprint_tools._production_owner("core@1") == \
        "OnlyG merges core to production"
    assert sprint_tools._production_owner("web@2") == \
        "Sam deploys web to production"


def test_slack_batches_are_paused_by_default(monkeypatch):
    monkeypatch.delenv("SPRINT_BATCHES_ENABLED", raising=False)
    assert sprint_batch.run_scheduled_batch()["status"] == "paused"
    assert sprint_batch.run_weekend_batch()["status"] == "paused"


@pytest.fixture
def db(monkeypatch):
    if not TEST_DB:
        pytest.skip("SPRINT_TEST_DATABASE_URL not set")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB)
    store.reset()
    for table in (sprint_store.ITEMS, "vomeos_release_resolutions",
                  "vomeos_ticket_fixes", "vomeos_release_fixes"):
        store.execute(f"DROP TABLE IF EXISTS {table}")
    store.reset()
    import release_resolutions  # noqa: F401  registers its table
    import release_watcher  # noqa: F401
    import ticket_fix  # noqa: F401

    assert sprint_store.ready()
    for table in ("vomeos_release_resolutions", "vomeos_ticket_fixes",
                  "vomeos_release_fixes"):
        assert store.ensure_table(table), table
    yield
    store.reset()


def _upsert(ticket_id, **over):
    fields = dict(
        ticket_id=ticket_id, ticket_number=ticket_id[-4:],
        category="clear_bug", account="Acme", subject="s", summary="q",
        important=False, card={"live_question": "q?"}, draft=DRAFT,
        draft_status="ok", draft_why="", recipients={"to": "a@b.co"},
        last_thread_id="T1")
    fields.update(over)
    return sprint_store.upsert_ticket(**fields)


def test_find_by_ticket_number(db):
    item = _upsert("569400009500")
    ticket_id, found = sprint_tools.find("#9500")
    assert ticket_id == "569400009500" and found["id"] == item["id"]


def test_followups_list_owed_replies_and_waiting_prs(db):
    owed = _upsert("569400009436")
    store.execute(
        "INSERT INTO vomeos_release_resolutions (child_id, parent_id,"
        " ticket_id, ticket_number, outcome) VALUES ('c1', 'p1',"
        " '569400009436', '9436', 'drafted')")
    store.execute(
        "INSERT INTO vomeos_ticket_fixes (ticket_id, ticket_number, outcome,"
        " pr_url, parent_task) VALUES ('569400009600', '9600', 'pr_opened',"
        " 'https://bb/pr/5', 'P5')")
    out = sprint_tools.sprint_followups()
    assert [r["item"] for r in out["replies_owed_for_shipped_work"]] == \
        [owed["number"]]
    assert out["replies_owed_for_shipped_work"][0]["draft"] == DRAFT
    assert out["agent_prs_waiting_on_sams_merge"][0]["pr_url"] == \
        "https://bb/pr/5"


def test_teach_agents_records_a_live_lesson(sam, monkeypatch):
    from vomeos import lessons

    added = []
    monkeypatch.setattr(lessons, "add", lambda agent, rule, source, **k:
                        added.append((agent, rule, source, k)) or 9)
    out = sprint_tools.teach_agents(
        "In a resolution reply, never mention an earlier reply.", "drafter",
        "stop mentioning the old email", SAM, ticket="#9485")
    assert out["lesson"] == "L9"
    agent, rule, source, extra = added[0]
    assert agent == "customer_success.reply_drafter"
    assert source == "claude_ai" and extra["ticket"] == "9485"
    assert extra["evidence"] == "Sam: stop mentioning the old email"


def test_teach_agents_refuses_vague_or_unknown(sam):
    with pytest.raises(RequestError, match="agent must be"):
        sprint_tools.teach_agents("Never mention an earlier reply.",
                                  "billing", "x", SAM)
    with pytest.raises(RequestError, match="imperative"):
        sprint_tools.teach_agents("be nice", "drafter", "x", SAM)
    with pytest.raises(RequestError, match="Only Sam"):
        sprint_tools.teach_agents("Never mention an earlier reply.",
                                  "drafter", "x", "ron@vome.co")


def test_view_names_the_client_and_person_on_every_card():
    # Sam (2026-10-20): reading item 3, 4, 5 he could not tell who an item
    # was for, and he decides how hard to check by who the client is.
    view = sprint_tools.view(_item(account="Sarasota County Government "
                                           "(very-high, 7000 CAD combined ARR)",
                                   important=True))
    assert view["client"] == "Sarasota County Government"
    assert view["contact"] == "Ana <ana@ms.org>"
    assert view["header"].startswith("Item 4 | #9604 | Sarasota County "
                                     "Government (very-high")
    assert "Ana <ana@ms.org>" in view["header"]
    assert view["header"].endswith("IMPORTANT")


def test_view_header_without_recipient_still_names_the_client():
    view = sprint_tools.view(_item(recipients={}))
    assert view["contact"] == ""
    assert "Mustard Seed (Pro)" in view["header"]
