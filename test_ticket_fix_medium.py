"""The #9501 review (Sam, 2026-10-22): what stopped a customer bug from
becoming an agent PR, and the medium risk route that replaced it.

#9501's only fix attempt went to a resolution card whose fix was already
live; the next day's real follow-up could not try again; the fix author was
told only "master"; and the incident matcher joined the ticket to an
unrelated one on another module. Medium risk fixes now open a PR for the
engineer who owns the repo, who is named reviewer and pinged on Slack.
"""

import pytest

import sentry_fix
import shadow_triage
import sprint_store
import ticket_fix

MEDIUM = {"code_check": {"verdict": "bug", "confidence": "medium",
                         "fix_risk": "medium",
                         "fix": "set removed steps aside",
                         "where": "core:sequence_app/deletion/entity.py:79",
                         "finding": "removed steps still count"},
          "live_question": "Removed fields still block the delete"}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_medium_risk_is_the_agents_to_fix():
    assert ticket_fix.should_attempt(MEDIUM)[0] is True


def test_high_risk_never_is_not_even_on_sams_word():
    card = {"code_check": {**MEDIUM["code_check"], "fix_risk": "high"}}
    assert ticket_fix.should_attempt(card)[0] is False
    assert ticket_fix.should_attempt(card, sam_approved=True)[0] is False


# ---------------------------------------------------------------------------
# One attempt per customer message
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger(monkeypatch):
    rows = []
    monkeypatch.setattr(ticket_fix.store, "ensure_table", lambda t: True)
    monkeypatch.setattr(ticket_fix.store, "query", lambda sql, p=None: rows)
    return rows


def test_never_attempted_is_free(ledger):
    assert ticket_fix._attempted("T1", "M2") is False


def test_a_new_customer_message_earns_a_new_attempt(ledger):
    ledger.append({"outcome": "declined", "thread_id": "M1"})
    assert ticket_fix._attempted("T1", "M2") is False


def test_the_same_message_does_not(ledger):
    ledger.append({"outcome": "declined", "thread_id": "M1"})
    assert ticket_fix._attempted("T1", "M1") is True


def test_an_open_pr_is_final_whatever_the_customer_writes(ledger):
    ledger.append({"outcome": "pr_opened", "thread_id": "M1"})
    assert ticket_fix._attempted("T1", "M2") is True


def test_an_old_row_with_no_message_still_counts(ledger):
    ledger.append({"outcome": "declined", "thread_id": None})
    assert ticket_fix._attempted("T1", "M2") is True


# ---------------------------------------------------------------------------
# Proposing: branch wording, reviewer, Merge Owner, the Slack DM
# ---------------------------------------------------------------------------

@pytest.fixture
def proposing(monkeypatch):
    import sentry_code
    import zoho_actions

    seen = {"filed": [], "pings": [], "context": {}, "pr_context": {}}
    monkeypatch.setenv("TICKET_FIX_ENABLED", "true")
    monkeypatch.setattr(ticket_fix, "_attempted", lambda t, th="": False)
    monkeypatch.setattr(ticket_fix, "_opened_today", lambda: 0)
    monkeypatch.setattr(ticket_fix, "_record", lambda *a, **k: None)
    monkeypatch.setattr(ticket_fix, "_pr_target", lambda repo: "development")
    monkeypatch.setattr(ticket_fix, "extra_context", lambda *a: "")
    monkeypatch.setattr(sentry_code, "gather",
                        lambda *a, **k: {"files": "src"})

    def propose(analysis, context, allow_risk=("low",)):
        seen["context"] = context
        seen["allow_risk"] = allow_risk
        return {"attempted": True, "diff": "d", "rationale": "why",
                "risks": "deletes removed steps"}

    def open_pr(issue, proposal, context):
        seen["issue"] = issue
        seen["pr_context"] = context
        return {"opened": True, "url": "https://bb/pr/9",
                "reviewer_added": True}

    monkeypatch.setattr(sentry_fix, "propose", propose)
    monkeypatch.setattr(sentry_fix, "open_pull_request", open_pr)
    monkeypatch.setattr(ticket_fix, "_file_tasks",
                        lambda *a: seen["filed"].append(a) or ("P1", "C1"))
    monkeypatch.setattr(zoho_actions, "mark_agent_fix",
                        lambda *a, **k: "")
    monkeypatch.setattr(ticket_fix, "_ping_reviewer",
                        lambda *a: seen["pings"].append(a) or "ts1")
    return seen


def test_the_fix_author_is_told_where_the_pr_goes(proposing):
    ticket_fix.propose_for_ticket("T1", "9501", "Sarasota", MEDIUM, "z")
    branch = proposing["context"]["branch"]
    assert "development" in branch and "never" in branch
    assert "medium" in proposing["allow_risk"]


def test_what_broke_is_the_whole_question(proposing):
    # #9637: clipped at 60, the PR's "What broke" read "...and when h".
    question = ("Why the app crashes when they tap I'm attending, and when "
                "they tap Not attending the shift does not refresh")
    card = {**MEDIUM, "live_question": question}
    ticket_fix.propose_for_ticket("T1", "9637", "X", card, "z")
    assert proposing["issue"]["triage_summary"] == question


def test_a_medium_backend_fix_goes_to_onlyg(proposing):
    out = ticket_fix.propose_for_ticket("T1", "9501", "Sarasota", MEDIUM,
                                        "z", thread_id="M2")
    assert out["outcome"] == "pr_opened" and out["reviewer"] == "onlyg"
    assert proposing["pr_context"]["reviewer"]["github"] == "Onlynfk"
    # Merge Owner is the engineer, not Sam.
    assert proposing["filed"][0][-1] == 49257687
    assert proposing["pings"] and proposing["pings"][0][0] == "onlyg"


def test_a_medium_web_fix_goes_to_sanjay(proposing):
    card = {**MEDIUM, "code_check": {
        **MEDIUM["code_check"], "where": "web:src/views/org/x/Y.js:10"}}
    out = ticket_fix.propose_for_ticket("T1", "9600", "X", card, "z")
    assert out["reviewer"] == "sanjay"
    assert proposing["filed"][0][-1] == 4434086


def test_a_low_risk_fix_stays_with_sam_and_pings_nobody(proposing):
    card = {**MEDIUM, "code_check": {**MEDIUM["code_check"],
                                     "fix_risk": "low"}}
    out = ticket_fix.propose_for_ticket("T1", "9501", "Sarasota", card, "z")
    assert out["reviewer"] == "" and not proposing["pings"]
    assert proposing["pr_context"]["reviewer"] == {}
    assert proposing["filed"][0][-1] == ticket_fix.MERGE_OWNER


def test_the_dm_says_what_to_check(monkeypatch):
    import sentry_notify
    import sprint_thread

    sent = []
    monkeypatch.setattr(sentry_notify, "user_id", lambda n: "U0ONLYG")
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, channel="": sent.append((channel, text))
                        or "ts")
    ts = ticket_fix._ping_reviewer(
        "onlyg", "9501", "Sarasota County (very-high, 7000 CAD)", MEDIUM,
        {"rationale": "sets removed steps aside", "risks": "deletes rows"},
        {"url": "https://bb/pr/9", "reviewer_added": False}, "P1", "zoho")
    channel, text = sent[0]
    assert ts == "ts" and channel == "U0ONLYG"
    assert "#9501" in text and "https://bb/pr/9" in text
    assert "deletes rows" in text and "add yourself" in text
    assert "—" not in text


# ---------------------------------------------------------------------------
# Naming the reviewer on the PR
# ---------------------------------------------------------------------------

def test_github_assigns_the_login():
    assert sentry_fix._reviewer_args(
        None, "github", ticket_fix.ENGINEERS["sanjay"]) == \
        {"assignee": "skjangid"}


def test_bitbucket_without_a_member_match_names_nobody():
    class W:
        def member_uuid(self, name, display=""):
            return ""

    assert sentry_fix._reviewer_args(
        W(), "bitbucket", ticket_fix.ENGINEERS["onlyg"]) == {}


def test_bitbucket_names_the_member():
    class W:
        def member_uuid(self, name, display=""):
            return "{abc}" if display == "OnlyGod" else ""

    assert sentry_fix._reviewer_args(
        W(), "bitbucket", ticket_fix.ENGINEERS["onlyg"]) == \
        {"reviewers": ("{abc}",)}


def test_no_reviewer_for_a_low_risk_fix():
    assert sentry_fix._reviewer_args(None, "bitbucket", {}) == {}


def test_bitbucket_puts_reviewers_on_the_create_call(monkeypatch):
    from vomeos.integrations import code_write

    sent = {}
    monkeypatch.setenv("VOMEOS_CODE_WRITE_REPOS", "core")
    monkeypatch.setenv("VOMEOS_PR_ALLOW_PROTECTED_TARGET", "true")
    writer = code_write.BitbucketWriter()
    monkeypatch.setattr(writer, "_call", lambda op, method, path, **kw:
                        sent.update(op=op, path=path, body=kw["json_body"]))
    writer.open_pull_request("core", "agent/sentry/x", "development", "t",
                             "b", reviewers=("{abc}",))
    assert sent["path"].endswith("/core/pullrequests")
    assert sent["body"]["reviewers"] == [{"uuid": "{abc}"}]


# ---------------------------------------------------------------------------
# Incidents: a different module is not the same problem
# ---------------------------------------------------------------------------

def test_module_of():
    assert shadow_triage.module_of(
        "core:sequence_app/deletion/entity.py:79") == "core:sequence_app"
    assert shadow_triage.module_of(
        "web:src/views/org/dashboard/program/Recruitment.js:1141") == \
        "web:src/views/org/dashboard"
    assert shadow_triage.module_of("") == ""


def _siblings(monkeypatch, cards):
    monkeypatch.setattr(sprint_store, "by_ticket_numbers", lambda nums: [
        {"ticket_number": n, "card": {"code_check": {"where": w}}}
        for n, w in cards.items() if n in [str(x) for x in nums]])


def test_9501_is_not_joined_to_9608(monkeypatch):
    _siblings(monkeypatch, {
        "9608": "web:src/views/org/dashboard/program/Recruitment.js:1141"})
    incident = {"key": "tickets:9501", "same_as": ["9608"],
                "sentry_issue": ""}
    assert shadow_triage._drop_other_modules(incident, MEDIUM) == {}


def test_the_same_module_still_joins(monkeypatch):
    _siblings(monkeypatch, {"9620": "core:sequence_app/institution/api.py:1"})
    incident = {"key": "tickets:9501", "same_as": ["9620"],
                "sentry_issue": ""}
    assert shadow_triage._drop_other_modules(
        incident, MEDIUM)["same_as"] == ["9620"]


def test_a_sibling_with_no_location_keeps_the_matchers_call(monkeypatch):
    _siblings(monkeypatch, {"9620": ""})
    incident = {"key": "k", "same_as": ["9620", "9700"], "sentry_issue": ""}
    assert shadow_triage._drop_other_modules(
        incident, MEDIUM)["same_as"] == ["9620", "9700"]
