"""A shipped pack's personal half reaches only the install it belongs to.

Written against `app/pack_ownership.py` before it is wired, so the seam itself
stays small. The defect it answers, measured on a simulated stranger's machine
with an empty database: `PersonalModel()` came back with exactly three voice
examples, all of them one person's verbatim replies at weight 0.9, complete with
@-mentions of real accounts. A new user's first draft was written in someone
else's voice.

The two properties that matter pull in opposite directions, so both are held:
a stranger gets none of the person, and the owner's own machine is untouched.

Every pack text below is synthetic: `@example_builder` owns it, "Alex" is the
name it carries and `@other_builder` is the account it quotes.
"""
from __future__ import annotations

from pathlib import Path

from app.pack_ownership import (PERSONAL_FIELDS, belongs_here, declared_owner,
                                normalize_handle, personal_text_report,
                                release_blocker, withhold)

OWNED = "owner-handle: @Example_Builder\n\nTHE MANUAL\n\n\"a verbatim reply\"\n"
UNOWNED = "THE MANUAL\n\n\"a verbatim reply\"\n"

FIELDS = {
    "exemplars": [{"text": "@other_builder you gotta be kidding", "why": [], "format": "reply"}],
    "banned_phrases": ["game changer"],
    "voice_traits": ["direct", "conversational"],
    "briefs": {"create_reply": "one short paragraph"},
}


def _shipped_pack() -> str:
    """The manual bundled with the engine, read the way a release reads it."""
    import app
    pack = Path(app.__file__).parent / "data" / "strategy_v2.txt"
    return pack.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# whose is it
# --------------------------------------------------------------------------- #

def test_a_pack_can_say_who_it_belongs_to():
    assert declared_owner(OWNED) == "example_builder"


def test_a_pack_that_says_nothing_belongs_to_nobody():
    """Not "everybody". A pack with no owner line is today's pack, and treating
    silence as universal ownership is exactly how it reached strangers."""
    assert declared_owner(UNOWNED) == ""
    assert not belongs_here(UNOWNED, "example_builder")
    assert not belongs_here(UNOWNED, "anyone")


def test_the_handle_spelling_does_not_decide_it():
    for spelling in ("@Example_Builder", "example_builder", "  EXAMPLE_BUILDER  ",
                     "@EXAMPLE_BUILDER"):
        assert belongs_here(OWNED, spelling), spelling


def test_an_install_with_no_handle_is_not_a_match():
    """A fresh machine has no handle recorded, and a fresh machine is precisely
    the case that was broken. "Unknown matches everything" is the bug."""
    for nothing in (None, "", "   ", "@"):
        assert not belongs_here(OWNED, nothing), repr(nothing)


# --------------------------------------------------------------------------- #
# what each install gets
# --------------------------------------------------------------------------- #

def test_a_stranger_gets_the_craft_and_none_of_the_person():
    got = withhold(FIELDS, OWNED, "someone_else")
    assert got["exemplars"] == [], "another person's replies reached a stranger"
    # The craft is the product and must survive intact.
    assert got["banned_phrases"] == ["game changer"]
    assert got["voice_traits"] == ["direct", "conversational"]
    assert got["briefs"] == {"create_reply": "one short paragraph"}


def test_the_owners_own_machine_is_untouched():
    """The calibration bar every gate here is held to: the owner's drafts must
    not move. A fix that cleans up a stranger's install by quietly changing the
    owner's own is not a fix, it is a second defect."""
    assert withhold(FIELDS, OWNED, "@Example_Builder") == FIELDS


def test_an_unowned_pack_gives_its_personal_half_to_no_one():
    assert withhold(FIELDS, UNOWNED, "example_builder")["exemplars"] == []
    assert withhold(FIELDS, UNOWNED, "stranger")["exemplars"] == []


def test_withholding_does_not_mutate_the_input():
    """The strategy object is cached and shared; emptying it in place would
    withhold from the owner too, on the second call."""
    before = list(FIELDS["exemplars"])
    withhold(FIELDS, OWNED, "stranger")
    assert FIELDS["exemplars"] == before


def test_only_named_fields_are_personal():
    """A heuristic here would fail in the expensive direction. Withholding craft
    makes the product worse for everyone; the list is explicit so a new field is
    personal only when somebody decides it is."""
    assert PERSONAL_FIELDS == frozenset({"exemplars"})
    got = withhold({"tier1_topics": ["Anthropic"], "exemplars": [1]}, OWNED, "stranger")
    # tier1_topics is parsed but read by nothing outside strategy_v2.py — it is
    # not a leak, and treating it as one would be a fix for a defect that is not
    # there.
    assert got["tier1_topics"] == ["Anthropic"]
    assert got["exemplars"] == []


def test_a_field_shape_survives_being_emptied():
    got = withhold({"exemplars": {}}, OWNED, "stranger")
    assert got["exemplars"] == {} and isinstance(got["exemplars"], dict)


# --------------------------------------------------------------------------- #
# the packaging half
# --------------------------------------------------------------------------- #

def test_the_report_names_what_identifies_a_person():
    report = personal_text_report(
        'THE MANUAL for Alex\n\n"@other_builder you gotta be kidding"\n\nAlex decides.\n',
        names=["Alex"])
    assert report["handles"] == ["@other_builder"]
    assert report["named"] == {"Alex": 2}
    assert report["ships_unowned_identity"] is True


def test_an_owned_pack_is_not_flagged():
    report = personal_text_report(OWNED, names=["Alex"])
    assert report["owner"] == "example_builder"
    assert report["ships_unowned_identity"] is False


def test_a_pack_with_no_identity_at_all_is_clean():
    report = personal_text_report("WRITE SHORT SENTENCES.\n\nAvoid cliches.\n", names=["Alex"])
    assert report["ships_unowned_identity"] is False
    assert report["handles"] == [] and report["quoted_blocks"] == 0


def test_the_report_is_a_report_and_not_a_scrubber():
    """It never rewrites the manual. A wide automatic net over human prose has
    been killed in calibration here twice; the release decides, this informs."""
    text = 'THE MANUAL\n\n"@someone said a thing"\n'
    assert personal_text_report(text, names=["Alex"])["handles"] == ["@someone"]
    assert "someone" in text                       # untouched


def test_the_shipped_pack_carries_no_identity():
    """The bundled manual is a neutral craft manual now. It declares no owner,
    @-mentions nobody and carries no verbatim quoted replies, so every install
    receives the same craft and nobody's voice. Author names are not listed here
    by value; the release scanner owns that list."""
    report = personal_text_report(_shipped_pack())
    assert report["owner"] == "" and report["declares_owner"] is False
    assert report["handles"] == [] and report["handle_count"] == 0
    assert report["named"] == {}
    assert report["quoted_blocks"] == 0
    assert report["ships_unowned_identity"] is False


def test_normalize_handle_is_total():
    """Never raises, and a mistyped extra @ still lands on the handle rather
    than on a string no install will ever match."""
    assert normalize_handle(None) == ""
    assert normalize_handle("@@Example_Builder") == "example_builder"


# --------------------------------------------------------------------------- #
# and the gate REFUSES, rather than describing
# --------------------------------------------------------------------------- #

def test_the_gate_returns_a_refusal_not_a_description():
    """A gate that warns is documentation, and documentation is how needles
    ship: the build keeps going and somebody was supposed to read the output.
    The verdict lives in the module so a caller cannot soften it to a print."""
    reason = release_blocker('THE MANUAL for Alex\n\n"@other_builder hi"\n', names=["Alex"])
    assert reason, "a pack carrying one person's identity was cleared to ship"
    assert "Alex" in reason and "@other_builder" in reason
    # It has to say what to do, or the next person turns the gate off — and what
    # to do differs by rule. A PUBLIC release needs the identity gone; an
    # install build only needs the pack to say whose it is.
    assert "Depersonalise" in reason
    assert "owner-handle:" in release_blocker(
        'THE MANUAL for Alex\n\n"@other_builder hi"\n', names=["Alex"], public=False)


def test_the_gate_clears_a_pack_that_declares_its_owner():
    """For an INSTALL build. Declaring an owner answers "whose is this", which
    is the whole question outside a public release."""
    assert release_blocker(OWNED, names=["Alex"], public=False) == ""


def test_the_gate_clears_a_pack_with_no_identity():
    assert release_blocker("WRITE SHORT SENTENCES.\n", names=["Alex"]) == ""


def test_a_public_release_is_still_refused_even_though_it_is_owned():
    """THE DISTINCTION THAT MATTERS. Declaring an owner fixes who may USE the
    personal half; it is not permission to put one person's replies on every
    user's disk. A gate that accepted the owner line as a licence would close
    the runtime leak and leave the distribution one wide open."""
    text = ("owner-handle: @example_builder\n\n"
            "THE MANUAL for Alex Rivera\n\n"
            "\"@other_builder that benchmark chart is doing a lot of work\"\n\n"
            "Alex Rivera keeps replies short.\n")
    public = release_blocker(text, names=["Alex Rivera"], public=True)
    assert public, "an owned pack full of one person's text was cleared to ship"
    assert "Alex Rivera x2" in public and "every user's disk" in public
    assert "not permission to ship" in public
    # On the owner's own install the same pack is fine; nothing is being distributed.
    assert release_blocker(text, names=["Alex Rivera"], public=False) == ""
    # And the manual that actually ships carries no identity, so it clears both.
    assert release_blocker(_shipped_pack(), public=True) == ""
    assert release_blocker(_shipped_pack(), public=False) == ""


def test_a_depersonalised_pack_clears_a_public_release():
    """What "fixed" looks like, so the gate is not merely permanent."""
    craft = "THE MANUAL\n\nWrite short sentences. Avoid cliches.\n"
    assert release_blocker(craft, names=["Alex"], public=True) == ""
