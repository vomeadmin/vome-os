"""eng_digest: each engineer's morning list from the Priority Queue."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

# The digest reads the queue through weekly_engineering_report, whose slack
# import needs a token (as in test_weekly_engineering_report).
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

import eng_digest as dg  # noqa: E402
import sprint_thread  # noqa: E402

NOW = datetime(2026, 10, 28, 11, 5, tzinfo=timezone.utc)  # a Wednesday


def _task(tid, priority=None, status="queued", days=10, name=None):
    created = NOW - timedelta(days=days)
    return {"id": tid, "name": name or f"Task {tid}",
            "url": f"https://app.clickup.com/t/{tid}",
            "status": {"status": status},
            "priority": {"id": str(priority)} if priority else None,
            "date_created": str(int(created.timestamp() * 1000))}


def test_order_is_priority_then_started_then_oldest():
    tasks = [_task("n_old", 3, days=30), _task("u", 1, days=1),
             _task("h_new", 2, days=1), _task("h_started", 2,
                                              status="in progress", days=1),
             _task("h_old", 2, days=20), _task("blank", None, days=5),
             _task("low", 4), _task("dev", 1, status="on dev"),
             _task("waiting", 1, status="awaiting client response")]
    assert [t["id"] for t in dg.order(tasks)] == [
        "u", "h_started", "h_old", "h_new", "blank", "n_old", "low"]


def test_every_urgent_task_is_listed_even_past_the_top():
    tasks = [_task(f"u{i}", 1) for i in range(7)] + [_task("h", 2)]
    assert len(dg.pick(dg.order(tasks), 5)) == 7


def test_the_message(monkeypatch):
    monkeypatch.setenv("ENG_DIGEST_TOP", "2")
    tasks = [_task("u", 1, status="in progress"), _task("h", 2),
             _task("n", 3, days=0), _task("blank", None),
             _task("d1", 2, status="on dev"),
             *[_task(f"ip{i}", 3, status="in progress") for i in range(3)]]
    text = dg.render("onlyg", tasks, NOW)
    lines = text.split("\n")
    assert lines[0].startswith(":sunrise: <@U0APZ7JUHRD> your Priority "
                               "Queue for Wednesday, Oct 28")
    assert lines[1].startswith("1. :red_circle: *Urgent* · in progress · "
                               "<https://app.clickup.com/t/u|Task u>")
    assert lines[2].startswith("2. :large_orange_circle: High · queued")
    assert "_5 more after these, in ClickUp._" in text
    assert "*New since Tuesday, Oct 27* (1): " \
           "<https://app.clickup.com/t/n|Task n> (normal)" in text
    assert "*On dev, waiting for production:* 1" in text
    assert "_4 tasks in progress." in text
    assert "_No priority yet on 1 (Sam places these): " in text
    assert "\N{EM DASH}" not in text


def test_monday_looks_back_to_friday():
    monday = datetime(2026, 10, 26, 11, 5, tzinfo=timezone.utc)
    assert dg._since(monday).weekday() == 4


def test_nothing_active_posts_nothing():
    assert dg.render("onlyg", [_task("d", 1, status="on dev")], NOW) == ""


def test_an_unreadable_queue_posts_nothing(monkeypatch):
    import weekly_engineering_report as report

    posts = []
    monkeypatch.setattr(report, "_fetch_one_assignee", lambda cid: None)
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda *a, **k: posts.append(a) or "1")
    out = dg.run_daily_digest(NOW)
    assert posts == [] and set(out.values()) == {
        "ClickUp unreadable, nothing posted"}


def test_each_engineer_gets_their_own_list_in_their_channel(monkeypatch):
    import weekly_engineering_report as report

    posts = []
    monkeypatch.delenv("ENG_DIGEST_DRY_RUN", raising=False)
    monkeypatch.setattr(report, "_fetch_one_assignee",
                        lambda cid: [_task(f"t{cid}", 2)])
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", channel="",
                        broadcast=False: posts.append((channel, text)) or "1")
    out = dg.run_daily_digest(NOW)
    assert out == {"onlyg": "posted", "sanjay": "posted"}
    assert {c for c, _ in posts} == {sprint_thread.ENG_BACKEND,
                                     sprint_thread.ENG_FRONTEND}
    assert "t49257687" in dict(posts)[sprint_thread.ENG_BACKEND]
