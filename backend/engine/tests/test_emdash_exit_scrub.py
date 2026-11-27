"""The em-dash exit scrub: push a dash through every producer.

His word was "kill them everywhere". The 105 remaining source strings stay AS
AUTHORED and the exit cleans them, so the test that matters is behavioural: no
product-prose field leaves an exit carrying a clause dash.

THE ONE ASSERTION THAT PROTECTS HIM: the draft field must be BYTE-IDENTICAL on
the way out. Both assist doors run an `enhance_composer` mode whose payload is
HIS writing, and `scrub_extra_dashes` has never been allowed to touch his text.
The scrub is an allow-list of product-prose fields precisely so a draft-bearing
key nobody thought of is silently SAFE rather than silently rewritten.
"""
from app.intelligence import scrub_product_prose, _PRODUCT_PROSE_FIELDS

HIS_OWN = "i shipped it at 3am — the tests were green and I did not care"


def test_product_prose_loses_every_clause_dash():
    payload = {
        "headline": "This is taking off — answer the best replies now",
        "rationale": "A follow-up can deepen it — do not manufacture activity.",
        "reasons": ["The context belongs to your account.",
                    "So far: 322 views — 14d in."],
        "timing": {"reason": "Respond while it is live — only with substance."},
    }
    out = scrub_product_prose(payload)
    flat = " ".join([out["headline"], out["rationale"], *out["reasons"],
                     out["timing"]["reason"]])
    assert "—" not in flat and "–" not in flat


def test_the_draft_field_is_byte_identical():
    """His text, and generated drafts already scrubbed at generation."""
    for key in ("draft", "text", "enhanced", "candidates"):
        out = scrub_product_prose({key: HIS_OWN})
        assert out[key] == HIS_OWN, f"{key} was rewritten at the exit"


def test_a_draft_bearing_key_nobody_named_is_safe():
    """The allow-list is why blocker 1 cannot come back."""
    out = scrub_product_prose({"some_future_draft_field": HIS_OWN})
    assert out["some_future_draft_field"] == HIS_OWN


def test_number_ranges_survive():
    out = scrub_product_prose({"stats_line": "posted 2m ago · 25–150s to warm"})
    assert "25–150s" in out["stats_line"]


def test_the_card_ships_three_fields_through_one_call():
    """headline/rationale/reasons[] exit together at /context/analyze."""
    for field in ("headline", "rationale", "reasons"):
        assert field in _PRODUCT_PROSE_FIELDS
