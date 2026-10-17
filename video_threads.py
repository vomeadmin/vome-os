"""
video_threads.py

What the video studio channel remembers between messages.

One row per Slack thread. A thread is one video: it starts with an idea,
carries the script through however many rounds of review, and ends when the
build is delivered into it. Without this, a reply saying "make the hook a
question" has no idea which video it is about.

Deliberately small: the pointer from a Slack thread to its video, the
storyboard itself, and enough state to know whether a greenlight is allowed
yet.

The storyboard JSON is kept here, not only in Studio. Deployed, Studio is a
separate service with a disk that is wiped on every redeploy, and this agent
has no checkout to read. Without its own copy a revision has nothing to
revise and a greenlight has nothing to send.

Follows database.py: same engine, same create-if-missing on init.
"""

from __future__ import annotations

from sqlalchemy import text

from database import DATABASE_URL, _get_engine

# A thread moves forward only: idea -> concepting -> awaiting_concept ->
# drafting -> awaiting_review -> building -> delivered. It can loop back to
# concepting while a person gives direction on the concepts, and to drafting
# any number of times while a reviewer asks for changes to the script. Both
# loops are the normal case and not an error.
STATE_IDEA = "idea"
STATE_CONCEPTING = "concepting"
STATE_AWAITING_CONCEPT = "awaiting_concept"
STATE_DRAFTING = "drafting"
STATE_AWAITING_REVIEW = "awaiting_review"
STATE_BUILDING = "building"
STATE_DELIVERED = "delivered"

_CREATE = """
CREATE TABLE IF NOT EXISTS video_threads (
  thread_ts      VARCHAR PRIMARY KEY,
  channel        VARCHAR NOT NULL DEFAULT '',
  storyboard     VARCHAR NOT NULL DEFAULT '',
  storyboard_json TEXT   NOT NULL DEFAULT '',
  feature_name   TEXT    NOT NULL DEFAULT '',
  faq_url        TEXT    NOT NULL DEFAULT '',
  state          VARCHAR NOT NULL DEFAULT 'idea',
  rounds         INTEGER NOT NULL DEFAULT 0,
  last_runtime   REAL    NULL,
  render_folder  TEXT    NOT NULL DEFAULT '',
  positioning_json TEXT  NOT NULL DEFAULT '',
  concepts_json  TEXT    NOT NULL DEFAULT '',
  chosen_concept INTEGER NULL,
  created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMP NOT NULL DEFAULT NOW()
)
"""

# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS
# does nothing to a table that already exists, so each needs its own ALTER.
#
# positioning_json is the strategist's brief, concepts_json the three
# concepts last posted to the thread, and chosen_concept the number (1 to 3)
# a person picked. NULL means nobody has picked yet, which is different from
# a thread that predates concepts and has a storyboard with no concept.
_ADD_COLUMNS = (
    "ALTER TABLE video_threads ADD COLUMN IF NOT EXISTS "
    "storyboard_json TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE video_threads ADD COLUMN IF NOT EXISTS "
    "positioning_json TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE video_threads ADD COLUMN IF NOT EXISTS "
    "concepts_json TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE video_threads ADD COLUMN IF NOT EXISTS "
    "chosen_concept INTEGER NULL",
)


def init() -> None:
    """Create the table. Safe to call on every boot."""
    if not DATABASE_URL:
        return
    with _get_engine().begin() as conn:
        conn.execute(text(_CREATE))
        for stmt in _ADD_COLUMNS:
            conn.execute(text(stmt))


def get(thread_ts: str) -> dict | None:
    """The thread's row, or None if this thread is not a video yet."""
    if not DATABASE_URL or not thread_ts:
        return None
    with _get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT * FROM video_threads WHERE thread_ts = :t"),
            {"t": thread_ts},
        ).mappings().first()
    return dict(row) if row else None


def start(thread_ts: str, channel: str, feature_name: str = "",
          faq_url: str = "") -> dict:
    """Record a new video thread, or return the existing one.

    Idempotent on purpose: Slack retries events, and a retry must not reset a
    thread that is already halfway through review.
    """
    existing = get(thread_ts)
    if existing:
        return existing
    with _get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO video_threads "
                "(thread_ts, channel, feature_name, faq_url, state) "
                "VALUES (:t, :c, :f, :u, :s) "
                "ON CONFLICT (thread_ts) DO NOTHING"
            ),
            {"t": thread_ts, "c": channel, "f": feature_name,
             "u": faq_url, "s": STATE_IDEA},
        )
    return get(thread_ts) or {}


def update(thread_ts: str, **fields) -> None:
    """Set named columns on a thread. Unknown columns are ignored."""
    allowed = {"storyboard", "storyboard_json", "feature_name", "faq_url",
               "state", "rounds", "last_runtime", "render_folder",
               "positioning_json", "concepts_json", "chosen_concept"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets or not DATABASE_URL:
        return
    assignments = ", ".join("%s = :%s" % (k, k) for k in sets)
    sets["t"] = thread_ts
    with _get_engine().begin() as conn:
        conn.execute(
            text("UPDATE video_threads SET %s, updated_at = NOW() "
                 "WHERE thread_ts = :t" % assignments),
            sets,
        )


def slugs_in_use(exclude_thread: str = "") -> set[str]:
    """Storyboard ids claimed by other threads.

    A new thread must not reuse one: its draft would overwrite that video's
    storyboard and its build would publish into that video's folder.
    """
    if not DATABASE_URL:
        return set()
    with _get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT storyboard FROM video_threads "
                 "WHERE storyboard <> '' AND thread_ts <> :t"),
            {"t": exclude_thread or ""},
        ).all()
    return {r[0] for r in rows}


def recent_videos(exclude_thread: str = "", limit: int = 8) -> list[dict]:
    """Other threads that got as far as a storyboard, newest first.

    Read so the strategist and creative director can see what has already
    been made and not make it again. Only the columns that describe a video
    are returned; the caller turns them into a short text list.
    """
    if not DATABASE_URL:
        return []
    with _get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT feature_name, storyboard, storyboard_json, "
                 "concepts_json, chosen_concept FROM video_threads "
                 "WHERE storyboard_json <> '' AND thread_ts <> :t "
                 "ORDER BY updated_at DESC LIMIT :n"),
            {"t": exclude_thread or "", "n": int(limit)},
        ).mappings().all()
    return [dict(r) for r in rows]


def bump_round(thread_ts: str) -> int:
    """Count a round of review. Returned so a post can say which one it is."""
    if not DATABASE_URL:
        return 0
    with _get_engine().begin() as conn:
        row = conn.execute(
            text("UPDATE video_threads SET rounds = rounds + 1, "
                 "updated_at = NOW() WHERE thread_ts = :t RETURNING rounds"),
            {"t": thread_ts},
        ).first()
    return int(row[0]) if row else 0
