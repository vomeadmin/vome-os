"""
video_social.py

From a delivered video to booked social posts.

Once a video is built, the social copywriter writes one caption per
platform. A person reviews them in the same Slack thread and replies
"schedule it", and each platform's post is booked in Buffer at the next free
video slot in the posting calendar. Sammy, 2026-10-30: at least two videos a
week, from a stock of ten to twelve built ahead, and the social team owns the
days and times (they move posts in Buffer; the calendar here is only the
default). LinkedIn, Facebook and Instagram to start (Buffer's free plan has
three channels); TikTok and YouTube Shorts are one SOCIAL_PLATFORMS change
away.

This module is the deterministic half: which platforms, which file goes to
which, the checks a caption must pass before anyone sees it, the calendar,
and the `social_posts` table. The agent call and the Slack posts are in
video_studio_jobs.py; the Buffer calls in vomeos/integrations/buffer_social.py.

Follows database.py and video_threads.py: same engine, create-if-missing on
init.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import DATABASE_URL, _get_engine

# One entry per platform, in the order captions are shown and booked.
#
# aspect: the render that goes to it (Studio's build_formats.FORMATS).
#   Instagram, TikTok and Shorts are vertical first; LinkedIn and Facebook
#   feeds give a 4:5 video more of the screen than a 1:1 and crop nothing.
# max_chars: our limit, well under each network's. Past these, the feed
#   truncates behind "see more" and the hook is the only line read.
# hashtags: (fewest, most). Instagram has capped posts at five since
#   December 2025; TikTok reads them for search; LinkedIn and Facebook
#   readers skip them.
# links: whether a URL is clickable in the caption. On Instagram and TikTok
#   it is dead text, so it is refused.
# one_line: TikTok drops line breaks on publish (Zoho Social's help, 2024).
# service: Buffer's name for the network, matched against its channels.
# metadata: Buffer's per-network post options. Instagram needs its type,
#   and a reel shared to the feed is what a 9:16 video should be.
PLATFORMS = (
    {"key": "linkedin", "label": "LinkedIn", "service": "linkedin",
     "aspect": "4x5", "max_chars": 1300, "hashtags": (0, 3), "links": True},
    {"key": "facebook", "label": "Facebook", "service": "facebook",
     "aspect": "4x5", "max_chars": 600, "hashtags": (0, 3), "links": True,
     "metadata": {"facebook": {"type": "post"}}},
    {"key": "instagram", "label": "Instagram", "service": "instagram",
     "aspect": "9x16", "max_chars": 1000, "hashtags": (3, 5),
     "links": False,
     "metadata": {"instagram": {"type": "reel", "shouldShareToFeed": True}}},
    {"key": "tiktok", "label": "TikTok", "service": "tiktok",
     "aspect": "9x16", "max_chars": 300, "hashtags": (3, 5), "links": False,
     "one_line": True},
    {"key": "youtube", "label": "YouTube Shorts", "service": "youtube",
     "aspect": "9x16", "max_chars": 1000, "hashtags": (1, 3), "links": True,
     "title_chars": 100},
)
PLATFORM_KEYS = tuple(p["key"] for p in PLATFORMS)

# The platforms posted to unless SOCIAL_PLATFORMS (comma separated keys)
# says otherwise. Sammy, 2026-10-30: where volunteer managers are, on
# Buffer's free three channels.
DEFAULT_PLATFORMS = ("linkedin", "facebook", "instagram")


def active() -> tuple:
    """The platforms captioned and booked, in PLATFORMS order."""
    raw = os.environ.get("SOCIAL_PLATFORMS", "")
    wanted = {k.strip().lower() for k in raw.split(",") if k.strip()}
    wanted = (wanted & set(PLATFORM_KEYS)) or set(DEFAULT_PLATFORMS)
    return tuple(p for p in PLATFORMS if p["key"] in wanted)


# Studio's render sizes, by aspect.
SIZES = {"9x16": (1080, 1920), "4x5": (1080, 1350), "1x1": (1080, 1080),
         "16x9": (1920, 1080)}

# Names that never go in a post. The playbook names competitors for the
# strategist's benefit; a caption that repeats one is advertising for them.
# Only names that are not also ordinary phrases ("your volunteer hub", "get
# connected"), so a fair caption is never refused for one. "Better impact"
# stays despite that: it is the competitor most worth never naming, and a
# refused caption is only rewritten once.
COMPETITORS = (
    "better impact", "volunteerhub", "galaxy digital", "bloomerang",
    "initlive", "signupgenius", "signup genius", "rosterfy",
    "volunteermatters", "civic champs", "timecounts", "track it forward",
    "volgistics", "cervis", "samaritan technologies",
)

DASHES = (chr(0x2014), chr(0x2013))  # em dash, en dash
HASHTAG = re.compile(r"(?<![\w&])#\w+")
URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|ca|org|net)\b", re.I)

# The default calendar. Overridden whole by SOCIAL_CALENDAR (same JSON
# shape) so the social team's days and times need no deploy.
DEFAULT_CALENDAR = {
    "zone": "America/Montreal",
    "days": ["tue", "thu"],
    "times": {"linkedin": "09:00", "facebook": "12:00",
              "instagram": "12:30", "youtube": "15:00", "tiktok": "18:00"},
}
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

STATUS_PLANNED = "planned"      # in our calendar, not yet in Buffer
STATUS_SCHEDULED = "scheduled"  # booked in Buffer
STATUS_FAILED = "failed"        # Buffer refused it; the error says why


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

WEBSITE = "https://www.vomevolunteer.com"


def platforms_brief() -> str:
    """The platform rules as the copywriter reads them: the same numbers
    check_captions enforces, so the two can never disagree."""
    lines = []
    for p in active():
        low, high = p["hashtags"]
        rule = ["%s (`%s`): %s video, at most %d characters, %d to %d "
                "hashtags" % (p["label"], p["key"], p["aspect"].replace(
                    "x", ":"), p["max_chars"], low, high)]
        rule.append("links work" if p["links"] else
                    "no links (not clickable)")
        if p.get("one_line"):
            rule.append("one paragraph, line breaks are removed")
        if p.get("title_chars"):
            rule.append("plus a `title` of at most %d characters"
                        % p["title_chars"])
        lines.append("- " + ", ".join(rule))
    return "\n".join(lines)


def _norm_caption(platform: dict, raw) -> dict:
    """{"text", "title"} with the whitespace the platform will keep."""
    if isinstance(raw, str):
        raw = {"text": raw}
    raw = raw if isinstance(raw, dict) else {}
    body = str(raw.get("text") or "").strip()
    if platform.get("one_line"):
        body = re.sub(r"\s+", " ", body)
    else:
        body = re.sub(r"\n{3,}", "\n\n", body)
    out = {"text": body}
    if platform.get("title_chars"):
        out["title"] = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()
    return out


def check_captions(data) -> tuple[dict, str]:
    """(captions by platform key, "") if they can be posted, else ({}, why).

    Deterministic backstops for the charter, the same way check_concepts
    backs up the creative director: every active platform present, no
    dashes, says "volunteer", within length, hashtags in range, no dead
    links, no competitor named. A caption for a platform not posted to is
    dropped.
    """
    raw = data.get("captions") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}, "the copywriter returned no captions"
    out = {}
    for p in active():
        c = _norm_caption(p, raw.get(p["key"]))
        name = p["label"]
        if not c["text"]:
            return {}, "there is no %s caption" % name
        if p.get("title_chars"):
            if not c["title"]:
                return {}, "the %s caption has no title" % name
            if len(c["title"]) > p["title_chars"]:
                return {}, ("the %s title is %d characters, over %d"
                            % (name, len(c["title"]), p["title_chars"]))
        whole = " ".join([c.get("title", ""), c["text"]])
        if any(d in whole for d in DASHES):
            return {}, "the %s caption has a dash" % name
        if "volunteer" not in whole.lower():
            return {}, "the %s caption does not say 'volunteer'" % name
        if len(c["text"]) > p["max_chars"]:
            return {}, ("the %s caption is %d characters, over %d"
                        % (name, len(c["text"]), p["max_chars"]))
        tags = len(HASHTAG.findall(c["text"]))
        low, high = p["hashtags"]
        if not low <= tags <= high:
            return {}, ("the %s caption has %d hashtags, needs %d to %d"
                        % (name, tags, low, high))
        if not p["links"] and URL.search(c["text"]):
            return {}, ("the %s caption has a link, which is not clickable "
                        "there" % name)
        lowered = " %s " % whole.lower()
        named = next((n for n in COMPETITORS
                      if re.search(r"\b%s\b" % re.escape(n), lowered)), "")
        if named:
            return {}, "the %s caption names a competitor" % name
        out[p["key"]] = c
    return out, ""


def load_captions(row: dict) -> dict:
    """The thread's current captions, or {}."""
    try:
        data = json.loads((row or {}).get("captions_json") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _esc(value) -> str:
    """Slack treats &, < and > as markup."""
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _quote(body: str) -> str:
    return "\n".join("> %s" % ln if ln.strip() else ">"
                     for ln in _esc(body).splitlines())


CAPTIONS_FOOTER = (
    "_Reply with notes to change the captions. To change the video itself, "
    "start your message with *recut*. When they are right, reply *schedule "
    "it* and the posts go into the calendar._")


def captions_post(captions: dict, feature_name: str = "", notes: str = "",
                  revised: bool = False) -> str:
    """The Slack message that shows every caption for review."""
    head = ("Captions updated." if revised else
            "Captions for *%s*, one per platform." % _esc(feature_name)
            if feature_name else "Captions, one per platform.")
    lines = [head]
    for p in active():
        c = captions.get(p["key"]) or {}
        lines.append("")
        lines.append("*%s*  _(%s video)_" % (p["label"],
                                             p["aspect"].replace("x", ":")))
        if c.get("title"):
            lines.append("Title: *%s*" % _esc(c["title"]))
        lines.append(_quote(c.get("text", "")))
    if notes:
        lines += ["", "_%s_" % _esc(notes)]
    lines += ["", CAPTIONS_FOOTER]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The rendered files
# ---------------------------------------------------------------------------

def outputs_for(row: dict) -> dict:
    """{aspect: {"url", "bytes"}} for the thread's published render.

    From the build report kept on the thread. Videos built before that was
    kept have only their folder, so their links are rebuilt from Studio's
    naming (<feature>_<aspect>_<date>.mp4 under R2_PUBLIC_BASE), with the
    size found when the post is booked.
    """
    try:
        outputs = json.loads((row or {}).get("outputs_json") or "[]")
    except ValueError:
        outputs = []
    found = {o["aspect"]: {"url": o["url"], "bytes": int(o.get("bytes") or 0)}
             for o in outputs if isinstance(o, dict) and o.get("url")
             and o.get("aspect")}
    if found:
        return found
    folder = ((row or {}).get("render_folder") or "").strip("/")
    base = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")
    parts = folder.split("/")
    if not base or len(parts) < 3:
        return {}
    feature, date = parts[-2], parts[-1]
    return {a: {"url": "%s/%s/%s/%s_%s_%s.mp4" % (base, feature, date,
                                                  feature, a, date),
                "bytes": 0}
            for a in SIZES}


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------

def calendar() -> dict:
    """The posting calendar: SOCIAL_CALENDAR if it parses, else the default."""
    raw = os.environ.get("SOCIAL_CALENDAR", "").strip()
    if raw:
        try:
            cal = json.loads(raw)
            days = [d for d in cal.get("days") or [] if d in _DAYS]
            times = cal.get("times") or {}
            if days and all(p["key"] in times for p in active()):
                ZoneInfo(cal.get("zone") or DEFAULT_CALENDAR["zone"])
                return {"zone": cal.get("zone") or DEFAULT_CALENDAR["zone"],
                        "days": days, "times": times}
        except Exception as exc:
            print("[SOCIAL] SOCIAL_CALENDAR ignored: %s" % exc)
    return DEFAULT_CALENDAR


def next_slot(taken: set, today: dt.date, cal: dict | None = None) -> dt.date:
    """The first calendar day after today that no video holds yet.

    Never today: a post booked for this afternoon gives the social team no
    time to see it in Zoho before it goes out.
    """
    cal = cal or calendar()
    wanted = {_DAYS.index(d) for d in cal["days"]}
    day = today + dt.timedelta(days=1)
    for _ in range(366 * 2):
        if day.weekday() in wanted and day not in taken:
            return day
        day += dt.timedelta(days=1)
    raise RuntimeError("no free posting day in two years")


def post_times(day: dt.date, cal: dict | None = None) -> dict:
    """{platform key: aware datetime} for one video's day."""
    cal = cal or calendar()
    zone = ZoneInfo(cal["zone"])
    out = {}
    for p in active():
        hh, mm = (int(x) for x in str(cal["times"][p["key"]]).split(":"))
        out[p["key"]] = dt.datetime.combine(day, dt.time(hh, mm),
                                            tzinfo=zone)
    return out


# ---------------------------------------------------------------------------
# The posts table
# ---------------------------------------------------------------------------

_CREATE = """
CREATE TABLE IF NOT EXISTS social_posts (
  id             SERIAL PRIMARY KEY,
  thread_ts      VARCHAR NOT NULL,
  feature_name   TEXT    NOT NULL DEFAULT '',
  platform       VARCHAR NOT NULL,
  network        VARCHAR NOT NULL,
  aspect         VARCHAR NOT NULL,
  video_url      TEXT    NOT NULL DEFAULT '',
  title          TEXT    NOT NULL DEFAULT '',
  caption        TEXT    NOT NULL,
  slot_date      DATE    NOT NULL,
  post_at        TIMESTAMPTZ NOT NULL,
  status         VARCHAR NOT NULL DEFAULT 'planned',
  external_id    VARCHAR NOT NULL DEFAULT '',
  error          TEXT    NOT NULL DEFAULT '',
  scheduled_by   VARCHAR NOT NULL DEFAULT '',
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


def for_thread(thread_ts: str) -> list[dict]:
    """The thread's posts, in booking order."""
    if not DATABASE_URL:
        return []
    with _get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT * FROM social_posts WHERE thread_ts = :t "
                 "ORDER BY post_at, id"), {"t": thread_ts},
        ).mappings().all()
    return [dict(r) for r in rows]


def taken_days() -> set:
    """Every day a video already holds, so two never share one."""
    if not DATABASE_URL:
        return set()
    with _get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT slot_date FROM social_posts "
                 "WHERE status <> :failed"), {"failed": STATUS_FAILED},
        ).all()
    return {r[0] for r in rows}


def add(rows: list[dict]) -> list[dict]:
    """Insert posts. Returns them with their ids."""
    if not DATABASE_URL or not rows:
        return rows
    cols = ("thread_ts", "feature_name", "platform", "network", "aspect",
            "video_url", "title", "caption", "slot_date", "post_at",
            "status", "external_id", "error", "scheduled_by")
    out = []
    with _get_engine().begin() as conn:
        for r in rows:
            row = conn.execute(
                text("INSERT INTO social_posts (%s) VALUES (%s) RETURNING id"
                     % (", ".join(cols), ", ".join(":" + c for c in cols))),
                {c: r.get(c, "") for c in cols},
            ).first()
            out.append(dict(r, id=int(row[0])))
    return out


def set_status(post_id: int, status: str, external_id: str = "",
               error: str = "") -> None:
    if not DATABASE_URL:
        return
    with _get_engine().begin() as conn:
        conn.execute(
            text("UPDATE social_posts SET status = :s, external_id = :x, "
                 "error = :e, updated_at = NOW() WHERE id = :i"),
            {"s": status, "x": external_id or "", "e": (error or "")[:2000],
             "i": int(post_id)},
        )


def planned() -> list[dict]:
    """Posts in our calendar that are not yet in Buffer, oldest first."""
    if not DATABASE_URL:
        return []
    with _get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT * FROM social_posts WHERE status IN (:p, :f) "
                 "ORDER BY post_at, id"),
            {"p": STATUS_PLANNED, "f": STATUS_FAILED},
        ).mappings().all()
    return [dict(r) for r in rows]


def runway(today: dt.date) -> tuple[int, dt.date | None]:
    """(videos still to post after today, the last day one is booked)."""
    if not DATABASE_URL:
        return 0, None
    with _get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT COUNT(DISTINCT thread_ts), MAX(slot_date) "
                 "FROM social_posts WHERE slot_date > :d AND status <> :f"),
            {"d": today, "f": STATUS_FAILED},
        ).first()
    return (int(row[0] or 0), row[1]) if row else (0, None)


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

def _platform(key: str) -> dict:
    return next((p for p in PLATFORMS if p["key"] == key), {})


def buffer_post(post: dict, channel_id: str) -> dict:
    """Buffer's CreatePostInput for one post.

    Published automatically at the slot's time (not a reminder to post by
    hand), with the video by its public link. A YouTube title rides on the
    video's metadata and on YouTube's own, which is where Buffer reads it.
    """
    video = {"url": post["video_url"]}
    if post.get("title"):
        video["metadata"] = {"title": post["title"]}
    due = post["post_at"].astimezone(dt.timezone.utc)
    body = {
        "channelId": channel_id,
        "text": post["caption"],
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": due.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "assets": [{"video": video}],
    }
    meta = {k: dict(v) for k, v in
            _platform(post["platform"]).get("metadata", {}).items()}
    if post.get("title") and post["platform"] == "youtube":
        meta.setdefault("youtube", {})["title"] = post["title"]
    if meta:
        body["metadata"] = meta
    return body


def video_size(url: str) -> int:
    """The file's size in bytes, from a HEAD request, or 0."""
    import httpx
    try:
        # Cloudflare answers Python's default User-Agent with 403 on r2.dev.
        r = httpx.head(url, timeout=15, follow_redirects=True,
                       headers={"User-Agent": "VomeOS/1.0"})
        return int(r.headers.get("content-length") or 0) if r.is_success \
            else 0
    except Exception:
        return 0


def resolve_channels(conn) -> tuple[dict, dict]:
    """({platform key: Buffer channel id}, {platform key: why there is none}).

    BUFFER_CHANNELS ({"linkedin": "<channel id>", ...}) wins. Otherwise a
    platform takes the one connected channel of its service; two (a LinkedIn
    profile and a LinkedIn page, say) is a choice for a person, not a guess.
    """
    found = {}
    raw = os.environ.get("BUFFER_CHANNELS", "").strip()
    if raw:
        try:
            found = {k: str(v) for k, v in json.loads(raw).items() if v}
        except (ValueError, AttributeError) as exc:
            print("[SOCIAL] BUFFER_CHANNELS ignored: %s" % exc)
    wanted = [p for p in active() if p["key"] not in found]
    if not wanted:
        return found, {}
    res = conn.channels()
    if not res.ok:
        return found, {p["key"]: "could not list the Buffer channels: %s"
                       % res.error for p in wanted}
    live = [c for c in res.data or [] if isinstance(c, dict)
            and not c.get("isDisconnected")]
    problems = {}
    for p in wanted:
        match = [c for c in live
                 if str(c.get("service") or "").lower() == p["service"]]
        if len(match) == 1:
            found[p["key"]] = str(match[0].get("id") or "")
        elif not match:
            problems[p["key"]] = ("no %s channel is connected in Buffer"
                                  % p["label"])
        else:
            problems[p["key"]] = ("%d %s channels are connected in Buffer; "
                                  "set BUFFER_CHANNELS to pick one"
                                  % (len(match), p["label"]))
    return found, problems


def book(post: dict, conn, channel_id: str,
         size: int = 0) -> tuple[str, str, str]:
    """Book one post in Buffer. (status, Buffer post id, error). Outward
    facing.

    `conn` is a BufferSocial connector that is ready_to_post.
    """
    size = size or video_size(post["video_url"])
    if not size:
        return STATUS_FAILED, "", "could not read the video at %s" \
            % post["video_url"]
    res = conn.create_post(buffer_post(post, channel_id))
    if not res.ok:
        return STATUS_FAILED, "", res.error or "Buffer refused the post"
    out = res.data if isinstance(res.data, dict) else {}
    created = out.get("post") or {}
    if not created.get("id"):
        return STATUS_FAILED, "", str(out.get("message") or out
                                      or "Buffer refused the post")[:500]
    return STATUS_SCHEDULED, str(created["id"]), ""


def book_all(posts: list[dict], conn) -> None:
    """Book each post, recording its status, id and error on it."""
    channels, problems = resolve_channels(conn)
    for p in posts:
        channel = channels.get(p["platform"])
        if not channel:
            p.update(status=STATUS_FAILED, external_id="",
                     error=problems.get(p["platform"])
                     or "no Buffer channel for %s" % label(p["platform"]))
            continue
        status, post_id, error = book(p, conn, channel,
                                      size=p.get("bytes", 0))
        p.update(status=status, external_id=post_id, error=error)


def plan(row: dict, captions: dict, outputs: dict, day: dt.date,
         scheduled_by: str = "", cal: dict | None = None) -> list[dict]:
    """One post per platform for a video, on its day. Nothing is booked."""
    times = post_times(day, cal)
    posts = []
    for p in active():
        c = captions.get(p["key"]) or {}
        out = outputs.get(p["aspect"]) or {}
        posts.append({
            "thread_ts": row.get("thread_ts", ""),
            "feature_name": row.get("feature_name", ""),
            "platform": p["key"], "network": p["service"],
            "aspect": p["aspect"], "video_url": out.get("url", ""),
            "bytes": out.get("bytes", 0),
            "title": c.get("title", ""), "caption": c.get("text", ""),
            "slot_date": day, "post_at": times[p["key"]],
            "status": STATUS_PLANNED, "external_id": "", "error": "",
            "scheduled_by": scheduled_by,
        })
    return posts


def label(key: str) -> str:
    return next((p["label"] for p in PLATFORMS if p["key"] == key), key)
