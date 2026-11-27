"""Enhance reads the draft before rewriting it.

Enhance was a copy-editor: `ENHANCE_DRAFT_RULES` is typos, sentence length and
plain words, and the prompt said "Keep their point." while nothing ever worked
out what the point was. These tests pin the read, and — more importantly — pin
that a bad read can never reach the rewrite, because injecting the wrong point
into an enhance prompt is how his meaning would get changed.
"""
from __future__ import annotations

from app import enhance_read


class FakeRouter:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    def chat(self, messages, *, options=None, fmt=None, think=False, lane="quality"):
        self.calls.append({"messages": messages, "options": options, "lane": lane})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeModel:
    def __init__(self, answer):
        self.router = FakeRouter(answer)


DRAFT = ("Groq cut inference prices again this morning. Everyone cheered the "
         "number on the slide. Nobody is pricing the margin risk that follows.")


def test_it_reads_the_point_out_of_his_own_draft():
    llm = FakeModel("Nobody is pricing the margin risk behind Groq's cuts.")
    point = enhance_read.read_the_point(llm, DRAFT)
    assert point == "Nobody is pricing the margin risk behind Groq's cuts."


def test_a_point_that_shares_no_vocabulary_with_the_draft_is_refused():
    """The failure that matters. A read which invents its own subject would be
    handed to the rewrite as the thing to protect, and the rewrite would then
    change what he said — the one harm enhance exists to avoid."""
    llm = FakeModel("Open source licensing is the real battleground here.")
    assert enhance_read.read_the_point(llm, DRAFT) is None


def test_a_model_that_narrates_is_cleaned_up_not_trusted_verbatim():
    llm = FakeModel('Point: "Groq\'s price cut hides a margin problem."\n')
    assert enhance_read.read_the_point(llm, DRAFT) == "Groq's price cut hides a margin problem."


def test_every_failure_path_leaves_enhance_exactly_as_it_was():
    # A model that returns nothing, one that raises, and a draft too short to
    # have a buried point must all mean "no read", never a crash and never a
    # half-formed instruction.
    assert enhance_read.read_the_point(FakeModel(""), DRAFT) is None
    assert enhance_read.read_the_point(FakeModel(RuntimeError("down")), DRAFT) is None
    assert enhance_read.read_the_point(FakeModel("A real point about Groq margins."), "too short") is None
    # Two words back is not a point.
    assert enhance_read.read_the_point(FakeModel("Groq margins."), DRAFT) is None


def test_it_does_not_spend_a_call_when_the_request_is_nearly_out_of_clock():
    """`point.py` records why: a planning call in front of generation once forced
    a cold load and pushed the whole request past the timeout — 128s, and the
    composer got nothing."""
    from app import deadline

    llm = FakeModel("Nobody is pricing the margin risk behind Groq's cuts.")
    with deadline.budget(enhance_read.MIN_BUDGET_FOR_READ_S - 5):
        assert enhance_read.read_the_point(llm, DRAFT) is None
        assert not llm.router.calls, "it asked the model with no budget to spend"
    # With room, it runs.
    with deadline.budget(enhance_read.MIN_BUDGET_FOR_READ_S + 60):
        assert enhance_read.read_the_point(llm, DRAFT)


def test_the_source_post_is_marked_as_context_not_as_the_thing_to_summarise():
    """Given a post and a draft with no instruction about which is which, the
    model reads the post back."""
    llm = FakeModel("Nobody is pricing the margin risk behind Groq's cuts.")
    enhance_read.read_the_point(llm, DRAFT, context_text="Groq just cut prices 40%.")
    sent = llm.router.calls[-1]["messages"][-1]["content"]
    assert "do not describe the post" in sent
    assert "Groq just cut prices 40%." in sent


def test_where_the_point_lands_is_answered_in_code_not_by_a_model():
    point = "Nobody is pricing the margin risk"
    assert enhance_read.point_arrives_at(DRAFT, point) == 3
    assert enhance_read.point_arrives_at("Nobody is pricing the margin risk. Groq cut prices.", point) == 1
    # No shared vocabulary anywhere: unknown, not a guess.
    assert enhance_read.point_arrives_at(DRAFT, "Licensing decides everything") == 0
    assert enhance_read.point_arrives_at(DRAFT, "") == 0


def test_the_buried_note_is_measured_but_not_shipped():
    """It fires on the phrasing of the POINT rather than the shape of the draft —
    see its docstring. Kept and tested so the next attempt can see how far it
    gets; asserted absent from the direction so it cannot drift into shipping."""
    point = "Nobody prices in Groq's margin risk after its inference price cuts."
    assert "sentence" not in enhance_read.direction_for_enhance(DRAFT, point)


def test_a_buried_point_earns_an_instruction_and_a_led_one_does_not():
    point = "Nobody is pricing the margin risk"
    note = enhance_read.buried_note(DRAFT, point)
    assert "sentence 3 of 3" in note
    assert "Open with it" in note
    # Already leading: nothing to say.
    assert enhance_read.buried_note("Nobody is pricing the margin risk. Groq cut prices. It shows.", point) == ""
    # Two sentences is a thought, not a burial — flattening it would cost his
    # shape for nothing.
    assert enhance_read.buried_note("Groq cut prices. Nobody is pricing the margin risk.", point) == ""


def test_the_direction_uses_the_exact_format_the_existing_point_guard_parses():
    """The whole reason this integrates for free: `_planned_point` reads the
    point out of `direction`, and `serves_the_point` then rejects a rewrite that
    dropped it. A rejected enhance candidate means the composer keeps HIS words.
    """
    from app.intelligence import _planned_point
    from app.point import serves_the_point

    point = "Nobody is pricing the margin risk behind Groq's cuts"
    direction = enhance_read.direction_for_enhance(DRAFT, point)

    assert _planned_point(direction) == point, "the existing guard cannot see this point"
    assert "never what it says" in direction
    # And the guard it unlocks actually protects him.
    assert serves_the_point("Nobody prices the margin risk Groq just created.", point)
    assert not serves_the_point("Groq shipped a price cut today.", point)


def test_no_point_means_no_direction_rather_than_an_empty_instruction():
    assert enhance_read.direction_for_enhance(DRAFT, "") == ""


# --- the other half: enhance is told WHO it is answering -------------------- #

def _reply_request(*, enhance: bool):
    from app.schemas import DraftRequest, PostContext
    return DraftRequest(
        action="reply", research_mode="off", enhancement_mode=enhance,
        original_draft="my rough answer" if enhance else None,
        context=PostContext(author="Tibo", author_handle="tibo", visibility="public",
                            post_id="p1", text="Inference margins are fine, actually."))


def test_enhance_is_finally_told_who_it_is_answering():
    """`_author_card` returned "" for enhancement_mode, so pressing Enhance on a
    reply never told the model it was looking at an answer to anybody — the
    source post arrived only as an untrusted-reference JSON blob."""
    from app.llm import _author_card

    card = _author_card(_reply_request(enhance=True))
    assert card, "enhance still gets no author card"
    assert "Tibo" in card
    # Worded for polishing, NOT for writing something new — that is the one
    # outcome enhance must avoid.
    assert "Keep the writer's point exactly" in card
    assert "Engage what THEY said" not in card


def test_creation_keeps_its_own_stronger_wording():
    from app.llm import _author_card

    card = _author_card(_reply_request(enhance=False))
    # API-native pass: creation names who wrote the post; what to do with it is the model's call.
    assert card.startswith("You are responding to a post BY") and "polishing" not in card


def test_a_post_enhance_never_gets_an_author_card():
    """On a post enhance the context is synthetic and `is_own_post` is True, so a
    card here would announce his own draft as somebody else's post."""
    from app.llm import _author_card
    from app.schemas import DraftRequest, PostContext

    request = DraftRequest(
        action="post", research_mode="off", enhancement_mode=True,
        original_draft="my draft",
        context=PostContext(author="You", text="my draft", visibility="public",
                            is_own_post=True))
    assert _author_card(request) == ""


# --- the two gates that called his own draft somebody else's post ----------- #

def _post_enhance(original, direction=""):
    from app.schemas import DraftRequest, PostContext
    return DraftRequest(
        action="post", research_mode="off", enhancement_mode=True,
        original_draft=original, direction=direction or REAL_ENHANCE_DIRECTION,
        # Exactly what main builds beside `if creating:` — the synthetic context's
        # text IS his own draft.
        context=PostContext(author="You", text=original, visibility="public",
                            is_own_post=True))


REAL_ENHANCE_DIRECTION = (
    "Enhance this exact user-written post. Preserve its real point and chosen format, "
    "but ACTUALLY IMPROVE the wording: sharpen the hook, tighten slack phrases, fix typos, "
    "and raise the specificity without adding claims or changing the meaning. "
    "Do not return the draft unchanged. Return one ready-to-post version only.")

REWRITE = ("Groq cut prices again this morning. Everyone cheered the drop on my timeline.\n\n"
           "Nobody is pricing in that margin risk yet.")


def test_a_post_enhance_is_not_accused_of_restating_his_own_draft():
    """`restates_the_source` fired on EVERY post enhance and the card said "this
    leans on saying the post back. Swap the opening for your own take before
    posting" — advice with no referent, because there is no other post. It also
    set needs_review, so a clean enhance always arrived looking suspect."""
    from app.intelligence import _draft_quality_issues

    issues = _draft_quality_issues(REWRITE, _post_enhance(DRAFT), [])
    assert "restates_the_source" not in issues, issues


def test_a_post_enhance_is_not_told_it_drifts_from_a_post_that_does_not_exist():
    """`_loses_the_thread` anchors on `direction`, which for an enhance is
    "sharpen the hook, tighten slack phrases..." — boilerplate anchoring nothing.
    A coherent three-sentence rewrite scored 1/3 connected against a 0.34 bar
    because "prices" is not "pricing"."""
    from app.intelligence import _draft_quality_issues

    issues = _draft_quality_issues(REWRITE, _post_enhance(DRAFT), [])
    assert "loses_the_thread" not in issues, issues


def test_a_reply_enhance_is_still_judged_against_the_real_source_post():
    """The fix is scoped to the case where the "source" IS his draft. On a reply
    the context really is somebody else's post, and leaning on it is the fault it
    always was."""
    from app.intelligence import _draft_quality_issues
    from app.schemas import DraftRequest, PostContext

    source = ("Inference margins are fine actually. The cost curve keeps falling and "
              "nobody serious is worried about it yet.")
    request = DraftRequest(
        action="reply", research_mode="off", enhancement_mode=True,
        original_draft="i dont think thats right, the margins are the whole problem",
        context=PostContext(author="Tibo", author_handle="tibo", visibility="public",
                            post_id="p1", text=source))

    parroting = ("Inference margins are fine actually, the cost curve keeps falling and "
                 "nobody serious is worried about it yet.")
    assert "restates_the_source" in _draft_quality_issues(parroting, request, [])
    # And his own answer stays clean.
    his_own = "The margins are the whole problem, not a footnote to the cost curve."
    assert "restates_the_source" not in _draft_quality_issues(his_own, request, [])


def test_the_position_finds_the_claim_not_the_subject():
    """The first version returned the first sentence sharing two words, which is
    where the SUBJECT is — and a buried point is precisely the case where the
    subject and the claim are in different sentences.

    Measured live: the model's point named Groq, the opening line named Groq, so
    it reported sentence 1, called the draft well-led and added no instruction,
    while the claim sat alone in sentence 3.
    """
    point = "Nobody is pricing in the margin risk from Groq's cuts"
    assert enhance_read.point_arrives_at(DRAFT, point) == 3
    assert "sentence 3 of 3" in enhance_read.buried_note(DRAFT, point)

    # And when the claim really does open the draft, nothing is added.
    led = ("Nobody is pricing in the margin risk from Groq's cuts. "
           "Groq cut inference prices again this morning. Everyone cheered.")
    assert enhance_read.point_arrives_at(led, point) == 1
    assert enhance_read.buried_note(led, point) == ""


# --- what the model ADDS during an enhance is checked too ------------------- #
#
# `enhancement_mode` skipped a block that had grown to cover five gates. The
# documented reason — "enhancement preserves the user's own draft" — is a fair
# defence for the figure and experience checks. It is not a defence for a link or
# a lab name the model put there, and an enhance also has the scrubbers switched
# off, so nothing else would remove them either.

HIS = "Groq cut inference prices again this morning. Nobody is pricing the margin risk."


def _assemble(text, *, enhance, original=None, ctx_text=None, action="post", direction=None):
    from app.intelligence import _assemble_candidates, assess_context, get_personal_model
    from app.schemas import DraftRequest, PostContext

    request = DraftRequest(
        action=action, research_mode="off", enhancement_mode=enhance,
        original_draft=original,
        direction=direction or ("Enhance this exact user-written post." if enhance
                                else "Write a post."),
        context=PostContext(
            author="You",
            text=ctx_text if ctx_text is not None else (original or ""),
            visibility="public", is_own_post=bool(enhance)))
    brief = {"objective": "conversation", "timing": {},
             "recent_angles_to_avoid_repeating": []}
    got = _assemble_candidates([{"text": text, "rationale": "r", "hook": "h"}],
                               [], "not_needed", request, [], get_personal_model(),
                               brief, assess_context(request.context))
    return got[0].text if got else None


def test_an_enhance_cannot_smuggle_in_a_link():
    """Measured before the fix: the URL reached the POSTED TEXT. That is the one
    outcome `unvouched_urls` exists to stop — "a draft that reads perfectly and
    carries an attacker's host... posts under his name" — and on this surface the
    scrubbers that removed it during creation are switched off as well."""
    attacker = ("Groq cut prices again. Nobody prices the margin risk. "
                "More at http://grab-your-keys.example.com/x")
    assert _assemble(attacker, enhance=True, original=HIS) is None


def test_an_enhance_cannot_smuggle_in_a_lab_the_draft_never_named():
    injected = ("Groq cut prices again, and Moonshot AI is about to undercut them. "
                "Nobody prices the margin risk.")
    assert _assemble(injected, enhance=True, original=HIS) is None


def test_his_own_link_still_survives_an_enhance():
    """The check must not fire on a link HE typed. On a POST enhance the synthetic
    context covers it; on a REPLY enhance the context is somebody else's post, so
    `_vouched_sources` had to learn about `original_draft`."""
    his_link = HIS + " Source: https://groq.com/pricing"
    assert _assemble(his_link.replace("again this morning", "again"),
                     enhance=True, original=his_link)

    reply = _assemble("The margins are the problem. https://groq.com/pricing",
                      enhance=True,
                      original="margins are the problem https://groq.com/pricing",
                      ctx_text="Inference margins are fine actually.", action="reply")
    assert reply, "a link in his own reply draft was treated as invented"


def test_a_clean_enhance_is_untouched_by_any_of_this():
    assert _assemble("Groq cut prices again this morning. Nobody is pricing the margin risk yet.",
                     enhance=True, original=HIS)


def test_the_point_guard_was_inert_on_enhance_and_now_rejects_a_lost_point():
    """The reason this matters: a rejected enhance candidate is what makes the
    composer keep HIS words. Before the fix the guard never ran on this surface —
    traced, and neither `_planned_point` nor `serves_the_point` was called."""
    original = ("Groq cut inference prices again this morning. "
                "Everyone on my timeline cheered the number on the slide. "
                "Nobody is pricing in the margin risk that follows.")
    point = "Nobody prices in Groq's margin risk after its inference price cuts"
    direction = enhance_read.direction_for_enhance(original, point)

    keeps = ("Nobody is pricing in the margin risk from Groq's inference price cuts. "
             "Everyone just cheered the number.")
    drops = "Groq shipped something this morning and the timeline noticed."

    assert _assemble(keeps, enhance=True, original=original, direction=direction)
    assert _assemble(drops, enhance=True, original=original, direction=direction) is None
