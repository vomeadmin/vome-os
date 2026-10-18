"""
test_learning_loop.py

Sam's corrections must shape the very next run, everywhere he makes them.
The cases here are the ways that could quietly fail: a lesson stored but never
reaching the prompt, the same rule stored twice, a ticket-specific fix (a
name, a date) turned into a rule for every ticket, "forget 7" being read as
an item command, or the partner and the keeper both recording one correction.
"""

import os

import pytest

import sprint_thread
from vomeos import config, lessons, store

TEST_DB = os.environ.get("SPRINT_TEST_DATABASE_URL", "")
DRAFTER = "customer_success.reply_drafter"


@pytest.fixture
def db(monkeypatch):
    if not TEST_DB:
        pytest.skip("SPRINT_TEST_DATABASE_URL not set")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB)
    store.reset()
    store.execute(f"DROP TABLE IF EXISTS {lessons.TABLE}")
    store.reset()
    yield
    store.reset()


# ---------------------------------------------------------------------------
# The store and the prompt
# ---------------------------------------------------------------------------

def test_a_lesson_reaches_the_agents_next_prompt(db):
    from vomeos.composer import compose_system_prompt
    from vomeos.registry import get_agent

    manifest = get_agent(DRAFTER)
    assert "Lessons from Sam" not in compose_system_prompt(manifest)
    lesson_id = lessons.add(DRAFTER, "In a resolution reply, do not "
                            "mention earlier replies.", "slack_edit")
    prompt = compose_system_prompt(manifest)
    assert f"[L{lesson_id}] In a resolution reply" in prompt
    assert "never override" in prompt
    # The partner does not get the drafter's lesson.
    partner = get_agent("customer_success.sprint_partner")
    assert f"[L{lesson_id}]" not in compose_system_prompt(partner)


def test_lessons_for_every_agent_reach_all(db):
    from vomeos.composer import compose_system_prompt
    from vomeos.registry import get_agent

    lesson_id = lessons.add("*", "Say user, not volunteer.", "manual")
    for name in (DRAFTER, "customer_success.triage_director"):
        assert f"[L{lesson_id}]" in compose_system_prompt(get_agent(name))


def test_the_same_rule_is_stored_once(db):
    a = lessons.add(DRAFTER, "Keep acks to two sentences.", "slack_edit")
    b = lessons.add(DRAFTER, "keep acks to two sentences.", "slack_feedback")
    assert a == b and len(lessons.all_active()) == 1


def test_retired_and_promoted_lessons_leave_the_prompt(db):
    a = lessons.add(DRAFTER, "Rule A.", "manual")
    b = lessons.add(DRAFTER, "Rule B.", "manual")
    assert lessons.retire(a, "wrong") and lessons.promote(b, "in charter")
    assert lessons.active_for(DRAFTER) == []
    assert lessons.render_block(DRAFTER) == ""


def test_cli_add_and_list(db, capsys):
    assert lessons.main(["add", "--agent", DRAFTER, "--rule",
                         "No filler.", "--source", "claude_code"]) == 0
    lessons.main(["list"])
    out = capsys.readouterr().out
    assert "No filler." in out and "1 active" in out


def test_no_database_means_no_block_not_a_crash(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    store.reset()
    assert lessons.render_block(DRAFTER) == ""
    assert lessons.add(DRAFTER, "x", "manual") is None
    store.reset()


# ---------------------------------------------------------------------------
# The lesson keeper decides
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, data):
        self.ok = True
        self.data = data


@pytest.fixture
def keeper(monkeypatch):
    class K:
        verdict = {}
        added = []
        next_id = 7

    def add(agent, rule, source, evidence="", ticket="", created_by=""):
        K.added.append((agent, rule, source, ticket))
        return K.next_id

    monkeypatch.setattr("vomeos.run_agent",
                        lambda *a, **k: _Result(K.verdict))
    monkeypatch.setattr(lessons, "add", add)
    monkeypatch.setattr(lessons, "active_for", lambda agent, limit=40: [])
    return K


ITEM = {"id": 1, "number": 3, "category": "clear_bug",
        "ticket_number": "9500"}


def test_a_general_edit_becomes_a_lesson(keeper):
    keeper.verdict = {"general": True, "rule": "Never apologise for an "
                      "earlier reply.", "applies_to": DRAFTER,
                      "confidence": "high", "duplicate_of": ""}
    note = sprint_thread._learn(ITEM, "Apologies... live.", "Live.", "")
    assert keeper.added == [(DRAFTER, "Never apologise for an earlier "
                             "reply.", "slack_edit", "9500")]
    assert "Learned *L7*" in note and "`forget 7`" in note


@pytest.mark.parametrize("verdict", [
    {"general": False, "rule": "", "confidence": "high"},
    {"general": True, "rule": "x", "confidence": "low"},
])
def test_ticket_fixes_and_unsure_calls_teach_nothing(keeper, verdict):
    keeper.verdict = verdict
    assert sprint_thread._learn(ITEM, "Hi Ali", "Hi Allie", "") == ""
    assert keeper.added == []


def test_a_duplicate_is_not_stored_again(keeper):
    keeper.verdict = {"general": True, "duplicate_of": "L3",
                      "confidence": "high"}
    note = sprint_thread._learn(ITEM, "a", "b", "")
    assert keeper.added == [] and "Already a lesson (L3)" in note


def test_an_unknown_target_falls_back_to_the_drafter(keeper):
    keeper.verdict = {"general": True, "rule": "r", "applies_to":
                      "engineering.fix_author", "confidence": "high"}
    sprint_thread._learn(ITEM, "a", "b", "")
    assert keeper.added[0][0] == DRAFTER


def test_no_change_is_not_a_correction(keeper):
    assert sprint_thread._learn(ITEM, "same", "same ", "") == ""


# ---------------------------------------------------------------------------
# Lessons from the conversation, and the commands
# ---------------------------------------------------------------------------

def test_partner_lessons_are_recorded(keeper):
    out = sprint_thread._record_partner_lessons(
        [{"agent": "customer_success.triage_director",
          "rule": "Kiosk chunk errors need no reply."},
         {"agent": "engineering.fix_author", "rule": "nope"},
         "garbage"], "kiosk chunk errors don't need a reply")
    assert [a[0] for a in keeper.added] == [
        "customer_success.triage_director"]
    assert len(out) == 1 and "triage director" in out[0]


def test_forget_and_lessons_are_not_item_commands(monkeypatch):
    retired = []
    monkeypatch.setattr(lessons, "get", lambda i: {"id": i, "rule": "R"})
    monkeypatch.setattr(lessons, "retire",
                        lambda i, note="": retired.append(i) or True)
    monkeypatch.setattr(lessons, "all_active",
                        lambda: [{"id": 4, "agent": DRAFTER, "rule": "R4"}])
    assert "Forgot L7" in sprint_thread._lesson_command("forget 7")
    assert "Forgot L7" in sprint_thread._lesson_command("forget L7")
    assert retired == [7, 7]
    assert "L4" in sprint_thread._lesson_command("lessons")
    assert sprint_thread._lesson_command("send 7") is None
    assert sprint_thread._lesson_command("what about 7?") is None


def test_one_correction_is_learned_once_per_turn(monkeypatch, keeper):
    """When the partner records the lesson, the keeper is not asked too."""
    import sprint_store

    item = {"id": 21, "number": 2, "ticket_number": "9415",
            "category": "clear_bug", "draft": "old", "status": "open",
            "draft_status": "ok", "recipients": {}, "card": {}}
    monkeypatch.setattr(sprint_store, "working_set", lambda: [item])
    monkeypatch.setattr(sprint_store, "set_draft", lambda *a, **k: None)
    monkeypatch.setattr(sprint_thread, "_turns",
                        lambda t: [{"role": "sam", "text": "x",
                                    "focus": None, "created_at": None}])
    monkeypatch.setattr(sprint_thread, "_ticket_text",
                        lambda item, n="": "thread")
    monkeypatch.setattr(sprint_thread, "_check_draft",
                        lambda item, text: ("ok", ""))
    keeper_calls = []
    monkeypatch.setattr(sprint_thread, "_learn",
                        lambda *a, **k: keeper_calls.append(a) or "")
    monkeypatch.setattr("vomeos.run_agent", lambda *a, **k: _Result({
        "reply": "Rewritten.", "focus": 2, "verbs": "edit:2",
        "actions": [{"verb": "edit", "number": 2, "text": "new draft"}],
        "lessons": [{"agent": DRAFTER, "rule": "Keep it to two lines."}],
    }))
    reply, _, _ = sprint_thread.converse("2 is too long, always keep these "
                                         "to two lines", "d", "t")
    assert keeper.added and keeper.added[0][1] == "Keep it to two lines."
    assert keeper_calls == []
    assert "Learned" in reply


def test_report_lists_live_lessons():
    import autonomy_report

    text = autonomy_report.render([], [], [
        {"id": 3, "agent": DRAFTER, "rule": "No filler."}])
    assert "1 live lessons" in text and "L3" in text and "promote" in text
