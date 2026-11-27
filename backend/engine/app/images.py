"""Image Radar, rights, quality and the composer handoff (§4.5b-d, §5.3d, §6.2c, §8.3i).

Entirely new: the archive had a rights-tagged reference-image import table and
an image-count feature, and nothing else. No discovery, no provenance record, no
quality inspector, no visual-gain analysis, no attachment decision.

Two rules govern everything here.

**Unknown rights is a hard stop.** An image whose reuse rights cannot be
established may be shown as context and linked to its source, but can never
enter the attachable step (§4.5b). There is no override.

**Relevant but ugly fails; beautiful but irrelevant fails** (§4.5d). Both gates
must pass, and when neither can, the answer is TEXT ONLY — which is a normal,
successful outcome, not a failure.
"""
from __future__ import annotations

import threading

import hashlib
import io
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import evidence as ev
from .safety import safe_source_url

# §4.5b source order. Lower number = more trustworthy provenance.
SOURCE_USER_OWNED = "user_owned"
SOURCE_USER_SUPPLIED = "user_supplied"
SOURCE_PRIMARY = "primary_source"
SOURCE_LICENSED = "publicly_licensed"
SOURCE_REFERENCE = "reference_only"

SOURCE_ORDER = (SOURCE_USER_OWNED, SOURCE_USER_SUPPLIED, SOURCE_PRIMARY,
                SOURCE_LICENSED, SOURCE_REFERENCE)

# Rights states. Only the first four may ever be attached.
RIGHTS_USER_OWNED = "user_owned"
RIGHTS_USER_CONFIRMED = "user_confirmed"
RIGHTS_LICENSE_CLEARED = "license_cleared"
RIGHTS_PUBLIC_DOMAIN = "public_domain"
RIGHTS_ATTRIBUTION_REQUIRED = "attribution_required"
# THE PUBLISHER'S OWN SOCIAL-PREVIEW IMAGE.
#
# `og:image` is not a picture we found near an article; it is the picture the
# publisher emits so that THIS article looks right when someone shares it.
# That is the specification of the tag, not an inference about intent, and
# attaching it to a post about that article is the use it was published for.
# We already cite the article as the evidence the post is built from.
#
# It is NOT a licence. It says nothing about the photographer's rights in an
# embedded photo, so it never travels as `license_cleared`, and the card names
# the publisher rather than claiming a licence nobody granted.
RIGHTS_PUBLISHER_PREVIEW = "publisher_preview"
RIGHTS_UNKNOWN = "unknown"
RIGHTS_BLOCKED = "blocked"

ATTACHABLE_RIGHTS = frozenset({
    RIGHTS_USER_OWNED, RIGHTS_USER_CONFIRMED, RIGHTS_LICENSE_CLEARED,
    RIGHTS_PUBLIC_DOMAIN, RIGHTS_ATTRIBUTION_REQUIRED,
    RIGHTS_PUBLISHER_PREVIEW,
})

# §5.3d visual states.
VISUAL_NONE = "none"
VISUAL_OPTIONAL = "optional"
VISUAL_RECOMMENDED = "recommended"
VISUAL_REQUIRED = "required"

# §6.2c decisions.
TEXT_ONLY = "text only"
ATTACH = "attach approved image"
OFFER = "offer optional image"
QUOTE_ORIGINAL = "quote or link original"
REQUEST_EVIDENCE = "request user evidence"
WAIT_REFRAME = "wait or reframe"

# §4.5c weights.
QUALITY_WEIGHTS = {
    # Semantic relevance comes from Analyzer's visual brief, not loose query
    # words. Technical quality is deliberately weighted almost as heavily as
    # legibility so a large-but-sloppy image cannot win.
    "claim_relevance": 0.29,
    "evidence_gain": 0.20,
    "mobile_legibility": 0.16,
    "visual_clarity": 0.16,
    "voice_fit": 0.08,
    "accessibility": 0.06,
    "source_quality": 0.05,
}

# An attachment must beat text-only by a real margin (§6.2c), not merely tie.
ATTACH_MARGIN = 0.15

# Minimums for a timeline image that is meant to be read on a phone.
MIN_WIDTH = 960
MIN_HEIGHT = 540
# X displays a single photo fully between roughly 2:1 and 3:4.
ASPECT_MIN, ASPECT_MAX = 0.75, 2.0
# Above this share of the frame covered in text, it is a screenshot of prose.
MAX_TEXT_DENSITY = 0.35
# Licensed web media clears a stricter score than an owned local image. This
# keeps technically valid thumbnails and washed-out stock photos out of offers.
WEB_MIN_QUALITY = 0.72

# §Data rules: prepared copies are local, temporary and swept.
CACHE_TTL_HOURS = 24



# Filenames a site uses for its ONE default preview image. Measured on the live
# pool: one vendor's `/public/og.png` was the og:image for THREE different
# candidates — three posts, one picture, which is a visible defect on his
# timeline before it is a rights question.
_SITE_DEFAULT_IMAGE = re.compile(
    r"/(og|og[-_]image|default|share|social|preview|card|banner|logo)"
    r"(?:[-_][a-z0-9]{1,12})?\.(png|jpe?g|webp|gif)$", re.IGNORECASE)


def article_image_is_specific(image_url: str, article_url: str,
                              seen: dict[str, str] | None = None) -> tuple[bool, str]:
    """Is this the ARTICLE's image, or the site's?

    Two independent checks, because they catch different things:

    * a site-default FILENAME (`/og.png`, `/share.jpg`) is generic by
      construction and catchable without any history;
    * the same URL served for a DIFFERENT article proves it is generic even
      when the filename looks specific. `seen` maps image_url -> article_url
      from previous offers; the first sighting is allowed through and the
      second is what convicts it.

    Returns (ok, reason). The reason is for the trail, never for the card.
    """
    url = str(image_url or "").strip()
    if not url:
        return False, "no image"
    if _SITE_DEFAULT_IMAGE.search(url.split("?")[0]):
        return False, "site default filename"
    if seen:
        previous = seen.get(url)
        if previous and previous != str(article_url or ""):
            return False, "same image already served for a different article"
    return True, "article-specific"


def article_image_provenance(image_url: str, article_url: str,
                             publisher: str | None = None,
                             description: str | None = None) -> Provenance:
    """Provenance for a publisher's own `og:image`.

    `source_tier` is PRIMARY — this is the source we are already citing, not a
    third party's copy of it. `rights` is PUBLISHER_PREVIEW, which is
    attachable, and deliberately NOT `license_cleared`: we know the publisher
    published it for sharing, and we do not know the licence of anything
    depicted inside it. The card says the publisher; it never claims a licence.
    """
    host = ""
    try:
        from urllib.parse import urlparse
        host = urlparse(article_url or image_url).netloc.lower().lstrip("www.")
    except Exception:                                          # noqa: BLE001
        host = ""
    return Provenance(
        source_url=article_url or image_url,
        publisher=publisher or host or None,
        rights=RIGHTS_PUBLISHER_PREVIEW,
        source_tier=SOURCE_PRIMARY,
        asset_kind="article_preview",
        description=description or None,
        required_credit=host or None,
        source_metadata={"image_url": image_url, "article_url": article_url,
                         "source_reputation": 0.9},
    )


def fetch_article_preview_image(article_url: str, *, budget_s: float = 6.0,
                                description: str | None = None,
                                seen: dict[str, str] | None = None
                                ) -> tuple[bytes, Provenance] | None:
    """TIER 1 — the article's own `og:image`, on the fetch we already make.

    `research.fetch_page_text_and_image` reads the page once and hands back both
    the text and the publisher's preview image; before it existed the markup
    went out of scope and the image with it. So this costs one image download,
    never a second page fetch.

    Returns None whenever the image is missing, generic, or unreachable —
    TEXT ONLY stays the normal outcome and every rejection is silent by design.
    """
    from urllib.parse import urljoin

    from . import research

    url = str(article_url or "").strip()
    if not url.startswith("http"):
        return None
    try:
        page = research.fetch_page_preview(url)
    except Exception:                                          # noqa: BLE001
        return None
    if not page or not page.get("image"):
        return None
    image_url = urljoin(url, str(page["image"]).strip())
    ok, _reason = article_image_is_specific(image_url, url, seen)
    if not ok:
        return None
    data = _download_licensed_image(image_url, deadline=time.monotonic() + budget_s)
    if not data:
        return None
    # THE IMAGE'S OWN WORDS, NOT THE RADAR TOPIC.
    #
    # `score_visual_candidate` decides whether a candidate DEPICTS the brief's
    # subject by reading this description. Passing the topic string made it
    # match the wrong text and reject good article images at 0.56-0.75 — a gate
    # given the wrong input, not a gate that was too strict. The caller's
    # `description` is now only a fallback for a page that publishes no words
    # of its own.
    own_words = " ".join(part for part in (page.get("title"), page.get("description"))
                         if part).strip()
    return data, article_image_provenance(
        image_url, url, description=own_words or description)

def observed_visual_habit(*, action: str = "post", recent_limit: int = 16) -> dict[str, Any]:
    """Summarize the user's *observed* image habit, never a generic norm.

    The extension already captures only the safe, visible fact that one of the
    user's own posts has an image.  This function turns that history into a
    small preference profile: how often images appear overall, in recent posts,
    and in the requested surface.  It does not inspect private image pixels or
    infer tastes from someone else's feed.
    """
    try:
        from .storage import image_feedback_summary, records

        # ``records`` is the account archive / Live Account Memory used for the
        # user's voice model. Its ``external`` origin means independently
        # authored (not generated by Starling), not someone else's feed.
        rows = records()
        feedback = image_feedback_summary()
    except Exception:
        rows = []
        feedback = {"sample": 0, "used": 0, "skipped": 0, "replaced": 0, "keep_rate": None}
    if not rows:
        keep_rate = float(feedback.get("keep_rate") or 0.0)
        choice_led = feedback.get("sample", 0) >= 5 and keep_rate >= 0.60
        return {
            "sample": 0, "overall_rate": 0.0, "recent_rate": 0.0,
            "action_rate": 0.0, "propensity": keep_rate if choice_led else 0.0,
            "image_forward": choice_led,
            "voice_fit": round(0.5 + 0.42 * keep_rate, 3) if choice_led else 0.5,
            "feedback": feedback,
            "reason": ("Explicit image choices show a strong image preference."
                       if choice_led else "No observed posting pattern yet."),
        }

    normalized_action = "post" if action not in {"reply", "quote"} else action

    def kind(row: dict[str, Any]) -> str:
        value = str(row.get("post_type") or "post").lower()
        return "post" if value in {"original", "poll", "unknown", "post"} else value

    def rate(values: list[dict[str, Any]]) -> float:
        return round(sum(bool(row.get("has_image")) for row in values) / max(1, len(values)), 3)

    overall = rate(rows)
    recent = rate(rows[-max(1, recent_limit):])
    same_surface = [row for row in rows if kind(row) == normalized_action]
    surface_rate = rate(same_surface) if len(same_surface) >= 3 else overall
    # Recent behaviour matters most, but a tiny run of posts must not erase a
    # stable account habit. This is a preference prior, never an image quota.
    observed_propensity = 0.45 * overall + 0.35 * recent + 0.20 * surface_rate
    keep_rate = feedback.get("keep_rate")
    # An explicit keep/skip decision has more meaning than an inferred habit,
    # but needs a few choices before it can shift recommendations noticeably.
    if feedback.get("sample", 0) >= 3 and keep_rate is not None:
        propensity = round(0.78 * observed_propensity + 0.22 * float(keep_rate), 3)
    else:
        propensity = round(observed_propensity, 3)
    enough_history = len(rows) >= 8
    image_forward = (enough_history and (propensity >= 0.24 or recent >= 0.38)) or (
        feedback.get("sample", 0) >= 5 and float(keep_rate or 0.0) >= 0.60)
    if image_forward:
        reason = (f"Observed images in {round(overall * 100)}% of {len(rows)} posts "
                  f"({round(recent * 100)}% recently).")
    else:
        reason = (f"Observed images in {round(overall * 100)}% of {len(rows)} posts; "
                  "there is not yet a strong image-forward pattern.")
    if feedback.get("sample", 0):
        reason += (f" You explicitly kept {feedback['used']} of {feedback['sample']} "
                   "suggested images.")
    return {
        "sample": len(rows), "overall_rate": overall, "recent_rate": recent,
        "action_rate": surface_rate, "propensity": propensity,
        "image_forward": image_forward,
        "feedback": feedback,
        # This affects ranking after relevance has passed; it can never make a
        # generic or low-quality image attachable.
        "voice_fit": round(0.5 + 0.42 * propensity if image_forward else 0.5, 3),
        "reason": reason,
    }


def cache_dir() -> Path:
    from .paths import app_dir
    path = app_dir() / "image-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Provenance:
    """§4.5b. Travels with the file; without it the file is unusable."""

    source_url: str | None = None
    creator: str | None = None
    publisher: str | None = None
    rights: str = RIGHTS_UNKNOWN
    source_tier: str = SOURCE_REFERENCE
    retrieved_at: str | None = None
    required_credit: str | None = None
    asset_kind: str = "unknown"     # screenshot | chart | photo | generated | stock
    description: str | None = None
    visible_text: str | None = None
    alt_text: str | None = None
    concerns: list[str] = field(default_factory=list)
    source_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def attachable(self) -> bool:
        """The hard stop. No override exists anywhere in this module."""
        return self.rights in ATTACHABLE_RIGHTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url, "creator": self.creator,
            "publisher": self.publisher, "rights": self.rights,
            "source_tier": self.source_tier, "retrieved_at": self.retrieved_at,
            "required_credit": self.required_credit, "asset_kind": self.asset_kind,
            "description": self.description, "visible_text": self.visible_text,
            "alt_text": self.alt_text, "concerns": self.concerns,
            "source_metadata": self.source_metadata,
            "attachable": self.attachable,
        }


# --------------------------------------------------------------------------- #
# §4.5b web sourcing — LICENSED images only (Openverse: CC / public domain)
# --------------------------------------------------------------------------- #

# The hosted engine makes no web requests of its own, so licensed-image search and
# image download are unavailable here (see "Optional extras" in
# docs/architecture/engine-notes.md). The ranking and rights logic below stays, so
# a world-service image route can feed it later without changing any caller.


def _download_licensed_image(url: str, *, deadline: float, redirects_remaining: int = 4) -> bytes | None:
    """Image downloads are not available in the hosted engine. Always None.

    The engine makes no web requests of its own (all web data comes through the
    services process), and the world service offers images only as vision
    input. See "Optional extras" in docs/architecture/engine-notes.md.
    """
    del url, deadline, redirects_remaining
    return None


# Openverse rejects an anonymous request asking for more than 20 results with
# "401: page_size may not exceed 20 for anonymous requests". Callers asked for
# 24, so EVERY image search 401'd, the bare except swallowed it, and the
# function returned [] every single time — which is why an image never once
# appeared. Clamped here rather than at the call sites so no future caller can
# reintroduce it.
_OPENVERSE_MAX_PAGE_SIZE = 20


def _openverse_results(query: str, limit: int) -> list[dict]:
    """Licensed-image search is not available in the hosted engine. Always empty.

    The engine makes no web requests of its own and the world service has no
    image-search route yet; see "Optional extras" in
    docs/architecture/engine-notes.md.
    """
    del query, limit
    return []


def _native_dimensions(item: dict[str, Any]) -> tuple[int | None, int | None]:
    """Use source dimensions before downloading; thumbnails never qualify."""
    try:
        width = int(item.get("width")) if item.get("width") else None
        height = int(item.get("height")) if item.get("height") else None
        return width, height
    except (TypeError, ValueError):
        return None, None


def _source_reputation(item: dict[str, Any]) -> float:
    """A small, transparent preference among otherwise licensed sources."""
    source = str(item.get("source") or "").strip().lower()
    if source == "wikimedia":
        return 1.0
    if source == "flickr":
        return 0.86
    if source in {"behance", "500px"}:
        return 0.82
    return 0.65


def _discovery_priority(item: dict[str, Any], query_index: int) -> tuple[float, ...]:
    """Rank full-size, well-described candidates before any image download."""
    width, height = _native_dimensions(item)
    pixels = (width or 0) * (height or 0)
    tags = item.get("tags") or []
    title = str(item.get("title") or "").strip()
    # A source that cannot say what an image depicts is unfit for Analyzer's
    # final semantic check. It may stay in the source system, but not here.
    described = 1.0 if title and tags else 0.55 if title or tags else 0.0
    return (1.0 if pixels >= 1_200 * 675 else 0.0,
            min(1.0, pixels / float(2_000 * 1_125)),
            _source_reputation(item), described, -float(query_index))


def _relaxations(queries: list[str]) -> list[str]:
    """Each query, then progressively shorter versions of it.

    Openverse ANDs the terms, so Analyzer's exact subject phrases ("Claude
    Anthropic AI model") match NOTHING — measured 0 results, while "artificial
    intelligence" returns 20. Every image search therefore came back empty and
    the account never saw one.

    This widens only the SEARCH. It does not widen what is acceptable: every
    result still faces score_visual_candidate against the same brief, so a
    sculpture named Claude is rejected exactly as before. Narrow queries stay
    first so an exact match always outranks a relaxed one.
    """
    out: list[str] = []
    for query in queries:
        words = [w for w in str(query).split() if w]
        for size in range(len(words), 0, -1):
            variant = " ".join(words[:size])
            if variant and variant not in out:
                out.append(variant)
            if size <= 2:
                break
    return out[:8]


def search_web_image(queries: "str | list[str]", *, limit: int = 12,
                     budget_seconds: float = 8.0,
                     accept=None) -> list[tuple[bytes, "Provenance"]]:
    """Search Openverse for a topical, rights-cleared image (§4.5b).

    Openverse aggregates CC and public-domain images (Wikimedia, Flickr CC, …)
    with the license attached, so the reuse rights are KNOWN - which is what lets
    these reach the attachable step, unlike an arbitrary Google image. Filtered
    to licenses that permit commercial use and modification.

    The queries come from Analyzer's exact visual brief. They are searched as a
    small set and ranked together; this function never invents a broad fallback
    such as ``technology``. Only original image URLs are used (never search
    thumbnails), and source dimensions/title/tags are checked before download.
    Hard time budget so it never hangs a draft.
    """
    if isinstance(queries, str):
        queries = [queries]
    queries = [q.strip() for q in queries if q and q.strip()]
    if not queries:
        return []
    # An image is a nice-to-have; a draft is not. Any failure in here — a dead
    # host, a malformed payload, a decoder blowing up on a corrupt file — must
    # cost the user an image, never the post they were writing.
    try:
        return _search_web_image(queries, limit=limit,
                                 budget_seconds=budget_seconds, accept=accept)
    except Exception:
        return []


def _search_web_image(queries: list[str], *, limit: int,
                      budget_seconds: float,
                      accept=None) -> list[tuple[bytes, "Provenance"]]:
    """``accept(description) -> bool`` is consulted BEFORE each download, so a
    candidate the caller will reject on metadata never costs a fetch."""
    deadline = time.monotonic() + budget_seconds
    discovered: list[tuple[dict[str, Any], str, int]] = []
    seen_urls: set[str] = set()
    for index, query in enumerate(_relaxations(queries)):
        if time.monotonic() > deadline:
            break
        for item in _openverse_results(query, max(12, min(24, limit * 2))):
            if not isinstance(item, dict):
                continue
            # ``thumbnail`` is intentionally not a fallback. It is exactly the
            # small, muddy preview the timeline must not attach.
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            width, height = _native_dimensions(item)
            if width is not None and height is not None and (width < MIN_WIDTH or height < MIN_HEIGHT):
                continue
            title = str(item.get("title") or "").strip()
            tags = item.get("tags") or []
            if not title and not tags:
                continue
            seen_urls.add(url)
            discovered.append((item, query, index))
    if not discovered:
        return []

    out: list[tuple[bytes, "Provenance"]] = []
    for item, query, query_index in sorted(
        discovered, key=lambda row: _discovery_priority(row[0], row[2]), reverse=True
    ):
        if time.monotonic() > deadline:
            break
        url = str(item.get("url") or "").strip()
        # JUDGE THE SUBJECT BEFORE FETCHING THE PIXELS.
        #
        # The caller's semantic gate (`main.py` `consider()`) rejects on
        # `provenance.description`, which is built entirely from Openverse
        # metadata already in hand — no byte of the image is involved. It used
        # to run AFTER the download, so a post could download up to twelve
        # images and throw away every one on a check that needed none of them.
        #
        # Same gate, same brief, same candidate order; only the fetch moved.
        #
        # Fold the image's OWN tags into the description so relevance can be
        # judged against what the image actually depicts, not just its title.
        # This is what stops a sculpture titled "Claude the Dragon" from being
        # accepted for a post about Anthropic's Claude — the tags say dragon,
        # sculpture, burning man, which share nothing with the post.
        tag_names = " ".join(str(t.get("name") or "") for t in (item.get("tags") or [])
                             if isinstance(t, dict))[:200]
        title = str(item.get("title") or query)
        description = f"{title} {tag_names}".strip()
        if accept is not None and not accept(description):
            continue
        data = _download_licensed_image(url, deadline=deadline)
        if not data:
            continue
        licence = str(item.get("license") or "").lower()
        if licence in ("cc0", "pdm"):
            rights, credit = RIGHTS_PUBLIC_DOMAIN, None
        else:
            rights = RIGHTS_ATTRIBUTION_REQUIRED
            who = item.get("creator") or "Unknown"
            credit = f"{who} / {licence.upper() or 'CC'} via {item.get('source') or 'Openverse'}"
        width, height = _native_dimensions(item)
        out.append((data, Provenance(
            source_url=item.get("foreign_landing_url") or str(url),
            creator=item.get("creator"), publisher=item.get("source"),
            rights=rights, source_tier=SOURCE_LICENSED, asset_kind="photo",
            description=description,
            required_credit=credit,
            source_metadata={
                "search_query": query, "query_order": query_index,
                "title": title, "tags": tag_names, "native_width": width,
                "native_height": height, "source_reputation": _source_reputation(item),
                "license": licence,
            },
            retrieved_at=datetime.now(timezone.utc).isoformat())))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# §4.5c the quality inspector
# --------------------------------------------------------------------------- #

def inspect_image(data: bytes) -> dict[str, Any]:
    """Inspect resolution, legibility, sharpness and color restraint.

    "Nice" means useful and clean at mobile size, not merely high resolution
    (§4.5c), so this measures legibility rather than megapixels.
    """
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return {"ok": False, "reason": "Pillow is not installed.",
                "dependency": "Pillow"}
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:
        return {"ok": False, "reason": f"Unreadable image: {error}"}

    width, height = image.size
    aspect = width / height if height else 0.0
    problems: list[str] = []
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        problems.append(f"too small for a timeline ({width}x{height})")
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        problems.append(f"aspect {aspect:.2f} will be cropped in the timeline")

    grey = image.convert("L")
    contrast = 0.0
    text_density = 0.0
    sharpness = 0.0
    oversaturated_share = 0.0
    try:
        import numpy as np

        pixels = np.asarray(grey, dtype="float32") / 255.0
        contrast = float(pixels.std())
        # High-frequency edges approximate dense text well enough to catch a
        # screenshot of a paragraph, which is the failure mode §4.5c names.
        dx = np.abs(np.diff(pixels, axis=1)).mean() if pixels.shape[1] > 1 else 0.0
        dy = np.abs(np.diff(pixels, axis=0)).mean() if pixels.shape[0] > 1 else 0.0
        text_density = float(min(1.0, (dx + dy) * 6.0))
        # FIND_EDGES is inexpensive and gives a useful blur proxy without
        # pretending to identify a subject from pixels alone.
        edge = np.asarray(grey.filter(ImageFilter.FIND_EDGES), dtype="float32") / 255.0
        sharpness = float(edge.std())
        hsv = np.asarray(image.convert("HSV"), dtype="float32") / 255.0
        if hsv.ndim == 3 and hsv.shape[2] >= 3:
            saturation, brightness = hsv[..., 1], hsv[..., 2]
            oversaturated_share = float(((saturation > 0.86) & (brightness > 0.70)).mean())
    except Exception:
        pass

    if contrast and contrast < 0.08:
        problems.append("too flat to read on a phone")
    if text_density > MAX_TEXT_DENSITY:
        problems.append("dense text baked into the image")
    if contrast and sharpness < 0.035:
        problems.append("too soft or blurry for a clean timeline image")
    if oversaturated_share > 0.42:
        problems.append("harsh oversaturated colors")

    resolution_score = min(1.0, (width * height) / float(1_200 * 675))
    contrast_score = min(1.0, contrast / 0.18) if contrast else 0.0
    sharpness_score = min(1.0, sharpness / 0.12) if sharpness else 0.0
    color_score = max(0.0, 1.0 - min(1.0, oversaturated_share / 0.55))
    text_score = max(0.0, 1.0 - min(1.0, text_density / MAX_TEXT_DENSITY))
    quality_score = (0.28 * resolution_score + 0.22 * contrast_score +
                     0.22 * sharpness_score + 0.16 * color_score +
                     0.12 * text_score)

    return {
        "ok": not problems, "width": width, "height": height,
        "aspect": round(aspect, 3), "contrast": round(contrast, 4),
        "text_density": round(text_density, 4), "sharpness": round(sharpness, 4),
        "oversaturated_share": round(oversaturated_share, 4),
        "quality_score": round(quality_score, 4), "problems": problems,
        "format": image.format,
    }


def candidate_value(provenance: Provenance, inspection: dict[str, Any], *,
                    claim_relevance: float, evidence_gain: float,
                    voice_fit: float = 0.5) -> dict[str, Any]:
    """§4.5c visual_candidate_value. Rights gate multiplies everything."""
    if not provenance.attachable:
        return {"value": 0.0, "blocked": True,
                "reason": f"rights are {provenance.rights}; not attachable"}

    legibility = 1.0 if inspection.get("ok") else max(
        0.0, 1.0 - 0.35 * len(inspection.get("problems", [])))
    # The image inspector's composite is stricter than contrast alone: it also
    # accounts for native resolution, blur, dense text, and excessive color.
    clarity = float(inspection.get("quality_score") or 0.0)
    if not clarity:
        clarity = min(1.0, float(inspection.get("contrast") or 0.0) * 4)
    accessibility = 1.0 if provenance.alt_text else 0.0
    source_quality = {
        SOURCE_PRIMARY: 1.0,
        SOURCE_USER_OWNED: 0.96,
        SOURCE_USER_SUPPLIED: 0.94,
        SOURCE_LICENSED: max(0.55, float(provenance.source_metadata.get("source_reputation") or 0.75)),
        SOURCE_REFERENCE: 0.0,
    }.get(provenance.source_tier, 0.55)

    terms = {
        "claim_relevance": max(0.0, min(1.0, claim_relevance)),
        "evidence_gain": max(0.0, min(1.0, evidence_gain)),
        "mobile_legibility": legibility,
        "visual_clarity": clarity,
        "voice_fit": max(0.0, min(1.0, voice_fit)),
        "accessibility": accessibility,
        "source_quality": source_quality,
    }
    base = sum(QUALITY_WEIGHTS[k] * v for k, v in terms.items())

    penalties = 0.0
    if inspection.get("text_density", 0) > MAX_TEXT_DENSITY:
        penalties += 0.25
    if provenance.concerns:
        penalties += 0.25 * len(provenance.concerns)
    if provenance.source_tier == SOURCE_LICENSED and clarity < WEB_MIN_QUALITY:
        penalties += 0.18
    # Rights confidence scales the whole thing: attribution-required is usable
    # but carries a burden, so it is worth slightly less than owning the asset.
    confidence = 1.0 if provenance.rights in (
        RIGHTS_USER_OWNED, RIGHTS_USER_CONFIRMED) else 0.85

    value = max(0.0, confidence * base - penalties)
    return {"value": round(value, 4), "terms": {k: round(v, 3) for k, v in terms.items()},
            "penalties": round(penalties, 3), "blocked": False}


def passes_visual_bar(provenance: Provenance, inspection: dict[str, Any],
                      *, claim_relevance: float) -> dict[str, Any]:
    """§4.5d. BOTH gates, and the strictness is the point."""
    relevant = claim_relevance >= 0.5
    good = bool(inspection.get("ok"))
    if provenance.source_tier == SOURCE_LICENSED and inspection.get("quality_score") is not None:
        good = good and float(inspection.get("quality_score") or 0.0) >= WEB_MIN_QUALITY
    if not provenance.attachable:
        return {"pass": False, "gate": "rights",
                "reason": f"rights are {provenance.rights}"}
    if not relevant and not good:
        return {"pass": False, "gate": "both", "reason": "irrelevant and poor quality"}
    if not relevant:
        return {"pass": False, "gate": "relevance",
                "reason": "beautiful but irrelevant — it could fit any post"}
    if not good:
        details = list(inspection.get("problems", []))
        if (provenance.source_tier == SOURCE_LICENSED and
                float(inspection.get("quality_score") or 0.0) < WEB_MIN_QUALITY):
            details.append("does not clear the high-quality web threshold")
        return {"pass": False, "gate": "quality",
                "reason": "relevant but ugly: " + "; ".join(details)}
    return {"pass": True, "gate": None, "reason": "clears both gates"}


# --------------------------------------------------------------------------- #
# §5.3d visual gain and §6.2c the attachment decision
# --------------------------------------------------------------------------- #

def visual_state(*, claim_needs_proof: bool, comprehension_gain: float,
                 text_is_sufficient: bool, has_candidate: bool) -> str:
    """None / optional / recommended / required (§5.3d)."""
    if claim_needs_proof and not text_is_sufficient:
        return VISUAL_REQUIRED
    if text_is_sufficient or comprehension_gain < 0.25:
        return VISUAL_NONE
    if comprehension_gain >= 0.6 and has_candidate:
        return VISUAL_RECOMMENDED
    return VISUAL_OPTIONAL


def attachment_decision(state: str, provenance: Provenance | None,
                        value: dict[str, Any] | None, *,
                        action: str = "post") -> dict[str, Any]:
    """§6.2c. The Writer cannot add media; this owns the choice.

    Replies default to TEXT ONLY: a decorative image makes a reply harder to
    read and more likely to look opportunistic (§5.3d).
    """
    if state == VISUAL_NONE:
        return {"decision": TEXT_ONLY, "reason": "Text carries the point better."}

    if provenance is None or value is None:
        if state == VISUAL_REQUIRED:
            return {"decision": REQUEST_EVIDENCE,
                    "reason": "The claim needs visual proof Starling does not have."}
        return {"decision": TEXT_ONLY, "reason": "No candidate cleared the gates."}

    if not provenance.attachable:
        # Reference-only: link it, never copy it (§4.5b).
        return {"decision": QUOTE_ORIGINAL,
                "reason": f"Rights are {provenance.rights}; link the source instead.",
                "source_url": provenance.source_url}

    score = float(value.get("value") or 0.0)
    baseline = 0.5 if action != "reply" else 0.65  # replies must clear a higher bar
    if score >= baseline + ATTACH_MARGIN:
        return {"decision": ATTACH, "reason": "Rights-cleared and it earns its place.",
                "score": score}
    # A decent, rights-cleared, relevant image that doesn't fully beat text is
    # OFFERED as an option, not hidden. The user attaches manually, so showing a
    # fitting image they can decline beats suppressing it - which is why images
    # never appeared. Replies still stay text-only (an image reads as clutter).
    if score >= baseline and action != "reply":
        return {"decision": OFFER, "reason": "A fitting image if you want one.",
                "score": score, "optional": True}
    if state == VISUAL_REQUIRED:
        return {"decision": WAIT_REFRAME,
                "reason": "The visual claim cannot be responsibly supported."}
    return {"decision": TEXT_ONLY,
            "reason": f"The image ({score:.2f}) does not beat text by enough."}


# --------------------------------------------------------------------------- #
# §8.3i alt text, metadata stripping, the visual receipt, the local copy
# --------------------------------------------------------------------------- #

def propose_alt_text(provenance: Provenance, *, claim: str = "") -> str:
    """Concise, objective, and about what MATTERS — not a caption repeat."""
    parts = []
    if provenance.description:
        parts.append(provenance.description.strip().rstrip("."))
    elif provenance.asset_kind != "unknown":
        parts.append(f"A {provenance.asset_kind}")
    if provenance.visible_text:
        # Keep the numbers: on a chart the values ARE what matters, so the
        # whole visible-text block is condensed rather than only its first line.
        snippet = " ".join(provenance.visible_text.split())[:110]
        if snippet:
            parts.append(f'showing "{snippet}"')
    if not parts and claim:
        parts.append(claim.strip().rstrip(".")[:100])
    text = ", ".join(parts).strip()
    return (text[:1000] or "Image supporting the post.")


def strip_metadata(data: bytes) -> tuple[bytes, list[str]]:
    """Remove EXIF (including location) before an image is attached (§8.3i)."""
    try:
        from PIL import Image
    except ImportError:
        return data, ["Pillow missing: metadata could not be stripped"]
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        clean = Image.new(image.mode, image.size)
        clean.paste(image)
        buffer = io.BytesIO()
        clean.save(buffer, format=image.format or "PNG")
        return buffer.getvalue(), []
    except Exception as error:
        return data, [f"metadata not stripped: {error}"]


def prepare_attachable_copy(data: bytes, provenance: Provenance) -> dict[str, Any]:
    """Write a temporary local copy — only for rights-cleared assets.

    The hard stop lives here too, so no caller can reach the attachable step by
    skipping the decision function.
    """
    if not provenance.attachable:
        return {"ok": False,
                "reason": f"rights are {provenance.rights}; no copy is prepared"}
    clean, notes = strip_metadata(data)
    digest = hashlib.sha256(clean).hexdigest()[:16]
    path = cache_dir() / f"{digest}.img"
    path.write_bytes(clean)
    return {"ok": True, "path": str(path), "bytes": len(clean),
            "metadata_notes": notes,
            "expires_at": (ev._now() + timedelta(hours=CACHE_TTL_HOURS)).isoformat()}


def sweep_cache(*, ttl_hours: float = CACHE_TTL_HOURS) -> dict[str, Any]:
    """Delete expired local copies. Called on startup and after use."""
    removed, kept = 0, 0
    cutoff = time.time() - ttl_hours * 3600
    directory = cache_dir()
    for item in directory.glob("*.img"):
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
            else:
                kept += 1
        except OSError:
            continue
    return {"removed": removed, "kept": kept, "dir": str(directory)}


def purge_all() -> dict[str, Any]:
    """Used by DELETE /data: cached media goes with the rest of the archive."""
    directory = cache_dir()
    removed = 0
    for item in directory.glob("*.img"):
        try:
            item.unlink()
            removed += 1
        except OSError:
            continue
    return {"removed": removed}


def visual_receipt(provenance: Provenance, decision: dict[str, Any],
                   value: dict[str, Any] | None, *, action: str) -> dict[str, Any]:
    """§8.3i. What the user sees before anything touches the composer."""
    return {
        "why_this_image": decision.get("reason"),
        "source": {
            "url": provenance.source_url, "creator": provenance.creator,
            "publisher": provenance.publisher, "rights": provenance.rights,
            "required_credit": provenance.required_credit,
        },
        "alt_text": provenance.alt_text or propose_alt_text(provenance),
        "post_fit": {
            "action": action,
            "why_not_text_only": decision.get("reason")
            if decision.get("decision") == ATTACH else None,
            "score": (value or {}).get("value"),
        },
        "controls": ["add_to_composer", "use_text_only", "replace_image",
                     "open_source", "cancel"],
        # The user decides. Nothing is attached without this step (§8.3i).
        "requires_user_confirmation": True,
    }
