"""
Batches: one message holding everything that needs Sam (sprint_batch.py).

The rendering and command tests need no database. The posting tests run
against a real Postgres when SPRINT_TEST_DATABASE_URL is set (see
test_sprint_flow.py); without it they skip.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

import sprint_batch
import sprint_store
import sprint_thread
from vomeos import config, store

TEST_DB = os.environ.get("SPRINT_TEST_DATABASE_URL", "")
NOW = datetime(2026, 10, 19, 14, 0, 0)

DRAFT = ("Hi Dalena,\n\nAn update is now live that fixes removing an "
         "informational note from a form. Please give it a try and let us "
         "know how it goes.\n\nSam | Vome team\nsupport.vomevolunteer.com")


def _item(n, **over):
    item = {
        "id": 100 + n, "number": n, "ticket_id": f"5694{n}",
        "ticket_number": f"9{n:03d}", "status": "open",
        "category": "clear_bug", "account": "Main Line Health (high, 0 CAD)",
        "important": False, "draft": DRAFT, "draft_status": "ok",
        "draft_why": "", "summary": "s",
        "recipients": {"to": "phanda@mlhs.org", "cc": ["boss@mlhs.org"],
                       "received_at": "2026-10-16T14:00:00.000Z"},
        "card": {"live_question": "Is the note gone?",
                 "situation": "She could not remove a note."},
        "arrived_at": NOW - timedelta(hours=1),
    }
    item.update(over)
    return item


# ---------------------------------------------------------------------------
# What needs Sam, and in what order
# ---------------------------------------------------------------------------

def test_sections():
    assert sprint_batch.section(_item(1)) == sprint_batch.READY
    assert sprint_batch.section(
        _item(2, draft_status="blocked")) == sprint_batch.FIX
    assert sprint_batch.section(
        _item(3, draft="", draft_status="none")) == sprint_batch.DECIDE
    assert sprint_batch.section(_item(4, status="sent")) == ""


def test_a_ready_draft_with_no_recipient_is_not_ready():
    # #9538, the first production preview: "To: ?" under READY TO SEND.
    item = _item(1, recipients={"cc": []})
    assert sprint_batch.section(item) == sprint_batch.FIX
    top, parts = sprint_batch.render(
        7, sprint_batch.select([item], now=NOW), {}, NOW)
    assert "READY TO SEND" not in top
    assert sprint_batch.NO_RECIPIENT in "\n".join(parts)


def test_send_refuses_a_reply_with_no_recipient(monkeypatch):
    import zoho_actions

    calls = []
    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda tid: {"id": "T-LATEST"})
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda *a: calls.append(a))
    item = _item(1, last_thread_id="T-LATEST", recipients={"cc": []})
    out = sprint_thread._send(item)
    assert calls == [] and "no recipient" in out


def test_nothing_owed_is_one_line_not_a_decision():
    owed = _item(11, category="awaiting_client", draft="",
                 draft_status="none")
    picked = sprint_batch.select([owed, _item(1)], now=NOW)
    assert picked[sprint_batch.DECIDE] == []
    assert picked[sprint_batch.OWED_NOTHING] == [owed]
    top, _ = sprint_batch.render(7, picked, {}, NOW)
    assert "*NOTHING OWED (1)*" in top and "`done 11`" in top
    assert "*1 need you*" in top


def test_missing_customer_times_are_looked_up_once(monkeypatch):
    import zoho_actions

    looked, stored = [], []
    monkeypatch.setattr(zoho_actions, "latest_inbound",
                        lambda tid: looked.append(tid)
                        or {"createdTime": "2026-09-01T10:00:00.000Z"})
    monkeypatch.setattr(sprint_store, "set_received_at",
                        lambda i, v: stored.append((i, v)))
    old = _item(1, recipients={"to": "a@b.co"},
                arrived_at=NOW - timedelta(hours=1))
    known = _item(2)
    assert sprint_batch.fill_received([old, known]) == 1
    assert looked == [old["ticket_id"]]
    assert stored == [(old["id"], "2026-09-01T10:00:00.000Z")]
    assert sprint_batch.age(old, NOW).endswith("d")


def test_a_hold_comes_back_when_it_wakes():
    held = _item(5, status="held", wake_at=NOW + timedelta(hours=1))
    assert sprint_batch.section(held, NOW) == sprint_batch.HELD
    assert sprint_batch.section(held, NOW + timedelta(hours=2)) == \
        sprint_batch.READY


def test_important_first_then_longest_waiting():
    items = [
        _item(1, recipients={"to": "a@b.co",
                             "received_at": "2026-10-18T10:00:00Z"}),
        _item(2, recipients={"to": "a@b.co",
                             "received_at": "2026-10-10T10:00:00Z"}),
        _item(3, important=True,
              recipients={"to": "a@b.co",
                          "received_at": "2026-10-18T12:00:00Z"}),
    ]
    picked = sprint_batch.select(items, now=NOW)
    assert [i["number"] for i in picked[sprint_batch.READY]] == [3, 2, 1]


def test_a_batch_holds_at_most_the_batch_size_and_counts_the_rest():
    items = [_item(n) for n in range(1, 31)]
    picked = sprint_batch.select(items, limit=25, now=NOW)
    assert len(picked[sprint_batch.READY]) == 25 and picked["more"] == 5
    after = sprint_batch.select(items, limit=25, now=NOW,
                                exclude={i["id"] for i in
                                         picked[sprint_batch.READY]})
    assert len(after[sprint_batch.READY]) == 5 and after["more"] == 0


def test_age_is_from_the_customers_last_message():
    assert sprint_batch.age(_item(1), NOW) == "3d"
    no_time = _item(2, recipients={}, arrived_at=NOW - timedelta(hours=5))
    assert sprint_batch.age(no_time, NOW) == "5h"


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------

def _render(items, **context):
    picked = sprint_batch.select(items, now=NOW)
    return sprint_batch.render(7, picked, {"untriaged": 0, **context}, NOW)


def test_the_top_message_lists_everything_and_the_thread_has_each_reply():
    items = [_item(1), _item(2, draft_status="blocked",
                             draft_why="client_message: placeholders"),
             _item(3, draft="", draft_status="none", important=True)]
    top, parts = _render(items)
    assert "*3 need you*" in top
    assert "*READY TO SEND (1)*" in top and "*FIX FIRST (1)*" in top
    assert "*NEEDS YOUR DECISION (1)*" in top
    assert "send ready" in top
    # The top message is the index; the replies themselves are in the thread
    # in full, exactly as the client gets them (Sam, 2026-10-17).
    assert DRAFT not in top
    thread = "\n".join(parts)
    assert thread.count(DRAFT) == 2
    assert "To: phanda@mlhs.org · cc: boss@mlhs.org" in thread
    assert "Held: client_message: placeholders" in thread
    assert "*Waiting on:* Is the note gone?" in thread


def test_no_em_dashes_anywhere():
    top, parts = _render([_item(1), _item(2, draft="", draft_status="none")])
    for text in [top] + parts:
        assert "—" not in text and "–" not in text


def test_thread_parts_stay_readable():
    items = [_item(n) for n in range(1, 21)]
    _, parts = _render(items)
    assert len(parts) > 1
    assert all(len(p) <= sprint_batch.PART_CHARS for p in parts)
    assert "\n".join(parts).count(DRAFT) == 20


def test_nothing_needs_sam():
    top, parts = _render([_item(1, status="sent")])
    assert "Nothing needs you right now." in top and parts == []


def test_an_unreadable_queue_is_never_reported_as_clear():
    picked = sprint_batch.select([_item(1)], now=NOW)
    top, _ = sprint_batch.render(
        7, picked, {"untriaged": None, "queue_error": "HTTP 500"}, NOW)
    assert "could not be read" in top and "0 open" not in top


def test_resolutions_are_labelled():
    top, _ = _render([_item(1)], resolutions={"56941"})
    assert "resolution (fix is live)" in top


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    items = {101: _item(1), 102: _item(2), 103: _item(3)}
    out = []
    monkeypatch.setattr(sprint_store, "get_item", lambda i: items.get(i))
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda item: out.append(item["number"])
                        or f"*{item['number']}.* sent")
    return items, out


def _batch(*numbers):
    shown = {str(100 + n): sprint_batch.draft_hash(DRAFT) for n in numbers}
    return {"id": 7, "ts": "1.1", "shown": shown, "ready": dict(shown)}


def test_send_ready_sends_what_the_batch_showed(sent):
    _, out = sent
    reply = sprint_thread.send_ready(_batch(1, 2), set())
    assert out == [1, 2] and "*1.* sent" in reply


def test_send_ready_skips_a_reply_that_changed_after_the_batch(sent):
    items, out = sent
    items[102]["draft"] = DRAFT + " Edited."
    reply = sprint_thread.send_ready(_batch(1, 2), set())
    assert out == [1]
    assert "changed since this batch" in reply


def test_send_ready_except(sent):
    _, out = sent
    reply = sprint_thread.send_ready(_batch(1, 2, 3), {2})
    assert out == [1, 3] and "left out" in reply


@pytest.mark.parametrize("text, expect", [
    ("send ready", set()),
    ("Send all ready", set()),
    ("send ready except 4 and 7", {4, 7}),
    ("send the ready ones but not 2", {2}),
])
def test_send_ready_wording(monkeypatch, text, expect):
    seen = []
    monkeypatch.setattr(sprint_thread, "send_ready",
                        lambda batch, ex: seen.append(ex) or "ok")
    assert sprint_thread._batch_command(text, "1.1", _batch(1), "2.2") == "ok"
    assert seen == [expect]


def test_send_ready_needs_a_batch_thread():
    reply = sprint_thread._batch_command("send ready", "9.9", {}, "2.2")
    assert "batch's thread" in reply


@pytest.mark.parametrize("text", [
    "don't send ready yet", "send ready? not sure", "can you send 4",
])
def test_anything_else_is_not_send_ready(text):
    assert sprint_thread._SEND_READY.match(text) is None


def test_next_skips_what_this_batch_showed(monkeypatch):
    calls = []
    monkeypatch.setattr(sprint_batch, "run_batch",
                        lambda kind, exclude=None, reply_thread="":
                        calls.append((kind, exclude)) or
                        {"status": "ok", "batch": 8, "shown": 3, "more": 0})
    reply = sprint_thread._batch_command("next", "1.1", _batch(1, 2), "2.2")
    assert calls == [("next", {101, 102})]
    assert "Posted batch 8" in reply
    sprint_thread._batch_command("catch me up", "1.1", _batch(1), "2.3")
    assert calls[-1] == ("catch_up", None)


def test_hold_till_a_day_parses_and_lands_at_8am():
    commands = sprint_thread.parse("hold 4 till fri")
    assert commands == [{"verb": "hold", "numbers": [4], "arg": "fri"}]

    def local(wake):
        return wake.replace(tzinfo=timezone.utc).astimezone(
            sprint_store.ZONE)

    friday = local(sprint_thread._wake_at("fri"))
    assert friday.weekday() == 4 and friday.hour == 8
    assert sprint_thread._wake_at("someday") is None
    # No day named: the next weekday morning, never a weekend.
    assert local(sprint_thread._wake_at("")).weekday() < 5


def test_cards_and_release_posts_are_off_by_default(monkeypatch):
    monkeypatch.delenv("SPRINT_PER_TICKET_CARDS", raising=False)
    assert sprint_thread.post_card(_item(1)) == ""
    import release_watcher

    assert release_watcher._post("Fix live: something") is True


# ---------------------------------------------------------------------------
# Posting, against a real Postgres
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    if not TEST_DB:
        pytest.skip("SPRINT_TEST_DATABASE_URL not set")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB)
    store.reset()
    for table in (sprint_store.ITEMS, sprint_store.DAYS,
                  sprint_store.BATCHES, sprint_batch.LEASES):
        store.execute(f"DROP TABLE IF EXISTS {table}")
    store.reset()
    assert sprint_store.ready() and sprint_store.batches_ready()
    posts = []

    def post(text, thread_ts="", channel="", broadcast=False):
        posts.append((thread_ts, text))
        return "" if thread_ts else f"{len(posts)}.0"

    monkeypatch.setattr(sprint_thread, "_post", post)
    monkeypatch.setattr(sprint_batch, "_untriaged", lambda: (3, ""))
    import zoho_actions

    monkeypatch.setattr(zoho_actions, "latest_inbound", lambda tid: {})
    superseded = []
    monkeypatch.setattr(sprint_batch, "_supersede",
                        lambda prev, ts, n: superseded.append(prev["id"]))
    yield posts, superseded
    store.reset()


def _upsert(ticket_id, **over):
    fields = dict(
        ticket_id=ticket_id, ticket_number=ticket_id[-4:],
        category="clear_bug", account="Acme", subject="s", summary="q",
        important=False, card={"live_question": "q?"}, draft=DRAFT,
        draft_status="ok", draft_why="",
        recipients={"to": "a@b.co", "cc": []}, last_thread_id="T1",
    )
    fields.update(over)
    return sprint_store.upsert_ticket(**fields)


def test_a_batch_posts_once_and_records_what_it_showed(db):
    posts, superseded = db
    a = _upsert("t-8001")
    _upsert("t-8002", draft="", draft_status="none")
    result = sprint_batch.run_batch("catch_up")
    assert result["status"] == "ok" and result["shown"] == 2
    top_level = [t for ts, t in posts if not ts]
    assert len(top_level) == 1 and "*2 need you*" in top_level[0]
    assert "3 open ticket(s) not triaged yet" in top_level[0]
    batch = sprint_store.latest_batch()
    assert batch["ready"] == {str(a["id"]): sprint_batch.draft_hash(DRAFT)}
    assert sprint_store.batch_for_thread(batch["ts"])["id"] == batch["id"]
    sprint_batch.run_batch("catch_up")
    assert superseded == [batch["id"]]


def test_weekend_batch_only_when_something_needs_sam(db):
    posts, _ = db
    assert sprint_batch.run_batch("weekend")["status"] == "skipped"
    assert posts == []


def test_an_unchanged_weekday_batch_says_so_in_one_line(db):
    posts, _ = db
    _upsert("t-8101")
    sprint_batch.run_batch("scheduled")
    count = len(posts)
    result = sprint_batch.run_batch("scheduled")
    assert result["status"] == "unchanged"
    assert len(posts) == count + 1
    thread, text = posts[-1]
    assert thread and text.startswith("No change since this batch")


def test_a_command_in_a_batch_thread_is_heard(db, monkeypatch):
    posts, _ = db
    item = _upsert("t-8201")
    sprint_batch.run_batch("catch_up")
    batch = sprint_store.latest_batch()
    runs = []
    monkeypatch.setattr(sprint_thread, "_execute",
                        lambda c, i: runs.append(i["number"]) or "ok")
    out = sprint_thread.handle_command({
        "user": "U01B9FQNSBU", "text": f"done {item['number']}",
        "thread_ts": batch["ts"], "ts": "77.1"})
    assert out["status"] == "ok" and runs == [item["number"]]


def test_a_lease_is_held_once(db):
    assert sprint_batch._lease("triage:1", 20) is True
    assert sprint_batch._lease("triage:1", 20) is False
    assert sprint_batch._lease("triage:2", 20) is True
