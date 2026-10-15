"""
test_vomeos_store.py

The schema upgrade in vomeos/store.py.

`CREATE TABLE IF NOT EXISTS` never touches a table that exists, so columns
added to a registered schema did not reach production. vomeos_sentry_issues
ran without triage_summary, root_cause and fix_diff, and every verdict write
failed. These pin the repair: the column parser reads every real OS schema
correctly, and only columns that are actually missing get added.

No Postgres needed. The engine is a fake that records what it was asked.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import pytest

import sentry_ledger  # noqa: F401  registers vomeos_sentry_issues
import vomeos.trace  # noqa: F401  registers vomeos_agent_runs
import vomeos.worker.claim  # noqa: F401  registers vomeos_job_runs
from vomeos import store


@pytest.mark.parametrize(
    "table", ["vomeos_sentry_issues", "vomeos_agent_runs", "vomeos_job_runs"]
)
def test_every_registered_schema_parses_into_plain_columns(table):
    create_sql, _ = store._SCHEMA[table]
    columns = store.column_definitions(create_sql)

    assert columns, f"{table}: no columns parsed"
    for name, definition in columns:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", name), (table, name)
        assert definition.startswith(name + " ")
        assert not definition.endswith(","), definition
        assert "--" not in definition, definition
        assert "PRIMARY KEY" not in definition.upper()


def test_the_sentry_columns_that_were_missing_are_declared():
    create_sql, _ = store._SCHEMA["vomeos_sentry_issues"]
    names = {name for name, _ in store.column_definitions(create_sql)}
    assert {"triage_summary", "root_cause", "proposed_fix",
            "fix_diff", "fix_reason", "verdict", "severity"} <= names
    # The primary key is never re-added.
    assert "issue_id" not in names


class _FakeEngine:
    def __init__(self, existing):
        self.existing = existing
        self.statements = []

    @contextmanager
    def begin(self):
        engine = self

        class _Conn:
            def execute(self, statement, params=None):
                text = str(statement)
                engine.statements.append(text)
                if "information_schema.columns" in text:
                    return [(c,) for c in engine.existing]
                return []

        yield _Conn()


def test_only_missing_columns_are_added():
    create_sql = """
    CREATE TABLE IF NOT EXISTS vomeos_example (
        id          VARCHAR PRIMARY KEY,
        status      VARCHAR NOT NULL DEFAULT 'seen',
        -- a comment line on its own
        summary     TEXT,  -- trailing comment
        run_ids     JSONB DEFAULT '[]'::jsonb
    )
    """
    engine = _FakeEngine(existing={"id", "status"})
    store._add_missing_columns(engine, "vomeos_example", create_sql)

    alters = [s for s in engine.statements if s.startswith("ALTER")]
    assert alters == [
        "ALTER TABLE vomeos_example ADD COLUMN IF NOT EXISTS summary     TEXT",
        "ALTER TABLE vomeos_example ADD COLUMN IF NOT EXISTS run_ids     "
        "JSONB DEFAULT '[]'::jsonb",
    ]


def test_an_up_to_date_table_is_not_altered():
    create_sql, _ = store._SCHEMA["vomeos_job_runs"]
    every = {name for name, _ in store.column_definitions(create_sql)}
    engine = _FakeEngine(existing=every | {"claim_key"})
    store._add_missing_columns(engine, "vomeos_job_runs", create_sql)
    assert not [s for s in engine.statements if s.startswith("ALTER")]


def test_backfill_copies_verdicts_without_overwriting(monkeypatch):
    """Recovers verdicts from the run trace; never touches a stored one."""
    calls = []

    def fake_query(statement, params=None, *, write=False):
        calls.append((statement, params, write))
        return [{"issue_id": "1"}, {"issue_id": "2"}]

    monkeypatch.setattr(sentry_ledger, "_ready", lambda: True)
    monkeypatch.setattr(store, "ensure_table", lambda name: True)
    monkeypatch.setattr(store, "query", fake_query)

    result = sentry_ledger.backfill_from_runs()

    assert result == {"triaged": 2, "analysed": 2, "available": True}
    (triage_sql, triage_params, w1), (analysis_sql, analysis_params, w2) = calls
    # Both must commit; a RETURNING read would roll back on close.
    assert w1 and w2
    assert triage_params == {"agent": "engineering.sentry_triage"}
    assert analysis_params == {"agent": "engineering.bug_analyst"}
    # Fill-only guards.
    assert "s.verdict IS NULL" in triage_sql
    assert "s.root_cause IS NULL" in analysis_sql
    # Only runs that passed their guards.
    assert "status = 'ok'" in triage_sql and "status = 'ok'" in analysis_sql


def test_backfill_without_a_database_reports_unavailable(monkeypatch):
    monkeypatch.setattr(sentry_ledger, "_ready", lambda: False)
    assert sentry_ledger.backfill_from_runs()["available"] is False
