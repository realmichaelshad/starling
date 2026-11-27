"""The angle space decides whether Starling has anything to say at all.

Measured across the arena on 2026-07-26, seven reply surfaces declined on
100% of runs with "I could not find a point worth adding to this one" — a
direct question, someone answering him in his own thread, a chart, a linked
article, a nested reply. None of them were empty posts. They simply contained
no digit and none of the fifteen keywords the angle space looked for, because
every check it had was a keyword or digit match.

These pin the four situations that now supply their own reason to reply, and
the boundary that matters just as much: an empty post still gets no angle,
because a fabricated angle is worse than declining.
"""
from __future__ import annotations

from app.analyzer import _angle_space
from app.schemas import PostContext


def angles(text: str, **kw) -> list[str]:
    context = PostContext(author="someone", visibility="public", text=text, **kw)
    found, _ = _angle_space(context, {})
    return [angle["kind"] for angle in found]


def test_a_direct_question_is_answerable():
    assert "direct_answer" in angles(
        "Genuine question: is anyone actually saving money self-hosting models?")
    assert "direct_answer" in angles("which one are you all using in production?")


def test_someone_replying_to_him_is_worth_answering(monkeypatch):
    """His own thread is the cheapest reach he has.

    Ownership comes from the shared conversation read, so the account handle is
    stubbed rather than passed as a context field — there is no such field, and
    inventing one was how this went unnoticed.
    """
    from app import conversation
    monkeypatch.setattr(conversation, "_own_handle", lambda: "example_builder")
    assert "owed_response" in angles(
        "This is the clearest take I've read on local inference. What rig?",
        parent_author="example_builder")


def test_a_stranger_thread_is_not_owed_a_response(monkeypatch):
    from app import conversation
    monkeypatch.setattr(conversation, "_own_handle", lambda: "example_builder")
    assert "owed_response" not in angles(
        "This is the clearest take I've read on local inference. What rig?",
        parent_author="someone_else")


def test_an_image_post_offers_the_image():
    assert "read_the_image" in angles(
        "The cost curve nobody is pricing in.",
        media=[{"kind": "image", "url": "https://x.test/c.png",
                "alt": "Line chart showing inference cost falling 80%"}])


def test_first_hand_experience_can_be_measured_against():
    assert "compare_experience" in angles(
        "Been running Qwen locally for a month and the bottleneck was bandwidth.")
    assert "compare_experience" in angles("we switched to local inference in March.")


def test_an_opinion_is_not_experience():
    """'I think' is a view; 'I have been running it' is data. Only one is an angle."""
    assert "compare_experience" not in angles("i think local models are the future")


def test_an_empty_post_still_gets_no_angle():
    """A fabricated angle is worse than declining — that rule has to hold."""
    for text in ("this is huge", "big if true", "gm", "so true"):
        assert angles(text) == [], text


# ---------------------------------------------------------------------------
# A reply inside a thread. Measured in the arena: "Same. The tooling is what
# keeps me on the hosted version though." declined on every run, because the
# angle search read only the post in front of it and the substance was one
# level up. The conversation is the context, not the last message in it.
# ---------------------------------------------------------------------------
PARENT = ("Been running Qwen locally for a month. The bottleneck was never the "
          "model, it was memory bandwidth.")


def test_a_nested_reply_is_read_against_its_parent():
    nested = "Same. The tooling is what keeps me on the hosted version though."
    assert angles(nested) == [], "precondition: nothing to say on its own"
    assert angles(nested, parent_text=PARENT, parent_author="somedev"), \
        "the parent carries the substance and must be visible"


def test_a_nested_reply_under_an_empty_parent_still_declines():
    """Reading the thread must not manufacture an angle out of nothing."""
    assert angles("same tbh", parent_text="gm", parent_author="x") == []


def test_the_thread_does_not_count_as_his_own_repeated_wording():
    """recent_repeat compares against THIS post, not the whole conversation —
    otherwise someone else's words in the thread read as his repetition."""
    from app.analyzer import _angle_space
    from app.schemas import PostContext
    context = PostContext(author="third", visibility="public",
                          text="Same here.", parent_text=PARENT)
    _, anti = _angle_space(context, {"recent_user_texts": [PARENT]})
    assert "recent_repeat" not in anti


# ---------------------------------------------------------------------------
# Engagement bait was screened in his own drafts but never in the post he was
# replying to, so a bait post drew a real reply — the year in "AGI is coming in
# 2027" counted as a number worth questioning.
# ---------------------------------------------------------------------------
def test_bait_is_declined_rather_than_answered():
    from app.safety import ENGAGEMENT_BAIT_PATTERN
    bait = ("Like if you agree that AGI is coming in 2027. "
            "Retweet if you disagree.")
    assert ENGAGEMENT_BAIT_PATTERN.search(bait),         "the post the reply targets must register as bait"
    # The year makes it look like a checkable number, which is exactly why it
    # drew a reply before the bait screen ran on the source post.
    assert "verification_question" in angles(bait)


def test_a_pile_on_needs_a_person_not_a_product():
    """Calling a MODEL a scam is his register and must not read as a dogpile."""
    from app.safety import assess_context
    from app.schemas import PostContext

    def blocked(text: str) -> bool:
        return assess_context(PostContext(author="x", visibility="public",
                                          text=text)).blocked

    assert blocked("This guy is a fraud and everyone knows it.")
    assert blocked("@someone is a scammer, spread the word.")
    assert blocked("Absolutely disgraceful behaviour from this founder. "
                   "Everyone should see what he did.")
    assert not blocked("This model is a scam at that price point.")
    assert not blocked("OpenAI's pricing change is disgraceful.")


def test_the_reply_surface_declines_bait_outright():
    """The guarantee that matters: bait never reaches a draft, whatever
    angles its stray year happens to trip."""
    from app.analyzer import _angle_space, _reply_surface
    from app.schemas import PostContext
    context = PostContext(author="farmer", visibility="public",
                          text="Like if you agree that AGI is coming in 2027. "
                               "Retweet if you disagree.")
    found, anti = _angle_space(context, {})
    band, reason = _reply_surface(context, 20.0, {}, found, anti, 0)
    assert band == "weak"
    assert "bait" in reason.lower(), reason


# ---------------------------------------------------------------------------
# A joke. Declining is right — analysing a bit is the fastest way to read like
# a bot — but the catch-all reason said the post was vacuous, which is the
# opposite of true: the reader got exactly what it was for.
# ---------------------------------------------------------------------------
def test_a_joke_is_named_as_a_joke_not_as_an_empty_post():
    from app.analyzer import _angle_space, _reply_surface
    from app.schemas import PostContext
    context = PostContext(author="poster", visibility="public",
                          text="my model router has better uptime than my relationship")
    found, anti = _angle_space(context, {})
    band, reason = _reply_surface(context, 40.0, {}, found, anti, 0)
    assert band == "weak"
    assert "bit" in reason.lower() or "joke" in reason.lower(), reason
    assert "could not find a point" not in reason.lower()


def test_a_serious_post_is_never_called_a_joke():
    """Mislabelling a real post as a bit is worse than the catch-all."""
    from app.analyzer import _looks_like_a_joke
    for text in ("Opus 5 at half the price changes the maths for small teams.",
                 "My relationship with the API rate limits has been rocky.",
                 "at this rate Gemini 4 might launch before GPT-6"):
        assert not _looks_like_a_joke(text), text


def test_a_link_post_says_so_rather_than_blaming_the_post():
    from app.analyzer import _angle_space, _reply_surface
    from app.schemas import PostContext
    context = PostContext(author="reader", visibility="public",
                          text="This changes how I think about it. Worth the read.",
                          links=["https://example.org/piece"])
    found, anti = _angle_space(context, {})
    band, reason = _reply_surface(context, 22.0, {}, found, anti, 0)
    if band == "weak":
        assert "linked article" in reason.lower(), reason


# ---------------------------------------------------------------------------
# The coinage. "Tomorrow is feeling codexy" is the post that produced the worst
# output on record: the grounding query resolved codexy -> Codex and searched
# correctly, while the ANGLE search saw an unknown word, found nothing to say,
# and declined. Two halves of the system disagreed about what the post was
# about, and the half that spoke to the user was the one that had not resolved
# it. The resolver already existed; it just was not wired here.
# ---------------------------------------------------------------------------
def test_a_coined_word_resolves_to_the_thing_it_names(monkeypatch):
    from app import analyzer
    monkeypatch.setattr("app.radar._entity_lexicon",
                        lambda: frozenset({"codex", "claude", "opus"}))
    assert analyzer._coined_subject("Tomorrow is feeling codexy") == "codex"
    assert "named_subject" in angles(
        "Unbelievable excited for what's coming together. Tomorrow is feeling codexy")


def test_a_plain_entity_is_not_treated_as_a_coinage(monkeypatch):
    """Otherwise every post naming a product collects a free angle."""
    from app import analyzer
    monkeypatch.setattr("app.radar._entity_lexicon",
                        lambda: frozenset({"codex", "claude", "opus"}))
    assert analyzer._coined_subject("Claude Opus 5 is here.") is None
    assert analyzer._coined_subject("Exciting times ahead in AI!") is None


def test_an_empty_lexicon_resolves_nothing(monkeypatch):
    """A fresh install has no archive; it must decline, not guess."""
    from app import analyzer
    monkeypatch.setattr("app.radar._entity_lexicon", frozenset)
    assert analyzer._coined_subject("Tomorrow is feeling codexy") is None


# ---------------------------------------------------------------------------
# Widening the angle search had a consequence worth pinning. On a 15h-old
# thread with 1717 replies, the word "codexy" now resolved to Codex, produced
# an angle, and the decider promoted that into "post this yourself" — a
# standalone post about a launch whose "tomorrow" was already yesterday.
# Having something to say is not the same as there being somewhere to say it.
# ---------------------------------------------------------------------------
def test_a_dead_thread_yields_no_angles_however_rich_the_post_is():
    from datetime import datetime, timedelta, timezone

    from app.analyzer import _angle_space
    from app.schemas import PostContext
    dead = PostContext(
        author="Tibo", author_handle="thsottiaux", visibility="public",
        post_id="dead-1",
        post_created_at=(datetime.now(timezone.utc) - timedelta(hours=15)).isoformat(),
        text="Unbelievable excited for what's coming together. Tomorrow is feeling codexy",
        thread_scanned=True, visible_reply_count=1717,
        visible_reply_ages_minutes=[820.0, 900.0, 1100.0])
    found, anti = _angle_space(dead, {})
    assert found == []
    assert "dead_thread" in anti


def test_a_live_thread_with_the_same_text_still_finds_the_subject(monkeypatch):
    """The suppression must be about the THREAD, not about the words in it.

    The lexicon is stubbed because the test database carries no archive, and
    with no archive there is correctly nothing to resolve "codexy" against.
    """
    from datetime import datetime, timedelta, timezone

    from app.analyzer import _angle_space
    from app.schemas import PostContext
    monkeypatch.setattr("app.radar._entity_lexicon",
                        lambda: frozenset({"codex", "claude", "opus"}))
    live = PostContext(
        author="Tibo", author_handle="thsottiaux", visibility="public",
        post_id="live-1",
        post_created_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        text="Unbelievable excited for what's coming together. Tomorrow is feeling codexy",
        thread_scanned=True, visible_reply_count=12,
        visible_reply_ages_minutes=[2.0, 6.0, 9.0])
    found, _ = _angle_space(live, {})
    assert [angle["kind"] for angle in found], "a live thread must still be reachable"
