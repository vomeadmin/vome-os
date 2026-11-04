"""
test_courtesy_replies.py

#9470 (2026-10-27): Stephanie's "Thank you, very helpful!" and an Outlook
"reacted to your message" email landed five seconds apart. Only the reaction
was judged, it did not read as a thank-you, and the ticket went back to
triage and on to OnlyG as a new urgent task. Sam, 2026-11-04: once the fix
is made, a thank-you, a thumbs up or a reaction needs no reply, so close the
ticket in Zoho and ClickUp and keep it out of the queue.

What matters here: every email since our last one is judged together, a
reaction never needs the model, a thank-you closes only when nothing is
open or owed, work still open keeps the ticket with it, "could not read"
never closes anything, and nothing is ever sent to the client.
"""
import os

for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import pytest  # noqa: E402

import agent  # noqa: E402

STEPHANIE = {"type": "END_USER", "email": "ssielaty@scgov.net"}

# The two emails as the conversations list returned them on #9470.
REACTION = {
    "type": "thread", "direction": "in", "author": STEPHANIE,
    "createdTime": "2026-10-27T00:01:19.000Z", "attachmentCount": "-1",
    "summary": ("Stephanie Sielaty reacted to your message: From: Vome "
                "Volunteer <support@vomevolunteer.zohodesk.com> Sent: "
                "Thursday, 22 October 2026 01:56:59 To: Stephanie Sielaty "
                "<ssielaty@scgov...."),
}
THANKS = {
    "type": "thread", "direction": "in", "author": STEPHANIE,
    "createdTime": "2026-10-27T00:01:14.000Z", "attachmentCount": "-1",
    "summary": ("Thank you, very helpful! Stephanie Sielaty, CVA, VAL, "
                "ESF15, CERT, DOVIA CL Volunteer Sarasota Program "
                "Coordinator Human Resources c. 813-922-5043 o. "
                "941-861-5133 1 Apex Road, Sara..."),
}
OUR_RELEASE_EMAIL = {
    "type": "thread", "direction": "out",
    "author": {"type": "AGENT", "email": "admin@vomevolunteer.com"},
    "summary": "Hi Stephanie, I wanted to let you know that we just released",
}
HER_OLDER_QUESTION = {
    "type": "thread", "direction": "in", "author": STEPHANIE,
    "summary": "Can you also confirm the coordinator is notified?",
}
ROUTED_NOTE = {"type": "comment", "content": "Routed to onlyg automatically"}

BURST_9470 = {"data": [REACTION, ROUTED_NOTE, THANKS, OUR_RELEASE_EMAIL,
                       HER_OLDER_QUESTION]}


@pytest.fixture
def model_says_thanks(monkeypatch):
    """The cheap classifier, as it judges a plain thank-you."""
    seen = []

    def classify(text):
        seen.append(text)
        return text.lower().startswith(("thank", "merci"))

    monkeypatch.setattr(agent, "_classify_no_action_reply", classify)
    return seen


# ---------------------------------------------------------------------------
# What counts as a courtesy reply
# ---------------------------------------------------------------------------

def test_a_reaction_email_needs_no_model(monkeypatch):
    monkeypatch.setattr(agent, "_classify_no_action_reply",
                        lambda t: pytest.fail("a reaction is never asked"))
    assert agent._reply_kind(REACTION) == "courtesy"
    french = dict(REACTION, summary="Stephanie a réagi à votre message : De")
    assert agent._reply_kind(french) == "courtesy"


def test_a_bare_emoji_needs_no_model(monkeypatch):
    monkeypatch.setattr(agent, "_classify_no_action_reply",
                        lambda t: pytest.fail("an emoji is never asked"))
    assert agent._reply_kind({"summary": "👍"}) == "courtesy"
    assert agent._reply_kind({"summary": "🙏🎉!"}) == "courtesy"


def test_a_lone_question_mark_is_not_an_emoji(model_says_thanks):
    assert agent._reply_kind({"summary": "?"}) == "action"


def test_our_quoted_email_is_not_judged_as_hers(model_says_thanks):
    """The copy of our message carries our own "when" and "?", which the
    action signals read as hers."""
    full = ("Thank you, very helpful! From: Vome Volunteer Sent: Wednesday, "
            "October 21, 2026 To: Stephanie Hi Stephanie, when someone "
            "applies, can you check?")
    assert agent._strip_quoted(full) == "Thank you, very helpful!"
    assert agent._reply_kind({"summary": full}) == "courtesy"
    gmail = "Merci! On Tue, Oct 20, 2026 at 4:54 PM Sam wrote: why?"
    assert agent._strip_quoted(gmail) == "Merci!"


def test_attachments_always_need_action(model_says_thanks):
    assert agent._reply_kind(dict(THANKS, hasAttach=True)) == "action"


def test_see_below_is_not_a_thank_you(model_says_thanks):
    reply = {"summary": "Thanks, see my answers below in red."}
    assert agent._reply_kind(reply) == "action"
    assert model_says_thanks == []


# ---------------------------------------------------------------------------
# The burst: every email since our last one
# ---------------------------------------------------------------------------

def test_the_burst_stops_at_our_last_email_and_skips_notes():
    assert agent._new_client_messages(BURST_9470) == [REACTION, THANKS]


def test_the_9470_burst_is_a_courtesy_reply(model_says_thanks):
    verdict, message = agent._burst_verdict(
        agent._new_client_messages(BURST_9470))
    assert verdict == "courtesy"
    assert message is REACTION


def test_one_email_needing_action_makes_the_burst_need_it(model_says_thanks):
    also = dict(THANKS, summary="Thanks! But the site was not added.")
    verdict, message = agent._burst_verdict([REACTION, also])
    assert verdict == "action" and message is also


def test_a_burst_of_out_of_offices_is_still_an_auto_reply():
    away = dict(THANKS, summary="I will be out of the office until Monday")
    assert agent._burst_verdict([away])[0] == "auto"


def test_a_courtesy_burst_goes_to_the_closer_and_nowhere_else(
        monkeypatch, model_says_thanks):
    handed = []
    monkeypatch.setattr(agent, "_processing_updates", {})
    monkeypatch.setattr(agent, "fetch_ticket_conversations",
                        lambda t: BURST_9470)
    monkeypatch.setattr(agent, "_handle_courtesy_reply_later",
                        lambda t, r: handed.append((t, r)))
    monkeypatch.setattr(agent, "_classify_client_reply",
                        lambda t: pytest.fail("not reprocessed"))
    monkeypatch.setattr(agent, "_set_zoho_ticket_status",
                        lambda t, s: pytest.fail("no status set inline"))
    assert agent.process_ticket_update("569440000043022085") is None
    assert handed == [("569440000043022085", REACTION)]


def test_shadow_triage_skips_the_9470_burst(monkeypatch, model_says_thanks):
    import shadow_triage

    monkeypatch.setattr(agent, "fetch_ticket_conversations",
                        lambda t: BURST_9470)
    assert shadow_triage._auto_reply("569440000043022085") is True


def test_shadow_triage_still_triages_a_real_follow_up(monkeypatch,
                                                      model_says_thanks):
    import shadow_triage

    follow_up = dict(THANKS, summary="The coordinator was not notified.")
    monkeypatch.setattr(agent, "fetch_ticket_conversations",
                        lambda t: {"data": [REACTION, follow_up]})
    assert shadow_triage._auto_reply("T") is False


def test_a_reaction_is_not_a_message_we_owe_an_answer():
    import sprint_batch

    assert sprint_batch._out_of_office(REACTION) is True
    assert sprint_batch._out_of_office(HER_OLDER_QUESTION) is False


# ---------------------------------------------------------------------------
# What a courtesy reply does
# ---------------------------------------------------------------------------

def _work(**over):
    work = {"open": [], "finished": [], "shipped": False, "owed": False,
            "escalated": False, "item": {}}
    work.update(over)
    return work


@pytest.mark.parametrize("prior,work,outcome", [
    # The fix shipped: closed, whatever we were waiting on.
    ("Awaiting Client Response", _work(shipped=True), "close"),
    # We had already finished the ticket.
    ("Closed", _work(), "close"),
    ("In Progress", _work(finished=[{"id": "86a"}]), "close"),
    # Work still open (#9470's promise task): stays with it.
    ("Closed", _work(open=[{"id": "868mkc7cn"}]), "in_progress"),
    ("Closed", _work(shipped=True, open=[{"id": "86b"}]), "in_progress"),
    # The fix is live and the client was never told: Sam's resolution.
    ("Closed", _work(shipped=True, owed=True), "leave"),
    ("Closed", _work(escalated=True, shipped=True), "leave"),
    # We were waiting on them: their next email reopens it if needed.
    ("Awaiting Client Response", _work(), "close"),
    ("On Hold", _work(), "restore"),
    # A reply was owed before the thank-you came, or nobody knows.
    ("Processing", _work(), "leave"),
    ("", _work(), "leave"),
])
def test_the_outcome_of_a_courtesy_reply(prior, work, outcome):
    assert agent._courtesy_outcome(prior, work) == outcome


WORKFLOW_FLIP = {"time": "2026-10-27T00:01:26.000Z", "from": "Closed",
                 "to": "Processing", "actor_type": "Workflow",
                 "actor": "Customer Reply to Processing Status"}


@pytest.fixture
def desk(monkeypatch):
    """Zoho, ClickUp and the sprint store, recorded. Nothing reaches them."""
    import database
    import sprint_store
    import ticket_reconciler
    import zoho_actions
    import zoho_desk_api

    class D:
        statuses, closed, decided, threads, logs = [], [], [], [], []
        history = [WORKFLOW_FLIP]
        work: dict = _work()

    def ticket_work(ticket_id):
        if isinstance(D.work, Exception):
            raise D.work
        return D.work

    monkeypatch.setattr(agent, "_ticket_work", ticket_work)
    monkeypatch.setattr(zoho_desk_api, "status_history",
                        lambda t: D.history)
    monkeypatch.setattr(zoho_actions, "set_status",
                        lambda t, s: D.statuses.append(s) or {})
    monkeypatch.setattr(zoho_actions, "send_reply",
                        lambda *a: pytest.fail("never emails the client"))
    monkeypatch.setattr(agent, "_set_zoho_ticket_status",
                        lambda t, s: D.statuses.append(f"mcp:{s}") or True)
    monkeypatch.setattr(ticket_reconciler, "close_task",
                        lambda t, note: D.closed.append(t["id"]) or True)
    monkeypatch.setattr(sprint_store, "decide",
                        lambda i, s, d: D.decided.append((i, s)))
    monkeypatch.setattr(database, "get_thread_by_ticket_id",
                        lambda t: ("TS", {"clickup_task_id": "86a"}))
    monkeypatch.setattr(database, "update_thread",
                        lambda ts, **k: D.threads.append(k))
    monkeypatch.setattr(agent, "_log_courtesy",
                        lambda t, text, done, number="": D.logs.append(done))
    D.statuses, D.closed, D.decided, D.threads, D.logs = [], [], [], [], []
    D.history, D.work = [WORKFLOW_FLIP], _work()
    return D


def test_a_thank_you_after_the_fix_closes_everywhere(desk):
    import sprint_store

    desk.work = _work(
        shipped=True, item={"id": 33, "status": "open",
                            "ticket_number": "9470"},
        finished=[{"id": "86a", "status": "on prod", "shipped": True},
                  {"id": "86c", "status": "closed", "shipped": True}])
    assert agent._handle_courtesy_reply("T", REACTION) == "close"
    # Zoho Closed with no closure email, and never through the MCP setter.
    assert desk.statuses == ["Closed"]
    # Only the task not already closed is touched.
    assert desk.closed == ["86a"]
    assert desk.decided == [(33, sprint_store.DONE)]
    assert desk.threads == [{"status": agent.THREAD_CLOSED}]


def test_open_work_keeps_the_ticket_with_it(desk):
    desk.work = _work(open=[{"id": "868mkc7cn",
                             "url": "https://app.clickup.com/t/868mkc7cn"}],
                      item={"id": 33, "status": "open"})
    assert agent._handle_courtesy_reply("T", REACTION) == "in_progress"
    assert desk.statuses == ["In Progress"]
    assert desk.closed == [] and desk.decided == []
    assert "868mkc7cn" in desk.logs[0]


def test_an_untold_fix_is_left_for_sam(desk):
    desk.work = _work(shipped=True, owed=True)
    assert agent._handle_courtesy_reply("T", REACTION) == "leave"
    assert desk.statuses == [] and desk.closed == []
    assert "not been told" in desk.logs[0]


def test_on_hold_comes_back_when_nothing_shipped(desk):
    desk.history = [dict(WORKFLOW_FLIP, **{"from": "On Hold"})]
    assert agent._handle_courtesy_reply("T", REACTION) == "restore"
    assert desk.statuses == ["On Hold"]


def test_a_thank_you_while_we_wait_on_them_closes(desk):
    desk.history = [dict(WORKFLOW_FLIP,
                         **{"from": "Awaiting Client Response"})]
    assert agent._handle_courtesy_reply("T", REACTION) == "close"
    assert desk.statuses == ["Closed"]


def test_unreadable_work_never_closes(desk, monkeypatch):
    restored = []
    desk.work = RuntimeError("ClickUp 429")
    monkeypatch.setattr(agent, "_restore_status_after_auto_reply",
                        lambda t, r: restored.append(t) or "Closed")
    assert agent._handle_courtesy_reply("T", REACTION) == "unreadable"
    assert desk.statuses == [] and desk.closed == []
    assert restored == ["T"]
    assert "nothing was closed" in desk.logs[0]


# ---------------------------------------------------------------------------
# Reading the work
# ---------------------------------------------------------------------------

@pytest.fixture
def clickup(monkeypatch):
    import clickup_search
    import database
    import shadow_triage
    import sprint_batch
    import sprint_store
    import ticket_routing

    class C:
        linked: list = []
        states: dict = {}
        release = {"text": "No agent fix is linked to this ticket.",
                   "live": False, "task_id": ""}
        pending: dict = {}
        item: dict = {"ticket_number": "9470"}

    monkeypatch.setattr(clickup_search, "linked_tasks",
                        lambda n, t: C.linked)
    monkeypatch.setattr(ticket_routing, "_read_state",
                        lambda tid: C.states[tid])
    monkeypatch.setattr(shadow_triage, "release_state",
                        lambda n, t: C.release)
    monkeypatch.setattr(sprint_batch, "_pending_resolutions",
                        lambda: C.pending)
    monkeypatch.setattr(sprint_store, "by_ticket", lambda t: C.item)
    monkeypatch.setattr(database, "get_thread_by_ticket_id", lambda t: None)
    return C


def test_the_9470_promise_task_is_open_work(clickup):
    clickup.linked = [
        {"id": "868mkc7cn", "list": "Sam Accepted (Build Now)"},
        {"id": "868mh58e0", "list": "Priority Queue"},
        {"id": "86idea", "list": "Raw Intake"},
    ]
    clickup.states = {
        "868mkc7cn": {"status": "queued", "shipped": False,
                      "list": "Sam Accepted (Build Now)"},
        "868mh58e0": {"status": "closed", "shipped": True,
                      "list": "Priority Queue"},
    }
    work = agent._ticket_work("569440000043022085")
    assert [t["id"] for t in work["open"]] == ["868mkc7cn"]
    assert [t["id"] for t in work["finished"]] == ["868mh58e0"]
    # A feature request in Raw Intake is never read, so never closed.
    assert "86idea" not in str(work)


def test_an_agent_fix_not_yet_live_is_open_work(clickup):
    clickup.release = {"text": "NOT LIVE.", "live": False, "task_id": "86fix"}
    work = agent._ticket_work("T")
    assert work["open"] and not work["shipped"]


def test_a_live_fix_still_owed_to_the_client(clickup):
    clickup.release = {"text": "LIVE.", "live": True, "task_id": "86fix"}
    clickup.pending = {"T": "2026-11-03"}
    work = agent._ticket_work("T")
    assert work["shipped"] and work["owed"] and not work["open"]


def test_an_unreadable_task_raises(clickup):
    clickup.linked = [{"id": "86pq", "list": "Priority Queue"}]
    clickup.states = {"86pq": {"status": "unreadable"}}
    with pytest.raises(RuntimeError):
        agent._ticket_work("T")


def test_unreadable_agent_fixes_raises(clickup):
    clickup.release = {"text": "Agent Fixes could not be read (429). Treat "
                               "any fix as UNVERIFIED.",
                       "live": False, "task_id": ""}
    with pytest.raises(RuntimeError):
        agent._ticket_work("T")
