"""
test_database_url.py

Regression tests for the connection URL, after an outage.

WHAT HAPPENED
-------------
2026-09-29. Every database call in production started failing with
`ModuleNotFoundError: No module named 'psycopg'`, while the identical code
worked on every developer machine.

`sqlalchemy` was unpinned in requirements.txt. SQLAlchemy 2.0 resolves a bare
`postgresql://` URL to the psycopg2 driver; 2.1 resolves it to psycopg3. A
rebuild picked up 2.1, psycopg3 was not installed, and the webhook handlers
started returning 500s. Nothing in the repository had changed about the
database. The driver was chosen by the dependency resolver.

Two fixes, both tested here:

  * the version is pinned in requirements.txt, and
  * the URL names its driver explicitly, so the resolver cannot pick for us
    even if someone unpins it later.

The second matters more. A pin is a note to the future that gets removed by
whoever is tidying up dependencies; naming the driver is load-bearing code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from database import normalize_database_url
from vomeos.store import normalize_url

ROOT = Path(__file__).parent

# The two implementations must agree. They are separate because the kernel
# may not import the application, but they solve the same problem.
IMPLEMENTATIONS = (normalize_database_url, normalize_url)


@pytest.mark.parametrize("normalize", IMPLEMENTATIONS)
def test_bare_postgresql_gets_an_explicit_driver(normalize):
    # The outage in one line: without this, the installed SQLAlchemy decides.
    assert normalize("postgresql://u:p@host:5432/db").startswith(
        "postgresql+psycopg2://"
    )


@pytest.mark.parametrize("normalize", IMPLEMENTATIONS)
def test_legacy_postgres_scheme_is_upgraded_and_pinned(normalize):
    # Some hosts still hand out postgres://, which SQLAlchemy 2.x rejects.
    assert normalize("postgres://u:p@host:5432/db") == (
        "postgresql+psycopg2://u:p@host:5432/db"
    )


@pytest.mark.parametrize("normalize", IMPLEMENTATIONS)
def test_an_explicit_driver_is_left_alone(normalize):
    # Someone naming asyncpg is being deliberate. Do not override them.
    url = "postgresql+asyncpg://u:p@host/db"
    assert normalize(url) == url


@pytest.mark.parametrize("normalize", IMPLEMENTATIONS)
def test_already_psycopg2_is_not_double_rewritten(normalize):
    url = "postgresql+psycopg2://u:p@host/db"
    assert normalize(url) == url


@pytest.mark.parametrize("normalize", IMPLEMENTATIONS)
def test_empty_stays_empty(normalize):
    # An unset variable must not become a valid-looking URL pointing at
    # localhost, which is how "no password supplied" errors send people
    # hunting for a Postgres problem instead of an unset variable.
    assert normalize("") == ""
    assert normalize(None) == ""


@pytest.mark.parametrize("normalize", IMPLEMENTATIONS)
def test_credentials_survive_the_rewrite(normalize):
    url = "postgres://someuser:s0me%40pass@host.proxy.rlwy.net:41234/railway"
    result = normalize(url)
    assert "someuser:s0me%40pass@host.proxy.rlwy.net:41234/railway" in result


def test_the_two_implementations_agree():
    for url in (
        "postgres://u:p@h/d",
        "postgresql://u:p@h/d",
        "postgresql+psycopg2://u:p@h/d",
        "postgresql+asyncpg://u:p@h/d",
        "",
    ):
        assert normalize_database_url(url) == normalize_url(url), url


def test_sqlalchemy_stays_pinned_until_psycopg3_is_shipped():
    """Unpinning SQLAlchemy without adding psycopg3 repeats the outage."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    specs = [
        line.split("#")[0].strip()
        for line in text.splitlines()
        if line.split("#")[0].strip()
    ]
    sqlalchemy = [s for s in specs if s.lower().startswith("sqlalchemy")]
    assert sqlalchemy, "sqlalchemy missing from requirements.txt"

    ships_psycopg3 = any(
        re.match(r"psycopg(\[|$|[<>=~!])", s.lower()) for s in specs
    )
    if not ships_psycopg3:
        assert "<2.1" in sqlalchemy[0], (
            "sqlalchemy is unpinned and psycopg3 is not installed. "
            "SQLAlchemy 2.1 defaults postgresql:// to psycopg3 and this "
            "combination took production down on 2026-09-29."
        )
