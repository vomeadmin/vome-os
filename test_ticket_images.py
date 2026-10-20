"""
test_ticket_images.py

Item one of the agent roadmap: an agent that is told "an attachment exists"
and cannot see it reasons as if there were none (#9415). The cases here are
the ones that would quietly put that back: a capital-I media type the API
rejects, a signature logo shown forty times, an agent without the grant being
shown images anyway, and a screen recording reported as "nothing sent".
"""

import base64

import pytest

import ticket_images as ti
from vomeos import images as vi
from vomeos import runner

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20_000
JPEG = b"\xff\xd8\xff" + b"y" * 20_000


# ---------------------------------------------------------------------------
# Kernel: vomeos.images
# ---------------------------------------------------------------------------

def test_zoho_capital_media_type_is_normalised():
    assert vi.normalize_media_type("Image/PNG; charset=x") == "image/png"
    assert vi.normalize_media_type("image/jpg") == "image/jpeg"


def test_bytes_beat_a_wrong_label():
    kept, _ = vi.prepare([vi.Image("application/octet-stream", PNG)], 6)
    assert kept[0].media_type == "image/png"


def test_unsupported_and_oversized_are_dropped_with_a_reason():
    big = b"\x89PNG\r\n\x1a\n" + b"z" * (vi.MAX_BYTES + 1)
    kept, dropped = vi.prepare([
        vi.Image("image/tiff", b"II*\x00" + b"q" * 100, "scan"),
        vi.Image("image/png", big, "huge"),
    ], 6)
    assert kept == []
    assert any("scan" in d for d in dropped)
    assert any("huge" in d for d in dropped)


def test_duplicates_are_shown_once():
    kept, _ = vi.prepare([vi.Image("image/png", PNG, "a"),
                          vi.Image("image/png", PNG, "quoted a")], 6)
    assert len(kept) == 1


def test_limit_keeps_the_first():
    many = [vi.Image("image/png", PNG + bytes([i]), f"n{i}") for i in range(5)]
    kept, dropped = vi.prepare(many, 2)
    assert [k.label for k in kept] == ["n0", "n1"]
    assert len(dropped) == 3


def test_content_blocks_text_first_then_labelled_images():
    blocks = vi.content_blocks(
        "ticket", [vi.Image("image/png", PNG, "pasted")]
    )
    assert blocks[0] == {"type": "text", "text": "ticket"}
    assert "pasted" in blocks[1]["text"]
    assert blocks[2]["type"] == "image"
    assert blocks[2]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[2]["source"]["data"]) == PNG


def test_no_images_sends_plain_text():
    assert vi.content_blocks("ticket", []) == "ticket"


# ---------------------------------------------------------------------------
# Kernel: the runner honours the manifest grant
# ---------------------------------------------------------------------------

def _capture(monkeypatch):
    sent = {}

    def fake_call(manifest, system, user, model):
        sent["user"] = user
        return '{"category": "clear_bug"}', 1, 1, 1

    monkeypatch.setattr(runner, "_call_model", fake_call)
    # Triage also has code tools, so it goes through the tool loop.
    monkeypatch.setattr(runner, "_call_with_tools", fake_call)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    return sent


def _context():
    return {k: "x" for k in (
        "ticket_number", "account", "thread", "attachments",
        "existing_tasks", "kb_matches",
    )}


def test_triage_director_is_shown_images(monkeypatch):
    sent = _capture(monkeypatch)
    runner.run_agent(
        "customer_success.triage_director", _context(),
        images=[vi.Image("Image/png", PNG, "pasted in Dalena's message")],
    )
    assert isinstance(sent["user"], list)
    assert sent["user"][2]["type"] == "image"


def test_an_agent_without_the_grant_sees_text_only(monkeypatch):
    sent = _capture(monkeypatch)
    runner.run_agent(
        "support.duplicate_reply_check",
        {"draft": "x", "thread": "y"},
        images=[vi.Image("image/png", PNG)],
    )
    assert isinstance(sent["user"], str)


# ---------------------------------------------------------------------------
# App: finding the screenshots in a Zoho thread
# ---------------------------------------------------------------------------

DALENA = (
    '<div><img style="max-width: 1208px; height: auto" '
    'src="/api/v1/threads/1/inlineImages/shot?et=1&amp;f=3.png" '
    'width="981" height="194"/></div>'
    '<img width="192" height="99" style="width: 192px; height: 99px" '
    'src="/api/v1/threads/1/inlineImages/logo?f=2.png"/>'
    '<img width="415" height="46" '
    'src="/api/v1/threads/1/inlineImages/banner?f=1.png"/>'
    '<img src="https://tracker.example.com/pixel.gif" width="1" height="1">'
)


def test_inline_sources_finds_only_zoho_hosted_images():
    found = ti.inline_sources(DALENA)
    assert [s for s, _, _ in found] == [
        "/api/v1/threads/1/inlineImages/shot?et=1&f=3.png",
        "/api/v1/threads/1/inlineImages/logo?f=2.png",
        "/api/v1/threads/1/inlineImages/banner?f=1.png",
    ]


@pytest.mark.parametrize("w,h,signature", [
    (981, 194, False),   # #9415's screenshot strip
    (192, 99, True),     # MLH logo
    (415, 46, True),     # MLH banner
    (0, 0, False),       # no size given: usually a full screenshot
    (1200, 800, False),
])
def test_signature_detection(w, h, signature):
    assert ti.looks_like_signature(w, h) is signature


def test_style_dimensions_are_read_when_attributes_are_missing():
    attrs = {"style": "max-width: 1208px; width: 300px; height: 40px"}
    assert ti._dimensions(attrs) == (300, 40)


def _fake_zoho(monkeypatch, threads, details, files):
    def get_json(path, params=None):
        if path.endswith("/threads"):
            return {"data": threads}
        return details[path.rsplit("/", 1)[1]]

    def download(url):
        return files[url]

    monkeypatch.setattr(ti, "_get_json", get_json)
    monkeypatch.setattr(ti, "_download", download)


def test_fetch_reads_inline_and_attached_and_skips_signatures(monkeypatch):
    _fake_zoho(
        monkeypatch,
        threads=[
            {"id": "t1", "direction": "in", "createdTime": "2026-10-15T14:44",
             "author": {"name": "Phan, Dalena"}},
            {"id": "t0", "direction": "out", "createdTime": "2026-10-12"},
        ],
        details={"t1": {
            "content": DALENA,
            "attachments": [
                {"name": "error.jpg", "href": "https://desk/att/1"},
                {"name": "Recording.webm", "href": "https://desk/att/2"},
            ],
        }},
        files={
            "/api/v1/threads/1/inlineImages/shot?et=1&f=3.png":
                (PNG, "Image/png"),
            "https://desk/att/1": (JPEG, "image/jpeg"),
        },
    )
    images, note = ti.fetch_ticket_images("1")
    assert [i.label for i in images] == [
        "pasted in Phan, Dalena's message of 2026-10-15",
        "attachment error.jpg from Phan, Dalena, 2026-10-15",
    ]
    assert "2 image(s)" in note
    assert "2 email signature" in note
    # The recording is evidence the agent cannot open, not an absence.
    assert "Recording.webm" in note and "not viewable" in note


def test_a_failed_listing_is_a_note_not_a_crash(monkeypatch):
    def boom(path, params=None):
        raise RuntimeError("Zoho 503")

    monkeypatch.setattr(ti, "_get_json", boom)
    images, note = ti.fetch_ticket_images("1")
    assert images == [] and "could not be read" in note


FORM_LOG = (
    b"2026-10-15T16:23:14Z form load ok\n"
    b"2026-10-15T16:23:16Z POST /api/v1/forms/submit 500\n"
    b"TypeError: Cannot read properties of undefined (reading 'id')\n"
)


def test_an_attached_error_log_is_read_into_the_note(monkeypatch):
    # #9533: the form's exported error log was listed as unviewable, so the
    # code check never saw the error it held.
    _fake_zoho(
        monkeypatch,
        threads=[{"id": "t1", "direction": "in",
                  "createdTime": "2026-10-15T16:30",
                  "author": {"name": "Elsa"}}],
        details={"t1": {"content": "", "attachments": [
            {"name": "vome-form-error-logs-2026-10-15.txt",
             "href": "https://desk/att/log"},
            {"name": "Recording.webm", "href": "https://desk/att/rec"},
        ]}},
        files={"https://desk/att/log": (FORM_LOG, "text/plain")},
    )
    images, note = ti.fetch_ticket_images("1")
    assert images == []
    assert "1 attached text file(s)" in note
    assert "POST /api/v1/forms/submit 500" in note
    assert "vome-form-error-logs-2026-10-15.txt from Elsa, 2026-10-15" in note
    # The recording still cannot be opened and is still named.
    assert "Recording.webm" in note and "not viewable" in note
    assert "vome-form-error-logs" not in note.split("Contents of")[0]


def test_a_long_log_keeps_its_start_and_end():
    data = b"START\n" + b"x" * 20_000 + b"\nTHE ERROR AT THE END"
    out = ti.text_excerpt(data, limit=1_000)
    assert out.startswith("START")
    assert out.endswith("THE ERROR AT THE END")
    assert "characters in all, trimmed" in out
    assert len(out) < 1_100


def test_an_error_in_the_middle_of_a_huge_log_survives_the_trim():
    # The shape of #9533's real export: a header, the form definition dumped
    # again and again, the error, then the submitted fields.
    dump = "sFormData {\n" + '  "label": "Question",\n' * 800 + "}"
    entries = ["SYSTEM INFORMATION:\nurl: https://example/form"]
    entries += [f"[{n}] [2026-10-15T16:22:{n:02d}Z] [LOG]\n{dump}"
                for n in range(10, 25)]
    entries += [
        "[47] [2026-10-15T16:23:16Z] [LOG]\nError Type: submitFormError",
        '[48] [2026-10-15T16:23:16Z] [LOG]\nlogError {\n  "submitted_for_user"'
        ': ["This endpoint requires submitted_for_user when submitting as an'
        ' admin"]\n}',
    ]
    entries += [f"[{n}] [2026-10-15T16:23:16Z] [LOG]\nanswer {n}"
                for n in range(49, 120)]
    data = "\n\n".join(entries).encode()
    assert len(data) > 200_000

    out = ti.text_excerpt(data)
    assert "submitFormError" in out
    assert "requires submitted_for_user when submitting as an admin" in out
    assert out.startswith("SYSTEM INFORMATION")
    assert "answer 119" in out
    assert len(out) <= ti.MAX_TEXT_CHARS + 300


def test_a_repeated_error_is_kept_once():
    entries = [f"[{n}] [2026-10-15T16:{n:02d}Z] [ERROR]\nTimeout talking to "
               "the API" for n in range(40)]
    entries += ["filler line " * 40 for _ in range(60)]
    out = ti.text_excerpt("\n\n".join(entries).encode(), limit=3_000)
    middle = out.split("look like errors follow ...]")[1]
    middle = middle.split("[... trimmed, the end")[0]
    assert middle.count("Timeout talking to the API") == 1


def test_text_files_past_the_cap_are_named_not_read(monkeypatch):
    attachments = [
        {"name": f"log{i}.txt", "href": f"https://desk/att/{i}"}
        for i in range(ti.MAX_TEXT_FILES + 1)
    ]
    _fake_zoho(
        monkeypatch,
        threads=[{"id": "t1", "direction": "in", "createdTime": "2026"}],
        details={"t1": {"content": "", "attachments": attachments}},
        files={f"https://desk/att/{i}": (f"log {i}".encode(), "text/plain")
               for i in range(ti.MAX_TEXT_FILES + 1)},
    )
    _, note = ti.fetch_ticket_images("1")
    assert f"{ti.MAX_TEXT_FILES} attached text file(s)" in note
    last = f"log{ti.MAX_TEXT_FILES}.txt"
    assert last in note.split("Contents of")[0]


def test_a_failed_log_download_is_named(monkeypatch):
    _fake_zoho(
        monkeypatch,
        threads=[{"id": "t1", "direction": "in", "createdTime": "2026"}],
        details={"t1": {"content": "", "attachments": [
            {"name": "errors.log", "href": "https://desk/att/gone"},
        ]}},
        files={},
    )
    _, note = ti.fetch_ticket_images("1")
    assert "attachment errors.log failed" in note
    assert "Contents of" not in note


def test_a_failed_download_is_named(monkeypatch):
    _fake_zoho(
        monkeypatch,
        threads=[{"id": "t1", "direction": "in", "createdTime": "2026"}],
        details={"t1": {"content": DALENA, "attachments": []}},
        files={},
    )
    images, note = ti.fetch_ticket_images("1")
    assert images == []
    assert "Not seen" in note


# ---------------------------------------------------------------------------
# App: support widget uploads (#9596)
# ---------------------------------------------------------------------------

BUCKET = "https://vomevolunteer-bucket1.s3.amazonaws.com/media/support-attachments"
WIDGET = (
    "<div><b>Customer</b><br><span>here is a screen recording</span></div>"
    f'<p><b>Attachments:</b> <a href="{BUCKET}/24f42f51.png" '
    'target="_blank">24f42f51.png</a>, '
    f'<a href="{BUCKET}/9c1d.webm">9c1d.webm</a></p>'
    '<a href="https://evil.example.com/media/support-attachments/x.png">x</a>'
)


def test_widget_links_are_only_our_bucket():
    assert ti.widget_links(WIDGET) == [f"{BUCKET}/24f42f51.png",
                                       f"{BUCKET}/9c1d.webm"]


def test_a_widget_screenshot_is_shown_and_a_recording_is_named(monkeypatch):
    _fake_zoho(
        monkeypatch,
        threads=[{"id": "t1", "direction": "in", "createdTime": "2026-10-20",
                  "author": {"name": "Ia Faye Sison"}}],
        details={"t1": {"content": WIDGET, "attachments": []}},
        files={},
    )
    fetched = []

    def widget(url):
        fetched.append(url)
        return PNG, "image/png"

    monkeypatch.setattr(ti, "_download_widget", widget)
    images, note = ti.fetch_ticket_images("1")
    # Fetched without the Zoho token (_download is never asked), and only
    # the screenshot: a recording cannot be shown, so it is named instead.
    assert fetched == [f"{BUCKET}/24f42f51.png"]
    assert [i.label for i in images] == [
        "screenshot Ia Faye Sison took in the support widget, 2026-10-20"]
    assert "9c1d.webm" in note and "not viewable" in note
