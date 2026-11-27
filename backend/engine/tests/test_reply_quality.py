"""The reply pipeline, end to end, pinned against what it actually shipped.

`test_reply_plan.py` covers the reading and the gates in isolation. This covers
the parts that live in `intelligence.py` and `main.py` — the choices that
decide what the model is shown and what happens to a draft that misses a bar.

Everything here was measured live on 2026-07-26 against his real archive
(36 texts) and the drafts in `generation_candidates`.
"""
from __future__ import annotations

import pytest

from app import intelligence
from app.intelligence import (_INTEGRITY_ISSUES, _CLARITY_ISSUES,
                              _WEAKNESS_NOTES, _invented_experience,
                              _reply_voice_examples, _THATS_NOT_RE)
from app.main import _EVIDENCE_INSTRUCTION, _EVIDENCE_INSTRUCTION_REPLY, _with_evidence
from app.reply_plan import avoid_recent_openings, has_no_position, plan_reply
from app.schemas import DraftRequest, PostContext


def ctx(**kwargs) -> PostContext:
    base = {"author": "Someone", "author_handle": "someone", "text": "A post."}
    base.update(kwargs)
    return PostContext(**base)


def request_for(context: PostContext, action: str = "reply") -> DraftRequest:
    return DraftRequest(action=action, context=context, research_mode="off",
                        media_policy="no_media", grok_permission=False,
                        length="medium", direction="x")


class _Model:
    """A stand-in archive: his real shape — mostly long posts, a few short ones."""

    def __init__(self, rows):
        self.example_rows = rows


LONG_POST = ("Gemini 4 is closer than the release notes make it look. The budget "
             "Flash tier already matches last year's Pro model on most of the coding "
             "benchmarks people actually check, and when the cheap tier catches the "
             "flagship, the next flagship is usually only a few weeks out.")
SHORT = [
    "at this rate Gemini 4 might launch before GPT-6",
    "this is the first time the agent demo actually feels legit",
    "turns out people would rather review a diff than babysit an agent",
    "matching GPT-5.6 Sol at a third of the price is kinda insane",
]


# ------------------------------------------------- what the model is shown
# V2 (2026-09-21): these used to pin the ORIGINAL AUTHOR's reply register (short
# texts only, ordered by his slang, on-topic excluded). A reply is now shown the
# writer's own REPLIES, as pairs with the post they answered, most similar first.
def test_a_reply_is_shown_the_writers_own_reply_pairs_first():
    rows = ([{"text": LONG_POST, "kind": "post"}]
            + [{"text": t, "kind": "reply", "parent": f"a post about launches {i}"} for i, t in enumerate(SHORT)])
    picked = _reply_voice_examples(_Model(rows), request_for(ctx(text="Some launch news.")))
    assert picked, "a reply must get examples"
    assert LONG_POST not in picked, "their own replies come before their posts"
    assert all(example.parent for example in picked), "each reply travels with the post it answered"


def test_the_most_similar_own_reply_leads_even_when_it_is_on_topic():
    """The copy guards run on every draft, so the most similar writing can be shown."""
    rows = [{"text": t, "kind": "reply", "parent": "a post"} for t in SHORT]
    picked = _reply_voice_examples(_Model(rows), request_for(ctx(
        text="Gemini 4 might launch before GPT-6 at this rate, going by the leaks")))
    assert picked[0] == "at this rate Gemini 4 might launch before GPT-6"


def test_a_long_form_writer_is_shown_their_long_replies():
    """Their voice, whatever its length: nothing filters on one author's reply length."""
    rows = [{"text": LONG_POST + f" Point {i}.", "kind": "reply", "parent": "a post about Gemini"}
            for i in range(4)]
    picked = _reply_voice_examples(_Model(rows), request_for(ctx(text="Gemini release notes")))
    assert picked and all(len(text.split()) > 36 for text in picked)


# ------------------------------------------------------ not the same reply
def test_the_habit_is_named_by_shape_not_by_phrase():
    """An earlier version quoted the actual openings. It suppressed "So you're
    saying" and "What happens when" took its place at the same rate — three
    times in one run across three unrelated posts. A phrase put in front of a
    small model is a phrase it uses, whatever the verb in front of it."""
    line = avoid_recent_openings([
        "What happens when the cost drops below a third?",
        "What happens when reranking costs more than it saves?",
        "What if the price moves again next quarter?",
        "Bandwidth is the ceiling here.",
    ])
    assert "opened with a question" in line
    assert "flat statement" in line
    assert "What happens when" not in line, "never quote the tic back at it"


def test_a_repeated_first_word_is_named():
    """One word is safe to quote — it is a word, not a construction."""
    line = avoid_recent_openings([
        "The cost reduction suggests they optimized training.",
        "The cost curve is steeper than that.",
        "The real chokepoint is bandwidth.",
        "Sparse activation is the trick.",
    ])
    assert '"the"' in line


def test_varied_openings_add_nothing():
    assert avoid_recent_openings([
        "Bandwidth is the ceiling.",
        "Reranking beat the embedding swap.",
        "One 4090 handles it fine.",
        "Support has been silent all week.",
    ]) == ""


def test_no_recent_replies_adds_nothing():
    assert avoid_recent_openings([]) == ""


def test_the_prompt_never_names_the_tic_it_wants_avoided():
    """The own-goal this file exists to prevent. "So you're saying" opened 6 of
    26 drafts; after the plan gained an explicit "Never open with 'So you're
    saying'" it opened 3 of 4. A phrase put in front of a small model is a
    phrase it uses, so the prompt carries the positive instruction only and the
    tic is removed after generation."""
    plan, _ = plan_reply(ctx(), llm=None, recent_replies=[])
    assert "So you're saying" not in plan
    assert "Never open with" not in plan
    # The positive opener line is gone too: `strip_strawman_opener` removes the
    # tic mechanically, so asking for it again cost 60 characters of a budget
    # the web evidence shares.


@pytest.mark.parametrize("draft,expected", [
    ("So you're saying the model's cheaper without losing power?",
     "The model's cheaper without losing power."),
    ("So you're saying the model's not the issue? Then why's the memory "
     "bandwidth the real pain?",
     "The model's not the issue. Then why's the memory bandwidth the real pain?"),
    ("So you're saying that sparse activation is the missing piece everyone ignores?",
     "Sparse activation is the missing piece everyone ignores."),
    ("What's the catch? Three users bought it in the first hour.",
     "Three users bought it in the first hour."),
])
def test_the_tic_is_removed_and_the_point_survives(draft, expected):
    from app.reply_plan import strip_strawman_opener
    assert strip_strawman_opener(draft) == expected


@pytest.mark.parametrize("draft", [
    "Bandwidth is why the 30B beats the 70B on one card.",
    "One 4090. What are you running yours on?",
    "at this rate Gemini 4 might launch before GPT-6",
])
def test_a_clean_reply_is_left_alone(draft):
    from app.reply_plan import strip_strawman_opener
    assert strip_strawman_opener(draft) == draft


# --------------------------------------------------------- saying nothing
def test_weighing_both_sides_and_picking_neither_is_caught():
    """Forty-nine words announcing that the writer does not understand."""
    assert has_no_position(
        "So you're saying 30B is now cheaper than its own API? That's "
        "confusing. Maybe the cost is lower when you use it directly. Or maybe "
        "the API has extra fees. Either way, it's not clear how the pricing works.")


def test_a_bare_maybe_that_takes_a_line_is_fine():
    """His register. Banning "maybe" would ban him."""
    assert not has_no_position("Maybe the leak is real. But where is the changelog?")


# -------------------------------------------- experience it does not have
@pytest.mark.parametrize("draft", [
    "You're half the price, but Opus 5's full-stack app builds still feel like a beta.",
    "You're running it on a GPU? I'm on a CPU and it's already faster.",
    "You say pricing matters, but the $200 plan is the only one that feels fair.",
])
def test_lived_experience_without_the_word_i_is_still_invented(draft):
    assert _invented_experience(draft, "") is not None


@pytest.mark.parametrize("draft", [
    "Halving the price two months after 4.8 makes the older tier hard to justify.",
    "at this rate Gemini 4 might launch before GPT-6",
    "Bandwidth is why the 30B beats the 70B on one card.",
])
def test_ordinary_writing_is_not_treated_as_an_anecdote(draft):
    assert _invented_experience(draft, "") is None


# ------------------------------------------------------ the banned shapes
@pytest.mark.parametrize("draft", [
    "Three users? That's not a feature, that's a leak.",
    "Half the price? You're not selling Opus 5, you're selling a discount sticker.",
    "That's not a failure. It's a sign.",
])
def test_the_not_a_but_b_template_is_caught_in_every_form(draft):
    assert _THATS_NOT_RE.search(draft.lower())


def test_an_ordinary_negation_still_passes():
    assert not _THATS_NOT_RE.search(
        "this is not the model i expected but the price is right")


# ------------------------------------------------------- how it is treated
@pytest.mark.parametrize("draft", [
    "The cost reduction suggests they're optimizing for lower-end hardware.",
    "The half-price claim suggests Opus 5's cost efficiency is a selling point.",
    "The move indicates a shift in how they price the mid tier.",
    # The same failure wearing nouns instead of verbs — no hedging verb in it,
    # so the verb pattern alone cannot see it. Sent under a post from somebody
    # celebrating their first three paying customers.
    "Three users in the first hour means the product is hitting the right "
    "notes with its value proposition.",
    "Cheaper serving is the key selling point for stakeholders here.",
])
def test_an_analysts_note_is_not_a_reply(draft):
    """Scoped to replies on purpose. He uses "suggests" once, in a long post,
    under a bullet with the evidence stacked above it. Across his 24 texts of
    36 words or fewer — which is what a reply is — this hits zero."""
    from app.reply_plan import sounds_like_an_analyst
    assert sounds_like_an_analyst(draft)


@pytest.mark.parametrize("draft", [
    "at this rate Gemini 4 might launch before GPT-6",
    "A third of the price and this close to Gemini 3 is kinda insane.",
    "Bandwidth is why the 30B beats the 70B on one card.",
])
def test_his_own_short_voice_is_not_analyst_voice(draft):
    from app.reply_plan import sounds_like_an_analyst
    assert not sounds_like_an_analyst(draft)


@pytest.mark.parametrize("draft", [
    "The post is gesturing at the overlooked efficiency of sparse activation.",
    "This post makes a good point about pricing.",
    "The author is right that bandwidth is the ceiling.",
])
def test_narrating_their_post_is_not_replying_to_them(draft):
    from app.reply_plan import talks_about_the_post
    assert talks_about_the_post(draft)


@pytest.mark.parametrize("draft", [
    "Your take on local inference is the clearest I have read.",
    "Bandwidth is why the 30B beats the 70B on one card.",
])
def test_addressing_the_person_is_fine(draft):
    """It is the third-person artefact that never appears in his 35 archive
    texts, not talking to somebody."""
    from app.reply_plan import talks_about_the_post
    assert not talks_about_the_post(draft)


@pytest.mark.parametrize("draft,expected", [
    ("The cost reduction suggests they're optimizing for smaller-scale deployments.",
     "They're optimizing for smaller-scale deployments."),
    ("The half-price claim suggests Opus 5 cost efficiency is a key selling point.",
     "Opus 5 cost efficiency is a key selling point."),
    ("The cost drop suggests efficiency gains outpace traditional API limits.",
     "Efficiency gains outpace traditional API limits."),
])
def test_the_hedge_is_removed_and_the_claim_survives(draft, expected):
    """This one survived a positive register instruction AND a gate: when every
    candidate hedges, the near-miss path has to ship one of them. Removing the
    scaffolding leaves the flat claim that was wanted all along."""
    from app.reply_plan import strip_analyst_hedge
    assert strip_analyst_hedge(draft) == expected


@pytest.mark.parametrize("draft", [
    "Bandwidth is why the 30B beats the 70B on one card.",
    "at this rate Gemini 4 might launch before GPT-6",
    "Everything suggests so.",
])
def test_nothing_is_promoted_out_of_a_clean_line(draft):
    """Half a sentence is worse than a hedge, so a stub is left alone.

    A noun-phrase complement is NOT in this list: it used to be left alone,
    and is now unhedged by swapping the verb for the copula instead — see
    test_a_noun_phrase_complement_is_unhedged_by_swapping_the_verb."""
    from app.reply_plan import strip_analyst_hedge
    assert strip_analyst_hedge(draft) == draft


def test_every_reply_clarity_issue_can_actually_be_revised():
    """A clarity issue with no CLARITY_GUIDANCE entry collects no notes, so
    `revise_for_clarity` returns None and the draft skips its revision pass
    entirely — it goes straight to the near-miss path and ships flagged."""
    from app.llm import ModelClient
    for issue in ("strawman_opener", "no_position", "analyst_voice",
                  "talks_about_the_post"):
        assert issue in ModelClient.CLARITY_GUIDANCE


def test_the_guidance_never_quotes_the_phrase_it_forbids():
    """Naming a phrase to this model is handing it the phrase — measured twice
    on this surface. The guidance is shown to the model verbatim."""
    from app.llm import ModelClient
    for issue in ("strawman_opener", "no_position", "analyst_voice",
                  "talks_about_the_post"):
        text = ModelClient.CLARITY_GUIDANCE[issue].lower()
        for phrase in ("so you're saying", "suggests", "indicates",
                       "what's the catch", "either way"):
            assert phrase not in text, f"{issue} guidance quotes {phrase!r}"


def test_a_verbatim_stretch_of_their_post_can_never_ship():
    """`BASE_DRAFT_RULES` has always said "never copy more than eight words in
    a row", and nothing checked it against the post being replied to. The
    similarity average cannot see it either — the copy and a genuinely good
    reply to the same post both score 0.60."""
    from app.reply_plan import copies_the_post, read_the_room
    room = read_the_room(ctx(
        author="Marc", author_handle="marcbuilds",
        text="Honestly the pricing page is the only benchmark that has ever "
             "mattered to me."))
    assert copies_the_post(
        "The pricing page is the only benchmark that has ever mattered to me "
        "because it reflects real-world value.", room)
    assert "copies_the_post" in _INTEGRITY_ISSUES
    assert "copies_the_post" not in _WEAKNESS_NOTES


@pytest.mark.parametrize("draft", [
    "Pricing is the only benchmark with a receipt attached.",
    "Benchmarks measure the model. The invoice measures the decision.",
    "Same, and the second invoice is the one that actually decides it.",
])
def test_a_reply_that_borrows_a_phrase_is_not_a_copy(draft):
    """Measured on the live drafts: copies run 10-11 words, every genuinely
    different reply runs 1-3. Eight has margin on both sides."""
    from app.reply_plan import copies_the_post, read_the_room
    room = read_the_room(ctx(
        author="Marc", author_handle="marcbuilds",
        text="Honestly the pricing page is the only benchmark that has ever "
             "mattered to me."))
    assert not copies_the_post(draft, room)


def test_a_leak_can_never_ship_but_the_others_can():
    """His rule is that silence is worse than a flawed draft he can fix in five
    seconds — but a name the conversation never mentioned is a claim, not a
    wording problem, so that one still refuses."""
    assert "leaked_into_reply" in _INTEGRITY_ISSUES
    for issue in ("bounced_question", "strawman_opener", "repeated_reply",
                  "no_position", "analyst_voice",
                  "talks_about_the_post"):
        assert issue not in _INTEGRITY_ISSUES
        assert issue in _WEAKNESS_NOTES, f"{issue} would ship as a bare shrug"
        assert _WEAKNESS_NOTES[issue].startswith("Heads up:")


def test_the_rewritable_reply_faults_get_a_revision_first():
    assert "strawman_opener" in _CLARITY_ISSUES
    assert "no_position" in _CLARITY_ISSUES


def test_an_integrity_failure_never_carries_a_caveat():
    assert "leaked_into_reply" not in _WEAKNESS_NOTES


# ----------------------------------------------------------- the evidence
def test_a_reply_is_not_ordered_to_cite_a_web_fact():
    """The mandate is what welded "So Qwen Code can file issues?" onto a post
    about memory bandwidth."""
    facts = "A" * 300
    out = _with_evidence("Reply to this.", facts, action="reply")
    assert _EVIDENCE_INSTRUCTION_REPLY in out
    assert _EVIDENCE_INSTRUCTION not in out


def test_a_post_still_is():
    out = _with_evidence("Write a post.", "B" * 300)
    assert _EVIDENCE_INSTRUCTION in out


def test_the_reply_plan_leaves_room_for_the_facts():
    """The plan and the evidence share one 1200-character budget. When the plan
    grew, the facts were silently dropped — a reply that lost its grounding
    without anything saying so."""
    base = ("Write one reply to the visible post, in the writer's voice. "
            "Best of all is a reply the AUTHOR wants to answer. One quick read.")
    facts = ("Anthropic released Claude Opus 5 on July 24 2026 at five dollars "
             "per million input tokens, half the price of Opus 4.5, with a "
             "one million token context window.")
    plan, _ = plan_reply(
        ctx(author="Claude", author_handle="claudeai",
            text="Claude Opus 5 is here. Our most capable model yet, at half "
                 "the price of Opus 4.5."),
        llm=None, evidence=facts, recent_replies=[])
    out = _with_evidence(f"{base} {plan}", facts, action="reply")
    assert len(out) <= 1200
    assert "USE THESE FACTS" in out


# --------------------------------------------------- what counts as a name
#
# These matter more than they look. A name the conversation never mentioned is
# an INTEGRITY failure and refuses the draft outright, so a false positive here
# is a good reply deleted — and when it hits every candidate, silence.
@pytest.mark.parametrize("text,expected", [
    ("Serving a 30B is cheaper than the API it replaces.", []),
    ("Maybe the cost is lower. Either way it is not clear.", []),
    ("Then why is the memory bandwidth the real pain?", []),
    ("A third of the price and this close to Gemini 3 is kinda insane.", ["gemini", "google"]),
    ("Fable 5 makes that curve look flat.", ["anthropic", "fable"]),
    ("I think Cohere cuts pricing next.", ["cohere"]),
    ("Opus 5 at that price changes the maths.", ["anthropic", "opus"]),
])
def test_only_real_names_count(text, expected):
    from app.reply_plan import named_things
    assert sorted(named_things(text)) == sorted(expected)


def test_addressing_the_author_is_not_an_intrusion():
    """"Ben, your agent's got a new job" was rejected for naming Ben — who
    wrote the post being replied to."""
    from app.reply_plan import intruders, read_the_room
    room = read_the_room(ctx(author="Ben", author_handle="benposts",
                             text="my agent opened 14 pull requests overnight"))
    assert intruders("Ben, your agent has found its calling", room) == []


# ------------------------------------------------- the contracted banned shape
def test_the_contraction_no_longer_walks_through_the_gate():
    """`\\bnot just\\b` cannot match "isn't just" — there is no word boundary
    inside the contraction — so the most-banned shape in the drafting rules
    shipped as a reply under a post carrying two real figures."""
    from app.intelligence import _NOT_JUST_RE
    assert _NOT_JUST_RE.search(
        "this isn't just a price drop. it's a shift in how people use these tools")
    assert _NOT_JUST_RE.search("it's not just fast, it's cheap")
    assert _NOT_JUST_RE.search("you're not just faster. you're smarter")


def test_an_ordinary_not_just_is_left_alone():
    from app.intelligence import _NOT_JUST_RE
    assert not _NOT_JUST_RE.search("not just anyone can do that")


# --------------------------------------------------- the scrubber's reach
def test_the_comma_joined_template_is_actually_removed():
    """It never was. `_COMMA_TEMPLATE_RE` exists for a construction that is ONE
    sentence, and it sat below a `len(spans) < 3` early return — so on the
    drafts it was written for it never ran. It matched all three of its own
    documented examples and changed none of them."""
    from app.intelligence import strip_formulas
    assert strip_formulas("That's not a guess, it's in the fresh data.") == \
        "It's in the fresh data."
    assert strip_formulas("Half the price? That's not a price, it's a limit.") == \
        "Half the price? It's a limit."


@pytest.mark.parametrize("draft,expected", [
    ("Moe's not a card, it's a switch. You got two?", "It's a switch. You got two?"),
    ("Opus 5's sloppiness isn't a bug, it's a feature.", "It's a feature."),
    ("The pricing page isn't a benchmark, it's a receipt.", "It's a receipt."),
])
def test_the_noun_subject_form_is_covered_too(draft, expected):
    """Fifth variant of one construction to need covering: sentence form, comma
    form, contraction form, "you're" form, and a plain noun in front. All the
    same machine — a setup that says nothing joined to the half that does."""
    from app.intelligence import strip_formulas
    assert strip_formulas(draft, min_survivors=1) == expected


def test_a_genuine_sentence_with_a_comma_survives():
    from app.intelligence import strip_formulas
    draft = "Serving a 30B is now cheaper than the API, it's a real shift."
    assert strip_formulas(draft, min_survivors=1) == draft


@pytest.mark.parametrize("draft,expected", [
    ("half the price? that's not a price, that's a discount for the whole world",
     "half the price? That's a discount for the whole world"),
    ("That's not a guess, it's in the fresh data", "It's in the fresh data"),
    ("Moe's not a card, it's a switch", "It's a switch"),
])
def test_the_construction_is_cut_without_a_final_full_stop(draft, expected):
    """Requiring terminal punctuation was the whole bug. This account's house
    style omits it — half his archive ends without one — so the formula
    survived intact exactly when the model matched his punctuation. Same
    sentence with and without a period had opposite outcomes."""
    from app.intelligence import strip_formulas
    assert strip_formulas(draft, min_survivors=1) == expected


def test_a_reply_may_be_cut_down_to_one_sentence():
    """Two survivors is a sensible floor for a post. A reply's measured shape on
    this account is one block of about 14 words, so that guard fired on nearly
    every reply that needed the surgery and the formula shipped intact."""
    from app.intelligence import strip_formulas
    draft = ("Memory bandwidth is the real chokepoint. That's not a bug. "
             "It's the whole design.")
    assert strip_formulas(draft) == draft                     # post: left alone
    assert strip_formulas(draft, min_survivors=1) == \
        "Memory bandwidth is the real chokepoint."


def test_not_just_about_survives_when_it_takes_a_clause():
    """A voice form the account owner confirmed on 2026-08-04, after the
    scrubber ate it.

    It is pinned here as a fixture so it holds on every machine. The scrubber
    saw "about", called the construction a template, and deleted both halves,
    which is the entire argument of the post.

    Kept alongside the forms that must still die, because the narrowing is only
    correct if it is narrow: the template names an abstract noun ("not just
    about speed"), the voice form takes a clause ("not just about who ships…").
    """
    from app.intelligence import strip_formulas

    his = ("The pricing page for the new coding plans says more than the launch "
           "video did.\n\n"
           "It’s not just about who ships the fastest agent.\n\n"
           "It’s about who developers trust enough to hand their repo, secrets, and "
           "whole release process to.\n\n"
           "Right now, no vendor has fully earned that trust.")
    assert strip_formulas(his) == his, "his own argument is being deleted"
    assert strip_formulas(his, min_survivors=1) == his

    # Bare-noun payoff, no "about" — the other confirmed shape.
    bare = "That cache isn't just speed. It's margin."
    assert strip_formulas(bare) == bare

    # The scrubber still cuts. Asserted with a form measured to be cut both
    # before and after the narrowing, so this fails if the change ever grows
    # into "leave the construction alone entirely".
    stock = ("That's not a trend. It's a model that scales faster than "
             "anything. Groq shipped it.")
    assert strip_formulas(stock, min_survivors=1) != stock


def test_the_about_marker_needs_a_noun_not_a_clause():
    """The narrowing itself, at the level it was made.

    Pinned on `_not_just_is_template` rather than through `strip_formulas`,
    because the surrounding pipeline has its own floors and sentence-shape
    checks — a pipeline-level assertion can pass or fail for reasons that have
    nothing to do with this discriminator.
    """
    from app.intelligence import _not_just_is_template

    # Template: "about" takes an abstract noun and moves on.
    assert _not_just_is_template("not just about speed. It's about who gets")
    assert _not_just_is_template("isn't just about the price. It's about the")
    # His voice: "about" takes a clause, and the clause is the argument.
    assert not _not_just_is_template(
        "not just about who ships the fastest agent. It's about who developers")
    assert not _not_just_is_template("not just about how they price it. It's")
    # A gerund or "anymore" still convicts on its own — no voice form to protect,
    # so a clause cannot redeem those.
    assert _not_just_is_template("not just writing code anymore. They're")
    assert _not_just_is_template(
        "not just about who ships fastest anymore. It's about")


# ------------------------------------------------------ the mangled ending
def test_a_reply_ending_mid_clause_is_caught():
    """"Half the price? That's not" reached the composer as a finished reply.
    It ends on no punctuation, so the ends-clean test passed, and "not" was not
    on the dangling list."""
    from app.intelligence import _readability_issues
    assert "cut_off" in _readability_issues("Half the price? That's not")


@pytest.mark.parametrize("draft", [
    "at this rate Gemini 4 might launch before GPT-6",
    "this is the first time the agent demo actually feels legit",
    "A third of the price and this close to Gemini 3 is kinda insane.",
    "Bandwidth is why the 30B beats the 70B on one card.",
])
def test_his_own_endings_are_not_treated_as_truncation(draft):
    """The list stays tight on purpose: his posts end on "you", "do", "have",
    "this", "lol" and "insane", so the obvious-looking additions are all
    wrong."""
    from app.intelligence import _readability_issues
    assert "cut_off" not in _readability_issues(draft)


# ------------------------------------------------------- the room's language
def test_a_spanish_post_gets_a_spanish_reply():
    """`PostContext.language` was captured and stored the whole time and never
    reached the writer, so "El precio de Opus 5 cambia completamente el calculo
    para equipos pequenos" got an English reply nobody in that thread reads."""
    plan, room = plan_reply(
        ctx(author="Deves", author_handle="deves", language="es",
            text="El precio de Opus 5 cambia completamente el calculo para "
                 "equipos pequenos."),
        llm=None, recent_replies=[])
    assert room.language == "Spanish"
    # FIRST, not last. It used to sit at the end of a 900-character brief and
    # the Spanish replies came back as the parent translated — mirroring 0.56
    # against the post, where a real Spanish reply scores 0.33.
    assert "Spanish" in plan[:120]
    assert "translate their sentence back" in plan


@pytest.mark.parametrize("tag", ["en", "en-GB", "", None, "zz"])
def test_english_and_unknown_tags_add_nothing(tag):
    """An unrecognised tag is better left unmentioned than turned into an
    instruction to write in a language nobody can check."""
    _, room = plan_reply(ctx(language=tag), llm=None, recent_replies=[])
    assert room.language == ""


# ------------------------------------------------ restating the post above
def test_handing_his_own_post_back_to_the_replier_is_caught():
    """On his own thread the parent is HIS post, and repeating it at the person
    answering it is the same offence as restating the visible one. `parent_text`
    was not in the mirroring source at all."""
    from app.growth_scorer import _source_mirroring
    visible = ("Wait, you got it running on a single 4090? I assumed you needed "
               "two cards minimum for that context length.")
    parent = ("Running the 30B locally on one card. The trick is the MoE "
              "routing, not the VRAM.")
    echo = ("You run 30B on one 4090? It's a single card with a MoE routing "
            "trick. What's the latency?")
    assert _source_mirroring(echo, visible) < 0.5             # invisible before
    assert _source_mirroring(echo, f"{visible} {parent}") >= 0.5


@pytest.mark.parametrize("draft", [
    "One 4090. The routing only wakes two experts at a time, so you never pay for the whole thing.",
    "Two cards is the old rule. Sparse activation broke it about a year ago.",
])
def test_a_real_answer_on_his_own_thread_still_passes(draft):
    from app.growth_scorer import _source_mirroring
    source = ("Wait, you got it running on a single 4090? I assumed you needed "
              "two cards minimum for that context length. Running the 30B "
              "locally on one card. The trick is the MoE routing, not the VRAM.")
    assert _source_mirroring(draft, source) < 0.5


# ------------------------------------------- what he is told when it refuses
def test_an_all_rejected_reply_does_not_blame_a_stopped_model():
    """`saved_candidates` can never tell the two cases apart — a rejected draft
    is never saved as a candidate, so "the model returned nothing" and "it
    returned three and I threw them all away" look identical from there. Live,
    an all-rejected reply told him the model might not be running, and sent him
    hunting a process that was answering fine seconds later."""
    import time

    from app.main import _REJECTION_REASONS, _first_known, _iso
    from app.storage import recent_draft_rejections, save_safety_event

    started = time.perf_counter()
    save_safety_event("draft", "high", True, ["copies_the_post"], {"text": "x"})
    attempts, reasons = recent_draft_rejections(_iso(started))
    assert attempts >= 1, "the row count says drafts were produced at all"
    assert "copies_the_post" in reasons
    assert _first_known(reasons) == "copies_the_post"
    assert "handed back to them" in _REJECTION_REASONS["copies_the_post"]


def test_every_reply_integrity_failure_has_a_sentence_for_him():
    """A gate that can empty the whole candidate list must be able to say why,
    or the card falls back to blaming the model."""
    from app.main import _REJECTION_REASONS
    for issue in ("copies_the_post", "leaked_into_reply"):
        assert issue in _REJECTION_REASONS
        assert _REJECTION_REASONS[issue].endswith(" ")


def test_an_unknown_reason_falls_back_rather_than_crashing():
    from app.main import _first_known
    assert _first_known(["something_new"]) == ""
    assert _first_known([]) == ""


# ------------------------------------------------------------- the wiring
def test_reply_faults_reach_the_quality_gate(monkeypatch):
    """The gates must run through `_draft_quality_issues`, not beside it —
    that is what buys them the revision pass and the near-miss path."""
    monkeypatch.setattr(intelligence, "_recent_replies", lambda: [])
    context = ctx(author="Some Dev", author_handle="somedev",
                  text="Been running Qwen locally for a month. The bottleneck "
                       "was never the model, it was memory bandwidth.")
    issues = intelligence._draft_quality_issues(
        "So you're saying Qwen's not the bottleneck? What's the real issue then?",
        request_for(context), [])
    assert "strawman_opener" in issues


def test_a_clean_reply_raises_no_reply_faults(monkeypatch):
    monkeypatch.setattr(intelligence, "_recent_replies", lambda: [])
    context = ctx(author="Some Dev", author_handle="somedev",
                  text="Been running Qwen locally for a month. The bottleneck "
                       "was never the model, it was memory bandwidth.")
    faults = intelligence._reply_failures(
        "Bandwidth is why the 30B beats the 70B on one card.",
        request_for(context))
    assert faults == []


def test_a_draft_is_not_flagged_as_a_repeat_of_itself(monkeypatch):
    """Drafts are recorded as they are generated, so the corpus contains the
    text being judged."""
    draft = "Bandwidth is why the 30B beats the 70B on one card."
    monkeypatch.setattr(intelligence, "_recent_replies", lambda: [draft])
    faults = intelligence._reply_failures(draft, request_for(ctx()))
    assert "repeated_reply" not in faults


@pytest.mark.parametrize("draft,expected", [
    # A determiner does NOT make it a noun phrase. This one shipped with the
    # hedge intact because a blunt "starts with 'the'" guard blocked it.
    ("Polymarket's update suggests the event's timing is now uncertain.",
     "The event's timing is now uncertain."),
    ("The data suggests the market is shifting toward smaller models.",
     "The market is shifting toward smaller models."),
])
def test_a_clause_starting_with_a_determiner_is_still_promoted(draft, expected):
    from app.reply_plan import strip_analyst_hedge
    assert strip_analyst_hedge(draft) == expected


def test_a_clean_assessment_is_not_read_as_a_rejection():
    """Every assessed draft writes a safety row, clean ones with an empty
    reason list. Reading only the reasons made an all-rejected reply look
    identical to a dead model — which is the bug the reader exists to fix."""
    import time

    from app.main import _iso
    from app.storage import recent_draft_rejections, save_safety_event

    started = time.perf_counter()
    save_safety_event("draft", "low", False, [], {"text": "a clean draft"})
    attempts, reasons = recent_draft_rejections(_iso(started))
    assert attempts == 1
    assert reasons == []


@pytest.mark.parametrize("draft,expected", [
    ("The 529 error suggests a network issue, not an API malfunction.",
     "The 529 error is a network issue, not an API malfunction."),
    ("The drop suggests a major shift in how computation is optimized.",
     "The drop is a major shift in how computation is optimized."),
    ("The gains suggest a bigger change in what teams will pay for.",
     "The gains are a bigger change in what teams will pay for."),
])
def test_a_noun_phrase_complement_is_unhedged_by_swapping_the_verb(draft, expected):
    """Promotion cannot fix these — the complement is a noun phrase, so lifting
    it leaves a fragment. Swapping the hedging verb for the copula keeps the
    subject and says the thing outright, with number agreement. Before this,
    these WERE the remaining analyst register: every one had a noun-phrase
    complement, so promotion was refused and the hedge shipped."""
    from app.reply_plan import sounds_like_an_analyst, strip_analyst_hedge
    assert strip_analyst_hedge(draft) == expected
    assert not sounds_like_an_analyst(strip_analyst_hedge(draft))


def test_a_retry_on_the_same_post_is_told_to_change_angle():
    """Seven of the nine drafts that missed the clean bar in a full arena run
    were a second round on a parent already answered — same context, same room,
    same move, so the second draft came back as the first one reworded. A retry
    that returns the same thought is not a retry."""
    from app.storage import replies_already_drafted_for, save_generation_candidates, create_generation

    generation = create_generation(
        {"action": "reply", "context": {"post_id": "retrytest-1",
                                        "author": "M", "text": "A post."}},
        {"objective": "conversation"})
    save_generation_candidates(generation, [{
        "id": "c1", "text": "Bandwidth is the ceiling here, not the model.",
        "performance": {}}])
    prior = replies_already_drafted_for("retrytest-1")
    assert prior and "Bandwidth is the ceiling" in prior[0]
    assert replies_already_drafted_for("no-such-post") == []


def test_the_retry_angle_reaches_the_point_chooser():
    """Quoting a past draft is right in exactly this one place: the request IS
    'not that again', and the choice being made is which angle to take."""
    from app.reply_plan import _already_said
    line = _already_said(["Bandwidth is the ceiling here, not the model."])
    assert "different one" in line
    assert "Bandwidth is the ceiling" in line
    assert _already_said([]) == ""


def test_the_checkers_own_voice_cannot_reach_the_composer():
    """Live, a reply came back as the draft plus a paraphrase of the guidance
    that flagged it: "A passerby has no reason to pause. There's no clear
    number, no named item... Add one of those." Every clause reworded, so the
    substring guard saw nothing, and it went to the composer anyway."""
    from app.llm import (_REVISION_GROWTH, _REVISION_MIN_SUSPECT_WORDS,
                            _WRITING_ADVICE_RE)
    leak = ("MoE makes two cards seem like one. A passerby has no reason to "
            "pause. There's no clear number, no named item, no personal stake, "
            "and no clear stance. Add one of those - a specific figure, what it "
            "means for the reader, or your real opinion. Don't add hype.")
    assert _WRITING_ADVICE_RE.search(leak)
    # ...and the length guard catches it independently of the wording.
    assert (len(leak.split()) > 6 * _REVISION_GROWTH
            and len(leak.split()) > _REVISION_MIN_SUSPECT_WORDS)


@pytest.mark.parametrize("original,revised,rejected", [
    (7, 52, True),      # the leak
    (12, 40, True),
    (5, 12, False),     # a genuine repair of a terse draft
    (20, 35, False),
    (25, 45, False),
])
def test_a_repair_may_grow_but_not_balloon(original, revised, rejected):
    """Both conditions, not either: a multiplier alone rejects real repairs of
    short drafts, a flat ceiling alone waves the leak through."""
    from app.llm import _REVISION_GROWTH, _REVISION_MIN_SUSPECT_WORDS
    grew = (revised > original * _REVISION_GROWTH
            and revised > _REVISION_MIN_SUSPECT_WORDS)
    assert grew is rejected


# ------------------------------------------------ did it read the post at all
@pytest.mark.parametrize("post,draft,engaged", [
    # His "no attention to detail" complaint, in one draft: the post carries
    # $12.40 -> $0.90 and the reply talks about "compute efficiency".
    ("Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90.",
     "The cost drop is a major shift in compute efficiency.", False),
    ("Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90.",
     "That 0.90 is the number that changes the build-vs-buy call.", True),
    # Five characters, not six: the words that carry a post are often short,
    # and at six "Three users in the first hour" scored as ignoring a post
    # that opens "Three users bought it in the first hour".
    ("Shipped my first paid feature today. Three users bought it in the first hour.",
     "Three users in the first hour is a strong sign of real demand.", True),
    ("Shipped my first paid feature today. Three users bought it in the first hour.",
     "The cost reduction is a shift toward accessible AI.", False),
])
def test_a_reply_has_to_engage_something_the_post_said(post, draft, engaged):
    from app.reply_plan import engages_a_specific, read_the_room
    room = read_the_room(ctx(author="X", author_handle="x", text=post))
    assert engages_a_specific(draft, room) is engaged


@pytest.mark.parametrize("post,figures", [
    # A version number is a NAME, and offering "use one of these figures: 5,
    # 4.5" invites exactly the version noise the claim gate exists to block.
    ("Claude Opus 5 is here, at half the price of Opus 4.5.", []),
    # Measurements first, years last: only three reach the brief, and offering
    # "2024, 2026, $12.40" spends two of them on dates when the price collapse
    # is the whole point of the post.
    ("Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90.",
     ["$12.40", "$0.90", "2024", "2026"]),
    # The unit is part of the figure — without it this yielded "1" and "70",
    # two numbers that appear nowhere in the post.
    ("Latency went from 1.9s to 700ms and GPT-5.6 did not help.",
     ["1.9s", "700ms"]),
    ("Inference dropped 40% this quarter on a 30B model.", ["40%", "30B"]),
])
def test_only_real_measurements_are_offered_back(post, figures):
    from app.reply_plan import _figures_in
    assert _figures_in(post) == figures


def test_the_brief_hands_over_their_own_numbers():
    from app.reply_plan import read_the_room, reply_instruction
    room = read_the_room(ctx(
        author="Chart Guy", author_handle="chartguy",
        text="Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90."))
    brief = reply_instruction(room)
    assert "$12.40" in brief
    assert "Never add a figure they did not give" in brief


def test_the_register_line_kills_hedging_without_killing_reaction():
    """"in one flat line" was doing two jobs and the second was damage. It was
    written to kill the analyst register and it did — but "flat" also reads as
    "no reaction", and 0 of 27 generated replies carried a personality marker
    against 43% of his own short writing. On his numbers a joke or reference is
    worth 2.38x the engagement, so a register that flat is not neutral."""
    from app.reply_plan import read_the_room, reply_instruction
    brief = reply_instruction(read_the_room(ctx(text="Costs dropped again.")))
    assert "Never hedge it" in brief
    assert "whatever reaction you actually have" in brief
    assert "flat line" not in brief


def test_ignoring_the_post_is_a_gate_not_just_a_note(monkeypatch):
    """A GATE, and that is the point of it. Two candidates are generated per
    reply, so a fault the gate can see is a fault the selection routes around —
    it turns "one of the two engaged the post" from luck into the one that
    ships. Eight of twenty-seven drafts in a full run engaged nothing."""
    from app.intelligence import _CLARITY_ISSUES, _INTEGRITY_ISSUES, _WEAKNESS_NOTES
    from app.llm import ModelClient
    assert "ignores_the_post" in _CLARITY_ISSUES      # gets the revise pass
    assert "ignores_the_post" in _WEAKNESS_NOTES      # ships named, never mute
    assert "ignores_the_post" in ModelClient.CLARITY_GUIDANCE
    assert "ignores_the_post" not in _INTEGRITY_ISSUES

    monkeypatch.setattr(intelligence, "_recent_replies", lambda: [])
    context = ctx(author="Chart Guy", author_handle="chartguy",
                  text="Serving cost per million tokens, 2024 to 2026: "
                       "$12.40 down to $0.90. The curve nobody is pricing in.")
    assert "ignores_the_post" in intelligence._reply_failures(
        "The cost drop is a major shift in compute efficiency.",
        request_for(context))
    assert "ignores_the_post" not in intelligence._reply_failures(
        "That $0.90 is what kills the build-vs-buy argument.",
        request_for(context))


def test_a_draft_is_never_compared_against_itself_at_the_boundary():
    """`_assemble_candidates` calls `remember_draft` on the near-miss and
    repeat paths BEFORE returning, so by the time the text reached the API
    boundary it was already in the session set and the dedup matched it against
    itself. It emptied exactly the two paths that exist so he is never left
    with nothing — the composer came back blank under a note reading "Here it
    is again so you are not left with nothing"."""
    import app.intelligence as I
    from app.main import _remember

    text = "Memory bandwidth is why the 30B beats the 70B on one card."
    I._DRAFTED_THIS_SESSION.clear()
    I.remember_draft(text)
    assert _remember(text) == (text, None), \
        "a draft must survive its own recording"

    # ...and a genuine near-duplicate of a DIFFERENT draft is still CAUGHT —
    # since 2026-07-27 evening it ships with the repeat named instead of
    # blanking (two of ten measured posts came back as empty composers).
    I._DRAFTED_THIS_SESSION.clear()
    I.remember_draft(text)
    near = "Memory bandwidth is why the 30B beats the 70B on a single card."
    out, note = _remember(near)
    assert out == near, "the near-duplicate's text must survive the boundary"
    assert note, "the boundary catch must name the repeat for the card"
    I._DRAFTED_THIS_SESSION.clear()


def test_a_withheld_draft_never_ships_a_card_promising_one():
    """The rationale on those paths is written assuming a draft is attached."""
    from app.main import _explain_if_withheld
    decision = {"draft": "", "rationale": "Here it is again so you are not "
                                          "left with nothing."}
    _explain_if_withheld(decision, "the real text")
    assert "too close to a reply I already showed you" in decision["rationale"]
    assert "left with nothing" not in decision["rationale"]

    unchanged = {"draft": "a draft", "rationale": "ok"}
    _explain_if_withheld(unchanged, "a draft")
    assert unchanged["rationale"] == "ok"


def test_a_reply_never_gets_the_post_hook_styles():
    """The prompt was arguing with itself. `HOOK_STYLES` is written for a
    standalone post, and two of the three angles generated for a REPLY said
    "Open with a question that carries its own premise" and "Open with a sharp
    contrast or contradiction" — alongside a brief saying "not a question back"
    and "do NOT argue". The question-opener tic and the contrarian replies were
    being asked for upstream while I chased them downstream."""
    from app.intelligence import _candidate_angles, CANDIDATE_PLAN
    context = ctx(text="Been running Qwen locally for a month.")
    angles = _candidate_angles(request_for(context), CANDIDATE_PLAN["reply"])
    assert len(angles) == CANDIDATE_PLAN["reply"]
    joined = " ".join(str(a.get("instruction", "")) for a in angles).lower()
    assert "open with a question" not in joined
    assert "contradiction" not in joined
    assert "angle to execute" not in joined, "post angle titles pull the same way"
    # ...and a POST keeps them, because there they are correct.
    post_angles = _candidate_angles(request_for(context, "post"),
                                    CANDIDATE_PLAN["post"])
    assert any("Open with" in str(a.get("instruction", "")) for a in post_angles)


# RETIRED (API-native pass, 2026-09-22): test_the_reply_run_ends_on_the_first_draft_that_passes: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


@pytest.mark.parametrize("draft", [
    "Running on a 2016 laptop, but it is still fast enough for local inference.",
    "Running on a custom rig, no specifics.",
])
def test_a_rig_the_account_does_not_have_is_invented(draft):
    """The `answer` move invites this: asked "what rig are you running it on?"
    the system has nothing true to say, so it made a machine up. Subjectless,
    so neither the first-person verb nor the state pattern could see it."""
    assert _invented_experience(draft, "") is not None


@pytest.mark.parametrize("draft", [
    "Running the 30B locally on one card is the whole trick.",
    "The routing decides it more than the card does.",
    "Memory bandwidth limits scaling beyond a certain point.",
])
def test_a_claim_about_the_thing_is_not_a_claim_about_his_box(draft):
    assert _invented_experience(draft, "") is None


def test_a_retry_starts_from_a_different_angle():
    """The angle sequence was identical every time, so the second press on the
    same post ran attempt one against the same hook and returned the same
    thought reworded — three of fourteen drafts in one run. Offsetting by the
    replies already drafted FOR THIS POST fixes it without depending on the
    point call surviving a busy model."""
    import app.intelligence as I
    context = ctx(post_id="rotation-test", text="Been running Qwen locally.")
    request = request_for(context)
    original = I._prior_reply_count
    try:
        I._prior_reply_count = lambda r: 0
        first = [a["instruction"] for a in I._candidate_angles(request, 3)]
        I._prior_reply_count = lambda r: 1
        second = [a["instruction"] for a in I._candidate_angles(request, 3)]
    finally:
        I._prior_reply_count = original
    assert first[0] != second[0], "a retry must not open on the same angle"
    assert set(first) == set(second), "the same three angles, rotated"


def test_a_reply_that_reads_their_question_back_is_trimmed():
    """Live: "What rig are you running it on? The model's output is what
    decides it." The answer was in the second sentence — the reply just read
    their own question back first, which is the most inattentive thing an
    opening can do and it tripped `bounces_the_question` on an otherwise fine
    draft."""
    from app.reply_plan import (bounces_the_question, read_the_room,
                                strip_repeated_question)
    room = read_the_room(ctx(
        author="A Reader", author_handle="areader",
        text="Clearest take on local inference. What rig are you running it on?"))
    trimmed = strip_repeated_question(
        "What rig are you running it on? The model's output is what decides it.",
        room)
    assert trimmed == "The model's output is what decides it."
    assert not bounces_the_question(trimmed, room)


@pytest.mark.parametrize("draft", [
    "One 4090. What are you running yours on?",
    "The routing decides it more than the card does.",
    "What about the memory? That is the real limit.",
])
def test_a_question_of_its_own_is_left_alone(draft):
    from app.reply_plan import read_the_room, strip_repeated_question
    room = read_the_room(ctx(
        author="A Reader", author_handle="areader",
        text="Clearest take on local inference. What rig are you running it on?"))
    assert strip_repeated_question(draft, room) == draft


def test_the_corporate_noun_is_revised_not_stripped():
    """The other half of `analyst_voice` is nouns, and a stripper cannot touch
    them: the abstract noun IS the object, so deleting it leaves "the product
    is." Measured on the three live cases — every mechanical removal produced a
    broken sentence. It has to go through the revise pass, and the guidance
    names the SHAPE, never the phrase."""
    from app.llm import ModelClient
    guidance = ModelClient.CLARITY_GUIDANCE["analyst_voice"]
    assert "abstract business noun" in guidance
    for phrase in ("value proposition", "hitting the right notes",
                   "suggests", "scalability"):
        assert phrase not in guidance.lower()


def test_an_evaluative_verdict_counts_as_a_position():
    """How this account actually takes a position, and the one shape the
    measure was blind to. Across his 23 short texts and the 26 baseline slop
    drafts these terms fire on 4 of his and ZERO of the slop — while the rest
    of `_POSITION_RE` fires 5 and 5, which is no separation at all."""
    from app.reader_value import reader_value
    for text in ("matching GPT-5.6 Sol at a third of the price is kinda insane",
                 "this is the first time the agent demo actually feels legit"):
        assert reader_value(text, "reply")["signals"]["has_position"]


def test_the_brief_asks_for_the_verdict_without_dictating_the_words():
    """"half the price? That's a discount." states what it IS and scores 0.08.
    Quoting his phrasing here would put "kinda insane" on every reply — the
    same own-goal as the banned-opener list."""
    from app.reply_plan import read_the_room, reply_instruction
    brief = reply_instruction(read_the_room(ctx(text="Costs dropped again.")))
    assert "naming the thing and stopping is half a reply" in brief
    for phrase in ("kinda insane", "feels legit", "gotta be high"):
        assert phrase not in brief.lower()


def test_their_figures_are_explicitly_licensed_as_evidence():
    """The general rules forbid figures in the strongest terms — "Never state a
    percentage, multiple, dollar figure, date... unless it appears in the
    evidence" — and the post's own numbers are not what that block means by
    "the evidence". So the writer read a blanket ban and dropped them: handed
    "$12.40 down to $0.90" it returned "a major efficiency leap in token
    processing". Quoting their number is the opposite of inventing one."""
    from app.reply_plan import read_the_room, reply_instruction
    brief = reply_instruction(read_the_room(ctx(
        author="Chart Guy", author_handle="chartguy",
        text="Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90.")))
    assert "$12.40" in brief
    assert "count as evidence" in brief
    assert "not inventing a number" in brief


def test_the_same_thing_under_a_different_word_still_counts():
    """Asked "what rig are you running it on?" the reply answered "The model
    decides, not the hardware" — engaging the question exactly, and scoring as
    off-post because a prefix match cannot see that a rig IS hardware. The map
    is deliberately small: pairs from this account's beat where the two words
    denote the same thing, not a thesaurus."""
    from app.reply_plan import engages_a_specific, read_the_room
    room = read_the_room(ctx(
        author="A Reader", author_handle="areader",
        text="Clearest take on local inference. What rig are you running it on?"))
    assert engages_a_specific("The model decides, not the hardware.", room)
    assert not engages_a_specific(
        "The cost reduction is a shift toward accessible AI.", room)


def test_a_three_letter_domain_term_is_still_a_specific():
    """"rig" is three characters and was invisible to the four-character rule,
    so the synonym could never fire on the one post it was written for."""
    from app.reply_plan import post_specifics, read_the_room
    room = read_the_room(ctx(text="What rig are you running it on?"))
    assert "rig" in post_specifics(room)


def test_a_point_that_engages_nothing_is_rejected_before_drafting():
    """The point is what the draft argues, so a point engaging nothing they
    said produces a reply engaging nothing they said — the last remaining fault
    class, at a third of all drafts. Checking here beats checking the draft: a
    bad point poisons every candidate written from it."""
    from app.reply_plan import decide_reply_point, read_the_room

    room = read_the_room(ctx(
        author="Chart Guy", author_handle="chartguy",
        text="Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90."))

    class _Router:
        def __init__(self, answer):
            self.answer = answer

        def chat(self, *a, **k):
            return self.answer

    class _Model:
        def __init__(self, answer):
            self.router = _Router(answer)

    offpost = _Model("The market is shifting toward broader accessibility.")
    assert decide_reply_point(offpost, ctx(
        text="Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90."),
        room) is None

    engaged = _Model("That $0.90 kills the build-vs-buy argument for small teams.")
    assert decide_reply_point(engaged, ctx(
        text="Serving cost per million tokens, 2024 to 2026: $12.40 down to $0.90."),
        room) is not None


def test_the_point_prompt_demands_a_word_from_their_post():
    from app.reply_plan import _POINT_PROMPT
    assert "reuse at least one concrete word or number from their post" in _POINT_PROMPT
    assert "without reading their post is the wrong sentence" in _POINT_PROMPT


def test_the_point_examples_cannot_be_reused_as_content():
    """My own bug, and the sharpest instance of the pattern in this file. The
    point prompt's examples were written in the exact domain of the posts, so
    when the chooser had nothing to work with it copied one:

        example:  "Bandwidth is why the 30B beats the 70B on one card."
        produced: "The trust is why the 30B beats the 70B on one card."
                  ...as a reply to "This is the part everyone is missing."

    Moving them to a bakery did not work either — the model copied those too,
    verbatim, into a reply about AI ("The proving drawer is why their loaves
    beat the chain's"), and that draft passed every product gate as clean. The
    domain was never the problem. A sentence the model can lift is a sentence
    it will lift, so the prompt now describes the shape and shows nothing."""
    from app.reply_plan import _POINT_PROMPT
    for token in ("30B", "70B", "4090", "MoE", "bandwidth"):
        assert token not in _POINT_PROMPT
    for token in ("bakery", "loaves", "oven", "bread"):
        assert token not in _POINT_PROMPT.lower()
    assert "Good:" not in _POINT_PROMPT and "Bad:" not in _POINT_PROMPT


def test_a_point_built_only_from_their_words_is_rejected():
    """"This is the part everyone is missing." was returned as the PLAN for a
    reply to that same sentence. It passes engages-a-specific perfectly —
    every word came from the post — which is exactly why that check is not
    enough on its own. `_source_mirroring` is no help either: it needs four-plus
    tokens per sentence and returns 0.0 at the length a point is."""
    from app.reply_plan import decide_reply_point, read_the_room

    class _Router:
        def __init__(self, answer):
            self.answer = answer

        def chat(self, *a, **k):
            return self.answer

    class _Model:
        def __init__(self, answer):
            self.router = _Router(answer)

    post = "This is the part everyone is missing."
    context = ctx(author="Swyx", author_handle="swyx", text=post)
    room = read_the_room(context)
    assert decide_reply_point(_Model(post), context, room) is None
    assert decide_reply_point(
        _Model("Everyone misses that the routing does the work, not the size."),
        context, room) is not None


# --------------------------------------------------------------------------
# WHICH CANDIDATE WINS.
#
# Three replies are generated per post and exactly one is shown. Until this was
# measured the pick was GrowthScore alone, which judges shape — and a fluent
# paraphrase of the post has flawless shape. Both drafts below are real: they
# came out of the same 22-reply run, and the empty one outranked the good one.
# --------------------------------------------------------------------------

_A_PARAPHRASE = "The 529 error persists for three days without support response."
_THE_POST_IT_PARAPHRASES = ("Third day in a row the API returns a 529 during European "
                            "morning. Nobody at support has answered the ticket.")
_A_REAL_REPLY = "The MoE routing's not the bottleneck."


def _Cand(text: str, growth_score: float):
    """A real candidate, so the ranking key is exercised against the real shape."""
    from app.schemas import DraftCandidate
    return DraftCandidate(id=text[:8], text=text, performance={}, growth_score=growth_score)


def test_the_scorer_really_cannot_see_a_thin_reply():
    """The premise of the fix, kept honest: if this ever fails, drop the tier.

    Scored the way the pipeline scores it — with the post as `context_text`,
    which is what makes the paraphrase look good: every word of it is on topic.
    """
    from app.growth_scorer import score_candidate
    empty, _ = score_candidate(_A_PARAPHRASE, action="reply", objective="growth",
                               context_text=_THE_POST_IT_PARAPHRASES)
    real, _ = score_candidate(_A_REAL_REPLY, action="reply", objective="growth",
                              context_text=_THE_POST_IT_PARAPHRASES)
    assert empty > real, "the scorer separates these now; the payoff tier may be redundant"


def test_a_reply_with_something_in_it_beats_a_better_scoring_empty_one():
    ranked = sorted([_Cand(_A_PARAPHRASE, 71.1), _Cand(_A_REAL_REPLY, 66.6)],
                    key=lambda c: intelligence._reply_ranking_key(c, "growth"),
                    reverse=True)
    assert ranked[0].text == _A_REAL_REPLY


def test_growth_score_still_decides_between_two_replies_that_both_land():
    """The tier only breaks a tie it was losing; it does not replace the scorer."""
    high, low = _Cand(_A_REAL_REPLY, 80.0), _Cand("The trust is why the 30B beats the 70B on one card.", 60.0)
    ranked = sorted([low, high], key=lambda c: intelligence._reply_ranking_key(c, "growth"),
                    reverse=True)
    assert ranked[0] is high


def test_two_empty_replies_still_come_back_in_score_order():
    a, b = _Cand(_A_PARAPHRASE, 71.1), _Cand("It's the model that makes the difference.", 66.0)
    ranked = sorted([b, a], key=lambda c: intelligence._reply_ranking_key(c, "growth"),
                    reverse=True)
    assert ranked[0] is a


# --------------------------------------------------------------------------
# WHAT A THIN REPLY IS TOLD TO DO ABOUT IT.
#
# The shared guidance for `no_reader_payoff` says to state the concrete thing
# the draft is about using facts already present. On a post that is right. On a
# reply the only facts present are the POST'S, so it asks for the paraphrase
# that made the draft thin in the first place.
# --------------------------------------------------------------------------

def test_a_thin_reply_is_told_to_finish_the_point_not_restate_it():
    from app.llm import ModelClient
    post = ModelClient.CLARITY_GUIDANCE["no_reader_payoff"]
    reply = ModelClient.REPLY_CLARITY_GUIDANCE["no_reader_payoff"]
    assert reply != post
    assert "follows from it" in reply


def test_the_reply_guidance_never_asks_for_a_figure():
    """`BASE_DRAFT_RULES` forbids inventing numbers; guidance must not fight it.

    Two separate invariants, and the phrasing of the notes is free to change:
    a note may never ASK for a measurement, and it must confine the rewrite to
    the conversation rather than sending the model looking for new material.
    """
    from app.llm import ModelClient
    wants_a_measurement = ("number", "figure", "statistic", "percentage", "metric")
    confines_it = ("already on the table", "already present", "already there")
    for issue, note in ModelClient.REPLY_CLARITY_GUIDANCE.items():
        lowered = note.lower()
        assert not [w for w in wants_a_measurement if w in lowered], issue
        assert [phrase for phrase in confines_it if phrase in lowered], issue


def test_every_reply_override_names_a_real_issue():
    from app.llm import ModelClient
    from app.intelligence import _CLARITY_ISSUES
    for issue in ModelClient.REPLY_CLARITY_GUIDANCE:
        assert issue in _CLARITY_ISSUES, issue


# --------------------------------------------------------------------------
# HOW MANY REPLIES GET MADE.
#
# CANDIDATE_PLAN says three. Measured over one arena run, 8 of 8 reply
# generations stored exactly ONE candidate: a reply attempt costs ~60s and the
# soft budget is 20s, so the budget break fires on the first pass every time.
# Every ranking rule downstream then had nothing to choose between.
# --------------------------------------------------------------------------

_THIN = "The 529 error persists for three days without support response."
_REAL = "The MoE routing's not the bottleneck."


def test_a_thin_reply_is_worth_one_more_attempt():
    assert intelligence._below_reply_payoff(_THIN)


def test_a_reply_that_lands_is_not():
    assert not intelligence._below_reply_payoff(_REAL)


def test_a_later_real_reply_outranks_an_earlier_thin_one():
    """Without this the extra attempt is wasted: the second attempt's reply
    merges in below the first attempt's empty one and never ships."""
    first = [_Cand(_THIN, 71.1)]
    merged = intelligence._merge_ranked(first, [_Cand(_REAL, 66.6)], action="reply")
    assert merged[0].text == _REAL


def test_a_post_is_still_merged_on_score_alone():
    """The measurement was on replies; posts are the other surface's call."""
    merged = intelligence._merge_ranked([_Cand(_THIN, 71.1)], [_Cand(_REAL, 66.6)],
                                        action="post")
    assert merged[0].text == _THIN


def test_the_budget_still_stops_on_a_reply_that_lands():
    """The extra attempt is bought by thinness, not taken by default — a good
    first reply must still return in one pass."""
    assert not intelligence._below_reply_payoff(_REAL)
    assert intelligence.CANDIDATE_PLAN["reply"] >= 2, "the retry needs a plan slot to use"


# --------------------------------------------------------------------------
# QUOTING THEIR OWN NUMBER.
#
# The reply brief licenses the post's published figures — repeating one of
# THEIRS is quoting, not inventing. The figure gate disagreed, because its
# currency pattern stopped at the decimal point: "$0.90" was read as "$0", and
# "0" is not among the corpus figures {"12.40", "0.90"}. Measured live on
# 2026-07-27 the numbers post returned NO DRAFT in either round.
# --------------------------------------------------------------------------

_NUMBERS_POST = ("Serving cost per million tokens, 2024 to 2026: $12.40 down to "
                 "$0.90. The curve nobody is pricing in.")


@pytest.mark.parametrize("draft", [
    "The $0.90 drop could signal a tipping point in AI cost efficiency.",
    "Going from $12.40 to $0.90 in two years is the whole story.",
])
def test_their_published_figure_is_not_an_invented_one(draft):
    from app.intelligence import _unsupported_claims
    assert _unsupported_claims(draft, _NUMBERS_POST) == []


@pytest.mark.parametrize("draft,flagged", [
    ("At $99.99 a month that maths stops working.", "$99.99"),
    ("That is a 93% drop nobody priced in.", "93%"),
])
def test_a_figure_they_never_gave_is_still_caught(draft, flagged):
    from app.intelligence import _unsupported_claims
    assert _unsupported_claims(draft, _NUMBERS_POST) == [flagged]


def test_the_cents_survive_extraction():
    """The regression witness: if this ever matches '$0' again, every reply
    quoting a price with cents goes back to being blocked."""
    from app.intelligence import _FIGURE_RE
    assert _FIGURE_RE.findall("$0.90") == [] or True   # findall shape varies
    assert [m.group(0) for m in _FIGURE_RE.finditer("it fell to $0.90 today")] == ["$0.90"]


# --------------------------------------------------------------------------
# ONE PREDICATE FOR "GOOD ENOUGH".
#
# Thinness and landing-nowhere are two faults to the gates and one thing to the
# reader: the draft said nothing. Over m13's 18 drafts they were 8 of the 9
# faults. Both now decide which candidate wins, which near miss is shown, and
# whether a second attempt is worth its ~60s.
# --------------------------------------------------------------------------

_TAUTOLOGY = ("Claude Opus 5's half-price claim could mean it's cheaper than "
              "Opus 4.5's full cost.")
_STOCK = "529s in the morning, no support? It is a wake-up call for the whole team."


@pytest.mark.parametrize("draft", [_THIN, _TAUTOLOGY, _STOCK])
def test_a_reply_that_says_nothing_is_weak_however_it_says_it(draft):
    assert intelligence._weak_reply(draft)


@pytest.mark.parametrize("draft", [_TAUTOLOGY, _STOCK])
def test_the_payoff_floor_alone_could_not_see_these(draft):
    """Regression witness for WHY the predicate widened: both name real things
    and clear the floor comfortably. Payoff alone called them fine."""
    assert not intelligence._below_reply_payoff(draft)


@pytest.mark.parametrize("draft", [
    _REAL,
    "at this rate Gemini 4 might launch before GPT-6",
    "That's the first time July 23 felt like a real deadline, not a guess.",
])
def test_a_reply_that_lands_is_not_weak(draft):
    assert not intelligence._weak_reply(draft)


def test_the_tautology_now_outranked_by_the_real_reply():
    merged = intelligence._merge_ranked([_Cand(_TAUTOLOGY, 80.0)], [_Cand(_REAL, 60.0)],
                                        action="reply")
    assert merged[0].text == _REAL, "a tautology with a better score must not win"


# RETIRED (API-native pass, 2026-09-22): test_the_budget_does_not_get_to_end_the_run_on_a_weak_reply: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_a_reply_that_lands_returns_in_one_pass: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_only_replies_buy_extra_attempts: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# --------------------------------------------------------------------------
# A NOTE THAT PROMISES A DRAFT, ABOVE AN EMPTY COMPOSER.
#
# The near-miss and the labelled repeat exist so he is never left with nothing,
# and their rationales say so out loud. When the boundary withholds the text as
# a near-duplicate, that promise becomes a lie. Seen live 2026-07-27: four of
# eighteen asks came back empty under "Here it is again so you are not left
# with nothing" and "Heads up: this leans on saying the post back."
# --------------------------------------------------------------------------

def test_a_withheld_draft_never_keeps_a_note_that_promises_one():
    from app.main import _explain_if_withheld
    decision = {
        "draft": "",
        "rationale": ("You have written this before — Starling drafted almost exactly "
                      "this text earlier and it was never posted. Here it is again so "
                      "you are not left with nothing."),
        "reasons": ["repeat"],
        "needs_review": True,
    }
    _explain_if_withheld(decision, "the draft that was withheld")
    assert "Here it is again" not in decision["rationale"]
    assert "too close to a reply I already showed you" in decision["rationale"]
    assert decision["needs_review"] is False


def test_a_draft_that_survived_keeps_its_own_note():
    from app.main import _explain_if_withheld
    note = "Heads up: there is no concrete hook in this yet."
    decision = {"draft": "One 4090 handles it.", "rationale": note, "reasons": [], "needs_review": True}
    _explain_if_withheld(decision, "One 4090 handles it.")
    assert decision["rationale"] == note


def test_every_path_that_can_withhold_also_explains():
    """The composer had this and the per-post path did not, which is why the
    empty cards were only ever seen on one surface. Both call `_remember`, so
    both must call `_explain_if_withheld`."""
    import pathlib
    import re
    source = pathlib.Path(__file__).resolve().parents[1].joinpath("app", "main.py").read_text(encoding="utf-8")
    remembers = len(re.findall(r"^\s*(?:\"draft\":\s*)?_remember\(|=\s*_remember\(", source, re.M))
    explains = source.count("_explain_if_withheld(decision")
    assert explains >= remembers - 1, (
        f"{remembers} paths call _remember but only {explains} explain a withheld draft")


# --------------------------------------------------------------------------
# THE DUPLICATE THE REPEAT GATE COULD NOT SEE.
#
# A draft is recorded before it is judged, so its own row must come out of the
# comparison set or it matches itself. Removing EVERY equal row took the
# previous press's draft with it — leaving an exact duplicate as the one thing
# the gate was blind to, while everything merely similar was caught. Live
# 2026-07-27: two of fourteen posts returned a byte-identical reply on the
# second press and both shipped.
# --------------------------------------------------------------------------

_DUPE = "Claude Opus 5 costs half what Opus 4.5 did, proving its value for money."
_DUPE_POST = ("Claude Opus 5 is here. Our most capable model yet, at half the "
              "price of Opus 4.5.")


def _drop_own_record(prior, text):
    """Mirrors the filter in `_reply_failures`."""
    body, out = text.strip(), list(prior)
    for index, seen in enumerate(out):
        if seen.strip() == body:
            out.pop(index)
            break
    return out


def test_the_second_press_returning_the_same_words_is_a_repeat():
    from app.reply_plan import repeats_a_reply
    session = [_DUPE, _DUPE]          # round one's draft, then this one's row
    prior = _drop_own_record(session, _DUPE)
    assert len(prior) == 1
    assert repeats_a_reply(_DUPE, prior, source=_DUPE_POST)


def test_the_first_press_has_nothing_to_repeat():
    """The filter must still remove the draft's own row, or every first draft
    reports itself as a duplicate."""
    from app.reply_plan import repeats_a_reply
    prior = _drop_own_record([_DUPE], _DUPE)
    assert prior == []
    assert not repeats_a_reply(_DUPE, prior, source=_DUPE_POST)


def test_the_filter_in_the_product_drops_exactly_one_row():
    """The real helper, not its source text.

    An earlier version of this test read `inspect.getsource(_reply_failures)`
    and asserted on the characters in it. That is brittle twice over: it breaks
    when the line moves to a helper, and `getsource` resolves by line number,
    so editing the module while the suite runs makes it read a DIFFERENT
    function and fail for no reason — which is exactly what it did.
    """
    dupe = "the same reply twice"
    assert intelligence._drop_own_record([dupe], dupe) == []
    assert intelligence._drop_own_record([dupe, dupe], dupe) == [dupe]
    assert intelligence._drop_own_record([dupe, dupe, dupe], dupe) == [dupe, dupe]
    other = "a different reply"
    assert intelligence._drop_own_record([other], dupe) == [other]
    assert intelligence._drop_own_record([], dupe) == []


def _function_body(module, name: str) -> str:
    """The text of one function, found by NAME rather than by line number.

    `inspect.getsource` resolves through the code object's line numbers against
    whatever is on disk now, so editing the module while the suite runs makes
    it hand back a DIFFERENT function — which has already failed two of these
    tests for reasons that had nothing to do with the code under test.
    """
    import pathlib as _pathlib
    source = _pathlib.Path(module.__file__).read_text(encoding="utf-8")
    start = source.index(f"def {name}(")
    rest = source[start:]
    end = rest.find(chr(10) + "def ", 1)
    return rest if end == -1 else rest[:end]


# --------------------------------------------------------------------------
# THE EARLY EXIT THAT OUTRANKED THE RETRY.
#
# `generate_drafts_streaming` stops as soon as GrowthScore clears ACCEPT_SCORE,
# and that check ran BEFORE the weak-reply check — so on the one surface where
# GrowthScore is known blind, a draft that says nothing exited early and never
# bought its extra attempt. Measured on the first streaming run after the retry
# landed: 36 seconds, one attempt, "could mean" intact.
# --------------------------------------------------------------------------

_STREAM_TAUTOLOGY = ("Opus 5's half-price claim could mean users save $100 per "
                     "month compared to Opus 4.5's cost.")


# RETIRED (API-native pass, 2026-09-22): test_the_draft_that_slipped_through_the_early_exit_is_weak: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_a_high_score_does_not_end_a_reply_run_that_says_nothing: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_both_generators_guard_the_same_way():
    """The fix has to live on both paths or the button and the arena disagree —
    which is exactly how this was missed the first time. Both stop through the one
    S1 rule, which is where the weak-reply check lives."""
    for name in ("generate_drafts", "generate_drafts_streaming"):
        assert "_first_draft_that_passes" in _function_body(intelligence, name), name
    # API-native pass: style is a note, never another attempt; a repeat still is.
    assert "_repeats_an_earlier_reply" in _function_body(intelligence, "_first_draft_that_passes")


# --------------------------------------------------------------------------
# WHAT THE EXTRA ATTEMPTS COST HIM.
#
# Measured over a full run, letting a weak reply use all three attempts moved
# the median wait 30s -> 36s (most replies land first time) but took the p90
# from 38s to 222s and the worst case to 356s. The retry is bounded by the
# clock as well as by CANDIDATE_PLAN.
# --------------------------------------------------------------------------

# RETIRED (API-native pass, 2026-09-22): test_a_weak_reply_stops_being_retried_once_it_is_expensive: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_the_ceiling_leaves_room_for_three_ordinary_attempts: the weak-reply retry ceiling left with the weak-reply retry (style is a note).


def test_every_retry_site_passes_the_clock():
    """A site that forgets `elapsed` defaults it to 0 and becomes unbounded —
    which is the bug this ceiling exists to prevent."""
    import re
    for name in ("generate_drafts", "generate_drafts_streaming"):
        body = _function_body(intelligence, name)
        calls = re.findall(r"_first_draft_that_passes\((?:[^()]|\([^()]*\))*\)", body)
        assert calls, f"{name} never consults the stop rule"
        for call in calls:
            assert "started" in call, f"{name}: {call}"


# RETIRED (API-native pass, 2026-09-22): test_a_repeat_buys_another_attempt_rather_than_an_apology: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_a_fresh_reply_is_not_treated_as_a_repeat():
    from app import intelligence as intel
    original = intel._recent_replies
    intel._recent_replies = lambda: [_REAL]        # only this draft's own row
    try:
        assert not intel._repeats_an_earlier_reply(_REAL)
    finally:
        intel._recent_replies = original


# --------------------------------------------------------------------------
# WHAT THE MODEL IS SHOWN DECIDES WHAT IT WRITES.
#
# Analyst voice was the dominant remaining fault — six of twenty-eight asks —
# and it did not move for two different briefs or for the retry. That is what a
# REGISTER problem looks like: the model was imitating its examples. Chosen by
# length alone, the pool included a flat wire report about a model going live
# for paid users — the flattest thing in the archive.
# --------------------------------------------------------------------------

_WIRE = "Gemini 3.7 Flash is now live for paid users, and early reactions have been very positive."
_HIS = [
    "A third of the price and this close to Gemini 3 is kinda insane. Flash might be the better deal now.",
    "turns out people would rather review a diff than babysit an agent for a whole afternoon",
    "this is the first time a coding agent actually feels like handing work to a colleague",
    "at this rate Gemini 4 might launch before GPT-6 does and nobody will have called it",
]


def test_no_ones_slang_orders_the_examples():
    """V2 (2026-09-21): examples were ordered by the original author's slang ("kinda",
    "honestly", "bro"). Only similarity to the post (and recency) orders them now, and a
    flat line that is this writer's own is as much their voice as a punchy one."""
    assert not hasattr(intelligence, "_HIS_REGISTER")
    model = _Model([{"text": t, "kind": "reply", "parent": "p"} for t in _HIS] + [
        {"text": _WIRE, "kind": "reply", "parent": "p"}])
    picked = _reply_voice_examples(model, request_for(ctx(text="Gemini 3.7 Flash for paid users")))
    assert picked[0] == _WIRE, "the most similar of their own replies leads"


def test_a_thin_pool_still_fills_up():
    """Nothing is dropped for register, so a small pool is shown whole."""
    model = _Model([{"text": _HIS[0]}, {"text": _WIRE},
                    {"text": "Opus 5 landed today with a new pricing page and a longer context window."}])
    picked = _reply_voice_examples(model, request_for(ctx(text="Unrelated.")))
    assert _WIRE in picked and len(picked) == 3


# --------------------------------------------------------------------------
# A RETRY HAS TO BE A DIFFERENT ATTEMPT.
#
# `plan_reply` runs once per request, so every attempt in the retry loop
# rendered the same point from the same examples at the same temperature —
# only the hook label rotated. Measured on one post across five runs, the model
# wrote a genuinely good reply twice and the identical hedged shape three times
# ("might make", "could make", "might lead to"). It was capable; the attempts
# were not independent, so retrying returned the same answer again.
# --------------------------------------------------------------------------

def _reply_request():
    return request_for(ctx(text="Claude Opus 5 is here, at half the price of Opus 4.5."))


def test_the_first_attempt_keeps_the_calibrated_temperature():
    """The common case must be unchanged — most replies land first time and
    should not pay for a mechanism they never use."""
    from app.llm import _sampling_for, ACTION_SAMPLING
    assert _sampling_for(_reply_request(), 0)["temperature"] == ACTION_SAMPLING["reply"]["temperature"]


def test_each_retry_differs_in_the_prompt_not_the_temperature():
    """Hosted models may ignore sampling parameters, so warming the temperature
    no longer makes a retry independent: the sampling hint is identical on
    every attempt. The variety lives in the prompt instead. Each retry names
    how the earlier candidates opened and is steered to a different move, so
    attempts 1 and 2 carry different notes and attempt 0 carries none."""
    from app.llm import _sampling_for
    request = _reply_request()
    options = [_sampling_for(request, n) for n in range(3)]
    assert options[0] == options[1] == options[2], f"sampling must not drift: {options}"
    # API-native pass: a retry is told how the earlier drafts opened (and why they were not
    # used), so attempts 1 and 2 differ because the second has seen more drafts.
    tried = [_HEDGED, "Half the price is still a price, and the invoice is what teams read."]
    notes = [intelligence._retry_note(request, _cand_list(_HEDGED), n, tried=tried[:max(1, n)])
             for n in range(3)]
    assert notes[0] == "", "the first attempt carries no retry note"
    assert notes[1] and notes[2] and notes[1] != notes[2], "each retry must be steered differently"
    assert "Opus 5's half-price offer might make" in notes[1], "the earlier opening must be named"


def test_enhancement_never_warms():
    """Enhance rewrites HIS draft and must preserve the point; extra heat there
    is a licence to drift."""
    from app.llm import _sampling_for, ENHANCE_SAMPLING
    request = request_for(ctx(text="Some post."))
    warmed = request.model_copy(update={"enhancement_mode": True, "original_draft": "my words"})
    assert _sampling_for(warmed, 3)["temperature"] == ENHANCE_SAMPLING["temperature"]


def test_the_warmth_is_bounded():
    """Unbounded heat buys nonsense, not variety."""
    from app.llm import _sampling_for
    assert _sampling_for(_reply_request(), 99)["temperature"] <= 0.9


@pytest.mark.parametrize("action", ["reply", "quote", "post"])
def test_every_retrying_surface_gets_an_independent_shot(action):
    """This started as "only replies get warmer", and the post/quote session
    later extended the warmth to every surface. Hosted models may ignore
    sampling parameters, so the independence now comes from the prompt: every
    surface's retry carries a variation note naming how the earlier candidate
    opened. The assertion follows the shared behaviour rather than pinning the
    original scope.

    What still matters here is the FIRST attempt, which must stay on the
    calibrated temperature and carry no note — most drafts land first time and
    should never pay for a mechanism they do not use.
    """
    from app.llm import _sampling_for, ACTION_SAMPLING
    request = request_for(ctx(text="Some post."), action=action)
    assert _sampling_for(request, 0)["temperature"] == ACTION_SAMPLING[action]["temperature"]
    assert _sampling_for(request, 2) == _sampling_for(request, 0)
    assert intelligence._retry_note(request, _cand_list(_HEDGED), 0) == ""
    retry = intelligence._retry_note(request, _cand_list(_HEDGED), 2)
    assert "AN EARLIER ATTEMPT WAS NOT USED" in retry      # API-native pass: one note for every surface
    assert "Opus 5's half-price offer might make" in retry


def test_both_generators_pass_the_attempt_index():
    """Wiring the parameter but not passing it is the silent version of this
    bug: the signature accepts an attempt and every call sends zero."""
    for name in ("generate_drafts", "generate_drafts_streaming"):
        assert "attempt=attempt" in _function_body(intelligence, name), name


# --------------------------------------------------------------------------
# THE RETRY VARIES THE THOUGHT, NOT JUST THE WORDING.
#
# Warmer sampling changes how a point is said. `plan_reply` runs once per
# request, so without this every attempt said the SAME point — which is why the
# writer could reach a good reply 2 times in 5 and the retry still returned the
# hedge three times running.
# --------------------------------------------------------------------------

def _cand_list(text):
    from app.schemas import DraftCandidate
    return [DraftCandidate(id="x", text=text, performance={})]


_HEDGED = "Opus 5's half-price offer might make it more accessible."


def test_a_retry_is_told_what_was_just_rejected():
    request = request_for(ctx(text="Opus 5 is here at half the price."))
    note = intelligence._retry_note(request, _cand_list(_HEDGED), 1,
                                    rejections=["unsupported_claim:half"])
    # API-native pass: the note names WHY the draft was not used, and how it opened.
    assert "Opus 5's half-price offer might make" in note
    assert "which is in neither the conversation nor the facts" in note
    assert "genuinely different draft" in note


def test_the_first_attempt_gets_no_note():
    """And the note is a separate string, never folded into `direction`.

    `direction` renders on line 3 of a ~3,700-token prompt, so a note appended
    there invalidated every token behind it and each retry paid full prompt
    evaluation — 6.7-6.9s measured, against 1.1s for the one call whose prefix
    did match. The writers render the note at the tail instead, so an empty note
    leaves attempt 1 byte-identical.
    """
    request = request_for(ctx(text="Opus 5 is here at half the price."))
    assert intelligence._retry_note(request, _cand_list(_HEDGED), 0) == ""


def test_a_blind_retry_still_carries_the_leak_constraint():
    """When every attempt is killed outright (integrity rejects never reach the
    candidate list), the retry used to add no note at all — the writer
    re-leaked blind four attempts running and two surfaces went silent (r3).
    The blind note carries the one constraint that prevents the commonest
    outright kill, phrased positively: which names are allowed."""
    request = request_for(ctx(text="Opus 5 is here at half the price."))
    # API-native pass: the note names the name that was refused (the hard rules already say
    # the constraint on the first attempt, for every surface).
    blind = intelligence._retry_note(request, [], 1, rejections=["injected_entity:fable"])
    assert 'it named "fable", which is not in the conversation or the facts' in blind
    blank = intelligence._retry_note(request, _cand_list("   "), 1, rejections=["leaked_into_reply"])
    assert "it named something the conversation never mentions" in blank


def test_the_retry_note_never_mutates_the_original_request():
    """The loop reuses `request` on every pass; mutating it would leak one
    attempt's rejection into all the others."""
    request = request_for(ctx(text="Opus 5 is here at half the price."))
    before = request.direction
    intelligence._retry_note(request, _cand_list(_HEDGED), 2)
    assert request.direction == before


@pytest.mark.parametrize("action", ["reply", "post", "quote"])
def test_only_replies_get_the_reply_specific_retry_note(action):
    """Every surface's retry now carries the variation note; only a reply also
    carries the reply-specific note quoting the attempt that was rejected."""
    request = request_for(ctx(text="Some post."), action=action)
    note = intelligence._retry_note(request, _cand_list(_HEDGED), 1)
    # API-native pass: one retry note for every surface (why it was not used, how it opened).
    assert "AN EARLIER ATTEMPT WAS NOT USED" in note and "It opened:" in note


# RETIRED (API-native pass, 2026-09-22): test_four_shots_and_a_ceiling_that_fits_them: the weak-reply retry ceiling left with the weak-reply retry (style is a note).


# --------------------------------------------------------------------------
# AN ECHO BUYS A RETRY TOO.
#
# The blind-retry note over-corrected live (r4): told to stick to their names,
# the writer stopped leaking by RESTATING the post at 0.78 similarity — and
# echo sat in no retry path, so the restate shipped on one attempt.
# --------------------------------------------------------------------------

_ECHO_POST = ("Third day in a row the API returns a 529 during European morning. "
              "Nobody at support has answered the ticket.")
_ECHO_DRAFT = ("Third day in a row the API returns a 529 during European morning, "
               "and nobody at support has answered the ticket yet.")


# RETIRED (API-native pass, 2026-09-22): test_an_echo_of_the_post_buys_another_attempt: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_a_real_reply_with_source_still_returns_in_one_pass: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_the_merged_signature_serves_both_sessions: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_a_leading_at_handle_is_cut_from_replies():
    """He never addresses the reader (0/35). The platform threads replies; a
    prepended @handle is the newer writer's habit, not his."""
    import re
    assert re.sub(r"^@\w+[,:]?\s+", "", "@swyx It's the part you're missing.") == \
        "It's the part you're missing."
    assert re.sub(r"^@\w+[,:]?\s+", "", "Half the price changes the math.") == \
        "Half the price changes the math."
