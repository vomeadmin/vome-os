"""Tests for the weekly security audit (security_audit.py)."""

import types
from datetime import datetime

import pytest

import security_audit as sa
from vomeos import onboarding, registry


def _finding(fp, severity="high", title="A gap"):
    return {"fingerprint": fp, "title": title, "severity_guess": severity,
            "attacker": "an admin of another org",
            "route": "PATCH /api/x/<pk>/", "where": "core:a.py:10",
            "what_happens": "They change org A's record.",
            "evidence": ["core:a.py:10 queryset is .all()"],
            "expected": "404 outside the caller's org",
            "fix": "filter by institution", "confidence": "high"}


@pytest.fixture
def ledger(monkeypatch):
    """An in-memory ledger in place of the database."""
    state = {"known": {}, "runs": {}}
    monkeypatch.setattr(sa.store, "ensure_table", lambda name: True)
    monkeypatch.setattr(sa, "_known", lambda area: [
        {"fingerprint": fp, "title": t}
        for fp, (a, t) in state["known"].items() if a == area])
    monkeypatch.setattr(sa, "_is_known", lambda fp: fp in state["known"])
    monkeypatch.setattr(sa, "_remember", lambda f, area, ts: state[
        "known"].__setitem__(f["fingerprint"], (area, f["title"])))
    monkeypatch.setattr(sa, "_record_run", lambda area, result: state[
        "runs"].__setitem__(area, result))
    return state


@pytest.fixture
def world(monkeypatch, ledger):
    """A fake auditor, Slack and bug report pipeline."""
    calls = {"agent": [], "posts": [], "handed": []}
    card = {"verdict": "findings", "summary": "Read the area.",
            "findings": [_finding("core:a.py:A:unscoped")]}

    def run_agent(name, context, **kwargs):
        calls["agent"].append((name, context))
        return types.SimpleNamespace(ok=True, data=card, why=lambda: "")

    import vomeos
    monkeypatch.setattr(vomeos, "run_agent", run_agent)
    monkeypatch.setattr(sa, "_post", lambda text: (
        calls["posts"].append(text) or f"17930.{len(calls['posts'])}"))
    import bug_reports
    monkeypatch.setattr(bug_reports, "handle_message",
                        lambda event: calls["handed"].append(event))
    return calls, card, ledger


def test_never_audited_areas_go_first_in_order(monkeypatch):
    monkeypatch.setattr(sa.store, "ensure_table", lambda name: True)
    monkeypatch.setattr(sa.store, "query", lambda *a, **k: [
        {"area": "core-auth", "last_run_at": datetime(2026, 10, 1)}])
    assert sa.pick_area().key == "core-admin"


def test_then_the_area_audited_longest_ago(monkeypatch):
    rows = [{"area": a.key, "last_run_at": datetime(2026, 10, 2)}
            for a in sa.AREAS]
    rows[3]["last_run_at"] = datetime(2026, 9, 1)
    monkeypatch.setattr(sa.store, "ensure_table", lambda name: True)
    monkeypatch.setattr(sa.store, "query", lambda *a, **k: rows)
    assert sa.pick_area().key == sa.AREAS[3].key


def test_new_findings_drop_known_duplicates_and_untitled(ledger):
    ledger["known"]["old"] = ("core-auth", "Old")
    card = {"findings": [
        _finding("old"),
        _finding("low-one", severity="low"),
        _finding("crit", severity="critical"),
        _finding("crit", severity="critical"),
        _finding("no-title", title=""),
        {"title": "no fingerprint"},
    ]}
    assert [f["fingerprint"] for f in sa.new_findings(card)] == [
        "crit", "low-one"]


def test_at_most_six_posts_a_run(ledger):
    card = {"findings": [_finding(f"fp{i}") for i in range(9)]}
    assert len(sa.new_findings(card)) == sa.MAX_POSTS


def test_a_finding_is_posted_and_handed_to_bug_reports(world):
    calls, card, ledger = world
    out = sa.audit_area("core-opportunity")

    assert out["posted"] == ["core:a.py:A:unscoped"]
    assert calls["agent"][0][0] == "engineering.security_auditor"
    text = calls["posts"][0]
    assert text.startswith("[Security audit] A gap")
    assert "core:a.py:10" in text
    assert "—" not in text
    assert calls["handed"] == [{"ts": "17930.1", "text": text}]
    assert ledger["known"]["core:a.py:A:unscoped"][0] == "core-opportunity"
    assert ledger["runs"]["core-opportunity"]["posted"] == out["posted"]


def test_a_second_run_does_not_repost(world):
    calls, card, ledger = world
    sa.audit_area("core-opportunity")
    out = sa.audit_area("core-opportunity")
    assert out["posted"] == []
    assert len(calls["posts"]) == 1


def test_the_auditor_is_told_what_was_already_reported(world):
    calls, card, ledger = world
    ledger["known"]["earlier"] = ("core-auth", "An earlier finding")
    sa.audit_area("core-auth")
    told = calls["agent"][0][1]["already_reported"]
    assert "Google mobile sign-in trusts the email" in told
    assert "Kiosk login skips 2FA and SSO: deliberate" in told
    assert "An earlier finding (earlier)" in told


def test_dry_run_posts_and_records_nothing(world, capsys):
    calls, card, ledger = world
    out = sa.audit_area("core-forms", dry_run=True)
    assert out["new"] == 1
    assert calls["posts"] == [] and calls["handed"] == []
    assert ledger["known"] == {} and ledger["runs"] == {}
    assert "[Security audit] A gap" in capsys.readouterr().out


def test_a_failed_run_is_logged_and_recorded(world, monkeypatch):
    calls, card, ledger = world
    import vomeos
    monkeypatch.setattr(vomeos, "run_agent", lambda *a, **k: (
        types.SimpleNamespace(ok=False, data=None, why=lambda: "timeout")))
    logged = []
    monkeypatch.setattr(sa, "_log_failure",
                        lambda area, why: logged.append((area.key, why)))
    out = sa.audit_area("web")
    assert out["status"] == "error"
    assert logged == [("web", "timeout")]
    assert ledger["runs"]["web"]["status"] == "error"
    assert calls["posts"] == []


def test_switch_off_skips_the_run(monkeypatch):
    monkeypatch.setenv("SECURITY_AUDIT_ENABLED", "false")
    assert sa.run_weekly_audit()["status"] == "skipped"


def test_every_area_key_is_unique_and_has_paths():
    keys = [a.key for a in sa.AREAS]
    assert len(keys) == len(set(keys))
    assert all(a.starting_paths for a in sa.AREAS)
    assert set(sa.PILOT_REPORTED) <= set(keys)


def test_the_auditor_is_ready_to_schedule():
    registry.reset_cache()
    manifest = registry.get_agent("engineering.security_auditor")
    blocking = [f for f in onboarding.check_agent(manifest)
                if f.severity == onboarding.BLOCKING]
    assert blocking == []
