"""
ticket_images.py

The pictures in a Zoho ticket, ready to show an agent.

WHY THIS EXISTS
---------------
Ticket #9415 (2026-10-16): the client pasted her screenshot INLINE in the
email body. Zoho reported no attachment (`attachmentCount` -1, `hasAttach`
false), the text-only triage concluded "screenshot missing", and a reply was
drafted asking her to send it again. The image showed a real bug.

Customers paste far more often than they attach, so both have to be read:

  * inline images live in the thread HTML as
    `<img src="/api/v1/threads/<thread>/inlineImages/...">`
  * attachments are listed on the thread with a download `href`
  * support widget uploads are only a link to our S3 bucket in the body
    (#9596), fetched without the Zoho token

Both download with the Desk OAuth token and an `orgId` header. Zoho returns
`Image/png` with a capital I; `vomeos.images` normalises that.

WHAT IS LEFT OUT
----------------
Email signatures. Every message from a hospital or a county carries a logo and
a banner, and showing the model forty copies of a logo teaches it nothing and
costs real money. They are recognisable by their rendered size in the HTML
(short banners, small logos), and the kernel drops exact duplicates. Tiny
files (tracking pixels, icons) are dropped by byte size.

Only messages from the customer are read. Our own replies do not carry the
evidence, and a quoted copy of their earlier email repeats images the kernel
would deduplicate anyway.

Failures return what was gathered so far plus a note. A missing image must
never stop a triage; the note tells the agent it was not seen.
"""

from __future__ import annotations

import hashlib
import html
import re

import httpx

from vomeos.images import Image

ZOHO_BASE = "https://desk.zoho.com"

# Newest customer messages first, and stop after this many threads. The live
# question is in the latest messages, and old ones are quoted in them anyway.
MAX_THREADS = 6
# Downloaded at most. The agent's own manifest caps what it is shown.
MAX_IMAGES = 8

# Below this an image is an icon, a pixel or a bullet.
MIN_BYTES = 6_000

_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"', re.IGNORECASE)
_IMAGE_NAME = re.compile(r"\.(png|jpe?g|gif|webp)$", re.IGNORECASE)

# The support widget uploads to our own bucket and only links the file in the
# ticket body: no Zoho attachment and no inline image, so #9596's screenshot
# was never shown to triage (2026-10-20). Screenshots are .png, recordings
# .webm (vome-react SupportWidget). Only this bucket path is fetched.
_WIDGET_LINK = re.compile(
    r'href="(https://vomevolunteer-bucket\d*\.s3\.amazonaws\.com/media/'
    r'support-attachments/[^"/]+)"',
    re.IGNORECASE,
)


def _attrs(tag: str) -> dict[str, str]:
    return {k.lower(): html.unescape(v) for k, v in _ATTR.findall(tag)}


def _px(value: str) -> int:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else 0


def _dimensions(attrs: dict[str, str]) -> tuple[int, int]:
    """Rendered width and height, from attributes or an inline style."""
    width, height = _px(attrs.get("width", "")), _px(attrs.get("height", ""))
    style = attrs.get("style", "")
    if not width:
        match = re.search(r"(?<!max-)width:\s*(\d+)px", style)
        width = int(match.group(1)) if match else 0
    if not height:
        match = re.search(r"(?<!max-)height:\s*(\d+)px", style)
        height = int(match.group(1)) if match else 0
    return width, height


def looks_like_signature(width: int, height: int) -> bool:
    """A logo or banner rather than a screenshot.

    Calibrated on real signatures: 192x99 and 415x46 logos, against a 981x194
    screenshot strip. Unknown dimensions are kept, because a pasted image
    without a size is usually a full screenshot.
    """
    if not width and not height:
        return False
    if height and height < 60:
        return True
    return bool(width and height and width < 250 and height < 250)


def inline_sources(content: str) -> list[tuple[str, int, int]]:
    """(src, width, height) for every Zoho-hosted inline image in a body."""
    found = []
    for tag in _IMG.findall(content or ""):
        attrs = _attrs(tag)
        src = attrs.get("src", "")
        if "/inlineImages/" not in src:
            continue
        width, height = _dimensions(attrs)
        found.append((src, width, height))
    return found


def widget_links(content: str) -> list[str]:
    """Files the support widget uploaded, as linked in the ticket body."""
    found = []
    for url in _WIDGET_LINK.findall(content or ""):
        url = html.unescape(url)
        if url not in found:
            found.append(url)
    return found


def _headers() -> dict[str, str]:
    from zoho_desk_api import ZOHO_ORG_ID, _get_token

    return {
        "Authorization": f"Zoho-oauthtoken {_get_token()}",
        "orgId": str(ZOHO_ORG_ID),
    }


def _get_json(path: str, params: dict | None = None) -> dict:
    resp = httpx.get(
        f"{ZOHO_BASE}/api/v1{path}", headers=_headers(), params=params,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _download(url: str) -> tuple[bytes, str]:
    if url.startswith("/"):
        url = ZOHO_BASE + url
    resp = httpx.get(url, headers=_headers(), timeout=30,
                     follow_redirects=True)
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type", "")


def _download_widget(url: str) -> tuple[bytes, str]:
    # Our bucket, not Zoho: the Desk token must never leave Zoho.
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type", "")


def _who(thread: dict) -> str:
    author = thread.get("author") or {}
    return (
        author.get("name") or author.get("email")
        or thread.get("fromEmailAddress") or "the customer"
    )


def _when(thread: dict) -> str:
    return str(thread.get("createdTime") or "")[:10]


def fetch_ticket_images(ticket_id: str) -> tuple[list[Image], str]:
    """The ticket's screenshots and image attachments, newest first.

    Returns (images, note). The note is one line for the agent's context:
    how many were found, how many were skipped as signatures, and whether
    anything could not be read.
    """
    images: list[Image] = []
    seen: set[str] = set()
    skipped_signatures = 0
    problems: list[str] = []
    unviewable: list[str] = []

    def keep(data: bytes, media: str, label: str) -> None:
        # A quoted reply carries the same screenshot again.
        digest = hashlib.sha256(data).hexdigest()
        if digest not in seen:
            seen.add(digest)
            images.append(Image(media, data, label))

    try:
        listing = _get_json(
            f"/tickets/{ticket_id}/threads", {"limit": 50},
        )
    except Exception as exc:
        return [], f"Images could not be read: {type(exc).__name__}."

    threads = [
        t for t in (listing.get("data") or [])
        if t.get("direction") == "in"
    ]
    threads.sort(key=lambda t: str(t.get("createdTime") or ""), reverse=True)

    for thread in threads[:MAX_THREADS]:
        if len(images) >= MAX_IMAGES:
            break
        try:
            detail = _get_json(
                f"/tickets/{ticket_id}/threads/{thread.get('id')}"
            )
        except Exception as exc:
            problems.append(f"one message unreadable ({type(exc).__name__})")
            continue
        who, when = _who(thread), _when(thread)

        for src, width, height in inline_sources(detail.get("content", "")):
            if len(images) >= MAX_IMAGES:
                break
            if looks_like_signature(width, height):
                skipped_signatures += 1
                continue
            try:
                data, media = _download(src)
            except Exception as exc:
                problems.append(f"an inline image failed "
                                f"({type(exc).__name__})")
                continue
            if len(data) < MIN_BYTES:
                skipped_signatures += 1
                continue
            keep(data, media, f"pasted in {who}'s message of {when}")

        for attachment in detail.get("attachments") or []:
            if len(images) >= MAX_IMAGES:
                break
            name = str(attachment.get("name") or "")
            href = attachment.get("href") or ""
            if not _IMAGE_NAME.search(name):
                # A screen recording or a PDF is evidence too. The agent
                # cannot open it, but must know it exists rather than
                # concluding the customer sent nothing.
                if name:
                    unviewable.append(f"{name} from {who}, {when}")
                continue
            if not href:
                continue
            try:
                data, media = _download(href)
            except Exception as exc:
                problems.append(f"attachment {name} failed "
                                f"({type(exc).__name__})")
                continue
            if len(data) < MIN_BYTES:
                continue
            keep(data, media, f"attachment {name} from {who}, {when}")

        for url in widget_links(detail.get("content", "")):
            if len(images) >= MAX_IMAGES:
                break
            name = url.rsplit("/", 1)[-1]
            if not _IMAGE_NAME.search(name):
                unviewable.append(
                    f"{name} (support widget upload, open the link in the "
                    f"ticket) from {who}, {when}")
                continue
            try:
                data, media = _download_widget(url)
            except Exception as exc:
                problems.append(f"widget upload {name} failed "
                                f"({type(exc).__name__})")
                continue
            if len(data) < MIN_BYTES:
                continue
            keep(data, media,
                 f"screenshot {who} took in the support widget, {when}")

    parts = []
    if images:
        parts.append(
            f"{len(images)} image(s) from the customer are shown after the "
            "text, labelled with where each came from."
        )
    else:
        parts.append("No screenshots or image attachments from the customer.")
    if skipped_signatures:
        parts.append(
            f"{skipped_signatures} email signature logo(s) were left out."
        )
    if unviewable:
        parts.append(
            "Attached but not viewable here (open it in Zoho before "
            "concluding anything is missing): " + "; ".join(unviewable) + "."
        )
    if problems:
        parts.append("Not seen: " + "; ".join(problems) + ".")
    return images, " ".join(parts)
