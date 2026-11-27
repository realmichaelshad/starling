"""The posts that made no sense, and why asking nicely never fixed them.

The complaint, in short: generated posts were unclear, the goal behind them
made no sense, and they read as technical slop that gave the reader no reason
to read them.

Read off live runs on 2026-07-26, the slop had one shape. A stock construction
that sounds like a point and states nothing:

    "That's not a trend. It's a model that scales faster than any I've seen."
    "If you're building with it, you're already ahead of the curve."
    "It's not about speed."
    "That's not a feature. It's a bug."

Measured against his 34 published posts, every one of these appears ZERO times.
They are the model's skeleton, not his voice.

Three rounds of prompt rules did not stop them, and the reason is structural:
the model put a formula in EVERY candidate, so every candidate was flagged, and
the near-miss path — which exists so a quality failure is not answered with
silence — shipped a flagged one anyway. The checker worked and the reader still
got the formula.

So they are cut mechanically instead. Each one is a deletable sentence: the
line after "That's not a trend." was carrying the whole point on its own.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import strip_formulas          # noqa: E402
from app.storage import performance_records          # noqa: E402
from app.text_normalizer import split_sentences      # noqa: E402


@pytest.mark.parametrize("draft,gone", [
    ("Listen Labs hit eight figures in nine months. That's not a trend. "
     "It's a model that scales without a team.", "not a trend"),
    ("Termic runs Claude Code free. But the CLI stays on Pro limits. "
     "If you're building with it, you're already ahead of the curve.",
     "ahead of the curve"),
    ("Midjourney v8.2 ships today. The FAQ mentions personalization. "
     "It's not about speed.", "not about speed"),
    ("Salesforce's Slackbot hit 96%. That's the highest they've shipped. "
     "Stay tuned.", "stay tuned"),
])
def test_the_formula_is_cut(draft: str, gone: str) -> None:
    assert gone.lower() not in strip_formulas(draft).lower()


def test_the_surviving_point_is_kept():
    """Deleting the setup must not delete the sentence carrying the point."""
    out = strip_formulas("Listen Labs hit eight figures in nine months. "
                         "That's not a trend. It's a model that scales without a team.")
    assert "eight figures" in out
    assert "scales without a team" in out


@pytest.mark.parametrize("draft", [
    "If you're building with it, you're already ahead of the curve.",
    "If you're coding, you're paying for a tool that costs nothing.",
    "If you're building, you're already past the demo phase.",
])
def test_the_mirrored_you_formula_is_cut_in_any_wording(draft: str) -> None:
    """A literal ban could not keep up.

    "if you're building with it, you're ahead of the curve" was banned, so the
    next run produced "if you're coding, you're paying for a tool" and "if
    you're building, you're already past the demo phase". Same empty machine,
    new nouns. The tell is the MIRROR — the clause repeats "you're" and lands
    on a state instead of a fact.
    """
    full = f"Grok 4.5 is priced at $2 per million tokens. That is real money. {draft}"
    assert draft.lower() not in strip_formulas(full).lower()


def test_a_real_conditional_survives():
    """"If you're making tools, this changes the math" does not mirror, and says
    something. The rule must not flinch at every second-person sentence."""
    draft = ("Listen Labs raised $69M this week. One million interviews ran through it. "
             "If you're making tools, this changes the math.")
    assert "changes the math" in strip_formulas(draft)


def test_the_name_placeholder_never_ships():
    """The prompt said "Name only ones in the evidence"; the model, with no real
    name to use, wrote the word. Live: "MiniMax's 2.7 trill Name", "Moonshot AI
    launched Name"."""
    assert "Name" not in strip_formulas(
        "Grok 4.5 claims 80 TPS. But MiniMax's 2.7 trill Name is in the works. "
        "Efficiency is not the only race.")


@pytest.mark.parametrize("draft,keep,gone", [
    ("GPT-5.6 Sol beats Fable 5 on benchmarks. That's not a guess, it's in the "
     "fresh data. Pick the one that works.", "in the fresh data", "not a guess"),
    ("Grok 4.5 is priced at 2 dollars per million tokens. It's not just cheap, "
     "it's the cheapest tier xAI has shipped. That matters.",
     "cheapest tier xAI has shipped", "not just cheap"),
])
def test_the_comma_joined_template_keeps_only_the_payoff(draft, keep, gone):
    """"That's not a guess, it's in the fresh data." is ONE sentence.

    Deleting whole sentences cannot reach it, and the half after the comma is
    the half carrying the point — so the setup is rewritten away rather than
    the line being dropped.
    """
    out = strip_formulas(draft)
    assert keep in out
    assert gone not in out.lower()


@pytest.mark.parametrize("joiner", [", ", "; ", ": ", " — ", " - ", " – "])
@pytest.mark.parametrize("apos", ["'", "’"])
def test_the_template_is_cut_whatever_joins_its_halves(joiner: str, apos: str) -> None:
    """The path that kept the formula reaching the composer with every gate green.

    The model writes "That's not a feature - it's a firewall." as ONE sentence.
    No sentence-level rule can see it. Then _match_account_style splits the dash
    onto its own line and the template reappears as two visible sentences —
    AFTER the scrubber has already run. Live, that shipped.

    Two separate blind spots met here: the joiner (only a comma was handled)
    and the apostrophe (the pattern ran on raw text, and the model emits
    U+2019, not U+0027 — the fourth time curly punctuation has quietly
    disabled a gate in this file).
    """
    draft = (f"Terminai forks Ratatui to keep scroll and copy. "
             f"That{apos}s not a feature{joiner}it{apos}s a firewall. "
             f"You are betting on a terminal that does not own your model.")
    out = strip_formulas(draft)
    assert "not a feature" not in out, out
    assert "firewall" in out, "the half carrying the point was lost"


def test_expanded_forms_are_cut_too():
    """"That is not a bug: it is a feature" is the same machine uncontracted."""
    out = strip_formulas("Opus 5 is sloppy. That is not a bug: it is a feature. "
                         "The trend just shifted.")
    assert "not a bug" not in out
    assert "feature" in out


def test_it_never_cuts_below_a_real_post():
    """Surgery must not turn a weak draft into a fragment."""
    thin = "That's not a trend. It's a model. Stay tuned."
    assert strip_formulas(thin) == thin


def test_his_own_published_posts_are_never_touched():
    """The calibration that matters.

    An earlier version rejoined sentences on spaces and rewrote 15 of his 34
    posts into one block — the sentences were fine, the paragraphs were the
    casualty. Sentences are now removed in place.
    """
    rows = [str(r.get("text") or "") for r in performance_records() if r.get("text")]
    if not rows:
        pytest.skip("no archive on this machine")
    changed = [t for t in rows if strip_formulas(t) != t]
    assert not changed, f"{len(changed)}/{len(rows)} of his own posts were altered"


@pytest.mark.parametrize("text,count", [
    ("If the U.S. lets this happen, it is about power. Stay tuned.", 2),
    ("Grok 4.5 scores 88.8 on the test. That beats Opus.", 2),
    ("Compare vs. the old one. It is faster.", 2),
])
def test_an_abbreviation_does_not_end_a_sentence(text: str, count: int) -> None:
    """Live, the paragraph formatter split after "U.S." and shipped:

        If the U.S.

        Lets this happen, it's not just about tech.
    """
    assert len(split_sentences(text)) == count


# --------------------------------------------------------------------------- #
# HYPE THAT NEEDS A SUBJECT — the day the scrubber ate his own post
# --------------------------------------------------------------------------- #

HIS_GAME_CHANGER_POST = (
    "Cursor letting background agents review each other's diffs is an absolute "
    "game changer.\n\n"
    "I ran 3 agents: 2 on GPT-5.6 and 1 on Gemini 3.7.\n\n"
    "Gemini acted as the reviewer. I gave it the spec, and it sent the other two "
    "agents back with notes on what to fix."
)


def test_his_own_game_changer_post_survives_whole():
    """2026-08-07, and this one was not found by reading source.

    A post of this shape was published, the extension captured it seconds
    later, and the archive calibration test went red minutes after that.
    `_FILLER_RE` carried "game changer", so the scrubber DELETED the first line
    — the sentence carrying the whole point — and returned the two supporting
    paragraphs as if they were the post.

    Every other finding that night was latent, took hours, and changed nothing
    on a real capture. This one cost one post and ten seconds, and it was found
    by the product meeting reality.

    Both survivor floors, because the archive test asserts both.
    """
    assert strip_formulas(HIS_GAME_CHANGER_POST) == HIS_GAME_CHANGER_POST
    assert strip_formulas(HIS_GAME_CHANGER_POST, min_survivors=1) == \
        HIS_GAME_CHANGER_POST


def test_hype_with_nothing_under_it_is_still_deleted():
    """The gate must not be weakened into uselessness — the point of the split.

    "This is a game changer." evaluates nothing and names nothing. His sentence
    names Cursor and a specific capability. Same words, opposite content,
    and the corroborator is what tells them apart.
    """
    text = ("This is a game changer.\n"
            "The vLLM scheduler now splits layers across both GPUs.\n"
            "The 70B runs at 18 tokens a second.")
    out = strip_formulas(text)
    assert "game changer" not in out
    assert "The vLLM scheduler now splits layers across both GPUs." in out


def test_the_phrase_was_split_out_and_not_merely_deleted():
    """THE FIX SHAPE, PINNED. Removing "game changer" from `_FILLER_RE` and
    stopping there would leave the next hype noun to be found by him, on a live
    post, instead of by us — the failure already recorded when "fatal flaw" and
    "let it rip" were blocked as human tragedy and the resolution was to SPLIT
    the gate rather than trim the list.

    So: the phrase must be gone from the unconditional list AND still reachable
    through the corroborated one. A future edit that deletes the class outright
    fails here.
    """
    from app.intelligence import _FILLER_RE, _HYPE_NEEDS_SUBJECT_RE
    assert not _FILLER_RE.search("this is a game changer")
    assert _HYPE_NEEDS_SUBJECT_RE.search("this is a game changer")
    assert _HYPE_NEEDS_SUBJECT_RE.search("a real game-changer")


def test_corroboration_is_named_things_not_a_second_word_list():
    """A list of allowed subjects would have to be maintained against a moving
    industry and would fail on exactly the launch he wants to post about. Using
    the reader the reply and quote briefs already share means a new hype noun
    inherits the test and a new product name inherits the exemption.
    """
    import inspect
    from app.intelligence import _is_uncorroborated_hype
    source = inspect.getsource(_is_uncorroborated_hype)
    assert "named_things" in source, (
        "corroboration must reuse named_things, not a private allow-list")
    assert _is_uncorroborated_hype("This is a game changer.")
    assert not _is_uncorroborated_hype(
        "Cursor letting background agents review each other's diffs is an absolute game changer.")
    assert not _is_uncorroborated_hype("Opus 5 at half the price is a game changer.")


def test_a_missing_corroborator_keeps_the_sentence():
    """The fallback direction matters and is the opposite of the usual one.

    With no corroborator available the sentence is KEPT: the cost of keeping
    the model's filler is a flagged draft, and the cost of dropping his is a
    published post with its first line missing. A bare `except: pass` that fell
    through to "delete" would reintroduce this defect silently on any import
    error.
    """
    import app.intelligence as intelligence
    from app.intelligence import _is_uncorroborated_hype
    import builtins

    real_import = builtins.__import__

    def explode(name, *args, **kwargs):
        if "reply_plan" in name:
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = explode
    try:
        assert _is_uncorroborated_hype("This is a game changer.") is False
    finally:
        builtins.__import__ = real_import
