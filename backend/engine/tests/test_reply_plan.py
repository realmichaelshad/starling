"""A reply is written from the conversation, never from the account.

Every fixture here is a draft that actually shipped out of the reply path on
2026-07-26, read off `generation_candidates` and `tools/reply_arena.py`. They
are pinned so the four bugs behind them cannot come back:

  1. NAMES FROM THE ARCHIVE. "I can't wait to see how Fable 5 handles a trip
     plan with 8 friends" under a post about a weekend trip. `Fable` is not in
     that post; it is in 55% of his archive. The existing entity gate was
     passed the voice examples as part of its allowed corpus, so it could never
     object. Measured directly before the fix:
         _injected_entities(draft, parent)            -> ['fable']
         _injected_entities(draft, parent + examples) -> []

  2. THE QUESTION BOUNCED BACK. "What rig are you running it on?" answered with
     "You're running it on a GPU? I'm on a CPU and it's already faster."

  3. THE SAME REPLY AGAIN. Four separate drafts ended "What's the catch?" and
     `_echoes_own_writing` flagged none of them — it wants two shared six-word
     runs, and a twelve-word reply only contains seven runs in total.

  4. ARGUING WITH SOMEBODY'S OWN MEASUREMENT. "Been running Qwen locally for a
     month, the bottleneck was memory bandwidth" answered with "So you're
     saying Qwen's not the bottleneck? What's the real issue then?" — a
     question the post's own second clause answers.
"""
from __future__ import annotations

import pytest

from app.reply_plan import (Room, bounces_the_question, intruders,
                            opens_with_a_tic, read_the_room, reply_instruction,
                            repeats_a_reply)
from app.schemas import PostContext

OWN = "example_builder"


def ctx(**kwargs) -> PostContext:
    base = {"author": "Someone", "author_handle": "someone", "text": ""}
    base.update(kwargs)
    return PostContext(**base)


# --------------------------------------------------------------- reading it
def test_a_direct_question_is_answered_not_discussed():
    room = read_the_room(ctx(
        author="A Reader", author_handle="areader",
        text="This is the clearest take I've read on local inference. "
             "What rig are you running it on?"))
    assert room.move == "answer"
    assert room.asked == "What rig are you running it on?"


def test_a_rhetorical_question_inside_an_announcement_is_not_an_ask():
    room = read_the_room(ctx(
        author="Claude", author_handle="claudeai",
        text="Claude Opus 5 is here, at half the price of Opus 4.5. "
             "Why does this matter? Because cost is the whole constraint."))
    assert room.asked == ""
    assert room.move == "add_detail"


def test_firsthand_experience_is_agreed_with_never_argued():
    """The failure: "So you're saying Qwen's not the bottleneck?" to a man who
    had just spent a month measuring it."""
    room = read_the_room(ctx(
        author="Some Dev", author_handle="somedev",
        text="Been running Qwen locally for a month. The bottleneck was never "
             "the model, it was memory bandwidth."))
    assert room.kind == "firsthand"
    assert room.move == "agree_and_add"


def test_a_complaint_gets_agreement_not_vendor_defence():
    room = read_the_room(ctx(
        author="Priya", author_handle="priyadev",
        text="Third day in a row the API returns a 529 during European "
             "morning. Nobody at support has answered the ticket."))
    assert room.kind == "complaint"
    assert room.move == "agree_and_add"


def test_a_post_with_no_subject_never_invents_one():
    """"This is the part everyone is missing." -> "So you're saying the missing
    piece is the moon?" There was no subject, so one was hallucinated."""
    room = read_the_room(ctx(author="Swyx", author_handle="swyx",
                             text="This is the part everyone is missing."))
    assert room.kind == "vague"
    assert room.move == "name_it"
    assert room.subject == []
    assert "name nothing" in reply_instruction(room)


def test_his_own_post_continues_the_thread():
    room = read_the_room(ctx(author="Alex Rivera", author_handle=OWN,
                             text="Gemini 4 might actually slip to next quarter.",
                             is_own_post=True), own_handle=OWN)
    assert room.move == "continue"
    assert "@-mention yourself" in reply_instruction(room)


def test_someone_replying_to_his_post_makes_him_the_host():
    room = read_the_room(ctx(
        author="Dana", author_handle="danacodes",
        text="Wait, you got it running on a single 4090?",
        parent_author=OWN,
        parent_text="Running the 30B locally on one card."), own_handle=OWN)
    assert room.move == "host"


def test_the_host_still_has_to_answer_what_they_asked():
    """`host` outranks `answer` — being the host changes who you address — but
    it must not lose the question. Live, Dana asked "you got it running on a
    single 4090?" and got back "So you're saying the model's MoE only wakes two
    experts?", which answers nobody."""
    room = read_the_room(ctx(
        author="Dana", author_handle="danacodes",
        text="Wait, you got it running on a single 4090? I assumed you needed "
             "two cards minimum for that context length.",
        parent_author=OWN,
        parent_text="Running the 30B locally on one card."), own_handle=OWN)
    assert room.move == "host"
    assert room.asked.startswith("Wait, you got it running")
    assert room.asked in reply_instruction(room)
    assert bounces_the_question(
        "So you're saying the model's MoE only wakes two experts?", room)


def test_a_thin_post_over_a_substantial_quote_is_not_vague():
    """"This is the number that should be scaring people" reads as subject-less
    on its own, and the reader can see the quoted post right under it. Judged
    on the post alone it was answered as if nothing had been said."""
    room = read_the_room(ctx(
        author="Nat", author_handle="natbuilds",
        text="This is the number that should be scaring people.",
        quote_text="Inference costs for open-weight models dropped again this "
                   "quarter. Serving a 30B is now cheaper than the API call it "
                   "replaces."))
    assert room.kind != "vague"
    assert room.move == "agree_and_add"


def test_a_second_reply_is_a_follow_up_not_a_first_reply():
    room = read_the_room(ctx(author="Nat", author_handle="nat",
                             text="Costs dropped again this quarter."),
                         already_replied=True)
    assert room.move == "follow_up"
    assert "ALREADY replied" in reply_instruction(room)


def test_a_joke_is_answered_in_register():
    room = read_the_room(ctx(
        author="Ben", author_handle="benposts",
        text="my agent opened 14 pull requests overnight and every single one "
             "renames a variable"))
    assert room.kind == "joke"
    assert room.move == "joke"


def test_the_thread_under_a_post_is_part_of_the_conversation():
    """Part 1 alone carries no facts; the reply must see the rest."""
    room = read_the_room(ctx(
        author="Ines", author_handle="inesml",
        text="Spent the week replacing our retrieval stack.",
        thread_text=["Spent the week replacing our retrieval stack.",
                     "Chunking moved the needle more than the embedding model.",
                     "Latency went from 1.9s to 700ms."]))
    assert "700ms" in room.allowed


def test_a_post_that_gets_the_lab_wrong_is_corrected():
    """The `correct` move was defined in this file and unreachable: nothing
    ever looked at whether the POST had its facts wrong. Getting the lab wrong
    is the one mistake this audience always catches, so correcting it is among
    the highest-value replies the account can send."""
    room = read_the_room(ctx(
        author="Someone", author_handle="someone",
        text="OpenAI's Fable 5 is cheaper than Claude now. Worth switching?"))
    assert room.move == "correct"
    assert "Anthropic" in room.correction
    assert "Anthropic" in reply_instruction(room)
    # `entity_truth.describe` closes with advice for a human reading a warning
    # card. That sentence goes into the prompt as "here is the fact", and a
    # small model handed it writes it into the reply.
    assert "audience always catches" not in room.correction
    assert "audience always catches" not in reply_instruction(room)


def test_the_correction_itself_is_never_treated_as_a_leak():
    """The true vendor is usually a name the post never used. If the naming
    rule rejected it, the correction could not be written at all."""
    room = read_the_room(ctx(
        author="Someone", author_handle="someone",
        text="OpenAI's Fable 5 is cheaper than Claude now."))
    assert intruders("Fable is Anthropic's model, not OpenAI's.", room) == []


def test_a_correct_post_is_not_corrected():
    room = read_the_room(ctx(author="Claude", author_handle="claudeai",
                             text="Claude Opus 5 is here, at half the price "
                                  "of Opus 4.5."))
    assert room.move != "correct"
    assert room.correction == ""


# ------------------------------------------------------------------ the leak
def test_a_name_from_the_archive_is_an_intruder():
    room = read_the_room(ctx(
        author="Sam Altman", author_handle="sama",
        text="chatgpt work is remarkable. from my phone i sent: use all my "
             "chat history to plan a long weekend trip with 8 friends."))
    draft = "I can't wait to see how Fable 5 handles a trip plan with 8 friends."
    assert "fable" in intruders(draft, room)


def test_a_name_the_post_did_mention_is_not_an_intruder():
    room = read_the_room(ctx(
        author="Claude", author_handle="claudeai",
        text="Claude Opus 5 is here, at half the price of Opus 4.5."))
    assert intruders("Opus 5 at that price changes the build-vs-buy maths", room) == []


def test_a_name_the_evidence_verified_is_allowed():
    room = read_the_room(
        ctx(author="Nat", author_handle="nat", text="The pricing changed again."),
        evidence="Gemini 3 dropped its per-token price on Tuesday.")
    assert intruders("Gemini 3 moved first and everyone matched it", room) == []


def test_the_quoted_layer_counts_as_the_conversation():
    room = read_the_room(ctx(
        author="Nat", author_handle="nat",
        text="This is the number that should be scaring people.",
        quote_text="Serving a 30B is now cheaper than the API call it replaces."))
    assert intruders("Serving a 30B in-house is the whole story", room) == []


def test_a_post_naming_nothing_licenses_nothing():
    room = read_the_room(ctx(author="Swyx", author_handle="swyx",
                             text="This is the part everyone is missing."))
    assert "fable" in intruders("The missing piece is Fable's context window", room)


# -------------------------------------------------------------- the bounce
def test_answering_a_question_with_a_question_is_caught():
    room = read_the_room(ctx(
        author="A Reader", author_handle="areader",
        text="This is the clearest take on local inference. What rig are you "
             "running it on?"))
    assert bounces_the_question(
        "You're running it on a GPU? I'm on a CPU and it's already faster.", room)


def test_a_real_answer_passes():
    room = read_the_room(ctx(
        author="A Reader", author_handle="areader",
        text="This is the clearest take on local inference. What rig are you "
             "running it on?"))
    assert not bounces_the_question(
        "One 4090. The MoE routing is what makes it fit, not the VRAM.", room)


def test_a_question_later_in_a_real_answer_is_fine():
    room = read_the_room(ctx(author="A Reader", author_handle="areader",
                             text="What rig are you running it on?"))
    assert not bounces_the_question(
        "A single 4090. What are you trying to run on yours?", room)


# ------------------------------------------------------------- the repeats
@pytest.mark.parametrize("draft", [
    "So Opus 5's half-price? That's a big deal. But how's it actually cheaper than 4.5?",
    "Alright, Opus 5's half-price claim. Does that mean it's actually cheaper?",
])
def test_the_same_reply_reworded_is_caught(draft):
    prior = ["So Opus 5's half price? What's the catch?",
             "Claude Opus 5's half-price claim feels like a trick. What's the catch?"]
    assert repeats_a_reply(draft, prior) is not None


def test_a_genuinely_different_reply_passes():
    prior = ["So Opus 5's half price? What's the catch?"]
    assert repeats_a_reply(
        "Halving the price two months after 4.8 means the 4.8 tier is dead.",
        prior) is None


_THE_ANNOUNCEMENT = ("Claude Opus 5 is here. Our most capable model yet, at half "
                     "the price of Opus 4.5.")


def test_naming_the_subject_twice_is_not_repeating_yourself():
    """Both of these are real, from one run, under the same post. They make
    different points and were called duplicates because both said "Opus 4.5" —
    which every honest reply to that post has to say."""
    prior = ["Half the price of Opus 4.5 means Opus 5 costs less than the previous tier."]
    assert repeats_a_reply("It might be a scaled-down version of Opus 4.5.", prior,
                           source=_THE_ANNOUNCEMENT) is None


def test_the_subject_does_not_become_a_free_pass():
    """Narrowing the run rule must not let a genuine reword through."""
    prior = ["So Opus 5's half price? What's the catch?"]
    assert repeats_a_reply("Alright, Opus 5's half-price claim. Does that mean it's actually cheaper?",
                           prior, source=_THE_ANNOUNCEMENT) is not None


def test_a_repeat_from_a_different_post_is_still_a_repeat():
    """Cross-post ruts are the ones that actually read as a bot: the shared run
    is not in THIS post, so it survives the filter."""
    prior = ["The 30B model outperforms 70B on one card due to bandwidth constraints."]
    assert repeats_a_reply("The trust is why the 30B beats the 70B on one card.", prior,
                           source="Everyone keeps saying local models are the future.") is not None


def test_the_old_gate_missed_exactly_this():
    """Regression witness: the general self-repeat gate does not fire here, so
    if this ever starts passing, the reply-specific check has been removed."""
    from app.intelligence import _echoes_own_writing
    prior = ["So Opus 5's half price? What's the catch?"]
    draft = "So Opus 5's half-price? That's a big deal. But how's it actually cheaper than 4.5?"
    assert not _echoes_own_writing(draft.lower(), [], corpus=prior)
    assert repeats_a_reply(draft, prior) is not None


# ----------------------------------------------------------------- the tics
@pytest.mark.parametrize("draft,tic", [
    ("So you're saying Qwen's not the bottleneck? What's the real issue?", "So you're saying"),
    ("So you're saying the missing piece is the moon?", "So you're saying"),
    ("What's the catch with Opus 5 at half price?", "What's the catch"),
])
def test_the_strawman_openers_are_caught(draft, tic):
    assert opens_with_a_tic(draft) == tic


def test_a_normal_opening_is_not_a_tic():
    assert opens_with_a_tic("Bandwidth is why the 30B beats the 70B on one card.") is None


# ------------------------------------------------------- the brief it writes
def test_the_agree_brief_forbids_arguing_and_demands_substance():
    room = read_the_room(ctx(author="Dev", author_handle="dev",
                             text="Been running Qwen locally for a month and "
                                  "the bottleneck was memory bandwidth."))
    brief = reply_instruction(room)
    assert "Do NOT argue" in brief
    # WHERE TO STAND, not just what to avoid. "Say the specific thing that
    # makes their point land harder" produced the parent inverted — "Qwen's
    # not the problem, but the memory bandwidth is" under a post reading "the
    # bottleneck was never the model, it was memory bandwidth". Agreeing and
    # restating are the same sentence unless the brief says one step PAST.
    assert "Start where their sentence STOPS" in brief
    # NOT RESTATING IS NOW ASKED FOR POSITIVELY. "Never say their point back in
    # different words" was a negative instruction naming the failure, which is
    # the construction this file has repeatedly measured as self-defeating. The
    # standalone test does the same work without naming anything: a line that
    # still stands with their post deleted cannot be their point reworded, and
    # it also rules out the deduction grammar ("X shows Y", "X proves Y") that
    # was six of twenty-three drafts in one run.
    # The standalone-line experiment was reverted: measured, it made the model
    # put their subject back into the reply so the line could stand alone, and
    # copy rejections went from none to two of fourteen — both returning no
    # draft at all — while the analyst count did not move.
    assert "stand up on its own" not in brief
    assert "flat statement" in brief
    assert "Never say their point back in different words" in brief
    assert "not a question back" in brief


def test_the_brief_lists_only_what_the_conversation_named():
    room = read_the_room(ctx(author="Claude", author_handle="claudeai",
                             text="Claude Opus 5 is here, half the price of Opus 4.5."))
    brief = reply_instruction(room)
    assert "opus" in brief
    assert "fable" not in brief
    assert "not WHAT about" in brief


# ------------------------------------------------- commentary, not a reply
@pytest.mark.parametrize("draft", [
    # Live 2026-07-27, and clean through all twelve product checks: no hedging
    # verb for the analyst pattern to catch, and a real specific in it, so the
    # payoff floor passed it at 0.44. It is still a write-up of the post.
    "Their claim about half the price of Opus 4.5 raises questions about the actual cost savings.",
    "It remains to be seen whether the price holds.",
    "Worth watching how the 30B handles longer context.",
    "It will be interesting to see what the benchmarks say.",
])
def test_declining_to_land_is_analyst_voice(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert sounds_like_an_analyst(draft)


@pytest.mark.parametrize("draft", [
    "a third of the price but it still feels like renting a space heater",
    "The MoE routing is not the bottleneck.",
    "at this rate Gemini 4 might launch before GPT-6",
    "Bandwidth is why the 30B beats the 70B on one card.",
])
def test_a_reply_that_lands_is_not_analyst_voice(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert not sounds_like_an_analyst(draft)


# ------------------------------------------- passing the bar, failing the eye
# These all came back CLEAN through every one of the twelve product checks, and
# reading them as him they are not replies he would post. That gap — absence of
# known faults is not presence of quality — is what these two classes close.
@pytest.mark.parametrize("draft", [
    # A tautology wearing a hedge: half price DOES mean cheaper.
    "Claude Opus 5's half-price claim could mean it's cheaper than Opus 4.5's full cost.",
    "Half the price of Opus 4.5 could mean Opus 5 is cheaper than any previous tier.",
    # Circular: it could affect the odds if the thing that moves the odds happens.
    "Opus 5's delay could affect the July 23 odds if the market reacts to the news.",
    # Stock verdicts.
    "529s in the morning, no support? It is a wake-up call for the whole team.",
    "so the cost cuts mean more people can use it without breaking the bank",
])
def test_a_hedge_or_a_stock_verdict_is_not_a_reply(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert sounds_like_an_analyst(draft)


@pytest.mark.parametrize("draft", [
    # His prediction voice. "X might BE true" is a call with his name on it;
    # "X could MEAN Y" is an inference he declined to make. Only the second is
    # caught, and this is the line that proves it.
    "at this rate Gemini 4 might launch before GPT-6",
    "Honestly, DeepSeek V4 might be the reason Gemini 4 slips.",
    "Gemini 4 might actually slip to next quarter.",
    # Deliberately spared: "the real win" and "at scale" show up inside replies
    # that land, so they are not on the stock-phrase list.
    "Reranking's the real win, but it's still a hack over a fix.",
    "The MoE routing's the real trick, not the card.",
    "That's the first time July 23 felt like a real deadline, not a guess.",
])
def test_his_own_shapes_survive_both_new_classes(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert not sounds_like_an_analyst(draft)


# ----------------------------------------------- the prompt's own words back
def test_the_point_prompt_carries_no_sentence_to_copy():
    """Twice the worked examples were lifted verbatim into live replies. The
    second time they had been moved to a neutral domain and fenced with "never
    about bread", which taught bread — the same own-goal already recorded for
    banned phrases. Shapes may be described; specimens may not be written."""
    from app.reply_plan import _POINT_PROMPT
    import re
    assert not re.search(r"bread|oven|loaf|loaves|bakery|proving drawer",
                         _POINT_PROMPT, re.I)
    # A "Good:" line IS a specimen — that is the shape of the mistake, whatever
    # domain it is written in.
    assert "Good:" not in _POINT_PROMPT
    assert "Bad:" not in _POINT_PROMPT


def test_a_point_built_from_the_template_is_rejected():
    """Guards the examples coming back. Empty by construction today, so this
    drives it with the words that actually leaked."""
    import app.reply_plan as rp
    from app.schemas import PostContext
    room = rp.read_the_room(
        PostContext(author="Swyx", author_handle="swyx",
                    text="This is the part everyone is missing."),
        own_handle="example_builder")
    original = rp._TEMPLATE_ONLY_WORDS
    rp._TEMPLATE_ONLY_WORDS = frozenset({"loaves", "oven", "proving", "drawer"})
    try:
        assert rp._speaks_the_templates_language(
            "The proving drawer is why their loaves beat the chain's.", room)
        assert not rp._speaks_the_templates_language(
            "Bandwidth is why the 30B beats the 70B on one card.", room)
    finally:
        rp._TEMPLATE_ONLY_WORDS = original


def test_the_guard_is_inert_while_there_is_nothing_to_leak():
    from app.reply_plan import _TEMPLATE_ONLY_WORDS, _speaks_the_templates_language
    from app.schemas import PostContext
    from app.reply_plan import read_the_room
    assert _TEMPLATE_ONLY_WORDS == frozenset()
    room = read_the_room(PostContext(author="A", author_handle="a", text="Some post."),
                         own_handle="example_builder")
    assert not _speaks_the_templates_language("anything at all here", room)


@pytest.mark.parametrize("draft", [
    # All three scored CLEAN through every other product check. Each restates
    # the post and appends a verdict the post did not ask for.
    "Claude Opus 5 costs half what Opus 4.5 did, proving its value for money.",
    "The memory bandwidth bottleneck shows the real limit of their local Qwen setup.",
    "Three users in the first hour shows how quickly your feature resonates.",
    "Qwen's memory bandwidth bottleneck proves local hardware outperforms it.",
    "The cost drop shows that scaling efficiency is now viable.",
])
def test_restating_the_post_and_appending_a_verdict_is_analyst_voice(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert sounds_like_an_analyst(draft)


@pytest.mark.parametrize("draft", [
    "That's gonna sting everyone who bet on prices staying flat.",
    "a third of the price but it still feels like renting a space heater",
    "The MoE routing's the real trick, not the card.",
])
def test_a_verdict_of_his_own_is_not_analyst_voice(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert not sounds_like_an_analyst(draft)


# ------------------------------------------- the brief that caused the fault
def test_the_agree_brief_asks_for_a_claim_not_a_deduction():
    """"Say what follows from it" was answered literally, and a consequence
    stated as a deduction has to name its premise — so the drafts came back as
    "the bottleneck SHOWS the real limit", "costs half, PROVING its value".
    Six of twenty-three drafts in one run. The standalone test is the fix: a
    line that still stands with their post deleted cannot be a deduction."""
    from app.reply_plan import _MOVES
    brief = _MOVES["agree_and_add"]
    assert "flat statement" in brief
    assert "what follows from it" not in brief
    assert "stand up on its own" not in brief, "the standalone clause measured worse"


@pytest.mark.parametrize("move", ["agree_and_add", "add_detail", "answer", "joke",
                                  "name_it", "correct", "continue"])
def test_no_move_brief_quotes_a_construction_it_wants_avoided(move):
    """The standing rule for this file: a phrase shown to the model is a phrase
    it uses. Briefs carry positive instructions; the mechanical strippers and
    the gates handle the forbidden shapes after generation."""
    import re
    from app.reply_plan import _MOVES
    brief = _MOVES[move]
    for banned in ("proving", "shows that", "could mean", "so you're saying",
                   "wake-up call", "raises questions"):
        assert banned.lower() not in brief.lower(), f"{move} names {banned!r}"


# ------------------------------------------- a short post used to be uncopyable
_VAGUE = {"author": "Swyx", "author_handle": "swyx",
          "text": "This is the part everyone is missing."}


def _vague_room():
    from app.reply_plan import read_the_room
    from app.schemas import PostContext
    return read_the_room(PostContext(**_VAGUE), own_handle="example_builder")


def test_swallowing_a_short_post_whole_is_a_copy():
    """The rule was "give up if the post is shorter than the run we look for",
    which is backwards — copying a seven-word post in full is more obviously a
    copy than lifting eight words out of forty. Live 2026-07-27 this shipped as
    CLEAN under that exact post."""
    from app.reply_plan import copies_the_post
    assert copies_the_post(
        "This is the part everyone is missing: the unspoken assumption that "
        "context length is free.", _vague_room())


@pytest.mark.parametrize("draft", [
    # Picking up their phrase and going somewhere with it is the move the whole
    # file is built to encourage; only swallowing the post whole is the fault.
    "The part everyone is missing is that context length is not free.",
    "Bandwidth is the ceiling nobody prices in.",
])
def test_borrowing_their_phrase_is_still_allowed(draft):
    from app.reply_plan import copies_the_post
    assert not copies_the_post(draft, _vague_room())


_STUDY_PAGE = ("The study reviewed 400 pricing pages of B2B SaaS companies in August 2026. Only 9% said on the "
               "page what happens when a customer outgrows their tier, and those pages had the lowest share of "
               "support questions about billing.")


def _linked_room():
    from app.reply_plan import read_the_room
    from app.schemas import PostContext
    return read_the_room(PostContext(author="Tom Reyes", author_handle="tomreyesgrowth",
                                     text="This study of 400 SaaS pricing pages is worth your time."),
                         evidence=_STUDY_PAGE, own_handle="example_builder")


def test_quoting_a_finding_of_the_linked_page_is_not_a_copy():
    """Golden run 2026-09-22: a reply citing one finding of the linked study, in eight of its words,
    was refused as "handed the post back" and replaced by a worse draft."""
    from app.reply_plan import copies_the_post
    assert not copies_the_post(
        "The figure I'd underline is the 9%. Only that share of pages said what happens when a customer "
        "outgrows their tier, and those pages got the fewest billing questions.", _linked_room())


def test_pasting_the_linked_page_is_still_a_copy():
    from app.reply_plan import copies_the_post
    assert copies_the_post(
        "Only 9% said on the page what happens when a customer outgrows their tier, and those pages had the "
        "lowest share of support questions about billing.", _linked_room())


def test_lifting_the_post_itself_is_still_a_copy():
    from app.reply_plan import copies_the_post
    assert copies_the_post("This study of 400 SaaS pricing pages is worth your time, and here is why.", _linked_room())


def test_the_authors_name_does_not_raise_the_bar():
    """`allowed` carries the author and handle, and counting them made a
    five-content-word post look like a seven-word one — which was exactly
    enough to keep the bar above the whole post."""
    from app.reply_plan import copies_the_post, read_the_room
    from app.schemas import PostContext
    long_name = read_the_room(
        PostContext(author="Somebody With A Very Long Name",
                    author_handle="somebodywithaverylongname",
                    text="This is the part everyone is missing."),
        own_handle="example_builder")
    assert copies_the_post(
        "This is the part everyone is missing: the unspoken assumption.", long_name)


@pytest.mark.parametrize("draft", [
    # Every one of these was written with a different inflection from the one
    # the pattern listed, and slipped through. Found live: a draft ending "it's
    # the data that MOVES the needle" while the list said "moving the needle".
    "The model's speed doesn't depend on the hardware, it's the data that moves the needle.",
    "This is what moved the needle for us.",
    "That raises the bar for everyone else.",
    "It breaks the bank for small teams.",
])
def test_a_stock_phrase_is_caught_in_every_inflection(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert sounds_like_an_analyst(draft)


def test_the_stock_phrase_list_is_written_in_stems_not_single_forms():
    """A list of exact word-forms is a list with holes in it. Each entry that
    contains a verb must allow its inflections, or the next draft picks the
    form nobody wrote down."""
    import re
    from app.reply_plan import _CORPORATE_REGISTER
    pattern = _CORPORATE_REGISTER.pattern
    for stem, forms in (("needle", ("ing", "es", "ed")),
                        ("the bar", ("es", "ed", "ing")),
                        ("the bank", ("ing", "s"))):
        window = pattern[max(0, pattern.find(stem) - 40):pattern.find(stem) + len(stem)]
        assert any(form in window for form in forms), f"{stem!r} is pinned to one form"


# ----------------------------------------------- reading the room correctly
def test_writing_something_down_is_not_an_outage():
    """"Spent the week replacing our retrieval stack. Writing down what actually
    mattered." was classified as a COMPLAINT, on the word "down". That surface
    then failed three runs out of three with a near-identical draft each time."""
    from app.reply_plan import _COMPLAINT
    assert not _COMPLAINT.search("Writing down what actually mattered.")
    assert not _COMPLAINT.search("it calmed down after an hour")
    assert not _COMPLAINT.search("we narrowed down the cause")


@pytest.mark.parametrize("text", [
    "The API is down again for everyone.",
    "the service went down twice today",
    "Third day in a row the API returns a 529 during European morning.",
    "Nobody at support has answered the ticket.",
])
def test_a_real_outage_still_reads_as_a_complaint(text):
    from app.reply_plan import _COMPLAINT
    assert _COMPLAINT.search(text)


@pytest.mark.parametrize("text", [
    "Spent the week replacing our retrieval stack. Writing down what mattered.",
    "Ran it on one card for a month and the bottleneck never moved.",
    "Tested both and the cheaper one won.",
])
def test_first_hand_experience_survives_a_dropped_pronoun(text):
    """Leaving the "I" off is ordinary on X, and the I/we requirement missed it
    entirely — those posts fell through to whatever pattern matched next."""
    from app.reply_plan import _FIRSTHAND
    assert _FIRSTHAND.search(text)


def test_the_thread_parent_post_reads_as_experience_not_a_grievance():
    from app.reply_plan import read_the_room
    from app.schemas import PostContext
    room = read_the_room(
        PostContext(author="Dev", author_handle="dev",
                    text="Spent the week replacing our retrieval stack. "
                         "Writing down what actually mattered."),
        own_handle="example_builder")
    assert room.kind == "firsthand"


# ------------------------------------------- the hedge family, not a phrase list
@pytest.mark.parametrize("draft", [
    # Each of these appeared live AFTER the previous verb was added to the gate.
    "Claude Opus 5's half-price deal might bring more users, but not cheaper ones.",
    "Opus 5's half-price offer could make it more accessible than Opus 4.5's.",
    "Opus 5's delay might mean the July 23 odds are now more volatile.",
    "The cost drop could shift where small teams spend.",
    "That might allow smaller shops to run it locally.",
])
def test_a_modal_in_front_of_a_causation_verb_is_a_hedge(draft):
    """Listing "could mean", then "could make", was losing a race: the next
    draft said "might BRING" and shipped because no gate saw it, so no retry
    fired. The family is what to catch."""
    from app.reply_plan import sounds_like_an_analyst
    assert sounds_like_an_analyst(draft)


@pytest.mark.parametrize("draft", [
    # His own calls. The verb class is what keeps these out: `be` and plain
    # event verbs are deliberately absent from the pattern.
    "Honestly, DeepSeek V4 might be the reason Gemini 4 slips.",
    "at this rate Gemini 4 might launch before GPT-6",
    "Gemini 4 might actually slip to next quarter.",
    "Grok 4.5 could drop before the end of the month.",
])
def test_a_prediction_with_his_name_on_it_survives(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert not sounds_like_an_analyst(draft)


# ------------------------------------------- the copy is only the opener
def _room(case_text, author="Claude", handle="claudeai"):
    from app.reply_plan import read_the_room
    from app.schemas import PostContext
    return read_the_room(PostContext(author=author, author_handle=handle, text=case_text),
                         own_handle="example_builder")


def test_the_post_worn_as_a_hat_is_cut_off():
    """`copies_the_post` refuses these, correctly — but in the commonest form
    the copy is only the first sentence and a real reply follows it. Refusing
    throws the good half away and leaves him with nothing."""
    from app.reply_plan import strip_copied_opener
    room = _room("Claude Opus 5 is here. Our most capable model yet, at half the price of Opus 4.5.")
    assert strip_copied_opener(
        "Claude Opus 5 is here. Half the price, but still way expensive for small teams.",
        room) == "Half the price, but still way expensive for small teams."


def test_cutting_the_opener_turns_a_refusal_into_a_reply():
    from app.reply_plan import strip_copied_opener, copies_the_post
    room = _room("This is the part everyone is missing.", author="Swyx", handle="swyx")
    draft = ("This is the part everyone is missing: the unspoken assumption that "
             "context length is free.")
    assert copies_the_post(draft, room), "the whole post is in there"
    fixed = strip_copied_opener(draft, room)
    assert fixed == "The unspoken assumption that context length is free."
    assert not copies_the_post(fixed, room)


@pytest.mark.parametrize("draft", [
    # An opener that says something of its own is not a hat.
    "Half the price of Opus 4.5 kills the 4.8 tier.",
    "Memory bandwidth, not the model. Same on my side.",
    "The MoE routing's the real trick, not the card.",
])
def test_a_reply_of_his_own_is_never_cut(draft):
    from app.reply_plan import strip_copied_opener
    room = _room("Claude Opus 5 is here. Our most capable model yet, at half the price of Opus 4.5.")
    assert strip_copied_opener(draft, room) == draft


def test_nothing_is_cut_when_nothing_survives():
    """Half a sentence is worse than a copy — if the remainder is not a reply,
    leave it for the gate."""
    from app.reply_plan import strip_copied_opener
    room = _room("Claude Opus 5 is here. Our most capable model yet.")
    draft = "Claude Opus 5 is here. Yeah."
    assert strip_copied_opener(draft, room) == draft


# ------------------------------------------- the deduction bolted onto a claim
@pytest.mark.parametrize("draft,expected", [
    ("Chunking strategy's impact outshone the 1.9s latency drop, proving that "
     "focused optimization beats brute force.",
     "Chunking strategy's impact outshone the 1.9s latency drop."),
    ("Claude Opus 5 costs half what Opus 4.5 did, proving its value for money.",
     "Claude Opus 5 costs half what Opus 4.5 did."),
    ("Three users bought it in the first hour, proving early traction can turn "
     "excitement into revenue.",
     "Three users bought it in the first hour."),
])
def test_the_deduction_tail_is_cut_and_the_claim_survives(draft, expected):
    """The writer bolts ", proving that ..." onto a concrete claim 2-3 attempts
    running, so retrying cannot fix it — the repair is mechanical, like the
    tics. Measured: 0 of his 35 archive texts carry the shape; 6 of 446 drafts
    did, and in all six the head stood alone."""
    from app.reply_plan import strip_deduction_tail, sounds_like_an_analyst
    out = strip_deduction_tail(draft)
    assert out == expected
    assert not sounds_like_an_analyst(out)


@pytest.mark.parametrize("draft", [
    "Bandwidth is why the 30B beats the 70B on one card.",
    "at this rate Gemini 4 might launch before GPT-6",
    "Half the price, but still way expensive for small teams.",
    # A head too short to stand alone is left for the gate.
    "Nice, proving that it works.",
])
def test_a_line_without_the_tail_is_untouched(draft):
    from app.reply_plan import strip_deduction_tail
    assert strip_deduction_tail(draft) == draft


def test_a_sentence_after_the_tail_survives_the_cut():
    from app.reply_plan import strip_deduction_tail
    draft = ("Chunking outshone the embedding swap, proving that focus wins. "
             "The rerank cost is the next thing to cut.")
    assert strip_deduction_tail(draft) == (
        "Chunking outshone the embedding swap. The rerank cost is the next thing to cut.")


def test_the_participial_tail_is_cut_too():
    """", making the market less predictable" recurred in three straight runs;
    0 of his 36 archive texts carry the shape, 7 of 495 drafts did."""
    from app.reply_plan import strip_deduction_tail
    assert strip_deduction_tail(
        "Opus 5's delay could affect the July 23 odds, making the market less predictable."
    ) == "Opus 5's delay could affect the July 23 odds."


def test_the_tail_cut_refuses_to_leave_an_echo():
    """Three runs measured the conversion: analyst went to zero while echo rose
    — cutting ", proving …" off a restatement leaves the restatement. A head
    whose content words are ≥70% the post's stays flagged so the retry ladder
    regenerates instead."""
    from app.reply_plan import strip_deduction_tail
    post = "Spent the week replacing our retrieval stack. Chunking mattered most; 1.9s to 700ms."
    echoy = "Chunking mattered most in the retrieval stack, proving that focus wins."
    fresh = "Reranking was the sleeper win over the embedding swap, proving that focus beats brute force."
    assert strip_deduction_tail(echoy, post) == echoy
    assert strip_deduction_tail(fresh, post) == "Reranking was the sleeper win over the embedding swap."
    assert strip_deduction_tail(fresh) != fresh, "no source keeps the old behaviour"


# --------------------------------------------------------------------------- #
# The same phrase, listed independently in two gates, in two files
# --------------------------------------------------------------------------- #

def test_his_own_hype_is_not_analyst_voice():
    r"""2026-08-07. A published post shaped "<tool> letting <feature> is an
    absolute game changer." set off TWO gates from two different files:

        reply_plan._CORPORATE_REGISTER   matched  game[- ]chang\w+
        intelligence._FILLER_RE          deleted the whole first line

    Neither list knew the other existed, so one phrase produced two failures
    and would have taken two separate fixes to find. Both are split the same
    way: hype that evaluates a NAMED thing is his register; hype that evaluates
    nothing is the model's.
    """
    from app.reply_plan import sounds_like_an_analyst
    his = ("Cursor letting agents review each other's diffs is an absolute "
           "game changer.")
    assert not sounds_like_an_analyst(his)
    assert not sounds_like_an_analyst("Opus 5 at half the price is a game changer.")
    # `named_things` reads capitalised names and known aliases; "vLLM" is
    # neither, so this sentence also names the model family it serves.
    assert not sounds_like_an_analyst(
        "The new vLLM scheduler is game-changing for Llama inference.")


def test_hype_about_nothing_is_still_analyst_voice():
    """The split must not become a hole. Every inflection the old stem covered
    is still caught when the sentence names nothing to be excited about."""
    from app.reply_plan import sounds_like_an_analyst
    for draft in ("Honestly this is a game changer for the whole space.",
                  "That would be game-changing for everyone involved.",
                  "It is a real game changer."):
        assert sounds_like_an_analyst(draft), draft


def test_the_hype_split_is_structural_not_a_deletion():
    """Removing the phrase and stopping would leave the next hype noun to be
    found by him on a live post — the failure already recorded when "fatal
    flaw" and "let it rip" were blocked as human tragedy. So the class must be
    ABSENT from the unconditional list and PRESENT in the corroborated one; an
    edit that drops it entirely fails here.
    """
    from app.reply_plan import _CORPORATE_REGISTER, _HYPE_NEEDS_SUBJECT
    assert not _CORPORATE_REGISTER.search("this is a game changer")
    assert _HYPE_NEEDS_SUBJECT.search("this is a game changer")
    assert _HYPE_NEEDS_SUBJECT.search("that is game-changing")

    import inspect
    from app.reply_plan import sounds_like_an_analyst
    assert "named_things" in inspect.getsource(sounds_like_an_analyst), (
        "corroboration must reuse named_things, not a second word list")
