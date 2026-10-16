"""
video_threads.py

What the video studio channel remembers between messages.

One row per Slack thread. A thread is one video: it starts with an idea,
carries the script through however many rounds of review, and ends when the
build is delivered into it. Without this, a reply saying "make the hook a
question" has no idea which video it is about.

Deliberately small. The storyboard itself lives in Vome Studio and is the
real artefact; this is only the pointer from a Slack thread to it, plus
enough state to know whether a greenlight is allowed yet.

Follows database.py: same engine, same create-if-missing on init.
"""

from __future__ import annotations

from sqlalchemy import text

from database import DATABASE_URL, _get_engine

# A thread moves forward only: idea -> drafting -> awaiting_review -> building
# -> delivered. It can loop back to drafting any number of times while a
# reviewer asks for changes, which is the normal case and not an error.
STATE_IDEA = "idea"
STATE_DRAFTING = "drafting"
STATE_AWAITING_REVIEW = "awaiting_review"
STATE_BUILDING = "building"
STATE_DELIVERED = "delivered"

_CREATE = """
CREATE TABLE IF NOT EXISTS video_threads (
  thread_ts      VARCHAR PRIMARY KEY,
  channel        VARCHAR NOT NULL DEFAULT '',
  storyboard     VARCHAR NOT NULL DEFAULT '',
  feature_name   TEXT    NOT NULL DEFAULT '',
  faq_url        TEXT    NOT NULL DEFAULT '',
  state          VARCHAR NOT NULL DEFAULT 'idea',
  rounds         INTEGER NOT NULL DEFAULT 0,
  last_runtime   REAL    NULL,
  render_folder  TEXT    NOT NULL DEFAULT '',
  created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMP NOT NULL DEFAULT NOW()
)
"""


def init() -> None:
    """Create the table. Safe to call on every boot."""
    if not DATABASE_URL:
        return
    with _get_engine().begin() as conn:
        conn.execute(text(_CREATE))


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
    allowed = {"storyboard", "feature_name", "faq_url", "state", "rounds",
               "last_runtime", "render_folder"}
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
