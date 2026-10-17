"""
vomeos/images.py

Pictures an agent is allowed to look at.

WHY THIS EXISTS
---------------
On 2026-10-16 ticket #9415 was triaged as "screenshot missing" and a reply
was drafted asking the customer to send it again. The screenshot was there,
pasted inline in the email body. Reading it showed a real bug, which was fixed
the same night. A text-only agent is told "an attachment exists" and then
reasons as if it had not seen it, because it has not.

Customers describe bugs with screenshots far more than with words. An agent
that cannot see them is triaging the half of the ticket that matters least.

WHAT THE KERNEL DOES, AND WHAT IT LEAVES TO THE CALLER
------------------------------------------------------
The kernel only knows bytes and a media type. Where the images came from
(a Zoho thread, an attachment, a Slack upload) is the application's business,
because reaching those systems is the application's business.

The kernel enforces three things, so no caller has to remember them:

  * an agent sees images only if its manifest says so (`job.images`, the
    maximum it may be shown). A capability is a reviewable line in a file.
  * only formats the model accepts, and only under the size limit. Zoho hands
    back `Image/png` with a capital I, which the API would reject.
  * duplicates are dropped. A quoted reply carries the same screenshot again,
    and showing it twice costs money and suggests two pictures to the model.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

# What the Messages API accepts.
SUPPORTED = ("image/png", "image/jpeg", "image/gif", "image/webp")

# The API limit is 5 MB per image, measured on the base64 payload. 3.75 MB of
# raw bytes is what fits under that once encoded.
MAX_BYTES = 3_750_000


@dataclass(frozen=True)
class Image:
    """One picture. `label` says where it came from, in plain words."""

    media_type: str
    data: bytes
    label: str = ""


def normalize_media_type(value: str) -> str:
    """`Image/PNG; charset=binary` -> `image/png`. `image/jpg` -> jpeg."""
    media = (value or "").split(";")[0].strip().lower()
    if media == "image/jpg":
        media = "image/jpeg"
    return media


def sniff_media_type(data: bytes) -> str:
    """The real format from the file's first bytes, or "".

    Servers mislabel images often enough (octet-stream, a wrong extension)
    that the bytes are the better witness.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def prepare(images, limit: int) -> tuple[list[Image], list[str]]:
    """The images that may actually be sent, and why any were dropped.

    Order is kept: the first image in a thread is usually the one the
    customer is talking about.
    """
    kept: list[Image] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for image in images or ():
        label = image.label or "image"
        media = sniff_media_type(image.data) or normalize_media_type(
            image.media_type
        )
        if media not in SUPPORTED:
            dropped.append(f"{label}: unsupported format {media or '?'}")
            continue
        if len(image.data) > MAX_BYTES:
            dropped.append(f"{label}: larger than {MAX_BYTES} bytes")
            continue
        digest = hashlib.sha256(image.data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        if len(kept) >= limit:
            dropped.append(f"{label}: over this agent's limit of {limit}")
            continue
        kept.append(Image(media, image.data, image.label))
    return kept, dropped


def content_blocks(text: str, images: list[Image]) -> list[dict] | str:
    """The user message: text first, then each image under its label.

    Plain text when there are no images, so an agent that never sees one
    sends exactly what it always sent.
    """
    if not images:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}]
    for index, image in enumerate(images, start=1):
        caption = f"Image {index} of {len(images)}"
        if image.label:
            caption += f": {image.label}"
        blocks.append({"type": "text", "text": caption})
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.b64encode(image.data).decode("ascii"),
            },
        })
    return blocks
