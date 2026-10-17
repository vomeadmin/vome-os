"""
video_lessons.py

What the video team has learned from the people reviewing its work.

Every piece of direction and every script note in the video studio channel
is read by the lesson_keeper agent. Standing taste ("always say volunteer",
"no stunt hooks") becomes a lesson here; a one-off edit ("make line 3
shorter") does not. Active lessons go to the strategist, the creative
director, the copywriter and the brand editor on every run, read at request
time, so a lesson learned in one thread shapes the very next one with no
deploy.

Nothing is learned silently: the thread is told each lesson as it is kept,
and "forget that" retires it. Retired lessons stay in the table, so the
history of what the team was taught, by whom and why, is never lost.

Follows database.py and video_threads.py: same engine, create-if-missing on
init.
"""

from __future__ import annotations

from sqlalchemy import text

from database import DATABASE_URL, _get_engine

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"

# The roles a lesson can be addressed to. "all" reaches every one of them.
ROLES = ("content_strategist", "creative_director", "content_producer",
         "brand_editor")

_CREATE = """
CREATE TABLE IF NOT EXISTS video_lessons (
  id             SERIAL PRIMARY KEY,
  rule           TEXT    NOT NULL,
  why            TEXT    NOT NULL DEFAULT '',
  applies_to     TEXT    NOT NULL DEFAULT 'all',
  source_thread  VARCHAR NOT NULL DEFAULT '',
  source_quote   TEXT    NOT NULL DEFAULT '',
  stage          VARCHAR NOT NULL DEFAULT '',
  status         VARCHAR NOT NULL DEFAULT 'active',
  replaced_by    INTEGER NULL,
  created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  retired_at     TIMESTAMP NULL
)
"""

# A lesson block in a prompt is capped: a long list stops being read as a
# list of rules and starts being read as noise.
MAX_ACTIVE_IN_PROMPT = 30


def init() -> None:
    """Create the table. Safe to call on every boot."""
    if not DATABASE_URL:
        return
    with _get_engine().begin() as conn:
        conn.execute(text(_CREATE))


def _roles(applies_to) -> str:
    """Normalise to a comma list of known roles, or 'all'."""
    if isinstance(applies_to, str):
        applies_to = [applies_to]
    wanted = [str(r).strip() for r in applies_to or [] if str(r).strip()]
    if not wanted or "all" in wanted:
        return "all"
    known = [r for r in wanted if r in ROLES]
    return ",".join(known) or "all"


def add(lessons: list[dict], thread_ts: str, quote: str, stage: str,
        retire_ids: list[int] | None = None) -> list[dict]:
    """Keep new lessons, retiring any they replace. Returns what was kept."""
    if not DATABASE_URL or not lessons:
        return []
    kept = []
    with _get_engine().begin() as conn:
        for lesson in lessons:
            rule = str(lesson.get("rule") or "").strip()
            if not rule:
                continue
            row = conn.execute(
                text("INSERT INTO video_lessons (rule, why, applies_to, "
                     "source_thread, source_quote, stage) VALUES "
                     "(:r, :w, :a, :t, :q, :s) RETURNING id"),
                {"r": rule, "w": str(lesson.get("why") or "").strip(),
                 "a": _roles(lesson.get("applies_to")), "t": thread_ts,
                 "q": (quote or "")[:2000], "s": stage},
            ).first()
            kept.append({"id": int(row[0]), "rule": rule})
        ids = [int(i) for i in retire_ids or [] if str(i).isdigit()]
        if ids and kept:
            conn.execute(
                text("UPDATE video_lessons SET status = :st, "
                     "retired_at = NOW(), replaced_by = :by "
                     "WHERE id = ANY(:ids) AND status = :active"),
                {"st": STATUS_RETIRED, "by": kept[0]["id"], "ids": ids,
                 "active": STATUS_ACTIVE},
            )
    return kept


def active(role: str = "") -> list[dict]:
    """Active lessons, oldest first, optionally only those for one role."""
    if not DATABASE_URL:
        return []
    with _get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT id, rule, why, applies_to FROM video_lessons "
                 "WHERE status = :active ORDER BY id"),
            {"active": STATUS_ACTIVE},
        ).mappings().all()
    out = []
    for r in rows:
        roles = (r["applies_to"] or "all").split(",")
        if not role or "all" in roles or role in roles:
            out.append(dict(r))
    return out


def render_for(role: str) -> str:
    """The lessons block one agent is given, newest last."""
    try:
        lessons = active(role)[-MAX_ACTIVE_IN_PROMPT:]
    except Exception as exc:  # a lesson outage must not stop a video
        print("[VIDEO] could not read lessons: %s" % exc)
        return "(lessons unavailable right now)"
    if not lessons:
        return "(none yet)"
    return "\n".join("- [%d] %s%s" % (l["id"], l["rule"],
                                      " (why: %s)" % l["why"]
                                      if l.get("why") else "")
                     for l in lessons)


def retire_latest(thread_ts: str) -> list[str]:
    """Undo the most recent lessons this thread taught. Returns their rules.

    "Forget that" means the last thing kept from this thread, which may be
    several lessons kept from one message: they share a created_at second.
    """
    if not DATABASE_URL or not thread_ts:
        return []
    with _get_engine().begin() as conn:
        latest = conn.execute(
            text("SELECT MAX(created_at) FROM video_lessons "
                 "WHERE source_thread = :t AND status = :active"),
            {"t": thread_ts, "active": STATUS_ACTIVE},
        ).first()
        if not latest or latest[0] is None:
            return []
        rows = conn.execute(
            text("UPDATE video_lessons SET status = :st, retired_at = NOW() "
                 "WHERE source_thread = :t AND status = :active "
                 "AND created_at >= :since - INTERVAL '2 seconds' "
                 "RETURNING rule"),
            {"st": STATUS_RETIRED, "t": thread_ts, "active": STATUS_ACTIVE,
             "since": latest[0]},
        ).all()
    return [r[0] for r in rows]
