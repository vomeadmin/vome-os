"""
test_data_check.py

Sam, 2026-11-05 (#9760): a ticket about specific people or records is
settled by the records, not by a plausible reading of the code. What
matters here: the card's data_check is read safely whatever shape it comes
in, a `required` check is never routed to an engineer and never gets an
agent PR, a `confirm` check still fixes but holds the "fixed" email, the
query Sam pastes can never write, the sprint shows the check with its
query, and `data N: <output>` keeps the paste and triages again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import data_check
import deep_triage
import resolution_scope
import sprint_batch
import sprint_store
import sprint_thread
import ticket_fix
import ticket_routing

NOW = datetime(2026, 11, 5, 14, 0, 0)

QUERY = "\n".join([
    "from opportunity_app.models import UserFormsAndWaivers",
    "rows = UserFormsAndWaivers.objects.filter(user__last_name__icontains="
    "\"stokes\")",
    "print(\"OPEN\", [(r.id, r.document_file.name) for r in rows])",
    "UserFormsAndWaivers.objects.filter(id=1).delete()",
    "import os",
    "from django.db import connection",
    "connection.cursor().execute(\"DELETE FROM x\")",
])


def _card(needed="required", **over):
    card = {
        "category": "clear_bug", "lane": "answer", "confidence": "medium",
        "live_question": "Why do three volunteers still show the request?",
        "situation": "Admin says she uploaded files for three people.",
        "code_check": {"verdict": "bug", "confidence": "high",
                       "fix_risk": "low", "where": "core:a/b.py:10",
                       "fix": "resolve the request on replace",
                       "finding": "replace does not resolve"},
        "data_check": {
            "needed": needed,
            "why": "only the records show whether the uploads saved",
            "hypothesis": "a file exists and the request is still open",
            "questions": ["Does Sirlilar Stokes have a saved Flu Shot "
                          "Record upload?"],
            "query": QUERY,
            "settles": "file and open request: close it. No file: "
                       "the upload never saved.",
        },
    }
    card.update(over)
    return card


def _item(n=12, card=None, **over):
    item = {
        "id": 500 + n, "number": n, "ticket_id": f"5694{n}",
        "ticket_number": f"97{n:02d}", "status": "open",
        "category": "clear_bug", "account": "UMMS (very-high, 39000 CAD)",
        "important": True, "draft": "Hi Laurel,\n\nWe are checking.",
        "draft_status": "ok", "draft_why": "", "summary": "s",
        "recipients": {"to": "laurel@umm.edu", "cc": [],
                       "received_at": "2026-11-05T13:00:00.000Z"},
        "card": card if card is not None else _card(),
        "data_results": [],
        "arrived_at": NOW - timedelta(hours=1),
    }
    item.update(over)
    return item


# ---------------------------------------------------------------------------
# Reading the card
# ---------------------------------------------------------------------------

def test_a_card_without_a_check_or_with_junk_reads_as_none():
    assert data_check.level({}) == "none"
    assert data_check.level(None) == "none"
    assert data_check.level({"data_check": "yes please"}) == "none"
    assert data_check.level({"data_check": {"needed": "maybe"}}) == "none"
    assert data_check.level({"data_check": {"needed": " Required "}}) \
        == "required"


def test_questions_written_as_one_string_become_a_list():
    card = {"data_check": {"needed": "confirm", "questions": "a\n\nb"}}
    assert data_check.of(card)["questions"] == ["a", "b"]


def test_pending_reads_an_item_or_a_card():
    assert data_check.pending(_item())
    assert data_check.pending(_card("confirm"))
    assert not data_check.pending(_card("none"))


# ---------------------------------------------------------------------------
# The query Sam pastes can never write
# ---------------------------------------------------------------------------

def test_the_query_drops_every_line_that_could_write():
    query, dropped = data_check.safe_query(QUERY)
    assert "delete" not in query.lower()
    assert "import os" not in query
    assert "connection" not in query
    assert len(dropped) == 4


def test_the_query_reads_the_replica_and_keeps_labelled_prints():
    query, _ = data_check.safe_query(QUERY)
    assert ('UserFormsAndWaivers.objects.using("readonly").filter(' in
            query)
    assert 'print("OPEN"' in query
    assert "from opportunity_app.models import" in query
    again, _ = data_check.safe_query(query)
    assert again.count('using("readonly")') == 1


def test_the_paste_block_opens_with_the_shell_setup():
    block = data_check.paste_block(_card())
    assert block.startswith("sudo su - ubuntu")
    assert "python manage.py shell" in block.split("\n\n")[0]


def test_slack_markup_is_taken_out_of_a_paste():
    raw = ("```OPEN [(1, '<mailto:a@b.com|a@b.com>')] &amp; "
           "<https://x.io/f|f>```")
    assert data_check.clean_paste(raw) == "OPEN [(1, 'a@b.com')] & " \
                                          "https://x.io/f"


# ---------------------------------------------------------------------------
# What a required check stops, and what a confirm check lets through
# ---------------------------------------------------------------------------

def test_a_required_check_is_never_routed_to_an_engineer():
    card = _card(lane="engineer", escalation_reason="low_confidence")
    assert ticket_routing.should_route(card, {}) == (
        False, "data check first: Sam runs it")


def test_an_old_data_lookup_card_is_not_routed_either():
    card = {"lane": "engineer", "escalation_reason": "data_lookup",
            "code_check": {"verdict": "unclear"}}
    assert ticket_routing.should_route(card, {})[0] is False


def test_a_confirm_check_does_not_change_routing():
    card = _card("confirm", lane="engineer",
                 escalation_reason="schema_or_migration")
    assert ticket_routing.should_route(card, {}) == (
        True, "schema_or_migration")


def test_a_required_check_gets_no_agent_pr_until_sam_asks():
    assert ticket_fix.should_attempt(_card())[0] is False
    assert ticket_fix.should_attempt(_card(), sam_approved=True)[0] is True


def test_a_confirm_check_still_fixes_the_bug():
    assert ticket_fix.should_attempt(_card("confirm")) == (
        True, "small bug the code confirms")


def test_the_deep_pass_writes_the_query():
    deep_triage_card = _card("confirm", confidence="high")
    assert deep_triage.reason_for(deep_triage_card) == "data check confirm"
    assert deep_triage.reason_for(_card("none", confidence="high",
                                        lane="agent_fix")) == ""


def test_new_results_always_get_the_deep_pass():
    card = _card("none", confidence="high", lane="housekeeping")
    assert deep_triage.reason_for(card, new_data=True) == \
        "data check results"


def test_the_fixed_email_waits_for_a_data_check(monkeypatch):
    monkeypatch.setattr(sprint_store, "by_ticket",
                        lambda tid: _item(card=_card("confirm")))
    held = resolution_scope.data_check_hold("569412")
    assert "data check is not done" in held and "`data 12:" in held
    out = resolution_scope.check("569412", "9712", [{"id": "t1"}])
    assert out["complete"] is False and out["reason"] == held


def test_a_settled_check_does_not_hold_the_email(monkeypatch):
    monkeypatch.setattr(sprint_store, "by_ticket",
                        lambda tid: _item(card=_card("none")))
    assert resolution_scope.data_check_hold("569412") == ""
    monkeypatch.setattr(sprint_store, "by_ticket", lambda tid: {})
    assert resolution_scope.data_check_hold("569412") == ""


# ---------------------------------------------------------------------------
# The sprint shows it, with the query
# ---------------------------------------------------------------------------

def test_a_required_check_has_its_own_section_and_needs_sam():
    item = _item()
    assert sprint_batch.section(item, NOW) == sprint_batch.DATA
    picked = sprint_batch.select([item], now=NOW)
    assert picked[sprint_batch.DATA] == [item]
    assert picked[sprint_batch.READY] == []
    top, parts = sprint_batch.render(7, picked, {}, NOW)
    assert "1 need you" in top
    assert "*DATA CHECK FIRST (1)*" in top
    assert "look up: Does Sirlilar Stokes" in top
    thread = "\n".join(parts)
    assert "sudo su - ubuntu" in thread
    assert 'objects.using("readonly")' in thread
    assert ".delete()" not in thread
    assert "Left out 4 line(s)" in thread
    assert "`data 12: <paste the output>`" in thread
    assert "`send 12` sends the holding reply · `route 12" in thread


def test_a_confirm_check_is_listed_even_while_the_agent_fixes_it():
    item = _item(card=_card("confirm"), status="fixing",
                 sent_at=NOW - timedelta(hours=2),
                 fix_pr="https://bitbucket.org/x/pull-requests/1")
    assert sprint_batch.section(item, NOW) == sprint_batch.IN_FLIGHT
    picked = sprint_batch.select([item], now=NOW)
    assert picked["confirm"] == [item]
    top, parts = sprint_batch.render(7, picked, {}, NOW)
    assert "confirm before the fixed email · fix PR open" in top
    assert "Nothing needs you right now." in top
    assert "confirm before the fixed email: the code found" in \
        "\n".join(parts)


def test_a_ready_reply_shows_its_confirm_check_above_the_draft():
    item = _item(card=_card("confirm"))
    assert sprint_batch.section(item, NOW) == sprint_batch.READY
    body = sprint_batch.detail(item, set(), NOW)
    assert body.index("Data check") < body.index("Hi Laurel")


# ---------------------------------------------------------------------------
# `data N: <output>` in the sprint thread
# ---------------------------------------------------------------------------

def test_data_takes_the_rest_of_the_message():
    commands = sprint_thread.parse_strict("data 12: OPEN []\nFILE [1, 2]")
    assert commands == [{"verb": "data", "numbers": [12],
                         "arg": "OPEN []\nFILE [1, 2]"}]
    commands = sprint_thread.parse_strict("data 12\nOPEN []")
    assert commands[0]["arg"] == "OPEN []"


def test_talking_about_data_is_not_a_paste():
    assert sprint_thread.parse_strict("data 12 looks wrong") is None
    assert sprint_thread.parse_strict("data 12") is None


def test_a_paste_is_kept_and_triaged_again(monkeypatch):
    stored, queued = [], []
    monkeypatch.setattr(sprint_store, "add_data_result",
                        lambda i, e, keep=5: stored.append((i, e)))
    import vomeos.worker

    monkeypatch.setattr(vomeos.worker, "enqueue_event",
                        lambda key, msg, queue="": queued.append(msg))
    reply = sprint_thread._execute(
        {"verb": "data", "numbers": [12], "arg": "```OPEN []```",
         "thread": "171.5"}, _item())
    assert "got it (1 lines)" in reply
    assert stored[0][0] == 512 and stored[0][1]["text"] == "OPEN []"
    assert queued == [{"ticket_id": "569412", "reason": "data",
                       "refresh": True, "force": True,
                       "reply_thread": "171.5"}]


def test_an_empty_paste_is_refused(monkeypatch):
    monkeypatch.setattr(sprint_store, "add_data_result",
                        lambda *a, **k: pytest.fail("nothing to store"))
    reply = sprint_thread._execute(
        {"verb": "data", "numbers": [12], "arg": "``` ```"}, _item())
    assert "nothing to read" in reply


def test_every_triage_reads_the_pasted_results():
    item = _item(data_results=[{"at": "2026-11-05 14:00 UTC", "by": "sam",
                                "text": "OPEN []"}])
    text = data_check.context_text(item)
    assert "Sam ran the data check on 2026-11-05 14:00 UTC" in text
    assert text.endswith("OPEN []")
    assert data_check.context_text(_item()) == ""


# ---------------------------------------------------------------------------
# Eval cases: #9760 as triage first saw it, either level agrees
# ---------------------------------------------------------------------------

def test_an_expected_list_means_any_of_these():
    from vomeos import evaluate

    want = {"data_check.needed": ["confirm", "required"]}
    assert evaluate.compare(want, _card("required")) == []
    assert evaluate.compare(want, _card("confirm")) == []
    assert evaluate.compare(want, _card("none")) != []


def test_expect_pairs_with_a_bar_become_options():
    import triage_cases

    assert triage_cases.parse_expect(["data_check.needed=confirm|required"]) \
        == {"data_check.needed": ["confirm", "required"]}


def test_a_case_can_cut_the_thread_to_how_triage_first_saw_it():
    import triage_cases

    thread = ("[2026-10-30 13:57] Laurel: uploaded, not complete\nmore\n"
              "[2026-11-02 20:27] Vome: looking\n"
              "[2026-11-05 17:22] Laurel: thanks")
    assert triage_cases.thread_before(thread, "2026-11-01") == \
        "[2026-10-30 13:57] Laurel: uploaded, not complete\nmore"
    assert triage_cases.thread_before(thread, "") == thread


def test_the_cut_reads_the_live_thread_format_and_drops_undated_notes():
    import triage_cases

    thread = ("--- Unknown [INTERNAL] --  ---\nRouted to sanjay\n\n"
              "--- Laurel Gaffney [in] -- 2026-11-05T17:22:06.000Z ---\n"
              "Tyrone's upload\n\n"
              "--- Support Agent [out] -- 2026-11-02T20:27:22.877Z ---\n"
              "looking\n\n"
              "--- Laurel Gaffney [in] -- 2026-10-30T13:57:15.000Z ---\n"
              "uploaded, not complete\n")
    cut = triage_cases.thread_before(thread, "2026-11-01")
    assert cut == ("--- Laurel Gaffney [in] -- 2026-10-30T13:57:15.000Z ---"
                   "\nuploaded, not complete")
