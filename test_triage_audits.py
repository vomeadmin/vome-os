"""
Claude Code audits: every audit records whether the agents' card agreed
with Sam, so agreement can be counted per category (the /audit skill).
"""

from __future__ import annotations

import triage_cases

DEEP_CARD = {
    "category": "clear_bug", "lane": "agent_fix",
    "code_check": {"verdict": "bug", "where": "core:x.py:10"},
    "data_check": {"needed": "none"},
    "first_pass": {"category": "how_to", "lane": "answer",
                   "confidence": "medium", "code_check": "works_as_designed"},
    "deep": {"status": "ok"},
}


def test_an_agreeing_audit_is_recorded_as_agreed():
    audit = triage_cases.card_vs_truth(
        {"category": "clear_bug", "lane": "agent_fix",
         "code_check.verdict": "bug"}, DEEP_CARD)
    assert audit["had_card"] and audit["agreed"]
    assert audit["misses"] == []
    assert audit["card_said"]["code_check.verdict"] == "bug"
    # The deep pass corrected the first pass, which missed.
    assert audit["first_agreed"] is False
    assert audit["first_said"]["category"] == "how_to"


def test_a_miss_names_the_field():
    audit = triage_cases.card_vs_truth(
        {"category": "clear_bug", "data_check.needed": "required"}, DEEP_CARD)
    assert audit["agreed"] is False
    assert any("data_check.needed" in m for m in audit["misses"])
    # The first pass never stored a data check: judged on category only.
    assert audit["first_agreed"] is False


def test_a_card_with_no_deep_pass_is_its_own_first_pass():
    card = {"category": "how_to", "lane": "answer",
            "code_check": {"verdict": "works_as_designed"}}
    audit = triage_cases.card_vs_truth(
        {"category": "how_to", "code_check.verdict": "works_as_designed"},
        card)
    assert audit["agreed"] and audit["first_agreed"]


def test_no_card_is_not_counted():
    audit = triage_cases.card_vs_truth({"category": "how_to"}, {})
    assert audit == {"had_card": False, "category": "how_to"}
    assert triage_cases.agreement([{"audit": audit}]) == {}


def test_agreement_groups_by_settled_category():
    rows = [
        {"audit": {"had_card": True, "category": "clear_bug",
                   "agreed": True, "first_agreed": False}},
        {"audit": {"had_card": True, "category": "clear_bug",
                   "agreed": False, "first_agreed": False}},
        {"audit": {"had_card": True, "category": "how_to",
                   "agreed": True, "first_agreed": True}},
        {"audit": None},
    ]
    assert triage_cases.agreement(rows) == {
        "clear_bug": {"audited": 2, "card_agreed": 1, "first_agreed": 0},
        "how_to": {"audited": 1, "card_agreed": 1, "first_agreed": 1},
    }


# ---------------------------------------------------------------------------
# Which tickets to audit, and the weekly trust lines
# ---------------------------------------------------------------------------

def _item(number, card, account="Acme (low, 500 CAD combined ARR)"):
    return {"ticket_number": str(number), "account": account, "card": card}


def _unsampled(start=1000):
    n = start
    while triage_cases.sampled(str(n)):
        n += 1
    return n


def test_audit_reasons_name_what_makes_a_card_risky():
    card = {"category": "clear_bug", "confidence": "low",
            "code_check": {"verdict": "unclear"},
            "data_check": {"needed": "required"},
            "deep": {"verdict": "corrected"}}
    reasons = triage_cases.audit_reasons(
        _item(_unsampled(), card, "Big (very-high, 7000 CAD combined ARR)"))
    assert reasons[0] == "data check required"
    assert "deep pass corrected the first card" in reasons
    assert any("unsure" in r for r in reasons)
    assert "4000+ ARR account" in reasons


def test_a_confident_card_is_only_picked_by_the_sample():
    calm = {"category": "user_education", "confidence": "high",
            "code_check": {"verdict": "works_as_designed"},
            "data_check": {"needed": "none"}, "deep": {"verdict": "confirmed"}}
    assert triage_cases.audit_reasons(_item(_unsampled(), calm)) == []
    n = 1000
    while not triage_cases.sampled(str(n)):
        n += 1
    assert triage_cases.audit_reasons(_item(n, calm)) == [
        "random sample (keeps the agreement number honest)"]
    assert triage_cases.audit_reasons(_item(n, {})) == []


def test_sampling_is_stable_and_about_one_in_ten():
    picks = [triage_cases.sampled(str(n)) for n in range(9000, 11000)]
    assert picks == [triage_cases.sampled(str(n)) for n in range(9000, 11000)]
    assert 140 < sum(picks) < 260


def test_audit_picks_skip_audited_tickets_and_rank_by_reasons(monkeypatch):
    monkeypatch.setattr(triage_cases, "audited_tickets",
                        lambda numbers: {"2"})
    one = {"category": "clear_bug", "confidence": "low"}
    many = {"category": "needs_human", "confidence": "low",
            "data_check": {"needed": "required"}}
    picks = triage_cases.audit_picks([
        _item(_unsampled(), one), _item(2, many), _item(_unsampled(5000), many)])
    assert [p["category"] for p in picks] == ["needs_human", "clear_bug"]
    assert picks[0]["run"].startswith("/audit ")
    assert all(p["ticket"] != "#2" for p in picks)


def test_trust_lines_read_per_category():
    lines = triage_cases.trust_lines(
        {"clear_bug": {"audited": 4, "card_agreed": 3, "first_agreed": 1}})
    assert lines == ["`clear_bug` audited 4 · card right 75% · "
                     "first pass right 25%"]
    assert "No audits" in triage_cases.trust_lines({})[0]


def test_the_weekly_report_carries_the_trust_lines():
    import autonomy_report

    text = autonomy_report.render([], [], trust=["`how_to` audited 2"])
    assert "Triage cards vs your audits" in text
    assert "`how_to` audited 2" in text
    assert "Triage cards" not in autonomy_report.render([], [])


def test_a_big_account_waiting_on_the_client_is_not_picked_for_its_arr():
    card = {"category": "awaiting_client", "confidence": "high"}
    big = "Big (very-high, 7000 CAD combined ARR)"
    assert triage_cases.audit_reasons(_item(_unsampled(), card, big)) == []
