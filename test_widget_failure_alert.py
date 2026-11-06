"""A widget ticket that fails to create posts once in #support-sprints
(Nov 3 to 5 2026: every widget ticket failed for three days unseen)."""

import intake
import sprint_thread


def test_first_failure_posts_then_waits_an_hour(monkeypatch):
    posts = []
    monkeypatch.setattr(sprint_thread, "_post", lambda text, *a, **k: posts.append(text))
    monkeypatch.setattr(intake, "_last_failure_alert", 0.0)
    ctx = {"user_email": "a@org.ca", "org_name": "Org"}
    assert intake._alert_ticket_failure(ctx) is True
    assert intake._alert_ticket_failure(ctx) is False
    assert len(posts) == 1 and "a@org.ca" in posts[0]


def test_a_slack_error_never_breaks_the_reply(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("slack down")
    monkeypatch.setattr(sprint_thread, "_post", boom)
    monkeypatch.setattr(intake, "_last_failure_alert", 0.0)
    assert intake._alert_ticket_failure({}) is False
