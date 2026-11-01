"""Tests for the social half of the video studio: captions, the calendar,
booking, and the thread replies that lead to them.

What is worth guarding here is the step onto public feeds. "schedule it"
puts words on five platforms under Vome's name, so it is matched on the whole
message like a greenlight, and the caption checks run before a person ever
reads a caption. A caption with a competitor's name or a dead link in it must
never be the one a tired reviewer approves.

No test reaches Buffer, Slack, a model or the database.
"""

import datetime as dt
import json
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-not-real")

import video_social as vs  # noqa: E402

ALL = ",".join(vs.PLATFORM_KEYS)


@pytest.fixture(autouse=True)
def every_platform(monkeypatch):
    """Every rule is tested on every platform; the three posted to by
    default have tests of their own below."""
    monkeypatch.setenv("SOCIAL_PLATFORMS", ALL)
    monkeypatch.delenv("BUFFER_CHANNELS", raising=False)


def _good() -> dict:
    """A set of captions that passes every check."""
    tags = "#VolunteerManagement #VolunteerCoordinator #Nonprofit"
    return {"captions": {
        "linkedin": {"text": "Every volunteer gets the right application "
                             "form in Vome.\n\nhttps://www.vomevolunteer.com"},
        "facebook": {"text": "Send each volunteer to the right form, "
                             "automatically. https://www.vomevolunteer.com"},
        "instagram": {"text": "The right form for every volunteer.\n\n"
                              "Link in bio\n\n" + tags},
        "tiktok": {"text": "The right form for every volunteer.\n" + tags},
        "youtube": {"title": "The right form for every volunteer",
                    "text": "Vome routes each volunteer applicant. "
                            "#VolunteerManagement"},
    }}


def _with(platform, **fields) -> dict:
    data = _good()
    data["captions"][platform].update(fields)
    return data


# ------------------------------------------------------------------ checks

def test_good_captions_pass():
    captions, why = vs.check_captions(_good())
    assert why == "" and set(captions) == set(vs.PLATFORM_KEYS)


def test_tiktok_is_flattened_to_one_paragraph():
    """TikTok drops line breaks on publish; the reviewer should read what
    will actually be posted."""
    captions, _ = vs.check_captions(_good())
    assert "\n" not in captions["tiktok"]["text"]


@pytest.mark.parametrize("data, reason", [
    ({"captions": {}}, "no LinkedIn caption"),
    ("not json", "no captions"),
    (_with("facebook", text="Volunteers — sorted. "
                            "https://www.vomevolunteer.com"), "dash"),
    (_with("linkedin", text="Send each applicant the right form."),
     "does not say 'volunteer'"),
    (_with("instagram", text="Every volunteer. www.vomevolunteer.com "
                             "#A #B #C"), "link"),
    (_with("instagram", text="Every volunteer. #A #B #C #D #E #F"),
     "6 hashtags"),
    (_with("tiktok", text="Every volunteer, the right form. #A"),
     "1 hashtags"),
    (_with("youtube", title=""), "no title"),
    (_with("youtube", title="Volunteer " + "x" * 100), "title is"),
    (_with("facebook", text="Volunteer forms, unlike Better Impact."),
     "competitor"),
    (_with("facebook", text="Volunteer " * 80), "characters"),
])
def test_bad_captions_are_refused_with_a_reason(data, reason):
    captions, why = vs.check_captions(data)
    assert captions == {}
    assert reason in why


def test_ordinary_phrases_are_not_competitors():
    """'your volunteer hub' is a fair thing to say."""
    data = _with("facebook", text="Your volunteer hub, in one place. "
                                  "https://www.vomevolunteer.com")
    assert vs.check_captions(data)[1] == ""


def test_the_agent_reads_the_numbers_the_code_checks():
    brief = vs.platforms_brief()
    for p in vs.PLATFORMS:
        assert "at most %d characters" % p["max_chars"] in brief
        assert "%d to %d hashtags" % p["hashtags"] in brief


def test_captions_post_shows_every_platform_and_the_way_on():
    captions, _ = vs.check_captions(_good())
    post = vs.captions_post(captions, feature_name="Recruitment workflows")
    for p in vs.PLATFORMS:
        assert "*%s*" % p["label"] in post
    assert "schedule it" in post and "recut" in post
    assert "—" not in post


# --------------------------------------------------------------- platforms

def test_linkedin_facebook_and_instagram_by_default(monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    assert [p["key"] for p in vs.active()] == ["linkedin", "facebook",
                                               "instagram"]


@pytest.mark.parametrize("raw, keys", [
    ("instagram, TikTok", ["instagram", "tiktok"]),
    ("myspace", ["linkedin", "facebook", "instagram"]),
])
def test_social_platforms_picks_them(monkeypatch, raw, keys):
    monkeypatch.setenv("SOCIAL_PLATFORMS", raw)
    assert [p["key"] for p in vs.active()] == keys


def test_only_the_active_platforms_are_asked_for_checked_and_shown(
        monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    brief = vs.platforms_brief()
    assert "TikTok" not in brief and "YouTube" not in brief
    data = _good()
    del data["captions"]["tiktok"], data["captions"]["youtube"]
    captions, why = vs.check_captions(data)
    assert why == "" and set(captions) == {"linkedin", "facebook",
                                           "instagram"}
    assert "TikTok" not in vs.captions_post(captions)


def test_a_caption_for_a_platform_not_posted_to_is_dropped(monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    captions, _ = vs.check_captions(_with("tiktok", text="Volunteers #A"))
    assert "tiktok" not in captions


def test_the_calendar_needs_times_only_for_active_platforms(monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    cal = {"zone": "America/Montreal", "days": ["wed"],
           "times": {"linkedin": "8:00", "facebook": "9:00",
                     "instagram": "10:00"}}
    monkeypatch.setenv("SOCIAL_CALENDAR", json.dumps(cal))
    assert vs.calendar()["days"] == ["wed"]
    assert set(vs.post_times(dt.date(2026, 11, 4))) == {
        "linkedin", "facebook", "instagram"}


# ---------------------------------------------------------------- calendar

def test_next_slot_is_never_today():
    tue = dt.date(2026, 11, 3)
    assert tue.weekday() == 1
    assert vs.next_slot(set(), tue, vs.DEFAULT_CALENDAR) == dt.date(2026, 11,
                                                                    5)


def test_next_slot_skips_days_another_video_holds():
    mon = dt.date(2026, 11, 2)
    taken = {dt.date(2026, 11, 3), dt.date(2026, 11, 5)}
    thu_next = dt.date(2026, 11, 10)
    assert vs.next_slot(taken, mon, vs.DEFAULT_CALENDAR) == thu_next


def test_post_times_are_local_to_the_calendar():
    times = vs.post_times(dt.date(2026, 11, 3), vs.DEFAULT_CALENDAR)
    assert times["linkedin"].tzinfo == ZoneInfo("America/Montreal")
    assert times["linkedin"].hour == 9
    assert set(times) == set(vs.PLATFORM_KEYS)


def test_the_social_team_can_set_the_calendar(monkeypatch):
    cal = {"zone": "America/Vancouver", "days": ["mon", "wed", "fri"],
           "times": {k: "10:00" for k in vs.PLATFORM_KEYS}}
    monkeypatch.setenv("SOCIAL_CALENDAR", json.dumps(cal))
    assert vs.calendar()["days"] == ["mon", "wed", "fri"]


@pytest.mark.parametrize("raw", ["not json", '{"days": ["mon"]}',
                                 '{"days": ["someday"], "times": {}}'])
def test_a_broken_calendar_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("SOCIAL_CALENDAR", raw)
    assert vs.calendar() == vs.DEFAULT_CALENDAR


# ----------------------------------------------------------------- outputs

def test_outputs_come_from_the_build_report():
    row = {"outputs_json": json.dumps([
        {"aspect": "9x16", "url": "https://r2/a_9x16.mp4", "bytes": 4000},
        {"aspect": "4x5", "url": "", "bytes": 1}])}
    assert vs.outputs_for(row) == {"9x16": {"url": "https://r2/a_9x16.mp4",
                                            "bytes": 4000}}


def test_older_videos_get_links_from_their_folder(monkeypatch):
    """Built before the report was kept: Studio's naming rebuilds them."""
    monkeypatch.setenv("R2_PUBLIC_BASE", "https://pub.r2.dev/")
    row = {"render_folder": "renders/email-formats-reveal/2026-10-17"}
    out = vs.outputs_for(row)
    assert out["9x16"]["url"] == (
        "https://pub.r2.dev/email-formats-reveal/2026-10-17/"
        "email-formats-reveal_9x16_2026-10-17.mp4")


def test_no_base_means_no_guessed_links(monkeypatch):
    monkeypatch.delenv("R2_PUBLIC_BASE", raising=False)
    assert vs.outputs_for({"render_folder": "renders/x/2026-10-17"}) == {}


# ----------------------------------------------------------------- booking

def _post(platform="youtube"):
    captions, _ = vs.check_captions(_good())
    outputs = {a: {"url": "https://r2/v_%s.mp4" % a, "bytes": 5}
               for a in vs.SIZES}
    posts = vs.plan({"thread_ts": "1.2", "feature_name": "F"}, captions,
                    outputs, dt.date(2026, 11, 3), cal=vs.DEFAULT_CALENDAR)
    return next(p for p in posts if p["platform"] == platform)


def test_plan_gives_each_platform_its_own_file():
    by = {p: _post(p) for p in vs.PLATFORM_KEYS}
    assert by["linkedin"]["video_url"].endswith("4x5.mp4")
    assert by["tiktok"]["video_url"].endswith("9x16.mp4")
    assert all(p["status"] == vs.STATUS_PLANNED for p in by.values())


def test_buffer_post_carries_the_video_and_the_time():
    post = _post("youtube")
    body = vs.buffer_post(post, "ch-1")
    assert body["channelId"] == "ch-1" and body["text"] == post["caption"]
    assert body["schedulingType"] == "automatic"
    assert body["mode"] == "customScheduled"
    # 15:00 in Montreal on November 3 is 20:00 UTC.
    assert body["dueAt"] == "2026-11-03T20:00:00.000Z"
    assert body["assets"] == [{"video": {
        "url": post["video_url"], "metadata": {"title": post["title"]}}}]
    assert body["metadata"] == {"youtube": {"title": post["title"]}}


def test_facebook_goes_up_as_a_feed_post():
    body = vs.buffer_post(_post("facebook"), "ch-3")
    assert body["metadata"] == {"facebook": {"type": "post"}}


def test_instagram_goes_up_as_a_reel_on_the_feed():
    body = vs.buffer_post(_post("instagram"), "ch-2")
    assert body["metadata"] == {"instagram": {"type": "reel",
                                              "shouldShareToFeed": True}}
    assert "metadata" not in body["assets"][0]["video"]


CHANNELS = [
    {"id": "li", "service": "linkedin", "isDisconnected": False},
    {"id": "fb", "service": "facebook", "isDisconnected": False},
    {"id": "ig", "service": "instagram", "isDisconnected": False},
    {"id": "ig-old", "service": "instagram", "isDisconnected": True},
]


class _Buffer:
    def __init__(self, result=None, channels=None):
        self.result, self.sent = result, []
        self.listed = SimpleNamespace(ok=True, error="",
                                      data=CHANNELS if channels is None
                                      else channels)

    def channels(self):
        return self.listed

    def create_post(self, post):
        self.sent.append(post)
        return self.result


def _created(post_id="777"):
    return SimpleNamespace(ok=True, error="", data={
        "__typename": "PostActionSuccess", "post": {"id": post_id}})


def test_each_platform_finds_its_one_connected_channel(monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    found, problems = vs.resolve_channels(_Buffer())
    assert found == {"linkedin": "li", "facebook": "fb", "instagram": "ig"}
    assert problems == {}


def test_two_channels_for_one_platform_is_a_choice_not_a_guess(
        monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    two = CHANNELS + [{"id": "li-2", "service": "linkedin"}]
    found, problems = vs.resolve_channels(_Buffer(channels=two))
    assert "linkedin" not in found
    assert "BUFFER_CHANNELS" in problems["linkedin"]


def test_buffer_channels_picks_one(monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    monkeypatch.setenv("BUFFER_CHANNELS", '{"linkedin": "li-page"}')
    two = CHANNELS + [{"id": "li-2", "service": "linkedin"}]
    found, problems = vs.resolve_channels(_Buffer(channels=two))
    assert found["linkedin"] == "li-page" and problems == {}


def test_a_platform_with_no_channel_says_so():
    found, problems = vs.resolve_channels(_Buffer())
    assert "tiktok" not in found
    assert "no TikTok channel" in problems["tiktok"]


def test_book_without_a_readable_video_sends_nothing(monkeypatch):
    monkeypatch.setattr(vs, "video_size", lambda url: 0)
    conn = _Buffer()
    status, _, error = vs.book(_post(), conn, "ch")
    assert status == vs.STATUS_FAILED and conn.sent == []
    assert "could not read the video" in error


def test_book_reports_a_failed_call():
    conn = _Buffer(SimpleNamespace(ok=False, data=None, error="401 nope"))
    assert vs.book(_post(), conn, "ch", size=9) == (vs.STATUS_FAILED, "",
                                                    "401 nope")


def test_book_reports_buffers_refusal():
    conn = _Buffer(SimpleNamespace(ok=True, error="", data={
        "__typename": "MutationError", "message": "Video too long"}))
    assert vs.book(_post(), conn, "ch", size=9) == (vs.STATUS_FAILED, "",
                                                    "Video too long")


def test_book_keeps_buffers_post_id():
    assert vs.book(_post(), _Buffer(_created()), "ch", size=9) == (
        vs.STATUS_SCHEDULED, "777", "")


def test_book_all_fails_only_the_platform_with_no_channel(monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    conn = _Buffer(_created(), channels=CHANNELS[:2])
    posts = [_post(k) for k in ("linkedin", "facebook", "instagram")]
    vs.book_all(posts, conn)
    assert [p["status"] for p in posts] == [vs.STATUS_SCHEDULED] * 2 + [
        vs.STATUS_FAILED]
    assert "no Instagram channel" in posts[2]["error"]
    assert [b["channelId"] for b in conn.sent] == ["li", "fb"]


# ----------------------------------------------------------------- handler

@pytest.fixture(scope="module")
def handler():
    import slack_video_studio_handler as h
    return h


CAPS = "awaiting_captions"


@pytest.mark.parametrize("text", [
    "schedule it", "Schedule it", "ok, schedule it please", "schedule",
    "queue it", "<@U1> schedule them",
])
def test_schedule_is_the_whole_message(handler, text):
    assert handler.classify(text, CAPS) == "schedule"


@pytest.mark.parametrize("text", [
    "schedule it for Tuesday instead",
    "before you schedule it, shorten the TikTok one",
    "make the LinkedIn one warmer",
])
def test_a_note_that_mentions_scheduling_is_a_note(handler, text):
    assert handler.classify(text, CAPS) == "caption_note"


@pytest.mark.parametrize("text", ["approve", "go build it", "ship it",
                                  "I don't like it"])
def test_approval_words_get_the_options_not_a_booking(handler, text):
    """'approve' could mean the video as easily as the posts."""
    assert handler.classify(text, CAPS) == "captions_options"


def test_recut_is_a_note_on_the_video(handler):
    assert handler.classify("recut, make the hook a question", CAPS) == "recut"
    assert handler.classify("Re-cut: slower ending", CAPS) == "recut"


def test_captions_can_be_asked_for_on_an_older_video(handler):
    assert handler.classify("write the captions", "delivered") == "captions"
    assert handler.classify("schedule it", "delivered") == "captions_first"


def test_a_note_on_a_delivered_video_still_revises_the_script(handler):
    assert handler.classify("make the hook shorter", "delivered") == "note"


def test_nothing_runs_while_captions_are_being_written(handler):
    assert handler.classify("schedule it", "captioning") == "busy_captions"


@pytest.mark.parametrize("text", ["schedule it", "scrap this",
                                  "start over", "make it warmer"])
def test_a_scheduled_video_points_to_buffer(handler, text):
    assert handler.classify(text, "scheduled") == "scheduled"


def test_forget_still_works_after_scheduling(handler):
    assert handler.classify("forget that", "scheduled") == "forget"


# -------------------------------------------------------------------- jobs

@pytest.fixture()
def jobs(monkeypatch):
    import video_studio_jobs as j
    monkeypatch.setattr(j.video_marketing, "playbook",
                        lambda strategy=False: "PLAYBOOK")
    monkeypatch.setattr(j.video_lessons, "render_for",
                        lambda role: "(none yet)")
    return j


STORYBOARD = {"id": "kiosk", "feature_name": "Kiosk",
              "scenes": [{"id": "hook", "template": "hook", "lines": []}]}
ROW = {"thread_ts": "1.2", "feature_name": "Kiosk",
       "storyboard": "kiosk", "storyboard_json": json.dumps(STORYBOARD)}


def _agent(monkeypatch, *answers):
    """The copywriter, answering in order; records each context."""
    import vomeos.runner as runner
    seen, queue = [], list(answers)

    def run(agent, context, **k):
        seen.append(dict(context))
        return SimpleNamespace(ok=True, blocked=False, data=queue.pop(0),
                               why=lambda: "")
    monkeypatch.setattr(runner, "run_agent", run)
    return seen


def test_a_refused_set_is_retried_once_with_the_reason(jobs, monkeypatch):
    seen = _agent(monkeypatch, _with("tiktok", text="Volunteers #A"), _good())
    out = jobs.run_captions(dict(ROW))
    assert out["ok"] is True
    assert "1 hashtags" in seen[1]["note"]
    assert seen[0]["platforms"] == vs.platforms_brief()


def test_refused_twice_is_an_error_not_a_post(jobs, monkeypatch):
    bad = _with("instagram", text="Volunteers www.x.com #A #B #C")
    _agent(monkeypatch, bad, bad)
    out = jobs.run_captions(dict(ROW))
    assert out["ok"] is False and "refused twice" in out["error"]


def test_a_note_sends_the_captions_it_is_about(jobs, monkeypatch):
    seen = _agent(monkeypatch, _good())
    row = dict(ROW, captions_json=json.dumps({"linkedin": {"text": "old"}}))
    jobs.run_captions(row, note="warmer please")
    assert '"old"' in seen[0]["previous_captions"]
    assert seen[0]["note"] == "warmer please"


def test_a_built_video_goes_on_to_captions(jobs, monkeypatch):
    report = {"ok": True, "folder": "renders/kiosk/2026-10-30",
              "outputs": [{"aspect": "9x16", "url": "https://r2/k.mp4",
                           "bytes": 4, "where": "Reels"}]}
    saved, captioned = {}, []
    monkeypatch.setattr(jobs, "_studio", lambda: None)
    monkeypatch.setattr(jobs, "_local_studio", lambda: "x")
    monkeypatch.setattr(jobs, "_studio_json", lambda *a, **k: report)
    monkeypatch.setattr(jobs, "_delivery_post", lambda r: "built")
    monkeypatch.setattr(jobs, "_post", lambda *a: None)
    monkeypatch.setattr(jobs.video_threads, "get", lambda t: dict(ROW))
    monkeypatch.setattr(jobs.video_threads, "update",
                        lambda t, **f: saved.update(f))
    monkeypatch.setattr(jobs, "write_captions", captioned.append)
    jobs.build_video("1.2")
    assert captioned == ["1.2"]
    assert json.loads(saved["outputs_json"])[0]["url"] == "https://r2/k.mp4"


def _scheduling(jobs, monkeypatch, row, conn=None):
    posted, added, states = [], [], []
    monkeypatch.setattr(jobs.video_threads, "get", lambda t: row)
    monkeypatch.setattr(jobs.video_threads, "update",
                        lambda t, **f: states.append(f.get("state")))
    monkeypatch.setattr(jobs, "_post", lambda t, m: posted.append(m))
    monkeypatch.setattr(jobs, "_buffer", lambda: conn)
    monkeypatch.setattr(jobs.video_social, "taken_days", lambda: set())
    monkeypatch.setattr(jobs.video_social, "add", added.extend)
    monkeypatch.setattr(jobs.video_social, "runway",
                        lambda today: (1, today + dt.timedelta(days=2)))
    jobs.schedule_video("1.2", "U1")
    return posted, added, states


def _captioned_row():
    captions, _ = vs.check_captions(_good())
    outputs = [{"aspect": a, "url": "https://r2/k_%s.mp4" % a, "bytes": 5}
               for a in vs.SIZES]
    return dict(ROW, captions_json=json.dumps(captions),
                outputs_json=json.dumps(outputs))


def test_without_buffer_the_posts_are_planned_not_booked(jobs,
                                                         monkeypatch):
    posted, added, states = _scheduling(jobs, monkeypatch, _captioned_row())
    assert len(added) == len(vs.PLATFORMS)
    assert {p["status"] for p in added} == {vs.STATUS_PLANNED}
    assert {p["scheduled_by"] for p in added} == {"U1"}
    assert "not connected" in posted[0]
    assert states[-1] == "scheduled"


def test_with_buffer_every_post_is_booked(jobs, monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    monkeypatch.setattr(vs, "video_size", lambda url: 9)
    conn = _Buffer(_created("9"))
    posted, added, _ = _scheduling(jobs, monkeypatch, _captioned_row(), conn)
    assert len(conn.sent) == 3
    assert {p["status"] for p in added} == {vs.STATUS_SCHEDULED}
    assert {p["external_id"] for p in added} == {"9"}
    assert "Scheduled in Buffer" in posted[0]


def test_all_refused_keeps_nothing_and_frees_the_thread(jobs, monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORMS")
    monkeypatch.setattr(vs, "video_size", lambda url: 9)
    conn = _Buffer(SimpleNamespace(ok=False, data=None, error="401 bad key"))
    posted, added, states = _scheduling(jobs, monkeypatch, _captioned_row(),
                                        conn)
    assert added == [] and states[-1] == "awaiting_captions"
    assert "refused all" in posted[0] and "401 bad key" in posted[0]


def test_no_published_files_means_nothing_is_booked(jobs, monkeypatch):
    row = dict(_captioned_row(), outputs_json="", render_folder="")
    posted, added, states = _scheduling(jobs, monkeypatch, row)
    assert added == [] and "cannot schedule" in posted[0]
    assert states[-1] == "awaiting_captions"


def test_a_crashed_schedule_frees_the_thread(handler, monkeypatch):
    """Stuck at 'scheduled', every later reply would say it is booked."""
    import video_studio_jobs as j

    def boom(*a):
        raise RuntimeError("db down")
    states, acks = [], []
    monkeypatch.setattr(j, "schedule_video", boom)
    monkeypatch.setattr(handler.video_threads, "update",
                        lambda t, **f: states.append(f.get("state")))
    monkeypatch.setattr(handler, "_ack", lambda t, m: acks.append(m))
    handler._schedule_video("1.2", "U1")
    assert states == ["awaiting_captions"]
    assert "Check Buffer" in acks[0]


def test_a_crashed_caption_run_frees_the_thread(handler, monkeypatch):
    import video_studio_jobs as j

    def boom(*a):
        raise RuntimeError("model down")
    states = []
    monkeypatch.setattr(j, "write_captions", boom)
    monkeypatch.setattr(handler.video_threads, "get", lambda t: {})
    monkeypatch.setattr(handler.video_threads, "update",
                        lambda t, **f: states.append(f.get("state")))
    monkeypatch.setattr(handler, "_ack", lambda t, m: None)
    handler._write_captions("1.2", "")
    assert states == ["delivered"]


# ------------------------------------------------------------------ pickup

@pytest.mark.parametrize("text, state", [
    ("caption recruitment-workflows", None),
    ("Caption recruitment-workflows.", None),
    ("<@U1> post email-formats-reveal", None),
    ("caption recruitment-workflows", "idea"),
])
def test_naming_a_built_video_picks_it_up(handler, text, state):
    assert handler.classify(text, state) == "pickup"


@pytest.mark.parametrize("text, state", [
    ("caption recruitment-workflows", "awaiting_captions"),
    ("a video about kiosks", None),
    ("caption kiosks", None),
])
def test_anything_else_is_not_a_pickup(handler, text, state):
    assert handler.classify(text, state) != "pickup"


def _pickup(jobs, monkeypatch, result):
    posted, saved, captioned = [], {}, []
    studio = SimpleNamespace(video=lambda slug: result)
    monkeypatch.setattr(jobs, "_studio", lambda: studio)
    monkeypatch.setattr(jobs.video_threads, "update",
                        lambda t, **f: saved.update(f))
    monkeypatch.setattr(jobs, "_post", lambda t, m: posted.append(m))
    monkeypatch.setattr(jobs, "write_captions",
                        lambda t, note="": captioned.append(t))
    jobs.pickup_video("1.2", "kiosk")
    return posted, saved, captioned


def test_a_picked_up_video_is_saved_and_captioned(jobs, monkeypatch):
    render = {"folder": "renders/kiosk/2026-10-31", "date": "2026-10-31",
              "outputs": [{"aspect": "4x5", "url": "https://r2/k.mp4",
                           "bytes": 9}]}
    posted, saved, captioned = _pickup(jobs, monkeypatch, SimpleNamespace(
        ok=True, error="", data={"storyboard": STORYBOARD, "render": render}))
    assert saved["state"] == "delivered" and saved["storyboard"] == "kiosk"
    assert json.loads(saved["storyboard_json"]) == STORYBOARD
    assert saved["render_folder"] == "renders/kiosk/2026-10-31"
    assert json.loads(saved["outputs_json"])[0]["url"] == "https://r2/k.mp4"
    assert "Picked up *Kiosk*" in posted[0] and captioned == ["1.2"]


def test_an_unknown_video_frees_the_thread(jobs, monkeypatch):
    posted, saved, captioned = _pickup(jobs, monkeypatch, SimpleNamespace(
        ok=False, error="404 no storyboard", data=None))
    assert saved["state"] == "idea" and captioned == []
    assert "could not find" in posted[0]


def test_a_video_with_no_published_files_is_not_captioned(
        jobs, monkeypatch):
    posted, saved, captioned = _pickup(jobs, monkeypatch, SimpleNamespace(
        ok=True, error="", data={"storyboard": STORYBOARD, "render": None}))
    assert saved["state"] == "idea" and captioned == []
    assert "no published files" in posted[0]
