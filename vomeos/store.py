"""
vomeos/store.py

The OS's own connection to its own database.

WHY THIS EXISTS
---------------
An operating system that cannot start without an application's database
module is not an operating system. The kernel previously borrowed the support
app's engine, which meant the OS could only run where that app ran.

This owns the connection instead: its own URL from `vomeos.config`, its own
engine, its own tables. Every table the OS creates is prefixed `vomeos_`, so
pointing `VOMEOS_DATABASE_URL` at a database an application already uses is
safe. Sharing an instance never means sharing a schema.

Nothing here raises. The OS must keep running agents when its database is
down: losing the trace degrades observability, it does not stop support. Every
function returns a falsy value and prints instead.
"""

from __future__ import annotations

import threading

from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.engine import Engine

from vomeos import config

_engine: Engine | None = None
_engine_lock = threading.Lock()
_engine_failed = False

# Tables the OS owns. Registered by the modules that use them and created on
# first use, rather than in one init function an embedding application has to
# remember to call.
_SCHEMA: dict[str, tuple[str, tuple[str, ...]]] = {}
_created: set[str] = set()
_schema_lock = threading.Lock()


def register_table(
    name: str, create_sql: str, index_sql: tuple[str, ...] = ()
) -> None:
    """Declare a table the OS owns. Idempotent."""
    if not name.startswith("vomeos_"):
        raise ValueError(
            f"OS table {name!r} must be prefixed 'vomeos_' so it cannot "
            "collide with an application's schema"
        )
    _SCHEMA[name] = (create_sql, index_sql)


def normalize_url(url: str) -> str:
    """Rewrite a connection URL to name the driver explicitly.

    `postgres://` is rejected by SQLAlchemy 2.x, and a bare `postgresql://`
    lets the installed SQLAlchemy pick the driver, which is not a decision
    that should change when a dependency resolves differently. A URL that
    already names a driver is left alone.
    """
    value = (url or "").strip()
    if value.startswith("postgres://"):
        value = value.replace("postgres://", "postgresql://", 1)
    if value.startswith("postgresql://"):
        value = value.replace("postgresql://", "postgresql+psycopg2://", 1)
    return value


def usable() -> bool:
    """True when a database URL is configured and looks like Postgres."""
    url = config.DATABASE_URL
    return bool(url) and url.startswith(("postgres://", "postgresql://"))


def get_engine() -> Engine | None:
    """The OS engine, or None when the database is unavailable.

    Built once. A failure is remembered so a down database does not mean a
    connection attempt on every single agent run.
    """
    global _engine, _engine_failed
    if _engine is not None:
        return _engine
    if _engine_failed or not usable():
        return None
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            # Same normalization as the support application, and for the
            # same reason: SQLAlchemy 2.0 defaults `postgresql://` to
            # psycopg2 and 2.1 defaults it to psycopg3, so an unpinned
            # rebuild silently changes the driver under you. Name it.
            _engine = create_engine(
                normalize_url(config.DATABASE_URL), pool_pre_ping=True
            )
            return _engine
        except Exception as exc:
            _engine_failed = True
            print(f"[VOMEOS] database unavailable: {exc}")
            return None


def ensure_table(name: str) -> bool:
    """Create one registered table and its indexes. Idempotent, cached."""
    if name in _created:
        return True
    if name not in _SCHEMA:
        raise KeyError(f"no OS table registered as {name!r}")
    engine = get_engine()
    if engine is None:
        return False
    create_sql, index_sql = _SCHEMA[name]
    with _schema_lock:
        if name in _created:
            return True
        try:
            with engine.begin() as conn:
                conn.execute(sql_text(create_sql))
                for stmt in index_sql:
                    conn.execute(sql_text(stmt))
            _created.add(name)
            return True
        except Exception as exc:
            print(f"[VOMEOS] could not create {name}: {exc}")
            return False


def execute(statement: str, params: dict | None = None) -> bool:
    """Run a write. False when it did not land."""
    engine = get_engine()
    if engine is None:
        return False
    try:
        with engine.begin() as conn:
            conn.execute(sql_text(statement), params or {})
        return True
    except Exception as exc:
        print(f"[VOMEOS] write failed: {exc}")
        return False


def query(statement: str, params: dict | None = None) -> list[dict]:
    """Run a read. Empty list when it could not run."""
    engine = get_engine()
    if engine is None:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sql_text(statement), params or {}
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        print(f"[VOMEOS] read failed: {exc}")
        return []


def reset() -> None:
    """Drop the cached engine and schema state. For tests."""
    global _engine, _engine_failed
    _engine = None
    _engine_failed = False
    _created.clear()
