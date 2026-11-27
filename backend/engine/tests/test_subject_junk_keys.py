"""An ordinary capitalised word is not a subject.

`subjects()` falls back to "any capitalised token is a subject key" so that an
organisation nobody added to the alias list still counts — "Cohere cuts
inference pricing" must have a subject. Its comment claims to skip sentence
openers; it does not, because a real organisation is often the first word,
which is the exact case the fallback was written for.

So the only thing separating a name from an ordinary capitalised word is the
stop list, and it was short enough to let these through (measured 2026-07-27):

    "Did you think the model was smart?"        -> subject "did"
    "Bringing Nunchaku 4-bit Inference..."      -> subject "bringing"
    "Newer Models, Same Advantage"              -> subject "advantage"
    "Model Routing Is Simple. Until It Isn't."  -> subject "isn"
    "Coding Has Agents. Trading Has One"        -> subject "one"

Each junk key costs twice: `names_nothing` reads it as "this post names a
thing" and passes an empty draft, and the variety rule reads two junk keys as
two different subjects, so a repeat looks like rotation.

The bar is both directions at once — no junk, and no lost names.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.subject import subjects                          # noqa: E402

JUNK_SOURCES = (
    "Did you think the model was smart?",
    "Coding Has Agents. Trading Has One",
    "Model Routing Is Simple. Until It Isn't.",
    "Newer Models, Same Advantage",
    "Bringing it all together",
)

# The reason the fallback exists. Losing any of these is the worse failure.
REAL_NAMES = {
    "Cohere cuts inference pricing": "cohere",
    "Bringing Nunchaku 4-bit Diffusion Inference to Diffusers": "nunchaku",
    "RelativeDB - OSS query engine for relational foundation models": "relativedb",
    "Termic - desktop app for running CLI coding agents": "termic",
    "Forkbench - a native Mac control room for coding agents": "forkbench",
    "Warden - run a fleet of coding agents": "warden",
    "Anthropic launches Cowork": "anthropic",
    "Newer Models, Same Advantage from Mistral OCR4": "mistral",
}


def test_an_ordinary_capitalised_word_is_not_a_subject():
    for text in JUNK_SOURCES:
        assert not subjects(text), f"{text!r} invented a subject: {sorted(subjects(text))}"


def test_a_real_organisation_still_registers():
    """The regression that matters more — this fallback exists for these."""
    for text, expected in REAL_NAMES.items():
        got = subjects(text)
        assert expected in got, f"lost {expected!r} from {text!r}: got {sorted(got)}"


def test_a_sentence_opening_organisation_still_counts():
    """Cohere is the first word, and skipping openers wholesale would lose it."""
    assert "cohere" in subjects("Cohere cuts inference pricing today")


def test_names_nothing_is_no_longer_fooled_by_an_opener():
    from app.intelligence import names_nothing
    assert names_nothing("Did you think the model was smart?")
    assert not names_nothing("Cohere cut inference pricing this week")
