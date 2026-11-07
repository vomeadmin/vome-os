"""The daily triage cap counts triage runs, not every item updated today
(#9661: a busy day's sends and syncs used the cap and a reply was lost)."""

import shadow_triage
from vomeos import store


def test_cap_counts_the_run_log(monkeypatch):
    seen = {}
    monkeypatch.setattr(store, "ensure_table", lambda t: True)

    def query(sql, params=None, **kw):
        seen["sql"] = sql
        return [{"n": 3}]
    monkeypatch.setattr(store, "query", query)
    assert shadow_triage._triaged_today() == 3
    assert "vomeos_triage_runs" in seen["sql"]
    assert "sprint" not in seen["sql"]
