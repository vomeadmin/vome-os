"""
buffer_check.py

Connect Buffer to the video studio, prove it takes our video posts, and book
the posts that were planned before it was connected.

Usage:
    py scripts/buffer_check.py check
        Read only. The organization, its channels, which channel each
        platform will post to, and the schema values the posts rely on.
    py scripts/buffer_check.py draft
        Creates one DRAFT per platform with a published render, so Buffer
        itself says whether it takes the video. A draft is never published;
        delete them in Buffer afterwards. Only on a person's say so.
    py scripts/buffer_check.py book
        Books every planned (or failed) post whose time has not passed.
        Outward facing: only on a person's say so.

Reads BUFFER_* and SOCIAL_PLATFORMS from .env (see
vomeos/integrations/buffer_social.py and video_social.py).
"""

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import video_social  # noqa: E402
from vomeos.integrations.buffer_social import BufferSocial  # noqa: E402

# A published render to test with, {aspect} filled per platform so each
# draft carries the file a real post would. The pilot is on R2 in 9:16 only.
SAMPLE = os.environ.get(
    "SOCIAL_SAMPLE_VIDEO",
    "https://pub-44ab2eeed6ca4f6d9a157f85e96d0b6d.r2.dev/email-formats-reveal/"
    "2026-10-17/email-formats-reveal_{aspect}_2026-10-17.mp4")

# The schema the posts rely on, printed by `check` so a guessed value is
# never the reason a real post fails.
ENUMS = ("PostType", "PostTypeFacebook", "SchedulingType", "ShareMode",
         "Service")
INPUTS = ("InstagramPostMetadataInput", "FacebookPostMetadataInput",
          "LinkedInPostMetadataInput", "YoutubePostMetadataInput",
          "TikTokPostMetadataInput", "VideoMetadataInput")


def _connector() -> BufferSocial:
    conn = BufferSocial()
    if not conn.configured():
        print("Set BUFFER_API_KEY first (publish.buffer.com/settings/api).")
        sys.exit(1)
    return conn


def cmd_check() -> None:
    conn = _connector()
    res = conn.organizations()
    print("organizations:", json.dumps(res.data if res.ok else res.error))
    if not res.ok:
        sys.exit(1)
    res = conn.channels()
    if not res.ok:
        print("channels: %s" % res.error)
        sys.exit(1)
    print("channels:")
    for c in res.data:
        print("  %-10s %-32s %s%s" % (
            c.get("service"), c.get("displayName") or c.get("name"),
            c.get("id"), "  (disconnected)" if c.get("isDisconnected")
            else ""))
    channels, problems = video_social.resolve_channels(conn)
    print("posting to:")
    for p in video_social.active():
        print("  %-15s %s" % (p["label"], channels.get(p["key"])
                              or "NONE: %s" % problems.get(p["key"], "")))
    print("schema:")
    for name in ENUMS:
        r = conn.describe(name)
        print("  %-27s %s" % (name, [v["name"] for v in
                                     r.data.get("enumValues") or []]
                              if r.ok else r.error))
    for name in INPUTS:
        r = conn.describe(name)
        fields = ["%s%s" % (f["name"], "!" if (f.get("type") or {}).get(
            "kind") == "NON_NULL" else "")
            for f in (r.data.get("inputFields") or [])] if r.ok else r.error
        print("  %-27s %s" % (name, fields))


def cmd_draft() -> None:
    conn = _connector()
    channels, problems = video_social.resolve_channels(conn)
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)
    for p in video_social.active():
        channel = channels.get(p["key"])
        if not channel:
            print("%-15s skipped: %s" % (p["label"], problems.get(p["key"])))
            continue
        url = SAMPLE.format(aspect=p["aspect"])
        if not video_social.video_size(url):
            print("%-15s skipped: no %s file at %s" % (p["label"],
                                                       p["aspect"], url))
            continue
        post = {"platform": p["key"], "video_url": url, "post_at": when,
                "title": "Test draft" if p.get("title_chars") else "",
                "caption": "Test draft from Vome OS, not for posting. "
                           "Delete me. #Volunteer #VolunteerManagement "
                           "#Nonprofit"}
        body = dict(video_social.buffer_post(post, channel), saveToDraft=True)
        res = conn.create_post(body)
        print("%-15s %s" % (p["label"], json.dumps(res.data) if res.ok
                            else res.error))


def cmd_book() -> None:
    conn = _connector()
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    posts = []
    for post in video_social.planned():
        if post["post_at"] < now:
            print("skip  #%d %s %s: its time has passed"
                  % (post["id"], post["feature_name"], post["platform"]))
        else:
            posts.append(post)
    video_social.book_all(posts, conn)
    for post in posts:
        video_social.set_status(post["id"], post["status"],
                                post["external_id"], post["error"])
        print("%-9s #%d %s %s %s" % (post["status"], post["id"],
                                     post["feature_name"], post["platform"],
                                     post["error"]))


if __name__ == "__main__":
    args = sys.argv[1:]
    commands = {"check": cmd_check, "draft": cmd_draft, "book": cmd_book}
    if len(args) == 1 and args[0] in commands:
        commands[args[0]]()
    else:
        print(__doc__)
        sys.exit(2)
