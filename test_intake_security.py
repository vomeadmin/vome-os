"""Vic's turn: security questions never reach the model, a yes files them
for the team, a reply with security detail is replaced before any side
effect, and lookups run in a bounded loop (intake.py)."""

import json
from types import SimpleNamespace

import pytest

import intake
import vic_security

SESSION = {
    "user_email": "admin@org.ca",
    "user_role": "admin",
    "org_name": "Depot",
    "platform": "web",
    "locale": "en",
}


@pytest.fixture
def no_model(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(intake, "_ask_vic", boom)
    monkeypatch.setattr(vic_security, "_classify", boom)


def test_security_question_refused_without_the_model(no_model):
    result = intake.run_intake_turn(
        "How long do your access tokens last?", SESSION, [])
    assert result["reply"] == vic_security.refusal("en")
    assert result["status"] == "collecting"
    assert not result["ticket_created"]


def test_french_refusal(no_model):
    result = intake.run_intake_turn(
        "Vos données sont-elles hébergées au Canada?",
        {**SESSION, "locale": "fr"}, [])
    assert result["reply"] == vic_security.refusal("fr")


def test_yes_files_the_question_word_for_word(monkeypatch, no_model):
    filed = {}

    def fake_create(extracted, session_context, conversation_history,
                    attachments, current_message="", current_reply=""):
        filed.update(extracted)
        return {"ticket_created": True, "ticket_id": "t1"}

    monkeypatch.setattr(intake, "_create_ticket_from_intake", fake_create)
    monkeypatch.setattr(intake, "log_vic_outcome", lambda **kw: None)
    history = [
        {"role": "user", "content": "Are you SOC 2 compliant?"},
        {"role": "assistant", "content": vic_security.refusal("en")},
    ]
    result = intake.run_intake_turn("yes please", SESSION, history)

    assert result["ticket_created"]
    assert result["status"] == "complete"
    assert result["reply"] == vic_security.handed_off("en")
    assert filed["description"] == "Are you SOC 2 compliant?"
    assert filed["module"] == "other"
    assert filed["affected_user_email"] == "admin@org.ca"


def test_leaky_reply_replaced_before_side_effects(monkeypatch):
    monkeypatch.setattr(
        vic_security, "_classify",
        lambda m, p="": vic_security.Screen(False, ""))
    leaky = (
        "Our links are valid for 24 hours, and everything is encrypted.\n\n"
        "```json\n" + json.dumps({
            "status": "complete",
            "auth_check": "someone@else.com",
            "extracted": {"description": "x", "module": "other",
                          "platform": "web",
                          "affected_user_email": "self"},
        }) + "\n```"
    )
    monkeypatch.setattr(intake, "_ask_vic", lambda s, m: leaky)

    def must_not_run(*a, **kw):
        raise AssertionError("no side effect may run")

    monkeypatch.setattr(intake, "_run_auth_check", must_not_run)
    monkeypatch.setattr(intake, "_run_auth_bypass", must_not_run)
    monkeypatch.setattr(intake, "_create_ticket_from_intake", must_not_run)
    monkeypatch.setattr(intake, "_search_kb_combined", lambda *a: None)

    result = intake.run_intake_turn(
        "Can you help me with my invite links please", SESSION, [])
    assert result["reply"] == vic_security.refusal("en")
    assert not result["ticket_created"]


# ---------------------------------------------------------------------------
# The lookup loop
# ---------------------------------------------------------------------------

def _tool_use(name="lookup_feature", block_id="tu1"):
    return SimpleNamespace(
        type="tool_use", id=block_id, name=name,
        input={"question": "approve hours"})


def _text(text):
    return SimpleNamespace(type="text", text=text)


class _FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def test_loop_runs_lookups_then_answers(monkeypatch):
    fake = _FakeMessages([
        SimpleNamespace(stop_reason="tool_use",
                        content=[_text("Let me check."), _tool_use()]),
        SimpleNamespace(stop_reason="end_turn",
                        content=[_text("Go to **Hours**.")]),
    ])
    monkeypatch.setattr(intake, "_client", SimpleNamespace(messages=fake))
    ran = []
    monkeypatch.setattr(
        intake.vic_tools, "run_tool",
        lambda name, args: ran.append(name) or "{}")

    reply = intake._ask_vic("system", [{"role": "user", "content": "hi"}])

    assert reply == "Go to **Hours**."
    assert ran == ["lookup_feature"]
    assert [c["tool_choice"]["type"] for c in fake.calls] == ["auto", "auto"]
    assert fake.calls[1]["messages"][-1]["content"][0]["type"] == (
        "tool_result")


def test_loop_stops_offering_tools_after_the_last_round(monkeypatch):
    fake = _FakeMessages([
        SimpleNamespace(stop_reason="tool_use", content=[_tool_use()]),
        SimpleNamespace(stop_reason="tool_use", content=[_tool_use()]),
        SimpleNamespace(stop_reason="end_turn", content=[_text("Done.")]),
    ])
    monkeypatch.setattr(intake, "_client", SimpleNamespace(messages=fake))
    monkeypatch.setattr(intake.vic_tools, "run_tool", lambda n, a: "{}")

    assert intake._ask_vic("s", [{"role": "user", "content": "x"}]) == "Done."
    assert [c["tool_choice"]["type"] for c in fake.calls] == [
        "auto", "auto", "none"]
    assert all(c["tools"] == intake.vic_tools.TOOLS for c in fake.calls)


def test_loop_stops_offering_tools_when_out_of_time(monkeypatch):
    fake = _FakeMessages([
        SimpleNamespace(stop_reason="end_turn", content=[_text("Done.")]),
    ])
    monkeypatch.setattr(intake, "_client", SimpleNamespace(messages=fake))
    clock = iter([0.0, 100.0])
    monkeypatch.setattr(intake.time, "monotonic", lambda: next(clock))

    intake._ask_vic("s", [{"role": "user", "content": "x"}])
    assert fake.calls[0]["tool_choice"]["type"] == "none"
