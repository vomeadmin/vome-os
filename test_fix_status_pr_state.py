"""fix_status reads the PR, not only the ledger (2026-11-06: four merged,
live fixes were reported as "PR open, waiting on your merge")."""

import ticket_fix
import sprint_tools


def test_merged_pr_is_not_reported_as_open(monkeypatch):
    monkeypatch.setattr(ticket_fix, "_pr_state", lambda url: ("merged", "web", "a7372ce78"))
    out = sprint_tools._opened_pr_summary("https://github.com/vomeadmin/vome-react/pull/841")
    assert "merged (web@a7372ce78)" in out
    assert "waiting on your merge" not in out


def test_closed_pr(monkeypatch):
    monkeypatch.setattr(ticket_fix, "_pr_state", lambda url: ("closed", "web", ""))
    assert "closed without merging" in sprint_tools._opened_pr_summary("u")


def test_unreadable_pr_falls_back_to_open(monkeypatch):
    def boom(url):
        raise RuntimeError("403")
    monkeypatch.setattr(ticket_fix, "_pr_state", boom)
    assert "PR open" in sprint_tools._opened_pr_summary("u")
