"""Web image sourcing: licensed (CC/public-domain) images so a topical image
actually shows, and the OFFER tier that surfaces a good-but-optional image
instead of hiding it (why images never appeared before)."""
from __future__ import annotations

from app import images as im


def test_offer_tier_surfaces_a_decent_image_instead_of_hiding_it():
    prov = im.Provenance(rights=im.RIGHTS_LICENSE_CLEARED, source_tier=im.SOURCE_LICENSED,
                         asset_kind="photo", description="ai chip")
    # A score above baseline (0.5) but below the auto-attach bar (0.65).
    d = im.attachment_decision(im.VISUAL_OPTIONAL, prov, {"value": 0.56}, action="post")
    assert d["decision"] == im.OFFER
    assert d.get("optional") is True


def test_strong_image_still_auto_attaches():
    prov = im.Provenance(rights=im.RIGHTS_PUBLIC_DOMAIN, source_tier=im.SOURCE_LICENSED,
                         asset_kind="chart", description="latency chart")
    d = im.attachment_decision(im.VISUAL_RECOMMENDED, prov, {"value": 0.8}, action="post")
    assert d["decision"] == im.ATTACH


def test_weak_image_stays_text_only():
    prov = im.Provenance(rights=im.RIGHTS_LICENSE_CLEARED, source_tier=im.SOURCE_LICENSED,
                         description="blurry thing")
    d = im.attachment_decision(im.VISUAL_OPTIONAL, prov, {"value": 0.3}, action="post")
    assert d["decision"] == im.TEXT_ONLY


def test_replies_never_offer_a_decorative_image():
    prov = im.Provenance(rights=im.RIGHTS_LICENSE_CLEARED, source_tier=im.SOURCE_LICENSED,
                         description="ai")
    d = im.attachment_decision(im.VISUAL_OPTIONAL, prov, {"value": 0.56}, action="reply")
    assert d["decision"] == im.TEXT_ONLY


def test_web_search_is_off_when_research_disabled(monkeypatch):
    from app import research
    monkeypatch.setattr(research, "research_enabled", lambda: False)
    assert im.search_web_image(["artificial intelligence"]) == []


def test_the_hosted_engine_never_downloads_or_searches_images(monkeypatch):
    """The engine makes no web requests of its own: licensed-image search and
    download are unavailable, so both seams answer "nothing" without a socket."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("image sourcing opened a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    assert im._download_licensed_image("https://images.example/start", deadline=10**12) is None
    assert im._openverse_results("artificial intelligence", 20) == []
    assert not hasattr(im, "_image_session")


def test_web_search_tries_queries_in_order(monkeypatch):
    from app import research
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    calls = []

    def fake_results(q, limit):
        calls.append(q)
        return [{"url": "https://example.com/a.jpg", "license": "by",
                 "creator": "X", "source": "flickr", "title": "ai"}] if q == "artificial intelligence" else []

    monkeypatch.setattr(im, "_openverse_results", fake_results)
    monkeypatch.setattr(im, "_download_licensed_image", lambda url, deadline: b"fake")
    out = im.search_web_image(["kimi", "artificial intelligence", "technology"])
    assert calls[:2] == ["kimi", "artificial intelligence"]   # fell through
    assert len(out) == 1
    assert out[0][1].rights == im.RIGHTS_ATTRIBUTION_REQUIRED
    assert "flickr" in (out[0][1].required_credit or "")
