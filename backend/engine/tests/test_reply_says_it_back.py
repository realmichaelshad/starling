"""The post rearranged is still the post — and the old measure could not see it.

He sent this in on 2026-07-31, under @thsottiaux's "The day we develop really
good models. There will be signs. Reliability increasing despite load going up
and up. Sudden efficiency gains. Things getting faster. Resets.":

    "Reliability up under load means resets are actually signs of progress not
     failure. Just sudden gains."

His complaint was that it never says the exact words, so nothing in it points
anywhere a reader can follow. Both halves of that trace to one thing: the draft
has no vocabulary of its own, because the brief forbade every new noun while
demanding something concrete (see `test_a_nameless_post_still_gets_told_how_to
_be_concrete`), so it filled sixteen words with the post's five nouns shuffled.

`growth_scorer._source_mirroring` scored it 0.222 against a 0.5 gate, and would
have scored it LOWER the more of the post it swallowed — it compares one draft
sentence to one source sentence, so fusing four of them beats echoing one.
"""
import pytest

from app.growth_scorer import _source_mirroring
from app.reply_plan import Room, says_it_back

TIBO = ("The day we develop really good models. There will be signs.\n\n"
        "Reliability increasing despite load going up and up. Sudden efficiency "
        "gains. Things getting faster. Resets.\n\nThese kinds of things.")

SHIPPED = ("Reliability up under load means resets are actually signs of "
           "progress not failure. Just sudden gains.")


def room_for(source: str) -> Room:
    return Room(visible=source, allowed=source)


def test_the_draft_he_complained_about_is_caught():
    assert says_it_back(SHIPPED, room_for(TIBO))


def test_the_old_measure_really_was_blind_to_it():
    """Pins WHY this exists — if the shared measure ever learns to see this
    case, the reply-side check has become redundant rather than load-bearing."""
    assert _source_mirroring(SHIPPED, TIBO) < 0.5


def test_swallowing_more_of_the_post_scored_lower():
    """The fusion blind spot, stated as the inversion it is."""
    one_sentence_echoed = "Reliability increasing despite load going up and up."
    assert _source_mirroring(one_sentence_echoed, TIBO) > _source_mirroring(SHIPPED, TIBO)


# --------------------------------------------------------------------------- #
# The bar: none of his own writing may be flagged.
# --------------------------------------------------------------------------- #
HIS_REAL_REPLIES = [
    ("Grok 4.5 is now live in GitHub Copilot. Switch from the model picker "
     "for frontier intelligence at top speeds.",
     "Grok 4.5 on Copilot is faster, but it doesn't solve the cost of running "
     "frontier models."),
    ("i see your moore's law and i raise you 20x",
     "Honestly, that gap is getting scary fast."),
]


@pytest.mark.parametrize("source,reply", HIS_REAL_REPLIES)
def test_his_own_replies_are_not_flagged(source, reply):
    assert not says_it_back(reply, room_for(source))


# The shape he asked for: point at ONE of their words, then say your thing.
# Both of these borrow a word from the post on purpose and must stay clear.
POINTS_AT_ONE_THING = [
    'The "resets" one is the tell. Nobody reboots a fleet that is coping.',
    "Resets are the one nobody games. The rest you can fake with caching.",
]


@pytest.mark.parametrize("reply", POINTS_AT_ONE_THING)
def test_borrowing_one_of_their_words_is_not_an_echo(reply):
    """Quoting their term is the repair, so it must not be the offence."""
    assert not says_it_back(reply, room_for(TIBO))


def test_a_reply_that_must_say_their_subject_survives():
    """Answering "a single 4090?" requires saying "4090"."""
    source = "you got it running on a single 4090?"
    reply = "A single 4090 handles it because MoE only wakes two experts."
    assert not says_it_back(reply, room_for(source))


# --------------------------------------------------------------------------- #
# Restatements the sentence-level measure missed, all real drafts.
# --------------------------------------------------------------------------- #
REARRANGED = [
    ("Everyone says local models are catching up. Mine still needs 16GB to be "
     "mediocre.",
     "Even with 16GB being mediocre, local models still need catching up as "
     "everyone says."),
    ("Claude Opus 5 is here. Our most capable model yet, at half the price of "
     "Opus 4.5.",
     "So Opus 5 is half the price of 4.5? What's the catch?"),
    # Their sentence back with the terms swapped, which makes it false as well
    # as empty — the "it never says the exact words" fault in one draft.
    ("Every eval I trust took longer to build than the feature it measures.",
     "Trust is cheap; building time isn't. Every feature I trust takes longer "
     "to build than the eval it measures."),
]


@pytest.mark.parametrize("source,draft", REARRANGED)
def test_real_rearrangements_are_caught(source, draft):
    assert says_it_back(draft, room_for(source))


def test_an_empty_room_cannot_flag_anything():
    assert not says_it_back("Anything at all here.", room_for(""))
    assert not says_it_back("", room_for(TIBO))
