"""
test_sprint_flow.py

Items three to five of the agent roadmap: shadow triage, the daily sprint
thread, and incident spotting.

What matters most here is the one path that can reach a customer. "send"
must only ever act on Sam's command, on a draft that passed its guards, to
the people the latest message went to, and never on a draft that answers a
question the customer has since moved past. Draft mode must not email
anyone. The rest (numbering, carry-forward, incidents) is what makes Sam's
"send 1-4" mean the same thing at 13:05 as it did at 13:00.

The store tests run against a real Postgres when SPRINT_TEST_DATABASE_URL is
set (a throwaway container locally) and skip otherwise.
"""

import os
from datetime import timedelta

import pytest

import shadow_triage
import sprint_store
import sprint_thread
from vomeos import config, store

TEST_DB = os.environ.get("SPRINT_TEST_DATABASE_URL", "")


# ---------------------------------------------------------------------------
# Parsing Sam's replies
# ---------------------------------------------------------------------------

def test_send_ranges_and_lists():
    assert sprint_thread.parse("send 1-4") == [
        {"verb": "send", "numbers": [1, 2, 3, 4], "arg": ""}]
    assert sprint_thread.parse("send 1, 3 and 5")[0]["numbers"] == [1, 3, 5]


def test_several_commands_one_per_line():
    cmds = sprint_thread.parse("send 1-2\nhold 6\nroute 7 to OnlyG\ndone 9")
    assert [c["verb"] for c in cmds] == ["send", "hold", "route", "done"]
    assert cmds[2]["arg"] == "onlyg"


def test_edit_takes_the_rest_of_the_message_verbatim():
    cmds = sprint_thread.parse(
        "edit 2: Thanks, our team is going to look into this.\n\nOne more "
        "line.")
    assert cmds == [{"verb": "edit", "numbers": [2],
                     "arg": "Thanks, our team is going to look into this."
                            "\n\nOne more line."}]


def test_chat_is_not_a_command():
    """Sam can talk in the thread without triggering anything."""
    assert sprint_thread.parse("looks good, I'll send after lunch") == []
    assert sprint_thread.parse("can you send me the link") == []
    # Someone other than Sam chatting in the thread is left alone.
    assert sprint_thread.handle_reply(
        {"user": "U0APZ7JUHRD", "text": "send 1"}) is False


def test_skip_is_done():
    assert sprint_thread.parse("skip 4")[0]["verb"] == "done"


# ---------------------------------------------------------------------------
# A fake store for the command paths (no database needed)
# ---------------------------------------------------------------------------

def _item(**over):
    item = {
        "id": 11, "number": 3, "ticket_id": "569440000042922001",
        "ticket_number": "9415", "status": "open", "category": "clear_bug",
        "account": "Main Line Health (Enterprise, 0 CAD)", "important": True,
        "summary": "Highlighted boxes", "draft_status": "ok",
        "draft": "Hi Dalena,\n\nThanks, our team is going to look into "
                 "this.\n\nSam | Vome team\nsupport.vomevolunteer.com",
        "draft_why": "", "last_thread_id": "T-LATEST",
        "recipients": {"to": "phanda@mlhs.org", "cc": ["boss@mlhs.org"],
                       "first_name": "Dalena", "thread_id": "T-LATEST"},
        "card": {"category": "clear_bug", "confidence": "high",
                 "live_question": "Why are boxes highlighted?",
                 "situation": "Submitted PDFs show green boxes."},
    }
    item.update(over)
    return item


@pytest.fixture
def world(monkeypatch):
    import zoho_actions

    class W:
        decided = []
        drafts = []
        posts = []
        zoho = []
        latest = "T-LATEST"

    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: W.decided.append((i, s, d)))
    monkeypatch.setattr(sprint_store, "set_draft",
                        lambda i, t, s, w="": W.drafts.append((i, t, s, w)))
    monkeypatch.setattr(sprint_store, "hold",
                        lambda i, wake, d: W.decided.append(
                            (i, sprint_store.HELD, d)))
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", channel="",
                        broadcast=False: W.posts.append(
                            (channel, text)) or "ts")
    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda tid: {"id": W.latest})
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda tid, to, cc, c: W.zoho.append(
                            ("send", tid, to, cc, c)) or {})
    return W


def test_send_emails_the_right_people_and_logs(world):
    out = sprint_thread._send(_item())
    _, tid, to, cc, content = world.zoho[0]
    assert to == "phanda@mlhs.org" and cc == ["boss@mlhs.org"]
    assert content.startswith("Hi Dalena")
    assert world.decided[0][1] == sprint_store.SENT
    assert any(ch == sprint_thread.AGENT_LOG for ch, _ in world.posts)
    assert "sent to phanda@mlhs.org" in out


def test_a_newer_customer_message_blocks_send(world):
    """The draft answers a question she has moved past."""
    world.latest = "T-NEWER"
    out = sprint_thread._send(_item())
    assert world.zoho == [] and world.decided == []
    assert "written again" in out


def test_a_guard_blocked_draft_is_never_sent(world):
    out = sprint_thread._send(_item(draft_status="blocked"))
    assert world.zoho == []
    assert "edit 3" in out


def test_an_item_already_sent_is_not_sent_twice(world):
    sprint_thread._send(_item(status="sent"))
    assert world.zoho == []


def test_a_logged_request_still_sends_the_reply_it_owes(world):
    """#9557 (2026-10-21): `build` finished the item before Sam's reply
    went, and `send` then refused it as already done."""
    out = sprint_thread._send(_item(status="done",
                                    decision="build: https://cu/86y"))
    assert world.zoho and "sent to phanda@mlhs.org" in out
    assert world.decided[0][1] == sprint_store.SENT
    assert world.decided[0][2] == "sent by Sam after build: https://cu/86y"


@pytest.mark.parametrize("over", [
    {"status": "done", "decision": "done by Sam"},
    {"status": "done", "decision": "log: https://cu/86y",
     "sent_at": "2026-10-21T14:00:00"},
    {"status": "sent", "decision": "log: https://cu/86y"},
])
def test_a_finished_item_is_not_sent(world, over):
    out = sprint_thread._send(_item(**over))
    assert world.zoho == [] and "nothing sent" in out


def test_edit_keeps_sams_words_and_adds_only_greeting_and_signature(world):
    out = sprint_thread._edit(_item(), "She can remove the note now.")
    body = world.drafts[0][1]
    assert body == ("Hi Dalena,\n\nShe can remove the note now.\n\n"
                    "Sam | Vome team\nsupport.vomevolunteer.com")
    assert world.drafts[0][2] == "ok"
    assert "send 3" in out


def test_edit_shows_guard_concerns_without_rewriting(world):
    out = sprint_thread._edit(
        _item(), "Sanjay fixed it " + chr(0x2014) + " try again.")
    assert "Sanjay" in world.drafts[0][1]
    assert "Check before sending" in out


def test_route_files_the_task_and_leaves_zoho_alone(monkeypatch, world):
    """Rule 6 (2026-10-19): routing by hand follows triage's own routing.
    New or Processing means Sam's reply is still owed, so the status, the
    Slack channel (not urgent) and the item all wait for that reply."""
    import clickup_search
    import zoho_actions

    created, status, filed = [], [], []
    monkeypatch.setattr(sprint_thread, "_clickup_create",
                        lambda list_id, body: created.append(
                            (list_id, body)) or {"id": "86x",
                                                 "url": "https://cu/86x"})
    monkeypatch.setattr(zoho_actions, "set_in_progress",
                        lambda tid, team: status.append((tid, team)))
    monkeypatch.setattr("zoho_desk_api.add_ticket_comment",
                        lambda tid, text: None)
    monkeypatch.setattr(clickup_search, "linked_tasks", lambda n, t: [])
    monkeypatch.setattr(sprint_store, "set_eng_task",
                        lambda i, t: filed.append(t))
    out = sprint_thread._route(_item(), "sanjay")
    list_id, body = created[0]
    assert list_id == sprint_thread.PRIORITY_QUEUE
    assert body["assignees"] == [4434086]
    assert filed[0]["owner"] == "sanjay"
    assert status == [] and world.posts == [] and world.decided == []
    # Routing is not a reply.
    assert world.zoho == []
    assert "still owed your reply" in out


def test_route_to_an_unknown_person(world):
    assert "OnlyG or Sanjay" in sprint_thread._route(_item(), "ron")


def test_build_goes_to_sam_accepted(monkeypatch, world):
    from mcp_connector import feature_requests

    created = []
    # No existing request matches, so both go ahead (the duplicate check
    # itself is covered in test_remaining_items.py).
    monkeypatch.setattr(feature_requests, "find_duplicates", lambda t, p: [])
    monkeypatch.setattr(sprint_thread, "_clickup_create",
                        lambda list_id, body: created.append(list_id) or
                        {"id": "86y"})
    sprint_thread._feature(_item(), "build")
    sprint_thread._feature(_item(), "log")
    assert created == [sprint_thread.SAM_ACCEPTED, sprint_thread.RAW_INTAKE]


def test_build_is_sams_and_log_is_for_later(monkeypatch, world):
    import clickup_tasks
    from mcp_connector import feature_requests

    bodies = {}
    monkeypatch.setattr(feature_requests, "find_duplicates", lambda t, p: [])
    monkeypatch.setattr(sprint_thread, "_clickup_create",
                        lambda list_id, body: bodies.setdefault(list_id, body)
                        and {"id": "86y"})
    item = _item(account="Sarasota County Government (very-high, 7000 CAD "
                         "combined ARR)")
    sprint_thread._feature(item, "build",
                           title="Shared settings for sequences",
                           module="sequences")
    sprint_thread._feature(item, "log")
    build = bodies[sprint_thread.SAM_ACCEPTED]
    later = bodies[sprint_thread.RAW_INTAKE]
    assert build["name"] == ("[#9415] Sarasota County Government -- Shared "
                             "settings for sequences")
    assert build["assignees"] == [clickup_tasks.CLICKUP_USER_SAM]
    assert build["priority"] == 2
    fields = {f["id"]: f["value"] for f in build["custom_fields"]}
    assert fields[clickup_tasks.FIELD_REQUESTING_CLIENTS] == (
        "Sarasota County Government")
    assert fields[clickup_tasks.FIELD_COMBINED_ARR] == 7000
    assert clickup_tasks.FIELD_MODULE in fields
    # Later is unassigned until Sam triages it; the card's question is the
    # fallback title.
    assert "assignees" not in later and "priority" not in later
    assert later["name"].endswith("Why are boxes highlighted?")


def test_build_onto_an_existing_request_moves_it(monkeypatch, world):
    from mcp_connector import feature_requests

    triaged = []
    monkeypatch.setattr(feature_requests, "add_plus_one",
                        lambda **kw: {"url": "u", "name": "Bulk settings"})
    monkeypatch.setattr(feature_requests, "triage",
                        lambda **kw: triaged.append(kw) or {})
    out = sprint_thread._feature(_item(), "build", "868abc123")
    assert triaged[0]["decision"] == "build_now"
    assert triaged[0]["priority"] == "high"
    assert "moved to Sam Accepted" in out
    sprint_thread._feature(_item(), "log", "868abc123")
    assert len(triaged) == 1  # log only adds the +1


def test_only_sam_can_command(monkeypatch, world):
    out = sprint_thread.handle_command(
        {"user": "U0APZ7JUHRD", "text": "send 1", "thread_ts": "1.1",
         "ts": "1.2"})
    assert out["status"] == "refused" and world.zoho == []


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def test_card_shows_the_draft_and_its_recipients(monkeypatch):
    text = sprint_thread.render_card(_item())
    assert "*3.*" in text and "#9415" in text and "*IMPORTANT*" in text
    assert "to phanda@mlhs.org, cc boss@mlhs.org" in text
    assert "`send 3`" in text
    assert chr(0x2014) not in text


def test_card_for_a_blocked_draft_says_why():
    text = sprint_thread.render_card(
        _item(draft_status="blocked", draft_why="claims a fix"))
    assert "held by a guard" in text and "claims a fix" in text
    assert "`send 3`" not in text


def test_card_without_a_draft_offers_the_decisions():
    text = sprint_thread.render_card(_item(draft="", draft_status="none"))
    assert "route 3 to OnlyG" in text and "build 3" in text


def test_update_lists_needs_held_and_done(monkeypatch):
    monkeypatch.setattr(sprint_thread, "_release_lines", lambda: [])
    items = [_item(number=1), _item(number=2, status="held"),
             _item(number=3, status="sent")]
    text = sprint_thread.render_update(items, "Update 1:00 PM")
    assert "1 need you" in text and "1 held" in text and "1 done" in text


def test_clock_and_day_labels_have_no_platform_flags():
    from datetime import date, datetime

    assert sprint_thread.day_label(date(2026, 10, 17)) == "Saturday, Oct 17"
    assert sprint_thread._clock(datetime(2026, 10, 17, 13, 0)) == "1:00 PM"
    assert sprint_thread._clock(datetime(2026, 10, 17, 0, 5)) == "12:05 AM"


# ---------------------------------------------------------------------------
# Shadow triage
# ---------------------------------------------------------------------------

def test_release_state_live_backs_a_resolution(monkeypatch):
    import release_watcher as rw

    parent = {"id": "868mjcvrn", "name": "[#9415] Fillable PDF",
              "parent": None, "custom_fields": [
                  {"id": rw.FIELD_FIX_COMMIT, "value": "web@259210699"},
                  {"id": rw.FIELD_RELEASE_CHECK, "value": 2,
                   "type_config": {"options": [
                       {"id": rw.RELEASE_CHECK_OPTIONS["live"],
                        "orderindex": 2}]}}]}
    child = {"id": "868mjcvrz", "parent": "868mjcvrn",
             "name": "[#9415] Main Line Health", "custom_fields": []}
    monkeypatch.setattr(rw, "_list_on_dev_tasks", lambda: [parent, child])
    state = shadow_triage.release_state("9415", "569440000042922001")
    assert state["live"] is True and state["task_id"] == "868mjcvrn"
    assert "LIVE" in state["text"]


def test_release_state_not_live_says_nothing_is_backed(monkeypatch):
    import release_watcher as rw

    parent = {"id": "868mjcv7q", "name": "[#9500] Form editor",
              "parent": None, "custom_fields": [
                  {"id": rw.FIELD_FIX_COMMIT, "value": "core@068fed0d"}]}
    child = {"id": "868mjcv84", "parent": "868mjcv7q",
             "name": "[#9500] Sarasota", "custom_fields": []}
    monkeypatch.setattr(rw, "_list_on_dev_tasks", lambda: [parent, child])
    state = shadow_triage.release_state("9500", "x")
    assert state["live"] is False and "NOT LIVE" in state["text"]


def test_release_state_unreadable_is_unverified(monkeypatch):
    import release_watcher as rw

    def boom():
        raise RuntimeError("ClickUp 503")

    monkeypatch.setattr(rw, "_list_on_dev_tasks", boom)
    assert "UNVERIFIED" in shadow_triage.release_state("1", "2")["text"]


def test_incident_low_confidence_is_ignored(monkeypatch):
    from vomeos import runner

    class R:
        ok = True
        data = {"same_as": [9543], "sentry_issue": "", "confidence": "low"}

    monkeypatch.setattr(runner, "run_agent", lambda *a, **k: R())
    monkeypatch.setattr("vomeos.run_agent", lambda *a, **k: R())
    monkeypatch.setattr(sprint_store, "recent", lambda h=24: [])
    assert shadow_triage.match_incident("9552", "x", {}, "now") == {}


def test_incident_medium_confidence_does_not_page_sam(monkeypatch):
    """#9500 and #9415 were paired at medium on Oct 17. Different bugs."""
    class R:
        ok = True
        data = {"same_as": [9415], "sentry_issue": "",
                "confidence": "medium", "symptom": "form save failures"}

    monkeypatch.setattr("vomeos.run_agent", lambda *a, **k: R())
    monkeypatch.setattr(sprint_store, "recent", lambda h=24: [])
    assert shadow_triage.match_incident("9500", "x", {}, "now") == {}


def test_incident_key_prefers_sentry(monkeypatch):
    class R:
        ok = True
        data = {"same_as": [9543], "sentry_issue": "7794622228",
                "confidence": "high", "symptom": "profiles won't save"}

    monkeypatch.setattr("vomeos.run_agent", lambda *a, **k: R())
    monkeypatch.setattr(sprint_store, "recent", lambda h=24: [])
    out = shadow_triage.match_incident("9552", "x", {}, "now")
    assert out["key"] == "sentry:7794622228"


def test_french_is_detected():
    assert shadow_triage._language(
        "Bonjour, les heures ne sont pas dans le rapport pour nous") == \
        "French"
    assert shadow_triage._language("Hi, the report is not working for "
                                   "us and the hours") == "English"


def test_feature_requests_and_escalations_get_no_draft():
    for category in ("feature_request", "needs_human", "escalated",
                     "awaiting_client"):
        assert category not in shadow_triage.DRAFT_CATEGORIES


# ---------------------------------------------------------------------------
# The store, against a real Postgres
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    if not TEST_DB:
        pytest.skip("SPRINT_TEST_DATABASE_URL not set")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB)
    store.reset()
    for table in (sprint_store.ITEMS, sprint_store.DAYS,
                  sprint_thread.COMMANDS_TABLE,
                  sprint_thread.INCIDENTS_TABLE,
                  sprint_thread.TURNS_TABLE, sprint_store.BATCHES,
                  "vomeos_sprint_leases"):
        store.execute(f"DROP TABLE IF EXISTS {table}")
    store.reset()
    assert sprint_store.ready()
    yield
    store.reset()


def _upsert(ticket_id, thread_id="T1", **over):
    fields = dict(
        ticket_id=ticket_id, ticket_number=ticket_id[-4:],
        category="clear_bug", account="Acme", subject="s", summary="q",
        important=False, card={"situation": "x"}, draft="d",
        draft_status="ok", draft_why="", recipients={"to": "a@b.co"},
        last_thread_id=thread_id,
    )
    fields.update(over)
    return sprint_store.upsert_ticket(**fields)


def test_numbers_are_assigned_in_order_and_kept(db):
    a, b = _upsert("t-1001"), _upsert("t-1002")
    assert (a["number"], b["number"]) == (1, 2)
    again = _upsert("t-1001", thread_id="T2")
    assert again["number"] == 1
    assert again["last_thread_id"] == "T2"
    assert again["recipients"] == {"to": "a@b.co"}


def test_a_new_reply_reopens_a_finished_item(db):
    item = _upsert("t-2001")
    sprint_store.decide(item["id"], sprint_store.DONE, "done")
    again = _upsert("t-2001", thread_id="T9")
    assert again["status"] == sprint_store.OPEN
    assert again["number"] == item["number"]


def test_carry_forward_keeps_permanent_numbers(db):
    old = _upsert("t-3001")
    held = _upsert("t-3002")
    sprint_store.decide(held["id"], sprint_store.HELD, "held")
    done = _upsert("t-3003")
    sprint_store.decide(done["id"], sprint_store.DONE, "done")
    yesterday = sprint_store.today() - timedelta(days=1)
    store.execute(f"UPDATE {sprint_store.ITEMS} SET day = :d",
                  {"d": yesterday})
    today_new = _upsert("t-3004")
    moved = sprint_store.carry_forward()
    assert moved == 2
    numbers = {i["ticket_id"]: i["number"]
               for i in sprint_store.items_for_day(sprint_store.today())}
    # The first weekend: carried items got new numbers while their cards
    # stayed in yesterday's thread. Now a number never changes.
    assert numbers == {"t-3001": 1, "t-3002": 2, "t-3004": 4}
    assert today_new["number"] == 4
    assert sprint_store.get_item(old["id"])["day"] == sprint_store.today()


def test_numbers_never_restart_on_a_new_day(db):
    first = _upsert("t-3101")
    store.execute(f"UPDATE {sprint_store.ITEMS} SET day = :d",
                  {"d": sprint_store.today() - timedelta(days=1)})
    second = _upsert("t-3102")
    assert second["number"] == first["number"] + 1
    again = _upsert("t-3101", thread_id="T5")
    assert again["number"] == first["number"]


def test_a_number_from_before_permanence_that_collides_is_replaced(db):
    old = _upsert("t-3201")
    store.execute(f"UPDATE {sprint_store.ITEMS} SET day = :d",
                  {"d": sprint_store.today() - timedelta(days=1)})
    clash = _upsert("t-3202")
    store.execute(f"UPDATE {sprint_store.ITEMS} SET number = :n WHERE id ="
                  " :i", {"n": old["number"], "i": clash["id"]})
    assert sprint_store.carry_forward() == 1
    moved = sprint_store.get_item(old["id"])
    assert moved["day"] == sprint_store.today()
    assert moved["number"] != old["number"]


def test_hold_until_and_working_set(db):
    item = _upsert("t-3301")
    wake = sprint_store._now() + timedelta(days=2)
    sprint_store.hold(item["id"], wake, "held by Sam until Friday")
    row = sprint_store.get_item(item["id"])
    assert row["status"] == sprint_store.HELD and row["wake_at"] == wake
    assert [i["id"] for i in sprint_store.working_set()] == [item["id"]]
    again = _upsert("t-3301", thread_id="T7")
    assert again["status"] == sprint_store.OPEN and again["wake_at"] is None


def test_by_number_and_day_thread(db):
    item = _upsert("t-4001")
    day = sprint_store.today()
    assert sprint_store.by_number(item["number"])["ticket_id"] == "t-4001"
    sprint_store.set_day_thread(day, "C1", "123.456")
    assert sprint_store.get_day(day)["thread_ts"] == "123.456"


def test_recent_returns_what_changed(db):
    _upsert("t-5001")
    assert [r["ticket_id"] for r in sprint_store.recent(24)] == ["t-5001"]


def test_command_is_run_once_per_slack_message(db, monkeypatch, world):
    day = sprint_store.today()
    sprint_store.set_day_thread(day, "C1", "999.1")
    _upsert("t-6001")
    runs = []
    monkeypatch.setattr(sprint_thread, "_execute",
                        lambda c, i: runs.append(i["number"]) or "ok")
    msg = {"user": "U01B9FQNSBU", "text": "done 1", "thread_ts": "999.1",
           "ts": "999.2"}
    sprint_thread.handle_command(msg)
    sprint_thread.handle_command(msg)
    assert runs == [1]


def test_the_engineer_task_survives_a_retriage(db):
    """A new customer message re-triages the item; the task triage filed
    must stay, or the next pass files a second one (ticket_routing)."""
    item = _upsert("t-8001")
    sprint_store.set_eng_task(item["id"], {"id": "86x", "url": "u",
                                           "owner": "onlyg"})
    again = _upsert("t-8001", thread_id="T2")
    assert again["eng_task"]["id"] == "86x"
    found = sprint_store.by_ticket_numbers(["8001", "9999"])
    assert [i["ticket_number"] for i in found] == ["8001"]
    assert found[0]["eng_task"]["owner"] == "onlyg"


def test_urgent_alert_is_posted_once_a_day(db, world):
    """Only urgent work interrupts Sam (2026-10-19), and only once."""
    assert sprint_thread.alert_sam("urgent:86x", "Urgent, with onlyg") is True
    assert sprint_thread.alert_sam("urgent:86x", "Urgent, with onlyg") is False
    assert sprint_thread.alert_sam("urgent:86y", "another task") is True
    assert sum("U01B9FQNSBU" in text for _, text in world.posts) == 2


# ---------------------------------------------------------------------------
# Conversation: the partner proposes, Sam's own words decide
# ---------------------------------------------------------------------------

def test_strict_commands_skip_the_model():
    assert sprint_thread.parse_strict("send 1-4")[0]["numbers"] == [1, 2, 3, 4]
    assert sprint_thread.parse_strict("hold 6\ndone 7") is not None
    assert sprint_thread.parse_strict("edit 2: new words\nmore") is not None


@pytest.mark.parametrize("text", [
    "send 3 and tell me about 5",
    "what's going on with 4?",
    "ok send it",
    "looks good",
])
def test_anything_conversational_goes_to_the_partner(text):
    assert sprint_thread.parse_strict(text) is None


def test_references_by_item_and_ticket_number():
    items = [_item(number=2, ticket_number="9415"),
             _item(number=4, ticket_number="9500")]
    assert sprint_thread.referenced_numbers("what about #9500 and 2?",
                                            items) == [4, 2]
    # Times and other numbers are not items.
    assert sprint_thread.referenced_numbers("call at 10:20, 3 days",
                                            items) == []


class _Partner:
    def __init__(self, data):
        self.ok = True
        self.data = data

    def why(self):
        return ""


@pytest.fixture
def chat(monkeypatch, world):
    """Two items today, a conversation, and a scripted partner."""
    items = [_item(id=21, number=2, ticket_number="9415"),
             _item(id=23, number=3, ticket_number="9436",
                   ticket_id="569440000042932075")]

    class C:
        turns = []
        partner = {"reply": "ok", "focus": None, "verbs": "", "actions": []}
        routed = []

    monkeypatch.setattr(sprint_store, "working_set", lambda: items)
    monkeypatch.setattr(sprint_thread, "_turns", lambda thread: C.turns)
    monkeypatch.setattr(sprint_thread, "_ticket_text",
                        lambda item, n="": "thread text")
    monkeypatch.setattr("vomeos.run_agent",
                        lambda *a, **k: _Partner(C.partner))
    monkeypatch.setattr(sprint_thread, "_route",
                        lambda item, who: C.routed.append(who) or "routed")
    return C


def _turn(role, text, focus=None):
    return {"role": role, "text": text, "focus": focus, "created_at": None}


def test_send_it_sends_the_item_in_focus(chat, world):
    chat.turns = [_turn("sam", "what's the story on 2?"),
                  _turn("agent", "Dalena at Main Line Health...", focus=2),
                  _turn("sam", "ok send it")]
    chat.partner = {"reply": "Sending 2.", "focus": 2, "verbs": "send:2",
                    "actions": [{"verb": "send", "number": 2}]}
    reply, focus, results = sprint_thread.converse("ok send it", "d", "t")
    assert world.zoho and world.zoho[0][0] == "send"
    assert "sent to phanda@mlhs.org" in reply


def test_the_model_cannot_send_what_sam_did_not_ask_for(chat, world):
    """Sam asked a question. A send proposed by the model is refused."""
    chat.turns = [_turn("sam", "what about 3?")]
    chat.partner = {"reply": "3 looks ready.", "focus": 3, "verbs": "send:3",
                    "actions": [{"verb": "send", "number": 3}]}
    reply, _, _ = sprint_thread.converse("what about 3?", "d", "t")
    assert world.zoho == []
    assert "Not sent" in reply


def test_send_it_with_nothing_in_focus_sends_nothing(chat, world):
    chat.turns = [_turn("sam", "send it")]
    chat.partner = {"reply": "Which one?", "focus": None, "verbs": "send:2",
                    "actions": [{"verb": "send", "number": 2}]}
    sprint_thread.converse("send it", "d", "t")
    assert world.zoho == []


def test_send_names_one_item_cannot_send_another(chat, world):
    chat.turns = [_turn("sam", "send 2")]
    chat.partner = {"reply": "", "focus": 2, "verbs": "send:2,send:3",
                    "actions": [{"verb": "send", "number": 2},
                                {"verb": "send", "number": 3}]}
    sprint_thread.converse("send 2 please", "d", "t")
    assert [z[1] for z in world.zoho] == ["569440000042922001"]


def test_edit_and_send_in_one_message_only_edits(chat, world, monkeypatch):
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda item, text: ("ok", ""))
    chat.turns = [_turn("sam", "change 3 to mention old forms, then send")]
    new = ("Hi Dalena,\n\nAn update is now live.\n\nSam | Vome team\n"
           "support.vomevolunteer.com")
    chat.partner = {"reply": "Here is the new draft.", "focus": 3,
                    "verbs": "edit:3,send:3",
                    "actions": [{"verb": "edit", "number": 3, "text": new},
                                {"verb": "send", "number": 3}]}
    reply, _, _ = sprint_thread.converse(
        "change 3 to mention old forms, then send", "d", "t")
    assert world.drafts[0][1] == new
    assert world.zoho == []
    assert "just rewritten" in reply


def test_a_partner_draft_that_fails_a_guard_is_held(chat, world):
    chat.turns = [_turn("sam", "rewrite 2 to say Sanjay is on it")]
    chat.partner = {"reply": "Done.", "focus": 2, "verbs": "edit:2",
                    "actions": [{"verb": "edit", "number": 2,
                                 "text": "Hi Dalena,\n\nSanjay is fixing "
                                         "it.\n\nSam | Vome team\n"
                                         "support.vomevolunteer.com"}]}
    reply, _, _ = sprint_thread.converse("rewrite 2 to say Sanjay is on it",
                                         "d", "t")
    assert world.drafts[0][2] == "blocked"
    assert "held by a guard" in reply


def test_route_needs_a_name_in_sams_message(chat, world):
    chat.turns = [_turn("sam", "what should we do with 2?")]
    chat.partner = {"reply": "Give it to Sanjay.", "focus": 2,
                    "verbs": "route:2",
                    "actions": [{"verb": "route", "number": 2,
                                 "who": "sanjay"}]}
    sprint_thread.converse("what should we do with 2?", "d", "t")
    assert chat.routed == []
    chat.turns = [_turn("sam", "ok give 2 to Sanjay")]
    sprint_thread.converse("ok give 2 to Sanjay", "d", "t")
    assert chat.routed == ["sanjay"]


def test_a_failed_partner_says_so(chat, monkeypatch):
    class Failed:
        ok = False
        data = None

        def why(self):
            return "timeout"

    monkeypatch.setattr("vomeos.run_agent", lambda *a, **k: Failed())
    chat.turns = [_turn("sam", "thoughts on 2?")]
    reply, _, _ = sprint_thread.converse("thoughts on 2?", "d", "t")
    assert "could not answer" in reply and "timeout" in reply


def test_conversation_is_remembered_across_messages(db, monkeypatch, world):
    """Real tables: Sam's turns and the agent's focus carry to the next."""
    day = sprint_store.today()
    sprint_store.set_day_thread(day, "C1", "500.1")
    _upsert("t-8001")
    calls = []

    def partner(*a, **k):
        calls.append(k["context"]["conversation"])
        return _Partner({"reply": "Here is 1.", "focus": 1, "verbs": "",
                         "actions": []})

    monkeypatch.setattr("vomeos.run_agent", partner)
    monkeypatch.setattr(sprint_thread, "_ticket_text",
                        lambda item, n="": "thread")
    first = {"user": "U01B9FQNSBU", "text": "tell me about 1",
             "thread_ts": "500.1", "ts": "500.2"}
    second = {"user": "U01B9FQNSBU", "text": "and what did we tell them?",
              "thread_ts": "500.1", "ts": "500.3"}
    assert sprint_thread.handle_command(first)["path"] == "partner"
    sprint_thread.handle_command(second)
    assert "tell me about 1" in calls[1]
    assert "Here is 1." in calls[1]
    turns = sprint_thread._turns("500.1")
    assert sprint_thread._last_focus(turns) == 1


def test_no_database_means_no_header_spam(monkeypatch):
    """Oct 17: four day headers in 13 minutes when the tables were missing."""
    posts = []
    monkeypatch.setattr(sprint_store, "ready", lambda: False)
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda *a, **k: posts.append(a) or "ts")
    assert sprint_thread.ensure_thread() == ""
    assert sprint_thread.ensure_thread() == ""
    assert posts == []
