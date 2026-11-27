"""No gate may reject the writing it exists to protect.

On 2026-07-26 generation went from working to producing NOTHING — zero drafts
across a twelve-post batch. No single change caused it. Three gates added in
quick succession were each slightly too strict, and slightly-too-strict stacks:
nine gates at 5% false positives apiece reject roughly a third of good drafts
between them, and the ones added last pushed it past everything.

Two of the three tested perfectly in isolation. The failure only appeared in
combination, on a database with a day of history in it, which is exactly the
situation no unit test was covering.

So the bar is measured, not argued: every gate is run against his own published
posts, and anything that rejects more than a small share of them is wrong —
whatever its reasoning sounded like when it was written. His archive is the
ground truth for what this account considers publishable, because he published
it.

The audit that caught the real culprit:

    leaks_instructions      0%      parrots_headline     0%
    invented_experience     0%      every template gate  0%
    unit compounds          0%      names_nothing        6%
    loses_the_thread       20%   <- wrong, and the one that mattered

Nine of ten were clean. The tenth flagged "Honestly, Kimi K3 might be the
reason Opus 5 gets delayed. Anthropic can't release..." — a perfectly coherent
post whose second sentence introduces a second name, which is how people write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.intelligence as intel                      # noqa: E402
from app.storage import performance_records           # noqa: E402

# A hard reject must almost never fire on his own writing. A revise trigger
# costs a rewrite pass rather than a post, so it gets more room — but not much.
REJECT_LIMIT = 0.10
REVISE_LIMIT = 0.20

UNRELATED = "An unrelated article about something else entirely, with no overlap."


def _archive() -> list[str]:
    """His real published posts, from a fixture — NOT from the live database.

    The first version read performance_records() and skipped when it came back
    short. On the test database that is always, so all thirteen checks skipped
    silently and the guard protected nothing on the exact day it was needed.
    A calibration test that only runs on one machine is not a test.
    """
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if fixture.exists():
        rows = [t for t in json.loads(fixture.read_text(encoding="utf-8")) if t.strip()]
        if len(rows) >= 10:
            return rows
    rows = [str(r.get("text") or "") for r in performance_records()
            if str(r.get("text") or "").strip()]
    if len(rows) < 10:
        pytest.skip("no archive fixture and none in the database")
    return rows


REJECTING_GATES = {
    "names_nothing": lambda t: intel.names_nothing(t),
    "leaks_instructions": lambda t: intel.leaks_instructions(t),
    "parrots_headline": lambda t: intel._parrots_the_headline(t, UNRELATED),
    "invented_experience": lambda t: bool(intel._invented_experience(t, UNRELATED)),
    "unsupported_claims_self": lambda t: bool(intel._unsupported_claims(t, t)),
    "thats_not_template": lambda t: bool(intel._THATS_NOT_RE.search(t)),
    "about_template": lambda t: bool(intel._ABOUT_TEMPLATE_RE.search(t)),
    "not_just_template": lambda t: bool(intel._NOT_JUST_RE.search(t)),
    "mirrored_you": lambda t: bool(intel._MIRRORED_YOU_RE.search(t)),
    "empty_filler": lambda t: bool(intel._FILLER_RE.search(t)),
}

REVISING_GATES = {
    "loses_the_thread": lambda t: intel._loses_the_thread(t, "", ""),
}


@pytest.mark.parametrize("name", sorted(REJECTING_GATES))
def test_a_rejecting_gate_does_not_reject_his_own_posts(name: str) -> None:
    rows = _archive()
    test = REJECTING_GATES[name]
    hits = [t for t in rows if test(t)]
    share = len(hits) / len(rows)
    assert share <= REJECT_LIMIT, (
        f"{name} rejects {len(hits)}/{len(rows)} ({share:.0%}) of his published "
        f"posts, e.g.: {hits[0][:100]!r}")


@pytest.mark.parametrize("name", sorted(REVISING_GATES))
def test_a_revise_trigger_stays_within_its_wider_bound(name: str) -> None:
    rows = _archive()
    test = REVISING_GATES[name]
    hits = [t for t in rows if test(t)]
    share = len(hits) / len(rows)
    assert share <= REVISE_LIMIT, (
        f"{name} fires on {len(hits)}/{len(rows)} ({share:.0%}) of his posts, "
        f"e.g.: {hits[0][:100]!r}")


def test_the_gates_do_not_stack_into_a_wall() -> None:
    """The failure that actually happened: each gate fine, all of them fatal.

    Nine gates at 5% apiece reject about a third of good drafts between them.
    This measures the union rather than each one alone, because the union is
    what a draft has to survive.
    """
    rows = _archive()
    blocked = []
    for text in rows:
        for name, test in REJECTING_GATES.items():
            try:
                if test(text):
                    blocked.append((name, text))
                    break
            except Exception:                        # noqa: BLE001
                pass
    share = len(blocked) / len(rows)
    assert share <= 0.15, (
        f"the gates together reject {len(blocked)}/{len(rows)} ({share:.0%}) of "
        f"his own posts — a draft has to survive ALL of them, so this is the "
        f"number that decides whether anything reaches the composer. "
        f"First: {blocked[0][0]} on {blocked[0][1][:80]!r}")


def test_a_scrubber_never_rewrites_his_own_posts() -> None:
    """Text surgery is held to a stricter bar than rejection: it changes words."""
    rows = _archive()
    for name, fn in (("strip_formulas", intel.strip_formulas),
                     ("tighten_to_the_point", intel.tighten_to_the_point),
                     ("drop_trailing_fragment", intel.drop_trailing_fragment)):
        changed = [t for t in rows if fn(t) != t]
        # drop_trailing_fragment legitimately repairs two archive rows that
        # really are truncated mid-clause by the capture, so allow a couple.
        assert len(changed) <= 2, (
            f"{name} rewrote {len(changed)}/{len(rows)} of his own posts, "
            f"e.g.: {changed[0][:90]!r}")
