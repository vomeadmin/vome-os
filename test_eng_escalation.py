"""eng_escalation: the daily raise-only pass (Sam, 2026-10-27)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

import eng_escalation as esc  # noqa: E402
import eng_priority as ep  # noqa: E402
import sprint_thread  # noqa: E402
import ticket_routing  # noqa: E402

NOW = datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc)  # a Wednesday


def _task(priority, days=1, status="queued", arr=0, ticket=True,
          tid="86t", updated_days=0):
    fields = [{"id": sprint_thread.FIELD_ZOHO_TICKET_LINK,
               "value": "https://zoho/1" if ticket else None}]
    if arr:
        fields.append({"id": ticket_routing.FIELD_COMBINED_ARR,
                       "value": str(arr)})
    return {"id": tid, "name": "Sarasota -- archived fields",
            "url": f"https://app.clickup.com/t/{tid}",
            "status": {"status": status},
            "priority": {"id": str(priority)} if priority else None,
            "custom_fields": fields,
            "date_created": str(int((NOW - timedelta(days=days))
                                    .timestamp() * 1000)),
            "date_updated": str(int((NOW - timedelta(days=updated_days))
                                    .timestamp() * 1000))}


def test_business_days_skip_weekends():
    friday = datetime(2026, 10, 23, tzinfo=timezone.utc)
    assert esc.business_days(friday, friday + timedelta(days=3)) == 1
    assert esc.business_days(friday, friday + timedelta(days=14)) == 10


def test_a_very_high_account_waiting_a_week_becomes_urgent():
    out = esc.decide(_task(ep.HIGH, days=7, arr=7000), [], NOW, set())
    assert out[:2] == ("very_high_wait", ep.URGENT)
    assert "7,000 CAD" in out[2] and "7 days" in out[2]
    assert esc.decide(_task(ep.HIGH, days=6, arr=7000), [], NOW,
                      set()) is None
    assert esc.decide(_task(ep.HIGH, days=30, arr=3900), [], NOW,
                      set()) is None


def test_normal_waiting_ten_business_days_becomes_high():
    assert esc.decide(_task(ep.NORMAL, days=14), [], NOW, set())[:2] == (
        "normal_wait", ep.HIGH)
    assert esc.decide(_task(ep.NORMAL, days=11), [], NOW, set()) is None


def test_a_ticket_that_came_back_after_a_fix_goes_up_one_level():
    history = ["queued", "on prod", "queued"]
    assert esc.decide(_task(ep.HIGH), history, NOW, set())[:2] == (
        "came_back", ep.URGENT)
    assert esc.decide(_task(ep.NORMAL), ["closed"], NOW, set())[:2] == (
        "came_back", ep.HIGH)


def test_a_very_high_account_is_never_below_high():
    assert esc.decide(_task(ep.NORMAL, arr=5000), [], NOW, set())[:2] == (
        "very_high_floor", ep.HIGH)


def test_what_is_never_raised():
    # Low (features), blank (Sam places), urgent (nowhere to go), on dev
    # (fixed), not a client ticket, and a rule already applied once.
    old = dict(days=60, arr=9000)
    for task in (_task(ep.LOW, **old), _task(None, **old),
                 _task(ep.URGENT, **old),
                 _task(ep.HIGH, status="on dev", **old),
                 _task(ep.HIGH, ticket=False, **old)):
        assert esc.decide(task, ["on prod"], NOW, set()) is None
    assert esc.decide(_task(ep.HIGH, **old), [], NOW,
                      {"very_high_wait"}) is None


def test_a_ticket_named_with_its_number_counts_without_the_link():
    task = _task(ep.NORMAL, days=20, ticket=False)
    task["name"] = "[#9501] Sarasota -- fields"
    assert esc.is_ticket(task)


def test_run_raises_comments_and_records_once(monkeypatch):
    import weekly_engineering_report as report

    puts, recorded = [], []
    tasks = [_task(ep.HIGH, days=9, arr=7000, tid="86a"),
             _task(ep.LOW, days=90, tid="86b")]
    monkeypatch.setattr(report, "_fetch_one_assignee",
                        lambda cid: tasks if cid == "49257687" else [])
    monkeypatch.setattr(esc, "_histories", lambda ids: {})
    monkeypatch.setattr(esc, "_applied", lambda ids: {})
    monkeypatch.setattr(esc, "_raise",
                        lambda tid, to, why: puts.append((tid, to, why))
                        or True)
    monkeypatch.setattr(esc, "_record",
                        lambda *a: recorded.append(a[:4]))
    out = esc.run(NOW)
    assert list(out) == ["86a"] and out["86a"]["to"] == ep.URGENT
    assert puts[0][:2] == ("86a", ep.URGENT)
    assert recorded == [("86a", "very_high_wait", ep.HIGH, ep.URGENT)]


def test_a_dry_run_changes_nothing(monkeypatch):
    import weekly_engineering_report as report

    monkeypatch.setattr(report, "_fetch_one_assignee",
                        lambda cid: [_task(ep.NORMAL, days=30, tid="86a")])
    monkeypatch.setattr(esc, "_histories", lambda ids: {})
    monkeypatch.setattr(esc, "_applied", lambda ids: {})
    monkeypatch.setattr(esc, "_raise", lambda *a: (_ for _ in ()).throw(
        AssertionError("dry run wrote to ClickUp")))
    assert esc.run(NOW, dry_run=True)["86a"]["to"] == ep.HIGH


def test_week_review(monkeypatch):
    monkeypatch.setattr(esc.store, "ensure_table", lambda t: True)
    monkeypatch.setattr(esc.store, "query", lambda sql, params=None: [
        {"task_id": "86a", "to_priority": 1, "engineer": "onlyg",
         "task_name": "[#9501] Sarasota", "reason": "very_high_wait"}])
    text = esc.week_review([
        _task(None, tid="86b"),
        _task(ep.NORMAL, status="in progress", tid="86c", updated_days=20),
        _task(ep.HIGH, status="in progress", tid="86d", updated_days=2)],
        NOW)
    assert "Raised by the daily pass (1):" in text
    assert "to urgent (very high wait, onlyg)" in text
    assert "No priority yet (1)" in text and "/86b|" in text
    assert "no update in 14+ days (1)" in text and "/86c|" in text
    assert "/86d|" not in text
    assert "\N{EM DASH}" not in text
