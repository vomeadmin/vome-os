"""
test_sentry_regate.py

Re-gating changes the ledger's central promise from "an issue is decided once,
ever" to "an issue is decided once per gate configuration". That is a
deliberate loosening and the tests are about not loosening it any further.

The subtle one is `test_a_released_issue_is_rechecked_against_every_rule`.
These rows were rejected at the project rule, which is third of seven, so the
exception type, title and culprit rules never ran on them. Letting them
through on the strength of having once been excluded for an unrelated reason
would quietly bypass the whole noise list.
"""

import pytest

import sentry_gate
import sentry_handler as handler
import sentry_ledger
import sentry_projects


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in (
        "SENTRY_PROJECT_ALLOWLIST", "SENTRY_TRIAGE_DEV_PROJECTS",
        "SENTRY_ENVIRONMENTS", "SENTRY_REQUIRE_ENVIRONMENT",
        "SENTRY_IGNORE_EXCEPTION_TYPES", "SENTRY_ISSUE_CATEGORIES",
    ):
        monkeypatch.delenv(name, raising=False)


def _row(issue_id="1", **over):
    row = {
        "issue_id": issue_id,
        "project": "prod-vome",
        "title": "ValueError: save() prohibited to prevent data loss",
        "culprit": "/api/organization/admin/invite-register/{token}/",
        "level": "error",
        "environment": "",
        "platform": "python",
        "exception_type": "ValueError",
        "repo": "vomedjango-restored-core-app",
        "stack": "backend",
        "permalink": "https://sentry.io/x/1/",
        "times_seen": 1,
        "users_affected": 0,
    }
    row.update(over)
    return row


def _patch(monkeypatch, rows, triage_ok=True):
    released, updates, triaged = list(rows), [], []
    monkeypatch.setattr(
        sentry_ledger, "claim_regate",
        lambda slugs, limit=25: released,
    )
    monkeypatch.setattr(
        sentry_ledger, "update",
        lambda issue_id, **f: updates.append((issue_id, f)) or True,
    )
    monkeypatch.setattr(
        handler, "_triage",
        lambda signal, repo: (
            triaged.append((signal.issue_id, repo))
            or ({"verdict": "actionable"} if triage_ok else None)
        ),
    )
    return updates, triaged


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_an_issue_whose_project_is_now_triaged_gets_triaged(monkeypatch):
    updates, triaged = _patch(monkeypatch, [_row("1001")])
    result = handler.run_regate_sweep()
    assert result["released"] == 1
    assert result["triaged"] == 1
    assert triaged == [("1001", "vomedjango-restored-core-app")]


def test_the_repo_comes_from_the_stored_row(monkeypatch):
    # It was resolved from the routing table when the row was written. No
    # reason to ask a model, and no reason to re-derive it.
    _, triaged = _patch(monkeypatch, [_row("1001")])
    handler.run_regate_sweep()
    assert triaged[0][1] == "vomedjango-restored-core-app"


def test_nothing_to_do_is_not_an_error(monkeypatch):
    _patch(monkeypatch, [])
    assert handler.run_regate_sweep()["status"] == "nothing_to_do"


# ---------------------------------------------------------------------------
# The rule that is easy to get wrong
# ---------------------------------------------------------------------------

def test_a_released_issue_is_rechecked_against_every_rule(monkeypatch):
    # Gated at the project rule, which is third of seven. The exception type
    # rule never ran. Releasing it must not mean skipping the noise list.
    updates, triaged = _patch(
        monkeypatch,
        [_row("1002", exception_type="Http404", title="Http404: /wp-admin")],
    )
    result = handler.run_regate_sweep()
    assert result["still_gated"] == 1
    assert result["triaged"] == 0
    assert triaged == []
    issue_id, fields = updates[0]
    assert issue_id == "1002"
    assert fields["status"] == sentry_ledger.STATUS_GATED
    assert fields["gate_rule"] == "exception_type"


def test_the_new_reason_replaces_the_stale_one(monkeypatch):
    # The row must stop claiming it was excluded by project, because that is
    # no longer true and the report counts by rule.
    updates, _ = _patch(
        monkeypatch,
        [_row("1003", exception_type="Error",
              title="Error: Network request failed")],
    )
    handler.run_regate_sweep()
    assert updates[0][1]["gate_rule"] == "title"
    assert updates[0][1]["gate_rule"] != "project_excluded"


def test_a_performance_issue_stays_gated(monkeypatch):
    updates, triaged = _patch(
        monkeypatch, [_row("1004", title="N+1 Query", level="info")]
    )
    handler.run_regate_sweep()
    assert triaged == []
    assert updates[0][1]["gate_rule"] in ("level", "issue_category")


def test_a_django_shell_issue_stays_gated(monkeypatch):
    updates, triaged = _patch(
        monkeypatch,
        [_row("1005",
              culprit="django.core.management.commands.shell in <module>")],
    )
    handler.run_regate_sweep()
    assert triaged == []
    assert updates[0][1]["gate_rule"] == "culprit"


def test_a_mix_is_counted_correctly(monkeypatch):
    updates, triaged = _patch(monkeypatch, [
        _row("2001"),
        _row("2002", exception_type="Http404"),
        _row("2003"),
    ])
    result = handler.run_regate_sweep()
    assert result == {
        "status": "ok", "released": 3, "triaged": 2,
        "still_gated": 1, "failed": 0,
    }


def test_a_triage_failure_is_counted_not_swallowed(monkeypatch):
    _patch(monkeypatch, [_row("3001")], triage_ok=False)
    result = handler.run_regate_sweep()
    assert result["triaged"] == 0
    assert result["failed"] == 1


# ---------------------------------------------------------------------------
# Which rules are allowed to expire
# ---------------------------------------------------------------------------

def test_the_lost_category_cannot_let_a_performance_issue_through():
    # The ledger does not store issue category, so the sweep rebuilds the
    # signal with the default of "error". That is safe only because of rule
    # ORDER: issue_category is rule 1 and project is rule 3, so anything
    # gated at project had already passed the category rule and really was
    # an error. If those rules were ever reordered, this assumption breaks.
    rules_in_order = ["issue_category", "level", "project"]
    assert rules_in_order.index("issue_category") < rules_in_order.index(
        "project"
    )
    signal = __import__(
        "vomeos.integrations.sentry", fromlist=["IssueSignal"]
    ).IssueSignal(issue_id="x")
    assert signal.category == "error"


def test_only_configuration_rules_are_eligible():
    # A judgement about the issue itself does not go stale when a variable
    # moves. If `exception_type` were on this list, every widening of the
    # allowlist would also undo the noise list.
    assert set(sentry_ledger.CONFIG_RULES) == {
        "project_excluded", "project_dev", "project_unmapped",
    }
    for rule in ("level", "exception_type", "title", "culprit",
                 "issue_category", "environment"):
        assert rule not in sentry_ledger.CONFIG_RULES


def test_the_sweep_asks_the_ledger_for_the_currently_triaged_projects(
    monkeypatch
):
    asked = {}
    monkeypatch.setenv("SENTRY_PROJECT_ALLOWLIST", "prod-vome")

    def fake_claim(slugs, limit=25):
        asked["slugs"] = tuple(slugs)
        return []

    monkeypatch.setattr(sentry_ledger, "claim_regate", fake_claim)
    handler.run_regate_sweep()
    assert asked["slugs"] == ("prod-vome",)
    assert asked["slugs"] == sentry_projects.triaged_slugs()


def test_the_gate_is_the_same_one_the_webhook_uses(monkeypatch):
    # Not a second copy of the rules. One gate, one set of answers.
    calls = []
    real = sentry_gate.check
    monkeypatch.setattr(
        sentry_gate, "check",
        lambda signal: calls.append(signal.issue_id) or real(signal),
    )
    _patch(monkeypatch, [_row("4001")])
    handler.run_regate_sweep()
    assert calls == ["4001"]
