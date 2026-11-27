"""Describe an attached image through the services AI proxy (lane `vision`).

Used when a post or a composer draft carries an image: the text should be
consistent with what is actually in the picture. The image travels as an OpenAI
`image_url` content part holding a data URI (`detail: "low"`); the proxy accepts
data URIs only, so a remote image is first turned into one by the world service
(`POST /v1/image`), which validates the address and the content type.

Vision is always optional. Every failure returns None and the draft proceeds
without an image line.
"""
from __future__ import annotations

import base64
import binascii
import re

from . import cloud, deadline

# The world service caps images at 5 MB; a data URI is about 4/3 of that.
_MAX_IMAGE_BYTES = 5_000_000
_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z.+-]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)
_ALLOWED_MIME = {"image/png": "image/png", "image/jpeg": "image/jpeg",
                 "image/jpg": "image/jpeg", "image/webp": "image/webp",
                 "image/gif": "image/gif"}

PROMPT = ("Describe what this image actually shows, in one or two plain sentences. "
          "Name the concrete things visible: people, objects, any readable text, "
          "and if it is a chart or screenshot say what it depicts. Do not guess or "
          "add anything that is not clearly there.")


def vision_status() -> dict[str, object]:
    """Whether images can be read right now."""
    if cloud.is_configured():
        return {"available": True, "model": cloud.model_for("vision")}
    return {"available": False, "model": None,
            "reason": "The AI service is not configured, so images are read from their alt text only."}


def _sniff_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def to_data_url(image: str) -> str | None:
    """A data URI the proxy accepts (`data:image/(png|jpeg|webp|gif);base64,...`), or None.

    Accepts a data URI or bare base64. The declared type is checked against the
    bytes, so a mislabelled or non-image payload is refused here rather than
    spending an AI call.
    """
    if not image or not isinstance(image, str):
        return None
    text = image.strip()
    declared = None
    match = _DATA_URL_RE.match(text)
    if match:
        declared = _ALLOWED_MIME.get(match.group(1).lower())
        if declared is None:
            return None
        text = match.group(2)
    text = re.sub(r"\s+", "", text)
    if not text or len(text) > (_MAX_IMAGE_BYTES * 4) // 3 + 4:
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        return None
    sniffed = _sniff_mime(raw)
    if sniffed is None or (declared is not None and declared != sniffed):
        return None
    return f"data:{sniffed};base64,{base64.b64encode(raw).decode('ascii')}"


# One short look at one image on the API vision lane (S5, 2026-09-21): ~1-2 s, so 8 s is a
# safety net. It was 60-75 s for a local vision model, and the draft waited behind it.
VISION_BUDGET_S = 8.0


def describe_image_url(url: str, *, draft: str = "", budget_seconds: float = VISION_BUDGET_S) -> str | None:
    """Describe an image by URL (a post's attached picture). None on any failure."""
    if not str(url or "").lower().startswith("https://"):
        return None
    from .safety import safe_source_url
    if not safe_source_url(url) or deadline.expired():
        return None
    fetched = cloud.image(url)
    if not fetched:
        return None
    return describe_image(str(fetched.get("data_url") or ""), draft=draft,
                          budget_seconds=budget_seconds)


def describe_image(image: str, *, draft: str = "", budget_seconds: float = VISION_BUDGET_S) -> str | None:
    """One plain-language description of what the image shows, or None."""
    data_url = to_data_url(image)
    if not data_url:
        return None
    allowed = deadline.cap(budget_seconds)
    if allowed <= 0 or deadline.expired():
        return None
    prompt = PROMPT
    if draft.strip():
        prompt += f"\n\nThe user is posting this alongside the image: \"{draft.strip()[:200]}\""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
    ]}]
    from .providers import get_router
    try:
        content = get_router().chat(messages, options={"temperature": 0.1, "num_predict": 120,
                                                      "timeout_s": allowed},
                                    lane="vision")
    except Exception:                                       # noqa: BLE001
        return None
    content = re.sub(r"<think>.*?</think>", "", str(content or ""), flags=re.DOTALL).strip()
    return content[:400] or None
