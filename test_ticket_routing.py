"""
test_ticket_routing.py

Sam, 2026-10-19: every bug goes to whoever will fix it as soon as it is
triaged, the client hears from Sam in his next sprint, and Slack only hears
about urgent work. What matters here: an engineer task is filed once per bug
(never a duplicate, never blind when ClickUp cannot be searched), nothing is
sent to the client, a non-urgent ticket makes no noise, Sam's decisions
(design questions, product calls) are never routed for him, and a "we are on
it" never goes out after the engineer already shipped.
"""

import pytest

import sprint_store
import sprint_thread
import ticket_routing as tr


def _card(**over):
    card = {"lane": "engineer", "escalation_reason": "schema_or_migration",
            "live_question": "Form renames do not save",
            "situation": "Renaming a duplicated form fails.",
            "code_check": {"verdict": "bug", "where": "core:form_app/v.py:9",
                           "fix_risk": "high", "confidence": "medium"}}
    card.update(over)
    return card


def _item(**over):
    item = {"id": 21, "number": 120, "ticket_id": "569440000043000001",
            "ticket_number": "9500", "status": "open",
            "category": "clear_bug", "summary": "Form renames do not save",
            "account": "Sarasota County Government (Ultimate, 7000 CAD)",
            "draft": "Hi Stephanie,\n\nThanks, our team is on it.",
            "draft_status": "ok", "last_thread_id": "T-LATEST",
            "recipients": {"to": "s@scgov.net", "cc": []}, "card": _card()}
    item.update(over)
    return item


# ---------------------------------------------------------------------------
# Who gets it
# ---------------------------------------------------------------------------

def test_engineer_lane_is_routed_with_its_reason():
    assert tr.should_route(_card(), {}) == (True, "schema_or_migration")


def test_sams_decisions_are_never_routed_for_him():
    assert tr.should_route(
        _card(escalation_reason="product_decision"), {})[0] is False
    design = _card(code_check={"verdict": "design_question"})
    assert tr.should_route(design, {})[0] is False


def test_an_agent_fix_stays_with_the_agent_until_it_declines():
    card = _card(lane="agent_fix")
    assert tr.should_route(card, {"pr_url": "https://pr/1"})[0] is False
    assert tr.should_route(card, {"outcome": "skipped",
                                  "reason": "daily cap reached"})[0] is False
    assert tr.should_route(card, {"outcome": "declined",
                                  "reason": "touches shared data"}) == (
        True, "agent_declined")


def test_answers_features_and_ops_are_not_engineering():
    for lane in ("answer", "feature", "sam_ops", "housekeeping"):
        assert tr.should_route(_card(lane=lane), {})[0] is False


def test_backend_goes_to_onlyg_web_and_mobile_to_sanjay():
    assert tr.engineer_for(_card()) == "onlyg"
    web = _card(code_check={"where": "web:src/views/org/forms/A.js:3"})
    assert tr.engineer_for(web) == "sanjay"
    assert tr.engineer_for(
        _card(code_check={}, escalation_reason="mobile_unreadable")
    ) == "sanjay"


# ---------------------------------------------------------------------------
# What is urgent
# ---------------------------------------------------------------------------

def test_a_very_high_account_is_urgent(monkeypatch):
    monkeypatch.setattr(sprint_store, "by_ticket_numbers", lambda n: [])
    assert "very-high" in tr.urgency(7000, "Sarasota", {})
    assert tr.urgency(3900, "Summit Metro Parks", {}) == ""


def test_one_customer_writing_twice_is_not_an_incident(monkeypatch):
    monkeypatch.setattr(sprint_store, "by_ticket_numbers", lambda n: [
        {"ticket_number": "9316",
         "account": "Kimberly Crest House (Pro, 2640 CAD)"}])
    assert tr.urgency(2640, "Kimberly Crest House (Pro, 2640 CAD)",
                      {"same_as": ["9316"]}) == ""


def test_the_same_problem_on_another_account_is_urgent(monkeypatch):
    monkeypatch.setattr(sprint_store, "by_ticket_numbers", lambda n: [
        {"ticket_number": "9485", "account": "Summit Metro Parks (Pro)"},
        {"ticket_number": "9486", "account": "Summit Metro Parks (Pro)"}])
    assert tr.urgency(500, "WaterFire Providence",
                      {"same_as": ["9485", "9486"]}) == (
        "same problem reported by 2 accounts")


# ---------------------------------------------------------------------------
# Filing the task
# ---------------------------------------------------------------------------

@pytest.fixture
def world(monkeypatch):
    import clickup_search
    import zoho_actions

    class W:
        created, comments, posts, alerts = [], [], [], []
        filed, zoho, notes, decided = [], [], [], []
        siblings: list = []
        linked: list = []
        live: dict = {}
        search_fails = False

    def linked(number, tid):
        if W.search_fails:
            raise RuntimeError("ClickUp 429")
        return W.linked

    def live(task_id):
        status = W.live.get(task_id) or next(
            t["status"] for t in W.linked if t["id"] == task_id)
        return {"status": status, "shipped": status in tr.SHIPPED}

    monkeypatch.setattr(sprint_thread, "_clickup_create",
                        lambda list_id, body: W.created.append(
                            (list_id, body)) or {"id": "86new",
                                                 "url": "https://cu/86new"})
    monkeypatch.setattr(tr, "_comment",
                        lambda tid, text: W.comments.append((tid, text)))
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", channel="",
                        broadcast=False: W.posts.append(
                            (channel, text)) or "ts")
    monkeypatch.setattr(sprint_thread, "alert_sam",
                        lambda key, text: W.alerts.append((key, text))
                        or True)
    monkeypatch.setattr(sprint_store, "set_eng_task",
                        lambda i, t: W.filed.append((i, t)))
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: W.decided.append((i, s, d)))
    monkeypatch.setattr(sprint_store, "by_ticket_numbers",
                        lambda n: W.siblings)
    monkeypatch.setattr(clickup_search, "linked_tasks", linked)
    monkeypatch.setattr(tr, "_read_state", live)
    monkeypatch.setattr(zoho_actions, "set_in_progress",
                        lambda tid, team: W.zoho.append((tid, team)))
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda *a: pytest.fail("routing never emails"))
    monkeypatch.setattr("zoho_desk_api.add_ticket_comment",
                        lambda tid, text: W.notes.append(text))
    monkeypatch.delenv("AUTO_ROUTE_ENABLED", raising=False)
    W.created, W.comments, W.posts, W.alerts = [], [], [], []
    W.filed, W.zoho, W.notes, W.decided = [], [], [], []
    W.siblings, W.linked, W.live, W.search_fails = [], [], {}, False
    return W


def test_a_routine_bug_is_filed_quietly(world):
    item = _item(account="Summit Metro Parks (Pro, 1200 CAD)")
    out = tr.route(item, _card(), {}, {}, 1200)
    assert out["routed"] is True and out["urgent"] == ""
    list_id, body = world.created[0]
    assert list_id == sprint_thread.PRIORITY_QUEUE
    assert body["assignees"] == [49257687] and "priority" not in body
    assert "Filed automatically" in body["markdown_description"]
    assert world.filed[0][1]["owner"] == "onlyg"
    # Zoho stays New/Processing: the client is still owed Sam's reply.
    assert world.zoho == [] and "Routed to onlyg" in world.notes[0]
    # No Slack, and the item stays open for Sam's reply.
    assert world.posts == [] and world.alerts == [] and world.decided == []


def test_an_urgent_bug_reaches_the_engineer_and_sam(world):
    out = tr.route(_item(), _card(), {}, {}, 7000)
    assert "very-high" in out["urgent"]
    assert world.created[0][1]["priority"] == 1
    channel, text = world.posts[0]
    assert channel == sprint_thread.ENG_BACKEND and "URGENT" in text
    assert world.alerts[0][0] == "urgent:86new"
    assert "Nothing was sent to the client" in world.alerts[0][1]


def test_the_same_problem_joins_the_task_already_filed(world):
    world.siblings = [{"ticket_number": "9501", "account": "Sarasota (U)",
                       "eng_task": {"id": "86old", "url": "https://cu/86old",
                                    "owner": "onlyg"}}]
    out = tr.route(_item(account="Sarasota (U)"), _card(),
                   {}, {"same_as": ["9501"]}, 0)
    assert world.created == []
    assert world.comments[0][0] == "86old"
    assert out["task"]["how"] == "joined (same problem as #9501)"


def test_a_task_already_linked_to_the_ticket_is_reused(world):
    world.linked = [
        {"id": "86done", "list": "Priority Queue", "status": "on prod"},
        {"id": "86fr", "list": "Raw Intake", "status": "queued"},
        {"id": "86pq", "list": "Priority Queue", "status": "in progress",
         "url": "https://cu/86pq"}]
    out = tr.route(_item(), _card(), {}, {}, 0)
    assert world.created == [] and out["task"]["id"] == "86pq"


def test_a_task_closed_since_the_cached_search_is_not_joined(world):
    """#9526: the list cache still said queued; the engineer had closed it."""
    world.linked = [{"id": "86pq", "list": "Priority Queue",
                     "status": "queued", "url": "https://cu/86pq"}]
    world.live = {"86pq": "closed"}
    tr.route(_item(), _card(), {}, {}, 0)
    assert world.comments == [] and world.created[0][1]["assignees"]


def test_nothing_is_joined_blind_when_the_task_cannot_be_read(
        monkeypatch, world):
    world.linked = [{"id": "86pq", "list": "Priority Queue",
                     "status": "queued", "url": "https://cu/86pq"}]
    monkeypatch.setattr(tr, "_read_state",
                        lambda tid: {"status": "unreadable"})
    out = tr.route(_item(), _card(), {}, {}, 0)
    assert out["routed"] is False
    assert world.comments == [] and world.created == []


def test_nothing_is_filed_blind_when_clickup_cannot_be_searched(world):
    world.search_fails = True
    out = tr.route(_item(), _card(), {}, {}, 7000)
    assert out["routed"] is False and "429" in out["why"]
    assert world.created == [] and world.filed == [] and world.posts == []


def test_a_routed_item_is_not_routed_again(world):
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x"})
    assert tr.route(item, _card(), {}, {}, 7000)["routed"] is False
    assert world.created == []


def test_the_switch_leaves_routing_to_sam(monkeypatch, world):
    monkeypatch.setenv("AUTO_ROUTE_ENABLED", "false")
    assert tr.route(_item(), _card(), {}, {}, 7000)["routed"] is False
    assert world.created == []


def test_an_urgent_agent_fix_asks_sam_to_merge(world):
    assert tr.flag_urgent_fix(_item(), _card(lane="agent_fix"),
                              "https://pr/9", {}, 7000) is True
    assert "OnlyG takes it to production" in world.alerts[0][1]
    assert tr.flag_urgent_fix(_item(), _card(), "https://pr/9", {}, 100) \
        is False


# ---------------------------------------------------------------------------
# Telling the client, in Sam's sprint
# ---------------------------------------------------------------------------

def test_a_manual_route_never_files_a_second_task(world):
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"})
    out = sprint_thread._route(item, "onlyg")
    assert "already with onlyg" in out and world.created == []


def test_sending_the_acknowledgment_hands_it_over(monkeypatch, world):
    """Only now, with the client told, does Zoho leave New/Processing."""
    import shadow_triage
    import zoho_actions

    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": False})
    monkeypatch.setattr(zoho_actions, "set_status",
                        lambda *a: pytest.fail("no Awaiting/Closed here"))
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "sanjay", "reason": "mobile_unreadable"})
    out = sprint_thread._after_send(item)
    assert world.decided == [(21, sprint_store.ROUTED,
                              "routed to sanjay (mobile_unreadable)")]
    assert world.zoho == [("569440000043000001", "frontend")]
    assert "In Progress with sanjay" in out


def test_acknowledging_an_agent_fix_moves_zoho_then(monkeypatch, world):
    import shadow_triage
    import zoho_actions

    moved = []
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": False})
    monkeypatch.setattr(zoho_actions, "set_agent_fixing",
                        lambda tid: moved.append(tid))
    out = sprint_thread._after_send(_item(fix_pr="https://pr/1"))
    assert moved == ["569440000043000001"] and "In Progress" in out


def test_no_acknowledgment_after_the_engineer_shipped(monkeypatch, world):
    import sprint_batch

    monkeypatch.setattr(sprint_batch, "_resolution_tickets", lambda: set())
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: {"status": "on prod", "shipped": True})
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"})
    out = sprint_thread._send(item)
    assert "nothing sent" in out and "already shipped" in out
    assert world.decided[0][1] == sprint_store.DONE


def test_a_resolution_still_goes_after_the_engineer_shipped(monkeypatch,
                                                            world):
    import sprint_batch

    monkeypatch.setattr(sprint_batch, "_resolution_tickets",
                        lambda: {"569440000043000001"})
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: pytest.fail("not asked"))
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x"})
    assert sprint_thread._engineer_shipped(item) == ""


def test_a_ticket_that_works_again_is_never_an_incident(monkeypatch):
    """#9556 and #9552 paged Sam after both customers confirmed the fix."""
    import shadow_triage
    import vomeos

    monkeypatch.setattr(vomeos, "run_agent",
                        lambda *a, **k: pytest.fail("matcher not needed"))
    assert shadow_triage.match_incident(
        "9556", "The Goodman Group", {"lane": "housekeeping"}, "now") == {}
    assert shadow_triage.match_incident(
        "9556", "The Goodman Group", {"category": "awaiting_client"},
        "now") == {}


class _PastTheStatusCheck(Exception):
    pass


@pytest.mark.parametrize("existing,triaged", [
    ({"eng_task": {"url": "https://cu/86x"}}, True),
    ({"fix_pr": "https://pr/1"}, True),
    ({}, False),
])
def test_a_reply_on_a_ticket_triage_routed_is_triaged_again(
        monkeypatch, existing, triaged):
    """Routing sets Zoho In Progress; the customer writing again must still
    get a fresh card, or the old draft could never be sent. In Progress set
    by someone else is still left alone."""
    import shadow_triage
    import sprint
    import zoho_actions

    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(zoho_actions, "recipients",
                        lambda tid: {"thread_id": "T-NEW"})
    monkeypatch.setattr(sprint_store, "by_ticket", lambda tid: {
        "status": "routed", "last_thread_id": "T-OLD", **existing})
    monkeypatch.setattr(shadow_triage, "_triaged_today", lambda: 0)
    monkeypatch.setattr(sprint, "build_context", lambda row: {
        "_status": "In Progress", "ticket_number": "9500"})

    def stop(*a, **k):
        raise _PastTheStatusCheck()

    monkeypatch.setattr(shadow_triage, "release_state", stop)
    if triaged:
        with pytest.raises(_PastTheStatusCheck):
            shadow_triage._handle("569440000043000001", "reply")
    else:
        out = shadow_triage._handle("569440000043000001", "reply")
        assert out == {"status": "skipped", "reason": "ticket is In Progress"}


# ---------------------------------------------------------------------------
# Walking the Zoho queue from Claude.ai
# ---------------------------------------------------------------------------

def test_arr_comes_from_the_account_label():
    assert tr.arr_of("Sarasota County (very-high, 7000 CAD combined ARR)") \
        == 7000
    assert tr.arr_of("Acme (low, 1,250.5 CAD combined ARR)") == 1250.5
    assert tr.arr_of("someone@example.org") == 0


@pytest.fixture
def queue(monkeypatch):
    import sprint_batch
    import sprint_tools

    items = {
        "Z1": _item(ticket_id="Z1", ticket_number="9544"),  # route, urgent
        "Z2": _item(ticket_id="Z2", ticket_number="9578",
                    account="Summit (high, 3900 CAD combined ARR)"),
        "Z3": _item(ticket_id="Z3", ticket_number="9531",
                    card=_card(lane="feature")),
        "Z4": _item(ticket_id="Z4", ticket_number="9583",
                    eng_task={"id": "86x", "url": "https://cu/86x",
                              "owner": "onlyg"}),
        "Z5": _item(ticket_id="Z5", ticket_number="9556",
                    card=_card(lane="housekeeping")),
        "Z6": _item(ticket_id="Z6", ticket_number="9590",
                    fix_pr="https://pr/3",
                    card=_card(lane="agent_fix")),
    }
    items["Z1"]["account"] = "Sarasota (very-high, 7000 CAD combined ARR)"
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_store, "by_ticket",
                        lambda tid: dict(items.get(tid) or {}))
    monkeypatch.setattr(sprint_store, "by_incident", lambda key: [])
    monkeypatch.setattr(sprint_batch, "_queue", lambda pages=None: [
        {"id": tid, "ticketNumber": i["ticket_number"], "status": "New"}
        for tid, i in items.items()] + [
        {"id": "Z7", "ticketNumber": "9600", "status": "Processing",
         "subject": "Help"}])
    monkeypatch.setattr(sprint_tools, "_ledger", lambda tid: {})
    monkeypatch.setattr(sprint_tools, "_agent_fix",
                        lambda item, card: "PR open, waiting on Sam's merge")
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: {"status": "in progress"})
    return sprint_tools


def test_walk_shows_who_has_every_open_ticket(queue, monkeypatch):
    monkeypatch.setattr(tr, "route",
                        lambda *a, **k: pytest.fail("walking never routes"))
    out = queue.walk_queue()
    assert out["open_in_zoho"] == 7
    assert [x["ticket"] for x in out["urgent"]] == ["#9544"]
    assert "very-high" in out["urgent"][0]["urgent"]
    assert [x["ticket"] for x in out["route"]] == ["#9578"]
    assert out["route"][0]["route_to"] == "onlyg"
    assert [x["ticket"] for x in out["your_call"]] == ["#9531"]
    assert out["with_engineer"][0]["engineer"]["status"] == "in progress"
    assert [x["ticket"] for x in out["agent_fixing"]] == ["#9590"]
    assert [x["ticket"] for x in out["close_quietly"]] == ["#9556"]
    assert out["not_triaged"][0]["ticket"] == "#9600"


def test_a_card_from_before_lanes_is_never_routed(queue):
    old = _item(ticket_id="Z1", ticket_number="9575",
                card={"live_question": "q", "situation": "s"})
    section, line = queue._walk_one(old)
    assert section == "old_card"


def test_route_queue_is_sams_and_routes_only_what_was_shown(queue,
                                                            monkeypatch):
    import shadow_triage
    from mcp_connector import config

    monkeypatch.setattr(config, "can_triage", lambda c: c == "sam")
    monkeypatch.setattr(queue, "find", lambda n: (
        "", next(i for i in [queue.sprint_store.by_ticket(t)
                             for t in ("Z1", "Z2")]
                 if i["ticket_number"] == n)))
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"task_id": ""})
    routed = []
    monkeypatch.setattr(tr, "route", lambda item, card, fix, inc, arr: (
        routed.append((item["ticket_number"], arr)) or {
            "routed": True, "urgent": "", "task": {
                "how": "created", "owner": "onlyg", "url": "u"}}))
    with pytest.raises(Exception):
        queue.route_queue("someone-else")
    out = queue.route_queue("sam", tickets="9578")
    assert routed == [("9578", 3900.0)]
    assert out["routed"][0]["result"].startswith("created for onlyg")
