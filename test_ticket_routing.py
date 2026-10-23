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


def test_works_as_designed_is_never_routed_for_him():
    """#9547: a works-as-built waitlist question went to OnlyG as a data
    lookup."""
    card = _card(escalation_reason="data_lookup",
                 code_check={"verdict": "works_as_designed",
                             "where": "core:opportunity_app/t.py:1"})
    assert tr.should_route(card, {}) == (
        False, "works as designed: Sam decides")


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
        filed, zoho, notes, decided, owners = [], [], [], [], []
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
    monkeypatch.setattr(zoho_actions, "follow_route",
                        lambda tid, team: W.owners.append((tid, team))
                        or False)
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda *a: pytest.fail("routing never emails"))
    monkeypatch.setattr("zoho_desk_api.add_ticket_comment",
                        lambda tid, text: W.notes.append(text))
    monkeypatch.delenv("AUTO_ROUTE_ENABLED", raising=False)
    W.created, W.comments, W.posts, W.alerts = [], [], [], []
    W.filed, W.zoho, W.notes, W.decided = [], [], [], []
    W.owners = []
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


def test_the_zoho_owner_follows_the_route_to_the_team(world):
    """#9501: routed to Sanjay on a reopen, Zoho still said Backend Team."""
    tr.route(_item(), _card(), {}, {}, 0, owner="sanjay")
    assert world.owners == [("569440000043000001", "frontend")]
    assert world.zoho == []  # the status is still Sam's to move


@pytest.fixture
def zoho_ticket(monkeypatch):
    import zoho_actions

    class Z:
        owner = ""
        patches: list = []

    monkeypatch.setattr(zoho_actions, "_get",
                        lambda path, params=None: {"assigneeId": Z.owner})
    monkeypatch.setattr(zoho_actions, "_send",
                        lambda method, path, body: Z.patches.append(body))
    Z.patches = []
    return Z


def test_the_other_teams_ticket_moves_owner_only(zoho_ticket):
    import zoho_actions

    zoho_ticket.owner = zoho_actions.BACKEND_TEAM
    assert zoho_actions.follow_route("T1", "frontend") is True
    assert zoho_ticket.patches == [
        {"assigneeId": zoho_actions.FRONTEND_TEAM}]


def test_an_owner_that_is_not_the_other_team_is_left(zoho_ticket):
    import zoho_actions

    for owner in (zoho_actions.FRONTEND_TEAM, zoho_actions.SAM_AGENT, ""):
        zoho_ticket.owner = owner
        assert zoho_actions.follow_route("T1", "frontend") is False
    assert zoho_ticket.patches == []


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
    world.live = {"86old": "in progress"}
    out = tr.route(_item(account="Sarasota (U)"), _card(),
                   {}, {"same_as": ["9501"]}, 0)
    assert world.created == []
    assert world.comments[0][0] == "86old"
    assert out["task"]["how"] == "joined (same problem as #9501)"


def test_a_closed_task_of_the_same_problem_is_not_joined(world):
    """#9462, #9563 and #9583 joined 868mka0f8 after it was closed."""
    world.siblings = [{"ticket_number": "9560", "account": "Sarasota (U)",
                       "eng_task": {"id": "86shut", "url": "https://cu/86s",
                                    "owner": "onlyg"}}]
    world.live = {"86shut": "closed"}
    out = tr.route(_item(account="Summit (P)"), _card(),
                   {}, {"same_as": ["9560"]}, 0)
    assert world.comments == [] and out["task"]["how"] == "created"


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
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: {"status": "queued", "shipped": False})
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "sanjay", "reason": "mobile_unreadable"})
    out = sprint_thread._after_send(item)
    assert world.decided == [(21, sprint_store.ROUTED,
                              "routed to sanjay (mobile_unreadable)")]
    assert world.zoho == [("569440000043000001", "frontend")]
    assert "In Progress with sanjay" in out


def test_answering_a_task_the_engineer_turned_down_closes_both(monkeypatch,
                                                              world):
    """#9547 (2026-10-22): the routed task was declined, Sam answered, and
    the ticket and task were both left for someone to tidy by hand."""
    import shadow_triage
    import zoho_actions

    statuses, closed = [], []
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": False})
    monkeypatch.setattr(zoho_actions, "set_status",
                        lambda tid, s: statuses.append((tid, s)))
    monkeypatch.setattr(sprint_thread, "_clickup_status",
                        lambda tid, s: closed.append((tid, s)))
    monkeypatch.setattr(tr, "task_state", lambda filed: {
        "status": "closed", "shipped": False, "declined": True})
    item = _item(category="needs_human",
                 eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"})
    out = sprint_thread._after_send(item)
    assert statuses == [("569440000043000001", "Closed")]
    assert world.zoho == [] and world.decided == []
    # Already Closed on ClickUp, so nothing to write there.
    assert closed == [] and "no closure email" in out


def test_closing_the_ticket_closes_the_routed_task(monkeypatch, world):
    import shadow_triage
    import zoho_actions

    closed = []
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": True})
    monkeypatch.setattr(zoho_actions, "set_status", lambda tid, s: None)
    monkeypatch.setattr(sprint_thread, "_close_agent_fix", lambda item: "")
    monkeypatch.setattr(sprint_thread, "_clickup_status",
                        lambda tid, s: closed.append((tid, s)))
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: {"status": "on dev", "shipped": False})
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"})
    out = sprint_thread._after_send(item)
    assert closed == [("86x", "Closed")] and "ClickUp task closed" in out


def test_a_closed_declined_task_is_not_read_as_shipped(monkeypatch):
    import httpx

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": {"status": "Closed"}, "assignees": [],
                    "custom_fields": [{
                        "id": tr.FIELD_RESOLUTION, "value": 1,
                        "type_config": {"options": [
                            {"id": "c51c", "orderindex": 0},
                            {"id": tr.RESOLUTION_DECLINED,
                             "orderindex": 1}]}}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    state = tr._read_state("86x")
    assert state["declined"] is True and state["shipped"] is False


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
    monkeypatch.setattr(sprint_batch, "answered_in_zoho", lambda t: True)
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: {"status": "on prod", "shipped": True})
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"})
    out = sprint_thread._send(item)
    assert "nothing sent" in out and "already shipped" in out
    assert world.decided[0][1] == sprint_store.DONE


def test_a_closed_task_nobody_told_the_client_about_stays(monkeypatch,
                                                          world):
    """#9441 and seven more (2026-10-22): the engineer closed the task, no
    email went, and the ticket left the sprint as if it had."""
    import sprint_batch

    monkeypatch.setattr(sprint_batch, "_resolution_tickets", lambda: set())
    monkeypatch.setattr(sprint_batch, "answered_in_zoho", lambda t: False)
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: {"status": "closed", "shipped": True})
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"})
    out = sprint_thread._send(item)
    assert "nothing sent" in out and "has not been told" in out
    assert world.decided == [] and item["status"] == "open"


def test_a_reply_on_a_closed_task_is_the_answer_not_in_progress(
        monkeypatch, world):
    """#9549 and #9575 (2026-10-23): answers on closed tasks were set to
    In Progress 'emailed when it ships', which never happens."""
    import shadow_triage
    import zoho_actions

    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: {"live": False})
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: {"status": "closed", "shipped": True})
    statuses = []
    monkeypatch.setattr(zoho_actions, "set_status",
                        lambda tid, s: statuses.append(s))
    monkeypatch.setattr(zoho_actions, "set_in_progress",
                        lambda *a: pytest.fail("not In Progress"))
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"}, category="needs_human")
    out = sprint_thread._after_send(item)
    assert statuses == ["Awaiting Client Response"]
    assert "emailed when it ships" not in out

    statuses.clear()
    sprint_thread._after_send({**item, "category": "user_education"})
    assert statuses == ["Closed"]


def test_sams_own_reply_is_the_outcome_and_skips_the_stale_ack_check(
        monkeypatch, world):
    """#9549 and #9575 (2026-10-23): Sam's corrected answers were refused
    as 'the team is on it' because the engineer task was closed."""
    import sprint_batch

    monkeypatch.setattr(sprint_batch, "_resolution_tickets", lambda: set())
    monkeypatch.setattr(tr, "task_state",
                        lambda filed: pytest.fail("not asked for Sam's text"))
    # Past the guard, stop at the next check (a newer customer message)
    # so nothing is really sent.
    import zoho_actions

    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda tid: {"id": "newer"})
    item = _item(eng_task={"id": "86x", "url": "https://cu/86x",
                           "owner": "onlyg"})
    out = sprint_thread._send(item, sam_wrote=True)
    assert "has not been told" not in out
    assert "written again" in out


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

def test_the_urgency_count_is_the_tickets_the_matcher_named(monkeypatch):
    """#9550 read "same problem reported by 7 accounts" because everyone
    tied to one noisy Sentry error shared its key."""
    crowd = [{"ticket_number": str(n), "account": f"Org {n} (low)"}
             for n in range(9400, 9409)]
    monkeypatch.setattr(sprint_store, "by_incident", lambda key: crowd)
    monkeypatch.setattr(sprint_store, "by_ticket_numbers", lambda numbers: [
        i for i in crowd if i["ticket_number"] in numbers])
    named = _item(incident_key="sentry:7624230698",
                  card=_card(incident={"same_as": [], "sentry_issue":
                                       "7624230698"}))
    assert tr.incident_of(named)["same_as"] == []
    assert tr.urgency(0, "Lone (low)", tr.incident_of(named)) == ""
    # Triaged before the card kept the match: a Sentry key names nobody.
    old = _item(incident_key="sentry:7624230698", card=_card())
    assert tr.incident_of(old)["same_as"] == []
    pair = _item(incident_key="tickets:9400",
                 card=_card(incident={"same_as": ["9401"]}))
    assert tr.urgency(0, "Lone (low)", tr.incident_of(pair)) == (
        "same problem reported by 2 accounts")


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


def test_an_engineer_card_that_is_not_routed_is_sams_call(queue):
    wad = _item(ticket_id="Z1", ticket_number="9547", card=_card(
        escalation_reason="data_lookup",
        code_check={"verdict": "works_as_designed"}))
    section, line = queue._walk_one(wad)
    assert section == "your_call"
    assert line["why_yours"] == "works as designed: Sam decides"


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


def test_an_important_client_is_filed_high_with_its_fields(world):
    # #9596: an important client's task was filed with no priority, no
    # Type or Platform, and a title cut mid-word.
    question = ("Why Jeremy Secaida's profile crashes the Database page "
                "while every other profile opens, and when it will open "
                "again.")
    item = _item(account="Candlelighters NV (high, 1980 CAD)",
                 important=True)
    card = _card(live_question=question,
                 code_check={"verdict": "unclear", "where": "web:src/x:1"})
    tr.route(item, card, {}, {}, 1980)
    body = world.created[0][1]
    assert body["priority"] == 2
    fields = {f["id"]: f["value"] for f in body["custom_fields"]}
    assert fields[tr.FIELD_TYPE] == tr.TYPE_BUG
    assert fields[tr.FIELD_PLATFORM] == tr.PLATFORMS["web"]
    assert fields[tr.FIELD_COMBINED_ARR] == 1980
    assert body["name"].endswith("every other profile opens...")


def test_a_short_title_is_kept_whole():
    assert tr._title("Form renames  do not save") == "Form renames do not save"
