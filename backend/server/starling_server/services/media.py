"""Image type detection from magic bytes and data URIs.

Nothing here decodes pixels: images are never opened with an imaging library in the services
process. Only the leading bytes are inspected.
"""
from __future__ import annotations

import base64
import re

IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_DATA_URI_RE = re.compile(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]*={0,2})")


class DataUriError(ValueError):
    """`kind` is `bad_request` or `too_large`."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def sniff_image_type(data: bytes) -> str | None:
    """The image type proven by the file signature, or None when it is not PNG, JPEG, WebP or GIF."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def to_data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def parse_image_data_uri(value: str, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[str, int]:
    """Validate a `data:image/...;base64,` URI; return `(mime, decoded_size)`.

    The alphabet and padding are checked by the pattern, the decoded size is computed from the
    length, and only the first 48 bytes are decoded to check the signature against the declared type.
    """
    match = _DATA_URI_RE.fullmatch(value)
    if match is None:
        raise DataUriError("bad_request", "image_url must be a base64 data URI of type png, jpeg, webp or gif")
    mime, payload = match.groups()
    if not payload or len(payload) % 4:
        raise DataUriError("bad_request", "image data is not valid base64")
    decoded_size = len(payload) // 4 * 3 - payload.count("=")
    if decoded_size > max_bytes:
        raise DataUriError("too_large", "an image is larger than 5 MB")
    head = base64.b64decode(payload[:64])
    if sniff_image_type(head) != mime:
        raise DataUriError("bad_request", "image content does not match its declared type")
    return mime, decoded_size
