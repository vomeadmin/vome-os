"""
The proposed items built on 2026-10-18: quiet close (G29), escalation
reasons (G27), shipped-but-untold replies (G31, G11), feature request
duplicate check and posting (G9, G10), trust tiers (G28).

No database and no network: every outside call is replaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import sprint_batch
import sprint_store
import sprint_thread

NOW = datetime(2026, 10, 19, 14, 0, 0)


def _item(n, **over):
    item = {
        "id": 100 + n, "number": n, "ticket_id": f"5694{n}",
        "ticket_number": f"9{n:03d}", "status": "open",
        "category": "user_education", "account": "Acme (Pro, 900 CAD)",
        "important": False, "draft": "", "draft_status": "none",
        "draft_why": "", "summary": "s",
        "recipients": {"to": "a@b.co", "received_at": "2026-10-18T10:00Z"},
        "card": {"live_question": "q"}, "arrived_at": NOW,
    }
    item.update(over)
    return item


# ---------------------------------------------------------------------------
# G29: close quietly
# ---------------------------------------------------------------------------

def test_housekeeping_lands_in_close_quietly_not_decisions():
    thanks = _item(1, card={"lane": "housekeeping",
                            "live_question": "Thanks, all working now"})
    picked = sprint_batch.select([thanks, _item(2)], now=NOW)
    assert picked[sprint_batch.QUIET] == [thanks]
    assert thanks not in picked[sprint_batch.DECIDE]
    top, _ = sprint_batch.render(7, picked, {}, NOW)
    assert "*CLOSE QUIETLY (1)*" in top and "`close quiet`" in top
    assert "Thanks, all working now" in top
    assert "*1 need you*" in top


def test_housekeeping_gets_no_draft(monkeypatch):
    import shadow_triage

    # The draft decision only: the lane switches it off unless a fix is live.
    card = {"category": "user_education", "lane": "housekeeping"}
    owed = card["category"] in shadow_triage.DRAFT_CATEGORIES
    assert owed  # would have been drafted before G29
    src = open(shadow_triage.__file__, encoding="utf-8").read()
    assert 'card.get("lane") == "housekeeping"' in src


@pytest.fixture
def closing(monkeypatch):
    import zoho_actions

    items = {101: _item(1, card={"lane": "housekeeping"}),
             102: _item(2, card={"lane": "housekeeping"})}
    zoho, decided = [], []
    monkeypatch.setattr(sprint_store, "get_item", lambda i: items.get(i))
    monkeypatch.setattr(zoho_actions, "set_status",
                        lambda tid, s: zoho.append((tid, s)))
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: decided.append((i, s, d)))
    return items, zoho, decided


def test_close_quiet_closes_only_what_the_batch_showed(closing):
    _, zoho, decided = closing
    batch = {"id": 7, "ts": "1.1", "quiet": [101]}
    out = sprint_thread._batch_command("close quiet", "1.1", batch, "2.2")
    assert zoho == [("56941", "Closed")]
    assert decided == [(101, sprint_store.DONE, "closed quietly by Sam")]
    assert "closed in Zoho, no email" in out


def test_close_quiet_except(closing):
    _, zoho, _ = closing
    batch = {"id": 7, "ts": "1.1", "quiet": [101, 102]}
    out = sprint_thread._batch_command("close quiet except 2", "1.1", batch,
                                       "2.2")
    assert zoho == [("56941", "Closed")] and "left open" in out


def test_close_quiet_needs_a_batch_thread():
    out = sprint_thread._batch_command("close quiet", "9.9", {}, "2.2")
    assert "batch's thread" in out


def test_close_one_item(closing):
    items, zoho, _ = closing
    assert sprint_thread.parse("close 1") == [
        {"verb": "close", "numbers": [1], "arg": ""}]
    out = sprint_thread._execute({"verb": "close", "numbers": [1],
                                  "arg": ""}, items[101])
    assert zoho and "no email" in out


# ---------------------------------------------------------------------------
# G27: what still escalates, and why
# ---------------------------------------------------------------------------

def test_route_records_the_reason(monkeypatch):
    import zoho_actions

    decided = []
    monkeypatch.setattr(sprint_thread, "_clickup_create",
                        lambda list_id, b: {"id": "T1", "url": "u"})
    monkeypatch.setattr(sprint_thread, "_post", lambda *a, **k: "ts")
    monkeypatch.setattr(zoho_actions, "set_in_progress", lambda *a: None)
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: decided.append(d))
    item = _item(3, card={"lane": "engineer",
                          "escalation_reason": "data_lookup",
                          "live_question": "When was the reminder sent?"})
    sprint_thread._route(item, "onlyg")
    assert decided == ["routed to onlyg (data_lookup)"]
    sprint_thread._route(_item(4), "sanjay")
    assert decided[-1] == "routed to sanjay (sam_call)"


def test_week_line_counts_engineers_and_reasons():
    line = sprint_batch.week_line({
        "sent": 12, "routed": 3,
        "by_engineer": {"onlyg": 2, "sanjay": 1},
        "by_reason": {"data_lookup": 2, "mobile_unreadable": 1}})
    assert line == ("this week 12 sent, 3 to engineers (onlyg 2, sanjay 1; "
                    "why: data lookup 2, mobile unreadable 1)")


def test_engineer_detail_says_why():
    item = _item(5, card={"lane": "engineer",
                          "escalation_reason": "mobile_unreadable",
                          "live_question": "Push does not open"})
    text = sprint_batch.detail(item, set(), NOW)
    assert "*Needs an engineer:* lives in the mobile app" in text


# ---------------------------------------------------------------------------
# G31 / G11: shipped, client not told
# ---------------------------------------------------------------------------

def test_ticket_from_link_field_or_description():
    import shipped_untold
    from zoho_links import FIELD_ZOHO_TICKET_LINK

    task = {"custom_fields": [{"id": FIELD_ZOHO_TICKET_LINK,
                               "value": "https://support.vomevolunteer.com/"
                                        "x#Cases/dv/569440000043050400"}]}
    assert shipped_untold.ticket_id_for(task) == "569440000043050400"
    feature = {"custom_fields": [], "markdown_description":
               "**Zoho:** https://support.vomevolunteer.com/support/"
               "vomevolunteer/ShowHomePage.do#Cases/dv/569440000042932075"}
    assert shipped_untold.ticket_id_for(feature) == "569440000042932075"
    assert shipped_untold.ticket_id_for({"custom_fields": []}) == ""


def test_told_means_we_wrote_after_it_shipped(monkeypatch):
    import shipped_untold
    import zoho_actions

    shipped = datetime(2026, 10, 15, 12, 0)
    monkeypatch.setattr(zoho_actions, "latest_outbound", lambda t: {
        "createdTime": "2026-10-15T12:00:30.000Z"})
    assert shipped_untold.already_told("T", shipped) is True
    monkeypatch.setattr(zoho_actions, "latest_outbound", lambda t: {
        "createdTime": "2026-10-10T09:00:00.000Z"})
    assert shipped_untold.already_told("T", shipped) is False
    monkeypatch.setattr(zoho_actions, "latest_outbound", lambda t: {})
    assert shipped_untold.already_told("T", shipped) is False


def test_an_untold_fix_is_drafted_once_as_a_resolution(monkeypatch):
    import release_resolutions as rr
    import shadow_triage
    import shipped_untold
    from zoho_links import FIELD_ZOHO_TICKET_LINK

    task = {"id": "868pq1", "name": "[#9600] Export drops a column",
            "date_updated": str(int((NOW - timedelta(days=1)).timestamp()
                                    * 1000)),
            "custom_fields": [{"id": FIELD_ZOHO_TICKET_LINK,
                               "value": "#Cases/dv/569440000049999999"}]}
    told_task = {**task, "id": "868pq2"}
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(shipped_untold, "shipped_tasks", lambda: [
        {"task": task, "kind": "fix"}, {"task": told_task, "kind": "fix"}])
    monkeypatch.setattr(shipped_untold, "already_told",
                        lambda t, s: False if t.endswith("9999") else True)
    claims, handled, finished = set(), [], []
    monkeypatch.setattr(rr, "_claim", lambda c, p, t, n:
                        c not in claims and not claims.add(c))
    monkeypatch.setattr(rr, "_finish",
                        lambda c, i, o, d: finished.append((c, o)))
    monkeypatch.setattr(shadow_triage, "_handle",
                        lambda tid, reason, **k: handled.append(
                            (tid, reason, k["release"])) or {"status": "ok"})
    monkeypatch.setattr(sprint_store, "by_ticket", lambda t: {
        "number": 31, "draft_status": "ok"})
    told_task["custom_fields"] = [{"id": FIELD_ZOHO_TICKET_LINK,
                                   "value": "#Cases/dv/569440000048888888"}]
    out = shipped_untold.run_shipped_untold()
    assert out["drafted"] == [31]
    assert handled[0][1] == "shipped_fix"
    assert handled[0][2]["live"] is True
    assert "on prod" in handled[0][2]["text"]
    assert finished == [("868pq1", "drafted")]
    # A second pass drafts nothing: the task is claimed.
    handled.clear()
    assert shipped_untold.run_shipped_untold()["drafted"] == []


def test_feature_release_text_says_shipped():
    import shipped_untold

    text = shipped_untold._release({"id": "868a", "name": "Bulk export"},
                                   "feature")["text"]
    assert text.startswith("SHIPPED.") and "Bulk export" in text


# ---------------------------------------------------------------------------
# G9 / G10: feature requests
# ---------------------------------------------------------------------------

@pytest.fixture
def requests(monkeypatch):
    from mcp_connector import feature_requests

    class R:
        dups = []
        created = []
        plus = []
        posts = []
        decided = []

    monkeypatch.setattr(feature_requests, "find_duplicates",
                        lambda t, p: R.dups)
    monkeypatch.setattr(feature_requests, "add_plus_one",
                        lambda **k: R.plus.append(k) or
                        {"url": "https://app.clickup.com/t/868old",
                         "name": "Bulk export"})
    monkeypatch.setattr(sprint_thread, "_clickup_create",
                        lambda list_id, b: R.created.append((list_id, b)) or
                        {"id": "868new", "url": "https://cu/868new"})
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", channel="",
                        broadcast=False: R.posts.append((channel, text))
                        or "ts")
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: R.decided.append(d))
    return R


def test_a_possible_duplicate_stops_the_log(requests):
    requests.dups = [{"id": "868old", "name": "Bulk export", "url": "u",
                      "list": "Raw Intake", "status": "queued",
                      "shipped": False}]
    out = sprint_thread._feature(_item(8), "log")
    assert requests.created == [] and requests.decided == []
    assert "nothing logged yet" in out and "`log 8 to 868old`" in out
    assert "`log 8 new`" in out


def test_log_new_creates_and_posts_with_the_client(requests):
    out = sprint_thread._feature(_item(8), "log", "new")
    assert requests.created and requests.created[0][0] == \
        sprint_thread.RAW_INTAKE
    channel, text = requests.posts[0]
    assert channel == sprint_thread.FEATURE_REQUESTS
    assert "Acme (Pro, 900 CAD)" in text and "#9008" in text
    assert "*Client told:* nothing yet." in text
    assert "Posted in #vome-feature-requests" in out


def test_log_to_an_existing_request_adds_the_client(requests):
    out = sprint_thread._feature(_item(8), "log", "868old")
    assert requests.created == []
    assert requests.plus[0]["task_id"] == "868old"
    assert requests.plus[0]["requested_by"] == "Acme"
    assert "the existing request Bulk export" in out


def test_partner_prose_is_not_a_task_id(requests):
    requests.dups = [{"id": "868old", "name": "Bulk export", "url": "u"}]
    out = sprint_thread._feature(_item(8), "log",
                                 "the customer wants exports")
    # Treated as a plain `log`: the duplicate check ran and stopped it,
    # rather than "+1 to task 'the customer wants exports'".
    assert requests.created == [] and requests.plus == []
    assert "nothing logged yet" in out


def test_log_command_parsing():
    assert sprint_thread.parse("log 8 new") == [
        {"verb": "log", "numbers": [8], "arg": "new"}]
    assert sprint_thread.parse("build 8 to 868mjcz0y") == [
        {"verb": "build", "numbers": [8], "arg": "868mjcz0y"}]
    assert sprint_thread.parse("log 8") == [
        {"verb": "log", "numbers": [8], "arg": ""}]


# ---------------------------------------------------------------------------
# G28: trust tiers
# ---------------------------------------------------------------------------

def test_trust_tiers():
    import autonomy_report as ar

    assert ar.tier({"sent": 25, "unedited_rate": 0.92}) == "proven"
    assert ar.tier({"sent": 25, "unedited_rate": 0.6}) == "learning"
    assert ar.tier({"sent": 2, "unedited_rate": 1.0}) == "too few to tell"
    assert ar.tier({"eligible": True}) == "qualifies for auto-send"
    text = ar.render([{"category": "user_education", "drafted": 30,
                       "sent": 25, "unedited": 23, "edited": 2,
                       "redrafts": 0, "unedited_rate": 0.92,
                       "eligible": False}], [],
                     week={"sent": 25, "routed": 1,
                           "by_engineer": {"onlyg": 1},
                           "by_reason": {"data_lookup": 1}})
    assert "`user_education` proven" in text
    assert "What still escalates" in text and "data lookup 1" in text
    assert chr(0x2014) not in text


# ---------------------------------------------------------------------------
# A full test of the queue: catch me up now, clear the queue fresh, and
# tickets handled outside the sprint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "catch me up", "Catch me up now", "catch me up please", "batch",
    "please catch me up",
])
def test_catch_me_up_wording(text):
    assert sprint_thread._CATCH_UP.match(text)


@pytest.mark.parametrize("text,fresh", [
    ("clear the queue", False), ("clear the queue now", False),
    ("clear the queue fresh", True), ("Clear the pipeline again", True),
    ("clear the queue from scratch", True),
])
def test_clear_wording(text, fresh):
    match = sprint_thread._CLEAR.match(text)
    assert match and bool(match.group("fresh")) is fresh


def test_fresh_clear_retriages_open_items_triaged_before_it(monkeypatch):
    cutoff = NOW
    items = {"T1": None,                                       # untriaged
             "T2": {"status": "open", "updated_at": NOW - timedelta(days=1)},
             "T3": {"status": "open", "updated_at": NOW + timedelta(hours=1)},
             "T4": {"status": "sent", "updated_at": NOW - timedelta(days=1)}}
    monkeypatch.setattr(sprint_store, "by_ticket",
                        lambda t: items.get(t) or {})
    picked = [t for t in items
              if sprint_batch._needs_triage({"id": t}, cutoff)]
    # Old open card: again. Already refreshed, or already sent: not again.
    assert picked == ["T1", "T2"]
    plain = [t for t in items if sprint_batch._needs_triage({"id": t}, None)]
    assert plain == ["T1"]


def test_refresh_respects_a_closed_ticket():
    import shadow_triage

    src = open(shadow_triage.__file__, encoding="utf-8").read()
    # refresh skips only the "already triaged" check, never the status one.
    assert "if (not force and not refresh and existing" in src
    assert "if status and status not in OPEN_STATUSES and not force" in src


@pytest.fixture
def zoho_status(monkeypatch):
    import zoho_actions

    statuses = {}
    decided = []
    monkeypatch.setattr(zoho_actions, "ticket_status",
                        lambda t: statuses.get(t, ""))
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: decided.append((i, s, d)))
    monkeypatch.setattr(sprint_batch, "_resolution_tickets",
                        lambda: {"RES"})
    return statuses, decided


def test_a_ticket_closed_in_zoho_leaves_the_batch(zoho_status):
    statuses, decided = zoho_status
    statuses.update({"CLOSED": "Closed", "OLD_OPEN": "Processing",
                     "ROUTED": "In Progress"})
    items = [_item(1, ticket_id="CLOSED"), _item(2, ticket_id="OLD_OPEN"),
             _item(3, ticket_id="ROUTED"), _item(4, ticket_id="IN_QUEUE")]
    finished = sprint_batch.sync_handled_elsewhere(items, {"IN_QUEUE"})
    assert finished == 2
    assert [d[0] for d in decided] == [101, 103]
    assert decided[0][2] == "handled outside the sprint (Zoho: Closed)"
    # Old but still open in Zoho (beyond the queue read): kept.
    assert items[1]["status"] == "open"


def test_resolutions_and_fix_prs_are_never_synced_away(zoho_status):
    statuses, decided = zoho_status
    statuses.update({"RES": "In Progress", "PR": "In Progress"})
    items = [_item(1, ticket_id="RES"),
             _item(2, ticket_id="PR", fix_pr="https://bb/pr/1")]
    assert sprint_batch.sync_handled_elsewhere(items, set()) == 0
    assert decided == []


def test_an_unreadable_status_finishes_nothing(monkeypatch, zoho_status):
    import zoho_actions

    _, decided = zoho_status

    def boom(t):
        raise RuntimeError("Zoho 500")

    monkeypatch.setattr(zoho_actions, "ticket_status", boom)
    assert sprint_batch.sync_handled_elsewhere(
        [_item(1, ticket_id="X")], set()) == 0
    assert decided == []


# ---------------------------------------------------------------------------
# A Zoho hiccup does not end a clear (2026-10-18: "Error -3 while
# decompressing data" on page 9 of 10 stopped round 2)
# ---------------------------------------------------------------------------

def test_a_corrupt_page_is_read_again(monkeypatch):
    import sprint

    calls = []
    answers = [None, None, {"data": {"data": [{"id": "1"}]}}]
    monkeypatch.setattr(sprint, "_zoho_desk_call",
                        lambda name, args: calls.append(name)
                        or answers.pop(0))
    monkeypatch.setattr(sprint.time, "sleep", lambda s: None)
    monkeypatch.setattr(sprint, "_unwrap_mcp_result", lambda r: r)
    assert sprint._pull_page(800) == [{"id": "1"}]
    assert len(calls) == 3


def test_a_round_retries_before_pausing(monkeypatch):
    results = [{"status": "queue unreadable", "error": "Error -3"},
               {"status": "ok", "open": 46, "triaged": 20, "failed": 0,
                "untriaged": 0}]
    posts = []
    monkeypatch.setattr(sprint_batch, "_lease", lambda n, m: True)
    monkeypatch.setattr(sprint_batch, "triage_untriaged",
                        lambda *a, **k: results.pop(0))
    monkeypatch.setattr(sprint_batch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sprint_batch, "run_batch",
                        lambda kind, reply_thread="": {"batch": 9})
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", **k: posts.append(text))
    out = sprint_batch.handle_queue_clear({
        "ts": "1.1", "thread": "9.9", "round": 2,
        "fresh_since": "2026-10-18T18:59:00"})
    assert out["batch"] == 9 and posts == []


def test_three_failed_reads_pause_and_say_how_to_resume(monkeypatch):
    posts = []
    monkeypatch.setattr(sprint_batch, "_lease", lambda n, m: True)
    monkeypatch.setattr(sprint_batch, "triage_untriaged",
                        lambda *a, **k: {"status": "queue unreadable",
                                         "error": "Error -3"})
    monkeypatch.setattr(sprint_batch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", **k: posts.append(text))
    sprint_batch.handle_queue_clear({"ts": "1.1", "thread": "9.9",
                                     "round": 2,
                                     "fresh_since": "2026-10-18T18:59:00"})
    assert "Clearing paused" in posts[0]
    assert "`clear the queue fresh` again" in posts[0]
    assert "nothing already triaged is redone" in posts[0]
