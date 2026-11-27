"""A figure quoted verbatim from the source must not be called invented.

`_unsupported_claims` scanned the corpus with `\\d+(?:\\.\\d+)?`, which reads
"$1,400" as the two separate numbers 1 and 400. The draft side strips every
non-digit and produces "1400". They can never match, so a figure copied
straight out of the post being quoted was reported as a fabrication — and
`unsupported_claim` is a hard reject, not a flag.

Measured live 2026-07-27, quoting @levelsio's "I audited my OpenAI and
Anthropic spend for the first time. $1,400 last month and I could not tell you
what half of it did." Both candidates were thrown away for
`unsupported_claim:$1,400` and the request returned no draft at all. The one
number in the post was the one number the gate refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import _unsupported_claims          # noqa: E402

SOURCE = ("I audited my OpenAI and Anthropic spend for the first time. $1,400 "
          "last month and I could not tell you what half of it did.")


def test_a_figure_from_the_source_is_supported():
    for draft in (
        "You spent $1,400 on AI tools and still don't know what half of it did.",
        "$1,400 a month and half of it is unaccounted for.",
        "That $1,400 bill is the whole argument for auditing spend.",
    ):
        assert not _unsupported_claims(draft, SOURCE), f"wrongly flagged: {draft!r}"


def test_it_works_whichever_side_carries_the_comma():
    assert not _unsupported_claims("It cost $1400 last month for the tools.",
                                   "The bill was $1,400 in total.")
    assert not _unsupported_claims("It cost $1,400 last month for the tools.",
                                   "The bill was $1400 in total.")


def test_a_figure_that_is_genuinely_absent_is_still_caught():
    """The gate must not have been loosened into uselessness."""
    assert _unsupported_claims("They burned $9,900 last month on inference.", SOURCE)
    assert _unsupported_claims("It runs 40% faster than the old one.", SOURCE)


def test_large_numbers_survive_both_forms():
    assert not _unsupported_claims("A coding agent refactored 750,000 LOC in 3 days.",
                                   "The agent refactored 750000 LOC in 3 days.")
    assert not _unsupported_claims("A coding agent refactored 750000 LOC in 3 days.",
                                   "The agent refactored 750,000 LOC in 3 days.")


def test_a_fragment_never_vouches_for_a_different_figure():
    """The second live failure this gate produced in one day, opposite
    direction. Keeping "$1,400"'s fragments ("1", "400") in the corpus made
    fragment "1" vouch for the draft's "$1k" — a 29% misquote of the source,
    shipped as supported. Whole numbers only, canonical on both sides."""
    assert _unsupported_claims("You spent $1k on AI but can't name a thing it did.",
                               SOURCE)
    assert _unsupported_claims("The bill was $400 in total.", SOURCE)


def test_suffix_and_longhand_spellings_are_the_same_number():
    assert not _unsupported_claims("It handles 750k LOC without choking.",
                                   "The agent refactored 750,000 LOC in 3 days.")
    assert not _unsupported_claims("A $1.4k monthly bill is the argument.",
                                   SOURCE)


def test_a_phantom_mid_number_match_cannot_flag_his_own_post():
    """"2,053x" used to yield the figure "053x", which then failed to match
    anything — including the post it was read out of. A match that begins
    inside a comma-grouped number is not a figure."""
    thor = ("Thor checking his Claude stats before Avengers: Doomsday just to "
            "find out he's used 2,053x more tokens than Doctor Doom")
    assert not _unsupported_claims(thor, thor)
