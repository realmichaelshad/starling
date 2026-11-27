"""A brief with no compliant answer is a prompt bug, not model randomness.

Under @thsottiaux's post — which names no company, no product and no number —
the planner produced this, and every line of it is doing its job:

    move `agree_and_add`  "One concrete addition, not a question back."
    naming rule           "This conversation names nothing, so name nothing."
    specifics block       SILENT — it had a branch for their figures and a
                          branch for their subject, and this post has neither.

Demanded something concrete, forbidden every new noun, and offered no third
place to get one. The only vocabulary left was the post's own, and the draft
came back as the post's own nouns rearranged. Four gates could have caught the
output; none of them would have made the instruction answerable.

The third place is their own sentence: picking ONE of the things they listed
and landing on it is perfectly concrete and names nothing new. That is also
what his archive does — 4 of his 48 posts carry a short borrowed term in quote
marks ("phishing", the "Jarvis" comparison, "AI agent") and never a whole
borrowed sentence.
"""
from app.reply_plan import read_the_room, reply_instruction

TIBO = ("The day we develop really good models. There will be signs.\n\n"
        "Reliability increasing despite load going up and up. Sudden efficiency "
        "gains. Things getting faster. Resets.\n\nThese kinds of things.")


class Ctx:
    """Only the attributes `read_the_room` actually reads."""
    text = TIBO
    author = "Tibo"
    author_handle = "thsottiaux"
    parent_text = ""
    quote_text = ""
    thread_text = ""
    language = ""
    is_own_post = False
    media: list = []
    links: list = []
    poll = None
    visible_metrics: dict = {}
    post_created_at = ""
    author_verified = True


def test_the_post_really_does_name_nothing():
    """If this ever fails the case has moved, and so has the fix."""
    room = read_the_room(Ctx())
    assert not room.subject
    assert not room.figures


def test_a_nameless_post_still_gets_told_how_to_be_concrete():
    """The hole: both specifics branches empty meant no instruction at all."""
    instruction = reply_instruction(read_the_room(Ctx())).lower()
    assert "one of their own words" in instruction
    assert "exactly as they wrote it" in instruction


def test_it_still_forbids_reaching_for_an_outside_name():
    """The naming rule is the reply-side profile-leak fix and must survive."""
    instruction = reply_instruction(read_the_room(Ctx())).lower()
    assert "names nothing, so name nothing" in instruction


def test_it_asks_for_one_of_their_things_not_all_of_them():
    """Covering the whole post is the restatement this class keeps producing."""
    instruction = reply_instruction(read_the_room(Ctx())).lower()
    assert "leave everything else they said alone" in instruction


def test_a_post_that_names_something_keeps_the_exactness_rule():
    """The other branch got the same instruction, for the same reason."""
    class Named(Ctx):
        text = ("Claude Opus 5 is here. Our most capable model yet, at half "
                "the price of Opus 4.5.")

    room = read_the_room(Named())
    assert room.subject
    assert "exactly as they wrote it" in reply_instruction(room).lower()


def test_the_brief_never_quotes_a_phrase_the_model_could_copy():
    """The own-goal this module keeps relearning: naming a phrase hands it over.

    Every borrowed term in the archive evidence lives in the test file and the
    source comments, never in the instruction the model reads.
    """
    instruction = reply_instruction(read_the_room(Ctx())).lower()
    for lifted in ("phishing", "jarvis", "ai agent", "resets", "reliability"):
        assert lifted not in instruction
