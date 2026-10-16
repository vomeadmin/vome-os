"""
mcp_connector/store.py

Where the connector keeps its OAuth state and its audit log.

WHY POSTGRES AND NOT MEMORY
---------------------------
claude.ai holds a refresh token for every person who connects, for weeks. If
those lived in process memory, every Railway deploy would sign the whole team
out at once, and nobody would know why their connector stopped working. So
they live in the application's own Postgres, in tables prefixed `mcp_`.

Tokens are stored as SHA-256 hashes. A database read (a backup, a support
query, a leak) yields nothing anyone can present as a bearer token.

The audit log records who asked what, which is the price of handing code
search to more than one person: if a question ever needs answering about what
the connector was used for, this is the answer.

When DATABASE_URL is unusable (local development), it falls back to an
in-process SQLite database and says so on boot. Sign-ins then last until the
process restarts, which is fine on a laptop and is exactly why production
must not run that way.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.pool import StaticPool

_metadata = MetaData()

clients = Table(
    "mcp_oauth_clients", _metadata,
    Column("client_id", String(128), primary_key=True),
    Column("info", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)

# A sign-in in progress: claude.ai has sent someone to /authorize and they
# are at Google. Holds what is needed to finish the flow when they return.
pending = Table(
    "mcp_oauth_pending", _metadata,
    Column("txn", String(128), primary_key=True),
    Column("data", Text, nullable=False),
    Column("expires_at", Float, nullable=False),
)

codes = Table(
    "mcp_oauth_codes", _metadata,
    Column("code_hash", String(64), primary_key=True),
    Column("data", Text, nullable=False),
    Column("expires_at", Float, nullable=False),
)

tokens = Table(
    "mcp_oauth_tokens", _metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("kind", String(16), nullable=False),  # access | refresh
    Column("client_id", String(128), nullable=False),
    Column("subject", String(320), nullable=False),
    Column("scopes", Text, nullable=False),
    Column("resource", Text, nullable=True),
    Column("expires_at", Float, nullable=True),
    # Every token minted from one sign-in shares a family, so revoking one
    # revokes the lot and a stolen refresh token cannot outlive a sign-out.
    Column("family", String(64), nullable=False),
    Column("revoked", Integer, nullable=False, default=0),
    Column("created_at", Float, nullable=False),
)

tool_calls = Table(
    "mcp_tool_calls", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", Float, nullable=False),
    Column("subject", String(320), nullable=False),
    Column("tool", String(64), nullable=False),
    Column("arguments", Text, nullable=False),
    Column("ok", Integer, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("error", Text, nullable=True),
)

# A piece of help-centre work handed to the kb_writer agent: the request, the
# draft it produced, the rule check, and what publishing did. One row per
# hand-off, so "what did we publish, from what, and who asked" has an answer.
faq_jobs = Table(
    "mcp_faq_jobs", _metadata,
    Column("id", String(32), primary_key=True),
    Column("created_by", String(320), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("status", String(24), nullable=False),
    Column("request", Text, nullable=False),
    Column("draft", Text, nullable=True),
    Column("lint", Text, nullable=True),
    Column("result", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("run_id", String(64), nullable=True),
)

_engine = None
_engine_lock = threading.Lock()
_durable = False


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def set_engine(engine) -> None:
    """Point the store at an engine. Tests use this with SQLite."""
    global _engine
    with _engine_lock:
        _engine = engine
        _metadata.create_all(engine, checkfirst=True)


def durable() -> bool:
    """True when sign-ins survive a restart."""
    _get_engine()
    return _durable


def _get_engine():
    global _engine, _durable
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        import database

        if database._database_url_is_usable(database.DATABASE_URL):
            # A configured database that is briefly unreachable raises here
            # and is retried on the next call. Falling back to SQLite would
            # quietly sign everyone out at the next deploy instead.
            engine = database._get_engine()
            _metadata.create_all(engine, checkfirst=True)
            _durable = True
        else:
            print(
                "[MCP STORE] no usable DATABASE_URL, using in-process SQLite. "
                "Sign-ins will not survive a restart."
            )
            engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            _metadata.create_all(engine, checkfirst=True)
            _durable = False
        _engine = engine
        return _engine


def _now() -> float:
    return time.time()


# -- clients --------------------------------------------------------------

def save_client(client_id: str, info: dict) -> None:
    with _get_engine().begin() as conn:
        conn.execute(delete(clients).where(clients.c.client_id == client_id))
        conn.execute(insert(clients).values(
            client_id=client_id, info=json.dumps(info), created_at=_now(),
        ))


def load_client(client_id: str) -> dict | None:
    with _get_engine().connect() as conn:
        row = conn.execute(
            select(clients.c.info).where(clients.c.client_id == client_id)
        ).first()
    return json.loads(row[0]) if row else None


# -- one-time records: pending sign-ins and authorization codes -------------

def _put_once(table, key_column: str, key: str, data: dict,
              ttl: float) -> None:
    with _get_engine().begin() as conn:
        conn.execute(delete(table).where(table.c.expires_at < _now()))
        conn.execute(insert(table).values(
            **{key_column: key}, data=json.dumps(data),
            expires_at=_now() + ttl,
        ))


def _peek(table, key_column: str, key: str) -> dict | None:
    column = table.c[key_column]
    with _get_engine().connect() as conn:
        row = conn.execute(
            select(table.c.data, table.c.expires_at).where(column == key)
        ).first()
    if not row or row[1] < _now():
        return None
    return json.loads(row[0])


def _take_once(table, key_column: str, key: str) -> dict | None:
    """Read and delete in one transaction. Only one caller ever wins.

    A code or a sign-in redeemed twice is a replay, so the delete's row count
    decides: whoever deletes the row owns it, anyone else gets None.
    """
    column = table.c[key_column]
    with _get_engine().begin() as conn:
        row = conn.execute(
            select(table.c.data, table.c.expires_at).where(column == key)
        ).first()
        if not row:
            return None
        gone = conn.execute(delete(table).where(column == key)).rowcount
    if gone != 1 or row[1] < _now():
        return None
    return json.loads(row[0])


def put_pending(txn: str, data: dict, ttl: float = 600) -> None:
    _put_once(pending, "txn", txn, data, ttl)


def take_pending(txn: str) -> dict | None:
    return _take_once(pending, "txn", txn)


def put_code(code: str, data: dict, ttl: float = 300) -> None:
    _put_once(codes, "code_hash", hash_secret(code), data, ttl)


def peek_code(code: str) -> dict | None:
    return _peek(codes, "code_hash", hash_secret(code))


def take_code(code: str) -> dict | None:
    return _take_once(codes, "code_hash", hash_secret(code))


# -- tokens ----------------------------------------------------------------

def save_token(
    token: str,
    *,
    kind: str,
    client_id: str,
    subject: str,
    scopes: list[str],
    resource: str | None,
    expires_at: float | None,
    family: str,
) -> None:
    with _get_engine().begin() as conn:
        conn.execute(insert(tokens).values(
            token_hash=hash_secret(token), kind=kind, client_id=client_id,
            subject=subject, scopes=" ".join(scopes), resource=resource,
            expires_at=expires_at, family=family, revoked=0,
            created_at=_now(),
        ))


def load_token(token: str, kind: str) -> dict | None:
    """A live, unrevoked, unexpired token of the given kind, or None."""
    with _get_engine().connect() as conn:
        row = conn.execute(
            select(tokens).where(tokens.c.token_hash == hash_secret(token))
        ).mappings().first()
    if not row or row["kind"] != kind or row["revoked"]:
        return None
    if row["expires_at"] is not None and row["expires_at"] < _now():
        return None
    return {
        "client_id": row["client_id"],
        "subject": row["subject"],
        "scopes": [s for s in (row["scopes"] or "").split() if s],
        "resource": row["resource"],
        "expires_at": row["expires_at"],
        "family": row["family"],
    }


def revoke_token(token: str) -> bool:
    """Revoke a token. Its whole family goes with it."""
    with _get_engine().begin() as conn:
        row = conn.execute(
            select(tokens.c.family).where(
                tokens.c.token_hash == hash_secret(token)
            )
        ).first()
        if not row:
            return False
        conn.execute(
            update(tokens).where(tokens.c.family == row[0]).values(revoked=1)
        )
    return True


def revoke_single(token: str) -> None:
    """Retire one token without touching its family (refresh rotation)."""
    with _get_engine().begin() as conn:
        conn.execute(
            update(tokens)
            .where(tokens.c.token_hash == hash_secret(token))
            .values(revoked=1)
        )


def revoke_subject(subject: str) -> int:
    """Sign one person out everywhere. For offboarding."""
    with _get_engine().begin() as conn:
        return conn.execute(
            update(tokens)
            .where(tokens.c.subject == subject.lower())
            .values(revoked=1)
        ).rowcount


# -- audit -----------------------------------------------------------------

def record_call(
    subject: str, tool: str, arguments: dict, ok: bool, duration_ms: int,
    error: str = "",
) -> None:
    """Never raises. An audit write failing must not fail the answer."""
    try:
        with _get_engine().begin() as conn:
            conn.execute(insert(tool_calls).values(
                at=_now(), subject=subject or "(unknown)", tool=tool,
                arguments=json.dumps(arguments, default=str)[:2000],
                ok=1 if ok else 0, duration_ms=duration_ms,
                error=(error or "")[:500] or None,
            ))
    except Exception as exc:
        print(f"[MCP STORE] audit write failed: {exc}")


def recent_calls(limit: int = 50) -> list[dict]:
    with _get_engine().connect() as conn:
        rows = conn.execute(
            select(tool_calls).order_by(tool_calls.c.id.desc()).limit(limit)
        ).mappings().all()
    return [dict(row) for row in rows]


# -- FAQ jobs --------------------------------------------------------------

_JSON_COLUMNS = ("request", "draft", "lint", "result")


def create_faq_job(job_id: str, created_by: str, request: dict,
                   status: str = "queued", **fields) -> None:
    now = _now()
    values = {
        "id": job_id, "created_by": created_by or "(unknown)",
        "created_at": now, "updated_at": now, "status": status,
        "request": json.dumps(request),
    }
    for key, value in fields.items():
        values[key] = json.dumps(value) if key in _JSON_COLUMNS else value
    with _get_engine().begin() as conn:
        conn.execute(insert(faq_jobs).values(**values))


def update_faq_job(job_id: str, *, expect_status: str | None = None,
                   **fields) -> bool:
    """Update a job. With expect_status, only if it is still in that state,
    which is what stops one draft being published twice."""
    values = {"updated_at": _now()}
    for key, value in fields.items():
        values[key] = json.dumps(value) if key in _JSON_COLUMNS else value
    statement = update(faq_jobs).where(faq_jobs.c.id == job_id)
    if expect_status is not None:
        statement = statement.where(faq_jobs.c.status == expect_status)
    with _get_engine().begin() as conn:
        return conn.execute(statement.values(**values)).rowcount == 1


def get_faq_job(job_id: str) -> dict | None:
    with _get_engine().connect() as conn:
        row = conn.execute(
            select(faq_jobs).where(faq_jobs.c.id == job_id)
        ).mappings().first()
    if not row:
        return None
    job = dict(row)
    for key in _JSON_COLUMNS:
        if job.get(key):
            job[key] = json.loads(job[key])
    return job


def recent_faq_jobs(limit: int = 20) -> list[dict]:
    with _get_engine().connect() as conn:
        rows = conn.execute(
            select(
                faq_jobs.c.id, faq_jobs.c.created_by, faq_jobs.c.status,
                faq_jobs.c.created_at, faq_jobs.c.request,
            ).order_by(faq_jobs.c.created_at.desc()).limit(limit)
        ).mappings().all()
    out = []
    for row in rows:
        request = json.loads(row["request"] or "{}")
        out.append({
            "job_id": row["id"], "status": row["status"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "request": (request.get("request") or "")[:160],
        })
    return out
