"""eng_priority: the ladder, and when an engineer is pinged (Sam,
2026-10-27)."""

from __future__ import annotations

import pytest

import eng_priority as ep
import sprint_thread


class FakeStore:
    """Just enough of vomeos_eng_pings for ping()."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def query(self, sql, params=None, write=False):
        params = params or {}
        if sql.startswith("INSERT"):
            if params["t"] in self.rows:
                return []
            self.rows[params["t"]] = {"engineer": params["e"], "ts": "",
                                      "thread_ts": "", "sent_at": params["at"]}
            return [{"task_id": params["t"]}]
        if sql.startswith("UPDATE"):
            self.rows[params["t"]].update(ts=params["ts"],
                                          thread_ts=params["root"])
            return [{"task_id": params["t"]}]
        roots = [r for r in self.rows.values()
                 if r["engineer"] == params["e"] and r["ts"]
                 and not r["thread_ts"] and r["sent_at"] >= params["since"]]
        roots.sort(key=lambda r: r["sent_at"], reverse=True)
        return [{"ts": r["ts"]} for r in roots[:1]]


@pytest.fixture
def slack(monkeypatch):
    posts = []
    fake = FakeStore()
    monkeypatch.setattr(ep.store, "ensure_table", lambda t: True)
    monkeypatch.setattr(ep.store, "query", fake.query)
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", channel="",
                        broadcast=False: posts.append(
                            (channel, thread_ts, text))
                        or f"{len(posts)}.0")
    return posts


def test_the_ladder_reads_names_and_clickup_values():
    assert ep.from_name("URGENT") == ep.URGENT
    assert ep.from_name("", ep.HIGH) == ep.HIGH
    assert ep.of_task({"priority": {"id": "2", "priority": "high"}}) == 2
    assert ep.of_task({"priority": None}) is None
    assert ep.name(None) == "none" and ep.name(4) == "low"


def test_higher_and_below():
    assert ep.higher(ep.URGENT, None) and ep.higher(ep.HIGH, ep.NORMAL)
    assert not ep.higher(ep.NORMAL, ep.HIGH)
    assert [ep.below(p) for p in (1, 2, 3, 4)] == [2, 3, 3, 4]


def test_only_urgent_pings(slack):
    for priority in (ep.HIGH, ep.NORMAL, ep.LOW):
        assert ep.ping("onlyg", "86a", "x", priority) == "not urgent"
    assert slack == []


def test_a_task_is_pinged_once(slack):
    assert ep.ping("onlyg", "86a", "Broker down", ep.URGENT) == "pinged"
    assert ep.ping("onlyg", "86a", "Broker down", ep.URGENT) == \
        "already pinged"
    assert len(slack) == 1
    channel, thread, text = slack[0]
    assert channel == sprint_thread.ENG_BACKEND and thread == ""
    assert text == ":rotating_light: <@U0APZ7JUHRD> Broker down"


def test_a_burst_folds_into_one_thread_with_one_mention(slack):
    """2026-10-26: 18 posts in ten minutes from one audit."""
    ep.ping("onlyg", "86a", "first", ep.URGENT)
    assert ep.ping("onlyg", "86b", "second", ep.URGENT) == "threaded"
    assert slack[1][1] == "1.0"
    assert "<@" not in slack[1][2] and "Also urgent: second" in slack[1][2]
    # Another engineer starts their own.
    assert ep.ping("sanjay", "86c", "web", ep.URGENT) == "pinged"


def test_an_urgent_still_goes_out_when_the_store_is_down(monkeypatch):
    posts = []
    monkeypatch.setattr(ep.store, "ensure_table", lambda t: False)
    monkeypatch.setattr(sprint_thread, "_post",
                        lambda text, thread_ts="", channel="",
                        broadcast=False: posts.append(text) or "1.0")
    assert ep.ping("onlyg", "86a", "x", ep.URGENT) == "pinged"
    assert ep.ping("onlyg", "86a", "x", ep.URGENT) == "pinged"
    assert len(posts) == 2
