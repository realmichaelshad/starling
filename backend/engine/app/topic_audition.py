"""Audition a post topic BEFORE drafting: is it worth this account's name today?

His diagnosis, 2026-07-30, and the measurements agree with it: the trouble
with posts is the topics. The arena — 12 frozen cases, real topics with real
material — scores 12/12, while live batches swing 30-70% on whatever the pool
happened to hold. The writer is fine; what it is handed is not.

The formula he asked for, in his words: if the topic is not relevant to what
he posts, or we don't have much to say about it, or it is not interesting
enough — search for a different topic; and if that fails too, say wait.

Three questions, each with a measurable answer:

  RELEVANT   his beat at all.  `subject.builder_fit` — a model or a tool you
             build with, not enterprise/policy/funding news that mentions AI.
             (By audition time the pool filters have usually guaranteed this;
             the question is asked anyway so the verdict is complete.)

  MATERIAL   do we have enough to say?  Characters of evidence BEYOND the
             topic label itself, after the article fetch has had its chance.
             The floor is calibrated, not chosen: the arena's 12 known-good
             frozen evidence blocks carry 143-203 chars beyond their titles
             (measured 2026-07-30), so the floor sits at 120 — every known-
             good case clears it with margin, and a bare headline (a 404'd
             source, a synthetic trend label) never does. Headline-only
             evidence is the starvation `source_facts` documents: the writer
             fills the space with shapes that sound like opinions.

  INTEREST   would a reader of his recognise the subject?  `names_notable` —
             his durable archive subjects, the labs and tools everyone in the
             lane knows, or a parameter-count release. The same definition the
             `off_lane` flag already uses, deliberately: one bar, not two that
             drift apart.

HOW THE PICKER MUST USE THIS — relatively, never absolutely. The reply lane's
law applies to topics too: a gate that silently kills every candidate produces
"nothing came back", the one outcome he has ruled out. So:

  * a failing candidate loses only to a better candidate that exists NOW, or
    to one more forced look at the news;
  * when nothing better exists, the weakest survivor ships WITH THE WEAKNESS
    NAMED on the card (off_lane / thin_material), and he decides;
  * only a pool with nothing usable at all becomes a wait — and the wait card
    says what was seen and why each was passed over, never a shrug.

A false rejection here deletes a good post, which is worse than shipping a
flagged weak one — the same asymmetry every calibrated gate in this repo
respects.
"""
from __future__ import annotations

from typing import Any

from .lane import classify as classify_lane
from .subject import builder_fit, names_notable

# The arena's known-good minimum is 143 beyond-label chars; the floor stays
# comfortably under it and far above a restated headline. Calibrated against
# tools/post_arena.py CASES — the test imports those cases so the floor can
# never quietly climb past the material it must admit.
MATERIAL_FLOOR = 120


def material_beyond_label(topic: str, evidence: str) -> int:
    """How much the evidence says beyond restating the topic label.

    Candidate evidence is built as "<title>. <summary>" and enrichment as
    "<title> <facts>", so the label is stripped as a prefix when present.
    This is a floor, not a judge: it cannot tell a good fact from a bad one,
    only material from its absence.
    """
    body = " ".join(str(evidence or "").split())
    label = " ".join(str(topic or "").split())
    if not body:
        return 0
    if label and body.lower().startswith(label.lower()):
        body = body[len(label):]
    return len(body.strip(" .—–-:;,"))


def audit_topic(topic: str, evidence: str) -> dict[str, Any]:
    """The three questions, answered. Never raises; never decides alone —
    the caller owns what a weakness costs (see module docstring)."""
    topic = str(topic or "")
    material = material_beyond_label(topic, evidence)
    relevant = builder_fit(topic)
    substantial = material >= MATERIAL_FLOOR
    notable = names_notable(topic)

    # OFF_LANE NOW MEANS OFF-LANE. It used to mean `not names_notable` - "names nothing he
    # would recognise" - and this module's own docstring defended that as one bar rather than
    # two that drift apart. THAT WAS CORRECT WHILE BOTH MEANT THE SAME THING, and it is not
    # correct any more, because a real lane test now exists.
    #
    # OVERRULED BY MEASUREMENT, on his last twelve chosen topics: a runtime vendor's funding round,
    # AMD acquiring Taalas and Google's search-box redesign are the three off-lane picks, and
    # `names_notable` fires TRUE for all three while the lane test fires False for all three.
    # They were never one bar - they were one bar only because the second had never been built.
    #
    # The old meaning survives under its own name (`unrecognised`) rather than being deleted:
    # "I do not recognise this subject" is still a real weakness worth naming on the card, it
    # is simply a different one from "this is not your lane".
    on_lane = classify_lane(topic, evidence=str(evidence or "")).lane is not None

    weaknesses: list[str] = []
    if not relevant:
        weaknesses.append("off_beat")
    if not substantial:
        weaknesses.append("thin_material")
    if not on_lane:
        weaknesses.append("off_lane")
    if not notable:
        weaknesses.append("unrecognised")
    return {
        "relevant": relevant,
        "material": material,
        "substantial": substantial,
        "notable": notable,
        "on_lane": on_lane,
        "weaknesses": weaknesses,
        "ok": not weaknesses,
    }


def has_prospect(candidates: list[dict[str, Any]]) -> bool:
    """Is there a candidate left that could plausibly beat a thin one?

    A prospect either already carries real material (a feed summary) or has a
    fetchable source we have not read yet. Candidates with neither — synthetic
    trend labels, bare titles — can never audit better than what we hold, so
    rejecting in their favour would trade a flagged draft for silence.
    """
    for candidate in candidates:
        topic = str(candidate.get("topic") or "")
        if material_beyond_label(topic, str(candidate.get("evidence") or "")) \
                >= MATERIAL_FLOOR:
            return True
        sources = candidate.get("evidence_sources") or []
        if any(str(u).startswith("http") for u in sources):
            return True
    return False
