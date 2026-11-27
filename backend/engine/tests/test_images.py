"""The image system (§4.5b-d, §5.3d, §6.2c, §8.3i) — entirely new.

Two rules dominate: unknown rights is a hard stop with no override, and
"relevant but ugly fails; beautiful but irrelevant fails". TEXT ONLY is a
successful outcome, not a failure.
"""
from __future__ import annotations

import io

import pytest

from app import images as im

PIL = pytest.importorskip("PIL", reason="Pillow is required for image inspection")


def _png(width: int, height: int, *, noise: bool = False, flat: bool = False) -> bytes:
    """A stand-in for a real timeline image.

    Default: a smooth gradient with one solid subject block — good contrast,
    low edge density, which is what a chart or photo actually looks like.
    `noise=True` mimics a screenshot of dense text; `flat=True` mimics an
    image with nothing to see.
    """
    from PIL import Image
    import random

    image = Image.new("RGB", (width, height), (128, 128, 128))
    pixels = image.load()
    if flat:
        pass
    elif noise:
        random.seed(7)
        for x in range(width):
            for y in range(height):
                shade = random.randint(0, 255)
                pixels[x, y] = (shade, shade, shade)
    else:
        for x in range(width):
            shade = int(255 * x / max(1, width - 1))
            for y in range(height):
                pixels[x, y] = (shade, shade, 200 - shade // 2)
        for x in range(width // 4, width // 2):
            for y in range(height // 4, height // 2):
                pixels[x, y] = (20, 20, 20)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _prov(**kw) -> im.Provenance:
    base = dict(source_url="https://example.com/chart.png", creator="Example",
                rights=im.RIGHTS_USER_OWNED, source_tier=im.SOURCE_USER_OWNED,
                asset_kind="chart", description="A latency comparison chart",
                alt_text="Latency comparison chart")
    base.update(kw)
    return im.Provenance(**base)


# --------------------------------------------------------------------------- #
# §4.5b rights — the hard stop
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rights", [im.RIGHTS_UNKNOWN, im.RIGHTS_BLOCKED])
def test_unknown_or_blocked_rights_is_never_attachable(rights):
    assert _prov(rights=rights).attachable is False


@pytest.mark.parametrize("rights", sorted(im.ATTACHABLE_RIGHTS))
def test_cleared_rights_are_attachable(rights):
    assert _prov(rights=rights).attachable is True


def test_no_local_copy_is_prepared_for_unknown_rights():
    """The hard stop is enforced at the copy step too, so a caller cannot reach
    the attachable state by skipping the decision function."""
    result = im.prepare_attachable_copy(_png(800, 450), _prov(rights=im.RIGHTS_UNKNOWN))
    assert result["ok"] is False
    assert "rights" in result["reason"]


def test_a_cleared_asset_does_get_a_local_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("STARLING_APP_DIR", str(tmp_path))
    result = im.prepare_attachable_copy(_png(800, 450), _prov())
    assert result["ok"] is True and result["expires_at"]
    from pathlib import Path
    assert Path(result["path"]).exists()


def test_the_source_order_is_the_spec_order():
    assert im.SOURCE_ORDER == (im.SOURCE_USER_OWNED, im.SOURCE_USER_SUPPLIED,
                               im.SOURCE_PRIMARY, im.SOURCE_LICENSED,
                               im.SOURCE_REFERENCE)


# --------------------------------------------------------------------------- #
# §4.5c the quality inspector
# --------------------------------------------------------------------------- #

def test_a_tiny_image_fails_inspection():
    result = im.inspect_image(_png(200, 120))
    assert result["ok"] is False
    assert any("too small" in p for p in result["problems"])


def test_a_wrong_aspect_ratio_is_flagged():
    result = im.inspect_image(_png(1600, 300))
    assert any("aspect" in p for p in result["problems"])


def test_a_flat_image_is_too_low_contrast_to_read():
    result = im.inspect_image(_png(800, 450, flat=True))
    assert any("flat" in p for p in result["problems"])


def test_a_dense_screenshot_of_text_is_rejected():
    """§4.5c names this failure mode explicitly."""
    result = im.inspect_image(_png(800, 450, noise=True))
    assert result["text_density"] > im.MAX_TEXT_DENSITY
    assert any("dense text" in p for p in result["problems"])


def test_a_clean_image_passes():
    assert im.inspect_image(_png(1200, 675))["ok"] is True


def test_unreadable_bytes_do_not_crash():
    assert im.inspect_image(b"not an image")["ok"] is False


# --------------------------------------------------------------------------- #
# §4.5d the non-negotiable bar
# --------------------------------------------------------------------------- #

def test_beautiful_but_irrelevant_fails():
    good = im.inspect_image(_png(1200, 675))
    result = im.passes_visual_bar(_prov(), good, claim_relevance=0.1)
    assert result["pass"] is False and result["gate"] == "relevance"


def test_relevant_but_ugly_fails():
    bad = im.inspect_image(_png(200, 120))
    result = im.passes_visual_bar(_prov(), bad, claim_relevance=0.9)
    assert result["pass"] is False and result["gate"] == "quality"


def test_rights_fail_before_anything_else_is_considered():
    good = im.inspect_image(_png(1200, 675))
    result = im.passes_visual_bar(_prov(rights=im.RIGHTS_UNKNOWN), good,
                                  claim_relevance=1.0)
    assert result["gate"] == "rights"


def test_relevant_and_clean_passes_both_gates():
    good = im.inspect_image(_png(1200, 675))
    assert im.passes_visual_bar(_prov(), good, claim_relevance=0.9)["pass"] is True


def test_rights_block_zeroes_the_candidate_value():
    good = im.inspect_image(_png(1200, 675))
    value = im.candidate_value(_prov(rights=im.RIGHTS_UNKNOWN), good,
                               claim_relevance=1.0, evidence_gain=1.0)
    assert value["value"] == 0.0 and value["blocked"] is True


# --------------------------------------------------------------------------- #
# §5.3d visual state
# --------------------------------------------------------------------------- #

def test_text_sufficient_means_no_visual():
    assert im.visual_state(claim_needs_proof=False, comprehension_gain=0.9,
                           text_is_sufficient=True, has_candidate=True) == im.VISUAL_NONE


def test_a_claim_needing_proof_is_required():
    assert im.visual_state(claim_needs_proof=True, comprehension_gain=0.5,
                           text_is_sufficient=False, has_candidate=False) == im.VISUAL_REQUIRED


def test_a_strong_comprehension_gain_is_recommended():
    assert im.visual_state(claim_needs_proof=False, comprehension_gain=0.8,
                           text_is_sufficient=False, has_candidate=True) == im.VISUAL_RECOMMENDED


# --------------------------------------------------------------------------- #
# §6.2c the attachment decision
# --------------------------------------------------------------------------- #

def test_no_visual_need_means_text_only():
    result = im.attachment_decision(im.VISUAL_NONE, None, None)
    assert result["decision"] == im.TEXT_ONLY


def test_reference_only_media_is_linked_never_copied():
    """§4.5b: show it as context, link the original, no copy-and-attach."""
    good = im.inspect_image(_png(1200, 675))
    prov = _prov(rights=im.RIGHTS_UNKNOWN, source_tier=im.SOURCE_REFERENCE)
    value = im.candidate_value(prov, good, claim_relevance=0.9, evidence_gain=0.9)
    result = im.attachment_decision(im.VISUAL_RECOMMENDED, prov, value)
    assert result["decision"] == im.QUOTE_ORIGINAL
    assert result["source_url"]


def test_a_required_visual_with_no_asset_asks_the_user_for_evidence():
    result = im.attachment_decision(im.VISUAL_REQUIRED, None, None)
    assert result["decision"] == im.REQUEST_EVIDENCE


def test_a_strong_cleared_image_is_attached():
    good = im.inspect_image(_png(1200, 675))
    value = im.candidate_value(_prov(), good, claim_relevance=1.0,
                               evidence_gain=1.0, voice_fit=1.0)
    result = im.attachment_decision(im.VISUAL_RECOMMENDED, _prov(), value)
    assert result["decision"] == im.ATTACH


def test_replies_hold_a_higher_bar_than_posts():
    """§5.3d: text-only is the default for a reply."""
    good = im.inspect_image(_png(1200, 675))
    value = im.candidate_value(_prov(), good, claim_relevance=0.7, evidence_gain=0.6)
    as_post = im.attachment_decision(im.VISUAL_OPTIONAL, _prov(), value, action="post")
    as_reply = im.attachment_decision(im.VISUAL_OPTIONAL, _prov(), value, action="reply")
    assert not (as_reply["decision"] == im.ATTACH and as_post["decision"] != im.ATTACH)


# --------------------------------------------------------------------------- #
# §8.3i alt text, metadata, receipt, cache
# --------------------------------------------------------------------------- #

def test_alt_text_describes_what_matters_not_the_caption():
    text = im.propose_alt_text(_prov(description="Latency across three model sizes",
                                     visible_text="p50 latency\n120ms"))
    assert "Latency across three model sizes" in text
    assert "120ms" in text


def test_alt_text_always_returns_something():
    assert im.propose_alt_text(im.Provenance())


def test_metadata_is_stripped_before_attachment():
    from PIL import Image

    buffer = io.BytesIO()
    image = Image.new("RGB", (800, 450), (10, 20, 30))
    exif = image.getexif()
    exif[0x9286] = "location comment"
    image.save(buffer, format="JPEG", exif=exif)
    original = buffer.getvalue()
    cleaned, notes = im.strip_metadata(original)
    assert cleaned != original or notes
    reopened = Image.open(io.BytesIO(cleaned))
    assert not dict(reopened.getexif())


def test_reference_image_reader_is_confined_to_starling_media(monkeypatch, tmp_path):
    from app import main, storage

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "starling.db")
    media = tmp_path / "reference-images"
    media.mkdir()
    owned = media / "owned.png"
    owned.write_bytes(b"owned-image")
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"not-media")

    stored = main._read_managed_reference_image(str(owned))
    assert stored is not None and stored[0] == b"owned-image"
    assert main._read_managed_reference_image(str(outside)) is None


def test_the_visual_receipt_shows_source_rights_and_alt_text():
    good = im.inspect_image(_png(1200, 675))
    value = im.candidate_value(_prov(), good, claim_relevance=1.0, evidence_gain=1.0)
    decision = im.attachment_decision(im.VISUAL_RECOMMENDED, _prov(), value)
    receipt = im.visual_receipt(_prov(), decision, value, action="post")
    assert receipt["why_this_image"] and receipt["alt_text"]
    assert receipt["source"]["rights"] == im.RIGHTS_USER_OWNED
    assert receipt["requires_user_confirmation"] is True
    assert "use_text_only" in receipt["controls"]


def test_the_cache_expires_and_can_be_purged(tmp_path, monkeypatch):
    monkeypatch.setenv("STARLING_APP_DIR", str(tmp_path))
    im.prepare_attachable_copy(_png(800, 450), _prov())
    assert im.sweep_cache(ttl_hours=99)["kept"] == 1
    assert im.purge_all()["removed"] == 1
    assert im.sweep_cache()["kept"] == 0
