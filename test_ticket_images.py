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
