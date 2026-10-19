"""
test_release_resolutions.py

G1 on the workflow map: when a fix is proven live, each waiting client gets a
resolution draft, and a clean one is sent (on by default since 2026-10-19,
Sam: fixed emails go out on their own). What matters: one draft per client
per fix (never twice), RELEASE_AUTO_SEND=false puts every resolution back in
front of Sam, and never an automatic send on a ticket the card says needs a
person.
"""

import os

import pytest

import release_resolutions as rr
from vomeos import config, store

TEST_DB = os.environ.get("SPRINT_TEST_DATABASE_URL", "")
LINK = "https://support.vomevolunteer.com/support/vomevolunteer/" \
       "ShowHomePage.do#Cases/dv/569440000042922001"


def _child(cid="k1", number="9415", link=LINK):
    return {"id": cid, "parent": "p1", "name": f"[#{number}] Client",
            "custom_fields": [{"id": "4776215b-c725-4d79-8f20-c16f0f0145ac",
                               "value": link}]}


@pytest.fixture
def world(monkeypatch):
    import shadow_triage
    import sprint_store
    import sprint_thread

    class W:
        claimed = set()
        triaged = []
        sent = []
        item = {"number": 3, "category": "clear_bug", "draft_status": "ok",
                "recipients": {"to": "d@x.org"}}

    def claim(child_id, parent_id, ticket_id, number):
        if child_id in W.claimed:
            return False
        W.claimed.add(child_id)
        return True

    monkeypatch.setattr(rr, "_claim", claim)
    monkeypatch.setattr(rr, "_finish", lambda *a: None)
    monkeypatch.setattr(shadow_triage, "_handle",
                        lambda tid, reason, force=False, release=None:
                        W.triaged.append(
                            (tid, reason, force)) or {"status": "ok"})
    monkeypatch.setattr(sprint_store, "by_ticket", lambda tid: dict(W.item))
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda item: W.sent.append(item["number"])
                        or "*3.* sent to d@x.org.")
    monkeypatch.setenv("RELEASE_AUTO_SEND", "false")
    return W


def test_a_live_fix_triages_with_force_and_reports_the_item(world):
    entries = rr.prepare({"id": "p1"}, [_child()])
    assert world.triaged == [("569440000042922001", "release_live", True)]
    assert entries == [{"ticket": "9415", "item": 3, "outcome": "drafted",
                        "detail": "", "to": "d@x.org"}]
    assert world.sent == []


def test_each_client_is_prepared_once(world):
    rr.prepare({"id": "p1"}, [_child()])
    assert rr.prepare({"id": "p1"}, [_child()]) == []
    assert len(world.triaged) == 1


def test_auto_send_is_on_by_default(monkeypatch, world):
    monkeypatch.delenv("RELEASE_AUTO_SEND", raising=False)
    entries = rr.prepare({"id": "p1"}, [_child()])
    assert world.sent == [3] and entries[0]["outcome"] == "sent"


def test_auto_send_can_be_switched_off(world):
    rr.prepare({"id": "p1"}, [_child()])
    assert world.sent == []


def test_auto_send_sends_a_clean_draft(monkeypatch, world):
    monkeypatch.setenv("RELEASE_AUTO_SEND", "true")
    entries = rr.prepare({"id": "p1"}, [_child()])
    assert world.sent == [3] and entries[0]["outcome"] == "sent"


def test_auto_send_never_takes_a_ticket_that_needs_a_person(monkeypatch,
                                                            world):
    monkeypatch.setenv("RELEASE_AUTO_SEND", "true")
    world.item = {**world.item, "category": "needs_human"}
    entries = rr.prepare({"id": "p1"}, [_child()])
    assert world.sent == [] and entries[0]["outcome"] == "drafted"


def test_auto_send_never_sends_a_held_draft(monkeypatch, world):
    monkeypatch.setenv("RELEASE_AUTO_SEND", "true")
    world.item = {**world.item, "draft_status": "blocked",
                  "draft_why": "claims a fix"}
    entries = rr.prepare({"id": "p1"}, [_child()])
    assert world.sent == [] and entries[0]["outcome"] == "held"


def test_a_task_without_a_zoho_link_is_named(world):
    entries = rr.prepare({"id": "p1"}, [_child(link="")])
    assert entries[0]["outcome"] == "no_link" and world.triaged == []


def test_lines_tell_sam_what_to_type():
    text = "\n".join(rr.lines([
        {"ticket": "9415", "item": 3, "outcome": "drafted", "detail": ""},
        {"ticket": "9436", "item": 4, "outcome": "held",
         "detail": "claims a fix"},
        {"ticket": "9500", "item": 5, "outcome": "sent",
         "detail": "*5.* sent to s@x.gov."},
    ]))
    assert "`send 3`" in text and "held by a guard" in text
    assert "sent automatically" in text and chr(0x2014) not in text


@pytest.fixture
def db(monkeypatch):
    if not TEST_DB:
        pytest.skip("SPRINT_TEST_DATABASE_URL not set")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB)
    store.reset()
    store.execute(f"DROP TABLE IF EXISTS {rr.TABLE}")
    store.reset()
    yield
    store.reset()


def test_claim_is_once_per_child_in_postgres(db):
    assert rr._claim("k9", "p9", "T9", "9999") is True
    assert rr._claim("k9", "p9", "T9", "9999") is False
    rr._finish("k9", 7, "drafted", "")
    assert rr._claim("k9", "p9", "T9", "9999") is False
    rows = store.query(f"SELECT item_number, outcome FROM {rr.TABLE}")
    assert rows == [{"item_number": 7, "outcome": "drafted"}]


def test_an_error_is_retried_up_to_the_limit(db):
    """Production, 2026-10-17 14:00: all three failed on missing tables."""
    assert rr._claim("k8", "p8", "T8", "9415") is True
    for _ in range(rr.MAX_ATTEMPTS - 1):
        rr._finish("k8", None, "error", "could not record the item")
        assert rr._claim("k8", "p8", "T8", "9415") is True
    rr._finish("k8", None, "error", "still failing")
    assert rr._claim("k8", "p8", "T8", "9415") is False
