"""A cut image caption says it was cut, and costs the reply 140 fewer chars.

`_post_image_line` pasted `str(shown)[:300]` straight in front of "Treat the
image as part of what the post says". Measured over every reply generation on
his machine (2026-08-08): 8 of 398 replies carried an image block; seven hit the
cap (300·300·300·300·300·300·299) and one 82-char caption arrived whole. So the
severed case was not an edge — it was every caption long enough to reach it.

Two things are pinned here. That a cut caption is MARKED as cut, so a fragment
ending "possibly illustrati" cannot read as a finished description; and that the
denial ("never contradict what it shows") still trails the caption, because that
sentence is the reason the caption is shortened rather than the reverse.
"""
from __future__ import annotations

import pytest

from app.main import _clip_caption, _post_image_line


class _Media:
    kind = "image"
    url = "https://pbs.twimg.com/media/x.jpg"

    def __init__(self, alt):
        self.alt = alt


class _Ctx:
    text = "some post"

    def __init__(self, alt):
        self.media = [_Media(alt)]


@pytest.fixture(autouse=True)
def _no_vision(monkeypatch):
    """Force the alt-text path. The vision model is a 60s cold load and this
    file is about the clip, not about reading pixels."""
    import app.vision
    # raising=True on purpose: if the name is ever renamed, this must fail
    # loudly rather than stub a function nobody calls and pass regardless.
    monkeypatch.setattr(app.vision, "describe_image_url", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# the clip
# --------------------------------------------------------------------------- #

def test_a_caption_that_fits_is_untouched():
    """The 82-char control from his data. Adding a marker to a complete
    caption would be a lie about the evidence."""
    short = ("The image is a chart of cost per million tokens falling 80% "
             "from 2024 to 2026")
    assert len(short) < 160
    assert _clip_caption(short) == short


def test_a_cut_caption_is_marked():
    long = "word " * 100
    out = _clip_caption(long)
    assert out.endswith("..."), out


def test_the_clip_does_not_end_mid_word():
    """`possibly illustrati` is a real tail from his archive."""
    text = ("This image shows a diagram with blue lines and dots, "
            "possibly illustrating a network topology and its interconnections "
            "across several regions of the country") * 2
    out = _clip_caption(text)
    body = out[:-3]
    assert text.startswith(body), "the clip must be a prefix of the caption"
    assert text[len(body)] in " ,;:-", repr(text[len(body) - 5:len(body) + 5])


def test_the_budget_actually_shrinks():
    """The point of 160 is the ~140 characters handed back to the direction
    field, which is what starves the researched facts on a reply."""
    text = "x" * 500
    assert len(_clip_caption(text)) <= 163
    assert len(_clip_caption(text)) < len(text[:300])


def test_an_unspaced_caption_still_clips():
    """No word boundary near the end — cut hard rather than return the lot."""
    out = _clip_caption("a" * 400)
    assert out == "a" * 160 + "..."


@pytest.mark.parametrize("bad", [None, "", 0])
def test_junk_does_not_raise(bad):
    assert isinstance(_clip_caption(bad), str)


# --------------------------------------------------------------------------- #
# the line the writer actually receives
# --------------------------------------------------------------------------- #

def test_the_line_carries_the_marker():
    line = _post_image_line(_Ctx("z" * 400))
    assert "IMAGE attached showing:" in line
    assert "..." in line


def test_the_denial_still_trails_the_caption():
    """Ordering, not presence: the caption is shortened so that this sentence
    survives, so it must come after it."""
    line = _post_image_line(_Ctx("z" * 400))
    assert line.index("IMAGE attached showing:") < line.index("never contradict what it shows")
    assert line.rstrip().endswith("never contradict what it shows.")


def test_no_image_still_means_no_line():
    class _Empty:
        media = []
        text = ""
    assert _post_image_line(_Empty()) == ""
