"""Read the brief out loud on every surface, and it says impossible things.

Found 2026-07-31 by running `read_the_room` + `reply_instruction` across the
context shapes a real user actually hits and reading the output — the same way
the quote-tweet and nameless-post bugs were found. Three of the four below were
introduced by the fix for those two, which is the point: a block appended to
"the brief" lands on every move, including the ones it contradicts.
"""
from app.reply_plan import read_the_room, reply_instruction


class C:
    text = ""; author = "dana"; author_handle = "dana"
    parent_text = ""; quote_text = ""; thread_text: list = []
    language = ""; is_own_post = False; media: list = []; links: list = []
    poll = None; visible_metrics: dict = {}; post_created_at = ""
    author_verified = True; parent_author = ""


def brief_for(**kw) -> str:
    return reply_instruction(read_the_room(type("Ctx", (C,), kw)()))


# --------------------------------------------------------------------------- #
# A joke is not a briefing.
# --------------------------------------------------------------------------- #
def test_a_joke_gets_no_analyst_instruction():
    """"No analysis" and "say why it is the one that counts" in one brief."""
    brief = brief_for(text="bro is not saving the multiverse lol")
    assert "No analysis" in brief
    assert "why it is the one that counts" not in brief
    assert "what it would really take" not in brief


def test_the_joke_brief_stays_short():
    """Register is the whole job here; length is the measurable proxy."""
    joke = brief_for(text="bro is not saving the multiverse lol")
    claim = brief_for(text="Everyone keeps saying local inference is cheaper "
                           "but nobody accounts for the engineer-hours.")
    assert len(joke) < len(claim)


# --------------------------------------------------------------------------- #
# One phrase, two opposite instructions.
# --------------------------------------------------------------------------- #
def test_name_it_does_not_ask_for_their_words_and_your_own_at_once():
    brief = brief_for(text="This is the part everyone is missing.")
    assert "in your own words" not in brief
    assert "exactly as they wrote it" in brief


def test_name_it_still_says_what_to_do():
    brief = brief_for(text="This is the part everyone is missing.")
    assert "the concrete thing they are gesturing at" in brief
    assert "NEVER invent a subject" in brief


# --------------------------------------------------------------------------- #
# A post can be a picture.
# --------------------------------------------------------------------------- #
def test_an_image_post_is_not_described_as_words():
    """With `text` empty the brief talked about "the phrase of theirs you have
    a view on" — about a post containing no phrases, and never mentioning that
    an image is what is actually there."""
    brief = brief_for(text="", media=[{"type": "photo"}])
    assert "IMAGE WITH NO TEXT" in brief
    assert "must not guess" in brief


def test_a_post_with_text_and_an_image_is_left_alone():
    brief = brief_for(text="Prices dropped again today.", media=[{"type": "photo"}])
    assert "IMAGE WITH NO TEXT" not in brief


def test_the_denial_stands_down_once_the_writer_has_a_description():
    """`main.py` appends `_post_image_line` — "The post has an IMAGE attached
    showing: … never contradict what it shows" — to the SAME direction string
    this brief joins, and this block then said "you cannot see it … must not
    describe it", denial last. Two instructions each written for a real live
    failure, contradicting each other in one prompt because neither knew the
    other had run.

    The fix is the fact passed between them, never a reordering: reordering
    makes a contradiction quieter, not gone.
    """
    room = read_the_room(type("Ctx", (C,), dict(text="", media=[{"type": "photo"}]))())
    assert "IMAGE WITH NO TEXT" in reply_instruction(room)
    assert "IMAGE WITH NO TEXT" in reply_instruction(room, description_obtained=False)
    assert "IMAGE WITH NO TEXT" not in reply_instruction(room, description_obtained=True)


def test_an_unknown_description_keeps_the_denial():
    """The default must mean "the writer was told nothing about the image".

    A default of True would silently delete a guard that exists because a draft
    invented what an image showed — the empty-default trap that once made a
    model pin never-falsy and switched off two subsystems. Pinned so nobody
    "simplifies" the parameter later.
    """
    room = read_the_room(type("Ctx", (C,), dict(text="", media=[{"type": "photo"}]))())
    assert "IMAGE WITH NO TEXT" in reply_instruction(room), (
        "the no-argument default must keep the denial firing")


# --------------------------------------------------------------------------- #
# A poll's options are the post.
# --------------------------------------------------------------------------- #
def test_a_poll_is_part_of_the_conversation():
    """"which one ships first?" over [Opus 5, GPT-5.6] read as naming nothing,
    so the brief said "name nothing" — and the only answer that engages the
    poll would then have been refused as an intrusion."""
    room = read_the_room(type("Ctx", (C,), dict(
        text="which one ships first?",
        poll={"options": ["Opus 5", "GPT-5.6"]}))())
    assert room.subject, "the poll names two products"
    assert "gpt-5.6" in room.subject
    assert "This conversation names nothing" not in reply_instruction(room)


def test_a_poll_in_any_of_its_shapes_does_not_crash():
    for poll in ({"options": [{"label": "Opus 5"}, {"text": "GPT-5.6"}]},
                 ["Opus 5", "GPT-5.6"], {"options": "Opus 5"}, [], {}, None,
                 {"options": None}, "nonsense"):
        room = read_the_room(type("Ctx", (C,), dict(
            text="which one ships first?", poll=poll))())
        assert isinstance(reply_instruction(room), str)
