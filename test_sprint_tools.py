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
                        lambda item, sam_wrote=False, **k:
                        sent.append(item["id"])
                        or "*4.* sent to ana@ms.org.")
    out = sprint_tools.send_ticket_reply("9604", "yes send it", SAM)
    assert sent == [104] and out["sent"] is True


def test_an_edited_reply_is_guarded_and_teaches(sam, one, monkeypatch):
    stored, learned = [], []
    checked = []
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda item, body, sam_wrote=False, **k:
                        checked.append(sam_wrote) or ("ok", ""))
    monkeypatch.setattr(sprint_store, "set_draft",
                        lambda i, t, s, w="": stored.append(t))
    monkeypatch.setattr(sprint_store, "get_item",
                        lambda i: {**one, "draft": stored[-1]})
    monkeypatch.setattr(sprint_thread, "_learn",
                        lambda item, b, a, said: learned.append(a) or
                        "\n:brain: Learned L2")
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda item, sam_wrote=False, **k:
                        f"sent to x with {item['draft'][:8]}")
    out = sprint_tools.send_ticket_reply(
        "9604", "send this instead", SAM, text="Use the People tab.")
    assert stored[0].startswith("Hi Ana,") and "Sam | Vome team" in stored[0]
    assert learned and out["lesson"] == ":brain: Learned L2"
    # Sam's own words: "a draft" may mean the product's drafts (#9537).
    assert checked == [True]


def test_an_edit_a_guard_blocks_is_not_sent(sam, one, monkeypatch):
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda item, body, sam_wrote=False, **k:
                        ("blocked", "no_confirmation"))
    monkeypatch.setattr(sprint_store, "set_draft", lambda *a, **k: None)
    monkeypatch.setattr(sprint_thread, "_learn", lambda *a: "")
    sent = []
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda i, sam_wrote=False, **k: sent.append(i))
    with pytest.raises(RequestError, match="held by a guard"):
        sprint_tools.send_ticket_reply("9604", "send", SAM,
                                       text="We fixed it.")
    assert sent == []


# #8204 (2026-10-30): the holding note went from the item, the fix was
# verified, and the "it's fixed" reply was refused as "already sent".

def test_a_follow_up_goes_out_on_an_answered_ticket(sam, one, monkeypatch):
    one.update(status="sent", sent_at=NOW)
    checked, followed = [], []
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda item, body, sam_wrote=False,
                        sam_verified=False:
                        checked.append((sam_wrote, sam_verified))
                        or ("ok", ""))
    monkeypatch.setattr(sprint_thread, "_send_follow_up",
                        lambda item, body, resolved=False:
                        followed.append((body, resolved))
                        or "*4.* follow-up sent to ana@ms.org.")

    def never(*a, **k):
        raise AssertionError("a follow-up is not an edit of the draft")

    monkeypatch.setattr(sprint_store, "set_draft", never)
    monkeypatch.setattr(sprint_thread, "_learn", never)
    monkeypatch.setattr(sprint_thread, "_send", never)
    out = sprint_tools.send_ticket_reply(
        "9604", "send it", SAM, text="The pop-up has been updated.",
        resolved=True)
    assert out["sent"] is True and out["lesson"] == ""
    assert checked == [(True, True)]
    assert followed[0][0].startswith("Hi Ana,") and followed[0][1] is True


def test_an_answered_ticket_never_sends_its_draft_again(sam, one,
                                                        monkeypatch):
    one.update(status="sent", sent_at=NOW)
    with pytest.raises(RequestError, match="follow-up in `text`"):
        sprint_tools.send_ticket_reply("9604", "send it", SAM)


def test_a_follow_up_a_guard_blocks_is_not_sent(sam, one, monkeypatch):
    one.update(status="routed", sent_at=NOW)
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda *a, **k: ("blocked", "no_unverified_claims"))
    sent = []
    monkeypatch.setattr(sprint_thread, "_send_follow_up",
                        lambda *a, **k: sent.append(a))
    with pytest.raises(RequestError, match="held by a guard"):
        sprint_tools.send_ticket_reply("9604", "send", SAM,
                                       text="It has been fixed.")
    assert sent == []


def test_an_open_ticket_still_takes_the_normal_path(sam, one, monkeypatch):
    assert not sprint_thread._answered(one)
    assert sprint_thread._answered({**one, "status": "done"})
    assert not sprint_thread._answered(
        {**one, "status": "done", "decision": "log: 86abc"})
    assert sprint_thread._answered({**one, "status": "fixing",
                                    "sent_at": NOW})
    assert not sprint_thread._answered({**one, "status": "fixing"})


def test_follow_up_sends_without_touching_the_draft(one, monkeypatch):
    import zoho_actions

    one.update(status="sent", sent_at=NOW,
               recipients={**one["recipients"], "cc": ["mc@ms.org"]})
    mails, decided, after, recorded = [], [], [], []
    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda t: {"id": "TH1"})
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda t, to, cc, body: mails.append((to, cc, body)))
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: decided.append((s, d)))
    monkeypatch.setattr(sprint_store, "record_sent",
                        lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(sprint_thread, "_post", lambda *a, **k: None)
    monkeypatch.setattr(sprint_thread, "_after_send",
                        lambda item, resolved=False:
                        after.append(resolved) or "Zoho set to Closed.")
    out = sprint_thread._send_follow_up(one, "Hi Ana,\n\nbody", resolved=True)
    assert mails == [("ana@ms.org", ["mc@ms.org"], "Hi Ana,\n\nbody")]
    assert decided == [("sent", "follow-up sent by Sam, resolved")]
    assert after == [True] and " sent to " in out
    # sent_text stays the first reply; Zoho holds the follow-up.
    assert recorded == []


def test_follow_up_refused_when_the_customer_wrote_again(one, monkeypatch):
    import zoho_actions

    one.update(status="sent", sent_at=NOW)
    mails = []
    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda t: {"id": "TH2"})
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda *a: mails.append(a))
    out = sprint_thread._send_follow_up(one, "body")
    assert mails == [] and "written again" in out


def test_resolved_closes_zoho_and_the_routed_task(one, monkeypatch):
    import shadow_triage
    import ticket_routing
    import zoho_actions

    one.update(category="bug", eng_task={
        "id": "868kur961", "url": "https://app.clickup.com/t/868kur961",
        "owner": "sanjay"})
    statuses, closed = [], []
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": False})
    monkeypatch.setattr(ticket_routing, "task_state",
                        lambda filed: {"status": "in progress",
                                       "shipped": False})
    monkeypatch.setattr(zoho_actions, "set_status",
                        lambda t, s: statuses.append(s))
    monkeypatch.setattr(zoho_actions, "set_in_progress",
                        lambda *a: statuses.append("In Progress"))
    monkeypatch.setattr(sprint_thread, "_clickup_status",
                        lambda task_id, s: closed.append((task_id, s)))
    monkeypatch.setattr(sprint_store, "decide", lambda *a: None)
    sprint_thread._after_send(one, resolved=True)
    assert statuses == ["Closed"] and closed == [("868kur961", "Closed")]
    statuses.clear()
    sprint_thread._after_send(one)
    assert statuses == ["In Progress"]


def test_sams_resolved_word_backs_a_fix_claim(one, monkeypatch):
    import shadow_triage

    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": False})
    body = ("Hi Ana,\n\nThis has been fixed, please give it a try.\n\n"
            "Sam | Vome team\nsupport.vomevolunteer.com")
    status, why = sprint_thread._check_draft(one, body, sam_wrote=True)
    assert status == "blocked" and "no_unverified_claims" in why
    assert sprint_thread._check_draft(one, body, sam_wrote=True,
                                      sam_verified=True) == ("ok", "")
    # Only Sam's own words carry his verification.
    assert sprint_thread._check_draft(one, body,
                                      sam_verified=True)[0] == "blocked"


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

@pytest.fixture
def zoho(monkeypatch):
    """Zoho as the reconcile reads it: status per ticket, and the newest
    (inbound, outbound) per ticket. Unknown tickets are Processing with
    nothing sent."""
    import zoho_actions

    class Z:
        status: dict = {}
        threads: dict = {}
        decided: list = []

    Z.status, Z.threads, Z.decided = {}, {}, []
    monkeypatch.setattr(zoho_actions, "ticket_status",
                        lambda t: Z.status.get(t, "Processing"))
    monkeypatch.setattr(zoho_actions, "last_exchange",
                        lambda t, skip=None: Z.threads.get(
                            t, ({}, {})))
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: Z.decided.append((i, s, d)))
    return Z


def test_queue_groups_like_the_sprint(monkeypatch, zoho):
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


def test_queue_never_lists_what_sam_answered_or_closed_in_zoho(
        monkeypatch, zoho):
    """2026-10-22: a Claude.ai sprint showed tickets Sam had already
    answered and closed by hand (lesson L27)."""
    items = [_item(1), _item(2), _item(3)]
    zoho.threads["56940001"] = ({"createdTime": "2026-10-21T18:38:00Z"},
                                {"createdTime": "2026-10-21T19:56:00Z"})
    zoho.status["56940002"] = "Closed"
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_store, "working_set", lambda: items)
    monkeypatch.setattr(sprint_batch, "_queue",
                        lambda pages=None: [{"id": "56940001"},
                                            {"id": "56940003"}])
    monkeypatch.setattr(store, "query", lambda *a, **k: [])
    out = sprint_tools.sprint_queue()
    listed = [i["item"] for i in out["ready_to_send"]]
    assert listed == [3]
    assert sorted(d[2] for d in zoho.decided) == [
        "handled outside the sprint (Zoho: Closed)", sprint_batch.ANSWERED]


def test_an_unreadable_queue_finishes_nothing(monkeypatch, zoho):
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_store, "working_set", lambda: [_item(1)])

    def boom(pages=None):
        raise RuntimeError("Zoho 500")

    monkeypatch.setattr(sprint_batch, "_queue", boom)
    monkeypatch.setattr(store, "query", lambda *a, **k: [])
    out = sprint_tools.sprint_queue()
    assert zoho.decided == [] and out["counts"]["ready"] == 1
    assert out["untriaged_open_tickets"].startswith("unknown")


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


def test_followups_drop_a_resolution_sam_answered_by_hand(db, monkeypatch):
    import zoho_actions

    _upsert("569400009436")
    store.execute(
        "INSERT INTO vomeos_release_resolutions (child_id, parent_id,"
        " ticket_id, ticket_number, outcome, created_at) VALUES ('c1',"
        " 'p1', '569400009436', '9436', 'drafted', '2026-10-20 12:00')")
    monkeypatch.setattr(zoho_actions, "ticket_status",
                        lambda t: "Processing")
    exchange = ({"createdTime": "2026-10-19T10:00:00Z"},
                {"createdTime": "2026-10-20T15:00:00Z"})
    monkeypatch.setattr(zoho_actions, "last_exchange",
                        lambda t, skip=None: exchange)
    out = sprint_tools.sprint_followups()
    assert out["replies_owed_for_shipped_work"] == []
    row = store.query("SELECT outcome FROM vomeos_release_resolutions")[0]
    assert row["outcome"] == "handled_by_hand"
    assert sprint_store.by_ticket("569400009436")["status"] == \
        sprint_store.DONE


def test_followups_list_owed_replies_and_waiting_prs(db, monkeypatch):
    import zoho_actions

    monkeypatch.setattr(zoho_actions, "ticket_status",
                        lambda t: "Processing")
    monkeypatch.setattr(zoho_actions, "last_exchange",
                        lambda t, skip=None: ({}, {}))
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


def _clickup(monkeypatch, linked=(), live=None):
    import clickup_search
    import ticket_routing

    monkeypatch.setattr(clickup_search, "linked_tasks",
                        lambda n, t: list(linked))
    monkeypatch.setattr(ticket_routing, "task_state",
                        lambda filed: (live or {}).get(
                            filed["id"], {"status": "unreadable"}))


def test_links_line_opens_zoho_and_clickup_and_names_the_list(monkeypatch):
    # Sam (2026-10-24): every card should say where the task lives now
    # (Sam Accepted (Build Now), Priority Queue...) with one click to the
    # Zoho ticket and the ClickUp task.
    _clickup(monkeypatch, live={"868mgyqdm": {
        "status": "queued", "name": "Board: sort by opportunity",
        "list": "Sam Accepted (Build Now)", "assignee": "Sam",
        "url": "https://app.clickup.com/t/868mgyqdm"}})
    view = sprint_tools.view(_item(card={
        "live_question": "Sort the Board by opportunity",
        "existing_work": "This is already logged on 868mgyqdm."}))
    assert view["links"].startswith("[Zoho #9604](")
    assert view["links"].endswith(
        "| [ClickUp](https://app.clickup.com/t/868mgyqdm) in Sam Accepted "
        "(Build Now), queued, assigned Sam")
    assert view["clickup_tasks"][0]["list"] == "Sam Accepted (Build Now)"


def test_links_line_lists_the_engineer_task_first_then_linked(monkeypatch):
    _clickup(monkeypatch, linked=[{
        "id": "86linked1", "name": "x", "list": "Raw Intake",
        "status": "queued", "assignee": "",
        "url": "https://app.clickup.com/t/86linked1"}], live={"86engtask": {
            "status": "in progress", "list": "Priority Queue",
            "assignee": "Ron", "url": "https://app.clickup.com/t/86engtask"}})
    view = sprint_tools.view(_item(eng_task={
        "id": "86engtask", "url": "https://app.clickup.com/t/86engtask"}))
    first, second = view["links"].split(" | ")[1:]
    assert first.endswith("in Priority Queue, in progress, assigned Ron")
    assert second.endswith("in Raw Intake, queued, unassigned")


def test_links_line_never_says_none_when_clickup_was_not_read(monkeypatch):
    import clickup_search

    _clickup(monkeypatch)

    def down(n, t):
        raise RuntimeError("429")

    monkeypatch.setattr(clickup_search, "linked_tasks", down)
    view = sprint_tools.view(_item())
    assert "no ClickUp task" not in view["links"]
    assert "could not be searched" in view["links"]
    _clickup(monkeypatch)
    assert sprint_tools.view(_item())["links"].endswith("| no ClickUp task")


# ---------------------------------------------------------------------------
# The board (Sam, 2026-10-29): replies owed, good news untold, internal
# ---------------------------------------------------------------------------

OLD = "2026-08-01T10:00:00Z"
ASKS = ("Hi Ana,\n\nCould you send us the name of one user it fails "
        "for?\n\nSam | Vome team\nsupport.vomevolunteer.com")
FIXED = ("Hi Ana,\n\nThe export has been updated and is now live.\n\n"
         "Sam | Vome team\nsupport.vomevolunteer.com")


@pytest.fixture
def board(monkeypatch, zoho):
    """The board over a list of items: tickets in `open` are New or
    Processing, everything else In Progress. No reconcile, no
    resolutions, no pipeline unless a test sets them."""
    state = {"items": [], "open": set(), "reconcile": {}, "resolutions": {}}
    _clickup(monkeypatch)
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_store, "working_set",
                        lambda: list(state["items"]))
    monkeypatch.setattr(sprint_batch, "_queue", lambda pages=None: [
        {"id": t, "ticketNumber": t[-4:]} for t in state["open"]])
    monkeypatch.setattr(sprint_batch, "_pending_resolutions", lambda: {
        t: NOW for t in state["resolutions"]})
    monkeypatch.setattr(sprint_batch, "_now", lambda: NOW)
    monkeypatch.setattr(store, "query", lambda *a, **k: [
        {"ticket_id": i["ticket_id"]} for i in state["items"]])
    monkeypatch.setattr(sprint_tools, "_reconcile_rows",
                        lambda: state["reconcile"])
    monkeypatch.setattr(sprint_tools, "_resolutions",
                        lambda: state["resolutions"])
    monkeypatch.setattr(sprint_tools, "_pipeline", lambda: {})
    return state


def _lanes(out):
    return {lane: [line["ticket"] for line in out[lane]]
            for lane in sprint_tools.BOARD_LANES}


def test_board_sends_clean_drafts_and_checks_the_few(board):
    board["items"] = [_item(1), _item(2, draft=ASKS),
                      _item(3, draft=FIXED)]
    board["open"] = {"56940001", "56940002", "56940003"}
    out = sprint_tools.sprint_board()
    lanes = _lanes(out)
    assert lanes["send"] == ["#9601"]
    assert sorted(lanes["check"]) == ["#9602", "#9603"]
    flags = {line["ticket"]: line["flags"] for line in out["check"]}
    assert "asks the client" in flags["#9602"][0]
    assert "says it is fixed" in flags["#9603"][0]
    assert out["send"][0]["draft"] == DRAFT


def test_in_progress_where_we_wrote_last_is_internal_never_a_reply(
        board, zoho):
    """#9247 on Oct 29: our Oct 16 reply was the latest message, and the
    sprint still offered her another holding reply."""
    item = _item(4, card={"live_question": "q", "lane": "engineer",
                          "reconciled": True},
                 eng_task={"id": "86eng0004",
                           "url": "https://app.clickup.com/t/86eng0004",
                           "owner": "sanjay"})
    board["items"] = [item]
    zoho.status["56940004"] = "In Progress"
    zoho.threads["56940004"] = ({"createdTime": "2026-10-10T10:00:00Z"},
                                {"createdTime": "2026-10-16T10:00:00Z"})
    out = sprint_tools.sprint_board()
    assert _lanes(out)["internal"] == ["#9604"]
    assert "draft" not in out["internal"][0]
    assert out["counts"]["send"] == 0


def test_a_client_quiet_for_weeks_is_internal_not_a_reply(board, zoho):
    """#8247: the client last wrote Aug 17. A 'thanks for your patience'
    73 days later is noise; the team gets pinged instead."""
    item = _item(5, card={"live_question": "q", "lane": "engineer",
                          "reconciled": True},
                 recipients={"to": "b@d.org", "cc": [], "first_name": "B",
                             "received_at": OLD})
    board["items"] = [item]
    zoho.status["56940005"] = "In Progress"
    out = sprint_tools.sprint_board()
    assert _lanes(out)["internal"] == ["#9605"]
    assert out["internal"][0]["next"].startswith("route_ticket")


def test_a_never_answered_new_ticket_stays_a_reply_however_old(board):
    board["items"] = [_item(6, recipients={
        "to": "c@d.org", "cc": [], "first_name": "C", "received_at": OLD})]
    board["open"] = {"56940006"}
    out = sprint_tools.sprint_board()
    assert _lanes(out)["check"] == ["#9606"]
    assert "waited" in out["check"][0]["flags"][0]


def test_shipped_work_with_a_resolution_is_good_news_to_send(board, zoho):
    board["items"] = [_item(7, draft=FIXED), _item(8, draft=FIXED)]
    board["resolutions"] = {
        "56940007": {"outcome": "drafted"},
        "56940008": {"outcome": "held",
                     "detail": "the copy link button is not fixed yet"}}
    zoho.status.update({"56940007": "In Progress",
                        "56940008": "In Progress"})
    out = sprint_tools.sprint_board()
    lanes = _lanes(out)
    assert lanes["tell_them_done"] == ["#9607"]
    assert lanes["check"] == ["#9608"]
    assert "held" in out["check"][0]["flags"][0]


def test_an_old_ack_on_shipped_work_is_not_sent_as_news(board):
    board["items"] = [_item(9)]
    board["reconcile"] = {"56940009": {"state": "shipped"}}
    board["open"] = {"56940009"}
    out = sprint_tools.sprint_board()
    assert _lanes(out)["check"] == ["#9609"]
    assert "not a resolution" in out["check"][0]["flags"][0]


def test_reconcile_only_rows_surface_untold_and_stuck_work(board):
    board["reconcile"] = {
        "T1": {"ticket_id": "T1", "ticket_number": "9001",
               "state": "shipped", "task_id": "86a",
               "task_updated": "2026-10-20",
               "last_customer_at": "2026-10-01"},
        "T2": {"ticket_id": "T2", "ticket_number": "9002",
               "state": "engineer_stalled", "task_id": "86b"},
        "T3": {"ticket_id": "T3", "ticket_number": "9003",
               "state": "engineer_active", "task_id": "86c"}}
    out = sprint_tools.sprint_board()
    lanes = _lanes(out)
    assert lanes["check"] == ["#9001"]
    assert lanes["internal"] == ["#9002"]
    assert out["internal"][0]["next"].startswith("ping_team")


@pytest.mark.parametrize("call", [
    lambda: sprint_tools.send_replies("9601", "send all", "ron@vome.co"),
    lambda: sprint_tools.ping_team("9601", "ron@vome.co"),
])
def test_only_sam_sends_in_bulk_or_pings(sam, call):
    with pytest.raises(RequestError):
        call()


def test_send_all_never_sweeps_in_a_flagged_draft(sam, monkeypatch):
    items = {"9601": _item(1), "9602": _item(2, draft=ASKS)}
    monkeypatch.setattr(sprint_tools, "find",
                        lambda t: (items[t]["ticket_id"], items[t]))
    monkeypatch.setattr(sprint_tools, "_reconcile_rows", lambda: {})
    monkeypatch.setattr(sprint_tools, "_resolutions", lambda: {})
    monkeypatch.setattr(sprint_tools, "_engineer", lambda item: {})
    monkeypatch.setattr(sprint_batch, "_now", lambda: NOW)
    sent = []
    monkeypatch.setattr(sprint_thread, "_send", lambda item: (
        sent.append(item["number"]) or f"*{item['number']}.* sent to x."))
    with pytest.raises(RequestError):
        sprint_tools.send_replies("9601", "looks good", SAM)
    out = sprint_tools.send_replies("9601 9602", "send all", SAM)
    assert sent == [1] and out["sent"] == 1
    assert "asks the client" in out["results"][1]["result"]
    out = sprint_tools.send_replies("9602", "send 9602", SAM,
                                    checked="9602")
    assert sent == [1, 2] and out["sent"] == 1


def test_ping_comments_on_the_task_and_never_emails(sam, monkeypatch):
    import httpx
    import ticket_reconciler

    posted, executed = [], []

    class Ok:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(ticket_reconciler, "_headers", lambda: {})
    monkeypatch.setattr(httpx, "post", lambda url, **k: (
        posted.append((url, k["json"])) or Ok()))
    monkeypatch.setattr(store, "execute",
                        lambda *a, **k: executed.append(a))
    monkeypatch.setattr(sprint_tools, "_reconcile_rows", lambda: {
        "T2": {"ticket_id": "T2", "ticket_number": "9002",
               "task_id": "86b", "last_customer_at": "2026-09-22"}})
    monkeypatch.setattr(sprint_tools, "find", lambda t: ("X", {}))
    out = sprint_tools.ping_team("9002 9003", SAM, note="any news?")
    assert [r["pinged"] for r in out["results"]] == [True, False]
    url, body = posted[0]
    assert url.endswith("/task/86b/comment") and body["notify_all"]
    assert "2026-09-22" in body["comment_text"]
    assert "any news?" in body["comment_text"]
    assert "—" not in body["comment_text"]
    assert executed
