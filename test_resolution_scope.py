"""
test_resolution_scope.py

Sam, 2026-10-23: clients told us we wrote "fixed" when only one of the
problems in their ticket had shipped. A client hears "fixed" once, when
every problem they raised is fixed. What matters here: an open task on the
same ticket holds the email without asking the model, an Agent Fix counts
as shipped only when its parent is Live, the agent's "not everything" holds
it, anything unreadable holds it, and each automatic path (on prod, release
watcher, shipped-untold) holds instead of sending.
"""

from datetime import timedelta

import pytest

import resolution_scope


class _Result:
    def __init__(self, data, ok=True):
        self.data, self.ok = data, ok

    def get(self, key, default=None):
        return self.data.get(key, default)

    def why(self):
        return "blocked"


@pytest.fixture
def world(monkeypatch):
    class W:
        queue = []
        fixes = []
        verdict = {"all_fixed": True, "open_issues": [], "reason": "covered"}
        asked = []

    monkeypatch.setattr(resolution_scope, "_linked_queue_tasks",
                        lambda n, t: list(W.queue))
    monkeypatch.setattr(resolution_scope, "_linked_agent_fixes",
                        lambda n, t: list(W.fixes))
    monkeypatch.setattr(resolution_scope, "_fix_text",
                        lambda task: f"{task.get('name')} (live)")
    import vomeos

    def run(agent, context, **kw):
        W.asked.append(context)
        return _Result(W.verdict)

    monkeypatch.setattr(vomeos, "run_agent", run)
    return W


FIX = [{"id": "cu1", "name": "[#9700] Export drops a column"}]


def test_one_problem_one_fix_sends(world):
    out = resolution_scope.check("T1", "9700", FIX, thread="thread")
    assert out["complete"] is True
    assert "Export drops a column" in world.asked[0]["shipped_fixes"]


def test_another_open_task_on_the_ticket_holds_without_asking(world):
    world.queue = [{"id": "cu2", "name": "[#9700] Totals wrong",
                    "status": "in progress", "list": "Priority Queue"}]
    out = resolution_scope.check("T1", "9700", FIX, thread="thread")
    assert out["complete"] is False
    assert out["open"] == ["[#9700] Totals wrong"]
    assert world.asked == []


def test_the_shipping_task_itself_is_not_open_work(world):
    world.queue = [{"id": "cu1", "name": "[#9700] Export drops a column",
                    "status": "in progress", "list": "Priority Queue"}]
    assert resolution_scope.check("T1", "9700", FIX, thread="t")["complete"]


def test_a_shipped_sibling_is_read_as_a_fix(world):
    world.queue = [{"id": "cu2", "name": "[#9700] Totals wrong",
                    "status": "on prod", "list": "Priority Queue"}]
    assert resolution_scope.check("T1", "9700", FIX, thread="t")["complete"]
    assert "Totals wrong" in world.asked[0]["shipped_fixes"]


def test_a_declined_sibling_does_not_hold(world):
    world.queue = [{"id": "cu2", "name": "[#9700] Badge printing",
                    "status": "declined", "list": "Priority Queue"}]
    assert resolution_scope.check("T1", "9700", FIX, thread="t")["complete"]


def test_an_agent_fix_not_yet_live_holds(world):
    world.fixes = [{"id": "af2", "parent": "", "name": "[#9700] Totals",
                    "status": "on dev", "shipped": False}]
    assert not resolution_scope.check("T1", "9700", FIX,
                                      thread="t")["complete"]


def test_a_live_agent_fix_counts_as_shipped(world):
    world.fixes = [{"id": "af3", "parent": "af2", "name": "[#9700] Client",
                    "status": "on dev", "shipped": True}]
    assert resolution_scope.check("T1", "9700", FIX, thread="t")["complete"]


def test_the_shipping_fix_family_is_excluded(world):
    world.fixes = [{"id": "kid", "parent": "par", "name": "[#9700] Client",
                    "status": "on dev", "shipped": False}]
    out = resolution_scope.check("T1", "9700", [{"id": "par", "name": "x"}],
                                 thread="t", exclude={"par", "kid"})
    assert out["complete"] is True


def test_a_problem_with_no_task_holds_on_the_agent(world):
    world.verdict = {"all_fixed": False,
                     "open_issues": ["reminder texts show the wrong time"],
                     "reason": "second problem not covered"}
    out = resolution_scope.check("T1", "9700", FIX, thread="t")
    assert out["complete"] is False
    assert "reminder texts" in out["reason"]


def test_all_fixed_with_issues_listed_still_holds(world):
    world.verdict = {"all_fixed": True, "open_issues": ["totals"],
                     "reason": ""}
    assert not resolution_scope.check("T1", "9700", FIX,
                                      thread="t")["complete"]


def test_clickup_unreadable_holds(world, monkeypatch):
    def boom(n, t):
        raise RuntimeError("401")

    monkeypatch.setattr(resolution_scope, "_linked_queue_tasks", boom)
    out = resolution_scope.check("T1", "9700", FIX, thread="t")
    assert out["complete"] is False and "401" in out["reason"]


def test_no_verdict_holds(world, monkeypatch):
    import vomeos

    monkeypatch.setattr(vomeos, "run_agent",
                        lambda *a, **k: _Result({}, ok=False))
    assert not resolution_scope.check("T1", "9700", FIX,
                                      thread="t")["complete"]


def test_an_empty_verdict_is_asked_again(world, monkeypatch):
    """#9571: one empty model reply held a live fix's email for good."""
    import vomeos

    replies = [_Result({}, ok=False), _Result(world.verdict)]
    monkeypatch.setattr(vomeos, "run_agent",
                        lambda *a, **k: replies.pop(0))
    assert resolution_scope.check("T1", "9700", FIX, thread="t")["complete"]
    assert replies == []


def test_the_fix_section_survives_the_cut():
    """#9528: the cause filled the 1200 characters, so the agent never saw
    the fix and held a fixed ticket."""
    brief = ("Description: Fix PR: https://github.com/x/pull/853 "
             "**Likely cause:** " + "slow load. " * 150
             + "**Potential fix:** initialise centralizedOwnerId.")
    text = resolution_scope._fix_first(brief)
    assert text.startswith("**Potential fix:**")
    assert "Fix PR: https://github.com/x/pull/853" in text


def test_fix_pr_is_not_the_fix_section():
    brief = "**Fix PR:** https://x **Fix (side by side).** kiosk label"
    assert resolution_scope._fix_first(brief).startswith("**Fix (side")
    assert resolution_scope._fix_first("no sections") == "no sections"


# ---------------------------------------------------------------------------
# The three automatic paths hold instead of sending
# ---------------------------------------------------------------------------

HELD = {"complete": False, "open": ["totals"],
        "reason": "not everything the client raised is fixed yet: totals"}


def test_on_prod_holds_and_closes_nothing(monkeypatch):
    import on_prod_handler
    from test_error_report_on_prod import _install_spies

    calls = _install_spies(monkeypatch, {
        "subject": "Reports page", "description": "Export and totals.",
        "contact_name": "Marcus Lee", "contact_email": "m@example.org",
        "cc_email": "",
    })
    monkeypatch.setattr(on_prod_handler.resolution_scope, "check",
                        lambda *a, **k: dict(HELD))
    posted = []
    monkeypatch.setattr(on_prod_handler, "_create_new_thread",
                        lambda **kw: posted.append(kw["notice"]) or "ts1")
    monkeypatch.setattr(on_prod_handler, "_store_pending_send",
                        lambda ts, d: None)
    monkeypatch.setattr(on_prod_handler, "set_thread_on_prod_pending",
                        lambda ts: None)

    assert on_prod_handler.handle_on_prod("cu9", "Sanjay") is True
    assert calls["sent"] == []
    assert calls["closed"] == [] and calls["clickup"] == []
    assert calls["final_review"] == []
    assert "Held" in posted[0] and "totals" in posted[0]


def test_on_prod_already_told_does_not_close_with_open_work(monkeypatch):
    import on_prod_handler
    from test_error_report_on_prod import _install_spies

    calls = _install_spies(monkeypatch, {
        "subject": "Reports page", "description": "Export and totals.",
        "contact_name": "Marcus Lee", "contact_email": "m@example.org",
        "cc_email": "",
    })
    monkeypatch.setattr(on_prod_handler.resolution_scope, "check",
                        lambda *a, **k: dict(HELD))
    monkeypatch.setattr(on_prod_handler, "_assess_resolution_state",
                        lambda f, t: {"already_confirmed_fixed": True,
                                      "recommendation": "skip",
                                      "reason": "", "last_team_reply": ""})
    monkeypatch.setattr(on_prod_handler, "_create_new_thread",
                        lambda **kw: "ts1")
    monkeypatch.setattr(on_prod_handler, "_store_pending_send",
                        lambda ts, d: None)
    monkeypatch.setattr(on_prod_handler, "set_thread_on_prod_pending",
                        lambda ts: None)

    on_prod_handler.handle_on_prod("cu9", "Sanjay")
    assert calls["closed"] == [] and calls["sent"] == []


def test_release_watcher_holds_the_resolution(monkeypatch):
    import release_resolutions as rr
    import test_release_resolutions as trr

    sent, finished = [], []
    import shadow_triage
    import sprint_store
    import sprint_thread

    monkeypatch.delenv("RELEASE_AUTO_SEND", raising=False)
    monkeypatch.setattr(rr, "_claim", lambda *a: True)
    monkeypatch.setattr(rr, "_finish", lambda c, i, o, d: finished.append(o))
    monkeypatch.setattr(shadow_triage, "_handle",
                        lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(sprint_store, "by_ticket", lambda t: {
        "number": 3, "category": "clear_bug", "draft_status": "ok",
        "recipients": {"to": "d@x.org"}})
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda item, **k: sent.append(1) or "sent to d@x.org")
    monkeypatch.setattr(resolution_scope, "check", lambda *a, **k: dict(HELD))

    entries = rr.prepare({"id": "p1"}, [trr._child()])
    assert sent == [] and finished == ["held"]
    assert entries[0]["outcome"] == "held"
    assert "totals" in entries[0]["detail"]


def test_shipped_untold_holds_the_fix_email(monkeypatch):
    import release_resolutions as rr
    import shadow_triage
    import shipped_untold
    import sprint_store
    import sprint_thread
    from test_remaining_items import NOW
    from zoho_links import FIELD_ZOHO_TICKET_LINK

    task = {"id": "868pq9", "name": "[#9702] Export drops a column",
            "date_updated": str(int((NOW - timedelta(days=1)).timestamp()
                                    * 1000)),
            "custom_fields": [{"id": FIELD_ZOHO_TICKET_LINK,
                               "value": "#Cases/dv/569440000046666666"}]}
    sent, finished = [], []
    monkeypatch.delenv("RELEASE_AUTO_SEND", raising=False)
    monkeypatch.setattr(sprint_store, "ready", lambda: True)
    monkeypatch.setattr(shipped_untold, "shipped_tasks",
                        lambda: [{"task": task, "kind": "fix"}])
    monkeypatch.setattr(shipped_untold, "already_told", lambda t, s: False)
    monkeypatch.setattr(rr, "_claim", lambda *a: True)
    monkeypatch.setattr(rr, "_finish", lambda c, i, o, d: finished.append(o))
    monkeypatch.setattr(shadow_triage, "_handle",
                        lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(sprint_store, "by_ticket", lambda t: {
        "number": 33, "draft_status": "ok", "category": "clear_bug"})
    monkeypatch.setattr(sprint_thread, "_send",
                        lambda i, **k: sent.append(1) or "sent to a@b.org")
    monkeypatch.setattr(resolution_scope, "check", lambda *a, **k: dict(HELD))

    shipped_untold.run_shipped_untold()
    assert sent == [] and finished == ["held"]
