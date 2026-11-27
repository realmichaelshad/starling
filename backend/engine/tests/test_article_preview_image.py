"""Tier 1: the article's own og:image, and the two ways it lies.

Openverse aggregates Creative Commons photography and does not contain the
model card, benchmark chart or product screenshot this account writes about —
measured: ONE image offered in the product's entire history. The article we
already cite publishes its own preview image for exactly this purpose, and we
already download that HTML for facts.

These tests cover the parts that are NOT obvious:

  * a page can ship a PLACEHOLDER and still parse — qwenlm.github.io served the
    literal "<link or path of image for opengraph...>" as its og:image content,
    URL-encoded by the time it resolves, fetchable, and useless;
  * a site-DEFAULT image passes any "has og:image" check — one model-hosting
    site's /public/og.png was the og:image for THREE different candidates, which
    is three posts sharing one picture on one timeline;
  * `fetch_page_text` has six callers that want a string, and the image was
    added by splitting the fetch rather than changing that return.
"""
from __future__ import annotations

import pytest

from app import images as im
from app.research import _TextExtractor


def _extract(head_html: str) -> str | None:
    parser = _TextExtractor()
    parser.feed(f"<html><head>{head_html}</head><body><p>body text</p></body></html>")
    parser.close()
    return parser.og_image


class TestOgImageExtraction:
    def test_reads_the_publishers_preview_image(self) -> None:
        assert _extract('<meta property="og:image" content="https://x.test/a.png"/>') == \
            "https://x.test/a.png"

    def test_falls_back_to_twitter_image(self) -> None:
        assert _extract('<meta name="twitter:image" content="https://t.test/b.jpg">') == \
            "https://t.test/b.jpg"

    def test_absent_when_the_page_has_none(self) -> None:
        assert _extract("<title>t</title>") is None

    def test_rejects_a_literal_placeholder(self) -> None:
        assert _extract('<meta property="og:image" content="<link or path of image>"/>') is None

    @pytest.mark.parametrize("encoded", ["%3Clink%20or%20path%3E", "%3CLINK%3E"])
    def test_rejects_a_url_encoded_placeholder_in_either_case(self, encoded: str) -> None:
        """The live case. A case-sensitive check against a lowercased string
        can never match, which is how this passed the first time."""
        assert _extract(f'<meta property="og:image" content="https://q.test/{encoded}"/>') is None

    def test_first_tag_wins_when_a_page_emits_several(self) -> None:
        assert _extract('<meta property="og:image" content="https://x.test/1.png"/>'
                        '<meta property="og:image" content="https://x.test/2.png"/>') == \
            "https://x.test/1.png"


class TestArticleSpecificity:
    @pytest.mark.parametrize("url", [
        "https://models.example/public/og.png",
        "https://x.test/assets/share.jpg",
        "https://x.test/img/default.png",
        "https://x.test/social-card.webp",
    ])
    def test_rejects_a_site_default_filename(self, url: str) -> None:
        ok, _reason = im.article_image_is_specific(url, "https://x.test/article")
        assert ok is False

    @pytest.mark.parametrize("url", [
        "https://static.simonwillison.net/static/2026/auto-mode-claude-code.jpg",
        "https://lh3.googleusercontent.com/Mj8GyJnsjROS",
    ])
    def test_accepts_a_real_article_image(self, url: str) -> None:
        ok, _reason = im.article_image_is_specific(url, "https://pub.test/article")
        assert ok is True

    def test_the_same_image_for_the_same_article_is_fine(self) -> None:
        seen = {"https://x.test/a1.png": "https://x.test/a"}
        ok, _ = im.article_image_is_specific("https://x.test/a1.png", "https://x.test/a", seen)
        assert ok is True

    def test_the_same_image_for_a_different_article_is_generic(self) -> None:
        """Three posts, one picture — the site-default case, caught by history
        rather than by filename."""
        seen = {"https://x.test/a1.png": "https://x.test/a"}
        ok, reason = im.article_image_is_specific("https://x.test/a1.png", "https://x.test/b", seen)
        assert ok is False
        assert "different article" in reason


class TestPublisherPreviewRights:
    def test_it_is_attachable(self) -> None:
        assert im.RIGHTS_PUBLISHER_PREVIEW in im.ATTACHABLE_RIGHTS

    def test_provenance_is_primary_and_attachable(self) -> None:
        prov = im.article_image_provenance(
            "https://static.x/img.jpg", "https://simonwillison.net/2026/auto-mode/")
        assert prov.attachable is True
        assert prov.source_tier == im.SOURCE_PRIMARY
        assert prov.rights == im.RIGHTS_PUBLISHER_PREVIEW

    def test_it_never_claims_a_licence_it_was_not_granted(self) -> None:
        """We know the publisher published it for sharing. We know nothing
        about the licence of anything depicted inside it."""
        prov = im.article_image_provenance("https://static.x/i.jpg", "https://pub.test/a")
        assert prov.rights != im.RIGHTS_LICENSE_CLEARED
        assert prov.rights != im.RIGHTS_PUBLIC_DOMAIN

    def test_the_card_can_name_the_publisher(self) -> None:
        prov = im.article_image_provenance("https://static.x/i.jpg",
                                           "https://simonwillison.net/2026/auto-mode/")
        assert prov.publisher == "simonwillison.net"


class TestLegacyCallersUnaffected:
    def test_fetch_page_text_still_returns_text_not_a_tuple(self) -> None:
        """Six call sites unpack this as a string. The image was added by
        splitting the fetch, never by changing this return."""
        import inspect

        from app import research
        source = inspect.getsource(research.fetch_page_text)
        assert "result[0] if result else None" in source
