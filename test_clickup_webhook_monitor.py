"""
test_clickup_webhook_monitor.py

The monitor's whole job is to be right about whether delivery is on, so the
cases that matter are the ones where it could quietly lie: an unreachable API
reported as healthy, or a suspended webhook reported as recovered when the
reactivate actually failed.
"""

import clickup_webhook_monitor as mon


def _hook(status="active", fail_count=0, endpoint=None):
    return {
        "id": "hook-1",
        "endpoint": endpoint or mon.ENDPOINT,
        "events": ["taskStatusUpdated"],
        "health": {"status": status, "fail_count": fail_count},
    }


def _patch(monkeypatch, hooks, reactivate=None, tasks=None):
    alerts = []
    monkeypatch.setattr(mon, "CLICKUP_API_TOKEN", "tok")
    monkeypatch.setattr(mon, "CLICKUP_TEAM_ID", "123")
    monkeypatch.setattr(mon, "_list_hooks", lambda: hooks())
    monkeypatch.setattr(mon, "_tasks_in_on_prod", lambda: tasks or [])
    monkeypatch.setattr(mon, "_alert", lambda text: alerts.append(text))
    if reactivate is not None:
        monkeypatch.setattr(mon, "_reactivate", reactivate)
    return alerts


def test_healthy_webhook_is_silent(monkeypatch):
    alerts = _patch(monkeypatch, lambda: [_hook()])
    result = mon.check_clickup_webhook_health()
    assert result["status"] == "healthy"
    # A monitor that posts on every healthy pass gets muted, and a muted
    # monitor is the same as no monitor.
    assert alerts == []


def test_suspended_webhook_is_reactivated_and_reported(monkeypatch):
    calls = []
    alerts = _patch(
        monkeypatch,
        lambda: [_hook(status="suspended", fail_count=100)],
        reactivate=lambda h: calls.append(h["id"]),
        tasks=[{"id": "t1", "name": "Wellspring bug", "url": "http://x/t1"}],
    )
    result = mon.check_clickup_webhook_health()
    assert result["status"] == "reactivated"
    assert calls == ["hook-1"]
    # Reactivating does not replay, so the alert has to name the tasks that
    # still need a manual re-fire or the recovery is only half done.
    assert "Wellspring bug" in alerts[0]
    assert "does not replay" in alerts[0]


def test_failed_reactivate_is_not_reported_as_recovered(monkeypatch):
    def boom(_hook):
        raise RuntimeError("clickup said no")

    alerts = _patch(
        monkeypatch,
        lambda: [_hook(status="suspended", fail_count=100)],
        reactivate=boom,
    )
    result = mon.check_clickup_webhook_health()
    assert result["status"] == "suspended"
    assert "could not be reactivated" in alerts[0]


def test_climbing_fail_count_warns_before_the_cliff(monkeypatch):
    alerts = _patch(
        monkeypatch, lambda: [_hook(status="active", fail_count=12)]
    )
    result = mon.check_clickup_webhook_health()
    # Still active, so nothing is broken yet. The point is to say so while
    # there is still time to look, instead of at fail_count 100.
    assert result["status"] == "degraded"
    assert "12 of 100" in alerts[0]


def test_missing_subscription_is_distinct_from_suspended(monkeypatch):
    alerts = _patch(
        monkeypatch, lambda: [_hook(endpoint="https://somewhere/else")]
    )
    result = mon.check_clickup_webhook_health()
    assert result["status"] == "missing"
    # Different recovery path: --recreate, not a reactivate.
    assert "--recreate" in alerts[0]


def test_unreachable_api_is_never_reported_as_healthy(monkeypatch):
    def boom():
        raise RuntimeError("connection reset")

    alerts = _patch(monkeypatch, boom)
    result = mon.check_clickup_webhook_health()
    assert result["status"] == "unreachable"
    assert "unknown" in alerts[0]
