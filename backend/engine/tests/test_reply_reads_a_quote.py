"""Two posts on the screen, and the brief used to describe them as one.

He reported 2026-07-31 that replies to quote-tweets "just don't make sense".
They don't, and the reason is upstream of the writer. `_visible` joined the
post, its parent, the quoted post and the thread with a SPACE, so four words of
somebody's own commentary and forty words of the post they were quoting arrived
as one run-on sentence — and every name, every figure and every "their sentence"
the brief pointed at was resolved against that blob.

The live shape of the failure, with @dana adding four words on top of somebody
else's measurements:

    room.figures  ['30B', '$12.40', '$0.90', ...]      <- all the QUOTED post's
    brief         "THEIR OWN FIGURES, which they published: 30B, $12.40, $0.90
                   ... a reply that drops their figures reads as unread. Use one."

So the instruction asserted, in the account's own voice, that dana published a
measurement she never made, and then ordered the writer to use it.
"""
import pytest

from app.reply_plan import read_the_room, reply_instruction

QUOTED = ("Serving a 30B is now cheaper than the API call it replaces. We "
          "measured $12.40 down to $0.90 per million tokens across 2024 to "
          "2026, and Anthropic's pricing has not moved.")


class QuoteTweet:
    """@dana adds four words on top of someone else's post."""
    text = "this changes the maths completely"
    author = "dana"
    author_handle = "danabuilds"
    parent_text = ""
    quote_text = QUOTED
    thread_text: list = []
    language = ""
    is_own_post = False
    media: list = []
    links: list = []
    poll = None
    visible_metrics: dict = {}
    post_created_at = ""
    author_verified = True


class PlainPost(QuoteTweet):
    """The same measurements, but this time the author really did publish them."""
    text = QUOTED
    quote_text = ""


def test_the_layers_do_not_run_into_each_other():
    """"...completely Serving a 30B..." is a sentence nobody wrote."""
    room = read_the_room(QuoteTweet())
    assert "completely Serving" not in room.visible
    assert "completely\nServing" in room.visible


def test_the_room_keeps_the_two_layers_apart():
    room = read_the_room(QuoteTweet())
    assert room.quotes_someone
    assert room.own_text == "this changes the maths completely"
    assert room.quoted.startswith("Serving a 30B")
    # The author measured nothing, so they have no figures of their own.
    assert room.own_figures == []


def test_a_plain_post_is_not_treated_as_a_quote():
    room = read_the_room(PlainPost())
    assert not room.quotes_someone
    assert room.own_figures, "the author really did publish these"


def test_the_brief_says_which_post_is_being_replied_to():
    brief = reply_instruction(read_the_room(QuoteTweet()))
    assert "MORE THAN ONE POST IS ON THE SCREEN" in brief
    assert "dana is quoting somebody else's post" in brief
    # Their own four words are the post being replied to and the easiest thing
    # to lose in the blob, so they go in verbatim.
    assert '"this changes the maths completely"' in brief


def test_the_brief_stops_crediting_them_with_the_quoted_figures():
    """The false sentence, gone — and the figures still usable."""
    brief = reply_instruction(read_the_room(QuoteTweet()))
    assert "THEIR OWN FIGURES, which they published" not in brief
    assert "NOT IN THEIR POST" in brief
    assert "$12.40" in brief, "still offered — they are just not hers"
    assert "Somebody else put them there" in brief


def test_an_author_who_really_published_them_still_gets_the_credit():
    """The fix must not cost the ordinary case its figures."""
    brief = reply_instruction(read_the_room(PlainPost()))
    assert "THEIR OWN FIGURES, which they published" in brief
    assert "$12.40" in brief


def test_a_bare_repost_with_no_words_of_their_own_is_handled():
    class BareQuote(QuoteTweet):
        text = ""

    room = read_the_room(BareQuote())
    assert room.quotes_someone
    brief = reply_instruction(room)
    assert "added no words of their own" in brief


@pytest.mark.parametrize("attribution", [
    "Somebody else put them there",
    "measured, published or found it is not",
])
def test_the_attribution_rule_is_stated_not_implied(attribution):
    assert attribution in reply_instruction(read_the_room(QuoteTweet()))


# --------------------------------------------------------------------------- #
# THE SAME BUG, ONE LAYER OVER. Fixing it for quotes and not for the parent
# left the commonest reply surface of all still asserting the false sentence.
# --------------------------------------------------------------------------- #
class ReplyIntoAThread(QuoteTweet):
    """@dana says four words under @ben's measurements. Nothing is quoted."""
    text = "yeah exactly"
    quote_text = ""
    parent_author = "ben"
    parent_text = QUOTED


def test_a_parent_is_not_theirs_either():
    room = read_the_room(ReplyIntoAThread())
    assert not room.quotes_someone, "no quote — this is a plain thread reply"
    assert room.own_figures == [], "dana measured nothing"
    brief = reply_instruction(room)
    assert "THEIR OWN FIGURES, which they published" not in brief
    assert "NOT IN THEIR POST" in brief
    assert "$12.40" in brief
