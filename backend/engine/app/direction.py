"""Building the drafting direction — the one place the schema cap is enforced.

WHY THIS IS ITS OWN MODULE. It used to live inside `main.py`'s composer, and
`tools/post_arena.py` — the instrument that is supposed to tell us whether the
product is any good — built a direction of its own instead: about 340
characters against the composer's ~1200. Two real faults hid in that gap.

  1. A ValidationError. `with_evidence` fills the direction to 1192 by
     construction, so the ~300-character shape block appended after it
     overflowed `DraftRequest.direction` and killed EVERY grounded
     from-scratch post. The arena never saw it, because the arena's short
     direction never came near the cap.

  2. Shape compliance. The arena asked for a silhouette in a direction with
     nothing else in it; the product asked for one at the end of 1200
     characters of evidence and instruction. Those are not the same request,
     and they do not get the same answer.

An instrument that exercises a different code path than the product measures a
program nobody is running. Both callers now go through `build_post_direction`.
"""
from __future__ import annotations

# The schema's own cap on `DraftRequest.direction`. Overflowing it raises a
# validation error that kills drafting outright — far worse than a thin draft.
DIRECTION_CAP = 1200

EVIDENCE_INSTRUCTION = (
    " Mention at least one specific thing from these facts — a model, a capability, "
    "a comparison, or a number that appears in them. Every sentence must say "
    "something a reader could check or disagree with. Never write a sentence "
    "whose only point is that something is big, exciting, or the future."
)
EVIDENCE_LEAD = " USE THESE FACTS, and only these: "

# A REPLY IS NOT OBLIGED TO CITE ANYTHING.
#
# The instruction above tells the writer to mention at least one specific thing
# from the facts. On a from-scratch post that is right: the facts are the
# subject. On a reply the subject is the post in front of it, and the facts are
# whatever a web search turned up about it — so the mandate forces them in
# whether they fit or not. Live, under "Been running Qwen locally for a month,
# the bottleneck was memory bandwidth", the search returned news about Qwen
# Code and the reply came back "So Qwen Code can file issues? I'd bet it's
# still slower than a human on memory bandwidth." Two unrelated halves welded
# together to satisfy a rule.
EVIDENCE_INSTRUCTION_REPLY = (
    " Use them only where they genuinely fit; never force one in, and never "
    "write in their register — they are news copy, your reply is a person "
    "talking."
)


def with_evidence(direction: str, evidence: str | None, *,
                  action: str = "post", reserve: int = 0) -> str:
    """Append web facts to a drafting direction, always inside the schema cap.

    The evidence is budgeted to whatever room is actually left, dropped
    entirely when too little remains to be useful, and the result is clamped
    regardless. Shared by the post path (radar) and the reply/quote path
    (context_grounding) so the cap can only ever be got right once.

    `reserve` — characters the CALLER still has to append after this returns.
    """
    cap = DIRECTION_CAP - max(0, int(reserve))
    if not evidence:
        return direction[:cap]
    instruction = (EVIDENCE_INSTRUCTION_REPLY if action == "reply"
                   else EVIDENCE_INSTRUCTION)
    room = cap - len(direction) - len(EVIDENCE_LEAD) - len(instruction) - 8
    whole = str(evidence).strip()
    facts = whole[:max(0, room)].strip()
    # THE FLOOR IS ABOUT TRUNCATION, NOT ABOUT SHORT EVIDENCE. It read
    # `len(facts) >= 80`, which also threw away evidence that was simply short
    # — and a news candidate's evidence is its headline, which is usually
    # 40-80 characters. So the commonest kind of post reached the writer with a
    # topic and NO facts at all, silently, and the drafts were exactly what
    # that predicts: "RelativeDB - OSS query engine for relational foundation
    # models" produced "No need for feature engineering. Just write better
    # queries." Nine words about nothing, because nothing is what it was given.
    #
    # A headline is thin evidence, but it is evidence, and it is the difference
    # between a post that names the thing and a post that cannot. The floor now
    # applies only when the text had to be CUT to fit, which is the case it was
    # written for: a fragment of a sentence is worse than no fragment.
    if facts and (len(facts) == len(whole) or len(facts) >= 80):
        direction = f"{direction}{EVIDENCE_LEAD}{facts}{instruction}"
    return direction[:cap]


def build_post_direction(direction: str, evidence: str | None, *,
                         action: str = "post",
                         shape_text: str = "") -> tuple[str, str]:
    """The composer's whole sequence: reserve the shape, fit the facts, clamp.

    Returns `(direction, shape_text)` — `shape_text` comes back EMPTY when the
    shape had to yield, so the caller can record what was actually asked for
    rather than what it hoped to ask for.

    THE SHAPE IS RESERVED, NOT APPENDED. `with_evidence` fills the direction to
    its cap by construction, so anything appended afterwards overflows and the
    DraftRequest raises.

    THE SHAPE YIELDS TO THE FACTS. Reserving room can push the evidence under
    its own floor, and a shaped post about nothing is worse than an unshaped
    post about something — that starvation is the exact failure the evidence
    budget exists to prevent. So when the reservation costs the facts, the
    reservation is dropped and the shape goes with it.
    """
    shape_reserve = (len(shape_text) + 2) if shape_text else 0
    shaped = with_evidence(direction, evidence, action=action,
                           reserve=shape_reserve)
    if evidence and shape_reserve and EVIDENCE_LEAD not in shaped:
        # ONLY YIELD IF YIELDING BUYS THE FACTS. The first version dropped the
        # shape whenever the reserved build came back without evidence — even
        # when the direction was so long that the evidence did not fit either
        # way. That paid the shape for nothing: no facts AND no silhouette.
        unreserved = with_evidence(direction, evidence, action=action)
        if EVIDENCE_LEAD in unreserved:
            shaped, shape_text, shape_reserve = unreserved, "", 0
    shaped = shaped[:DIRECTION_CAP - shape_reserve]
    # ONE SILHOUETTE PER POST, SAMPLED FROM HIS OWN SPREAD. The style block
    # states the account's MEDIAN shape, and a median instruction has zero
    # variance by construction: measured 2026-07-29, 71.3% of 663 generated
    # drafts came out at exactly two blocks against 27.5% of his archive, his
    # 3-5 block posts (37.5% of the archive) appeared in 0.3% of drafts, and
    # his lowercase openers (7 of 40 posts) appeared once in 663. This asks for
    # one concrete silhouette drawn from what the account actually does. It
    # rides in `direction`, so the stored request records which shape was asked
    # for and compliance is measurable after the fact.
    if shape_text:
        shaped = f"{shaped}\n\n{shape_text}"
    # BELT AND BRACES. A truncated instruction is survivable; a
    # ValidationError is not.
    return shaped[:DIRECTION_CAP], shape_text


def _value_cap(field: str) -> int:
    """The schema's own cap on one `DraftRequest` field, read rather than repeated."""
    from .schemas import DraftRequest
    for rule in DraftRequest.model_fields[field].metadata:
        cap = getattr(rule, "max_length", None)
        if cap:
            return int(cap)
    raise LookupError(f"DraftRequest.{field} has no max_length")


def value_copies(*, evidence: str = "", point: str = "", plan: str = "",
                 shape: str = "", image: str = "") -> dict:
    """The structured payloads for a `DraftRequest`, each held to its own cap.

    THESE MUST NEVER BE WHY A DRAFT FAILS. They are copies of text the
    direction already carries, passed as constructor arguments so pydantic
    checks them — which also means one character too many raises while the
    request is being built. That is the same fault this module was created for,
    one field along: measured on the reply path, the plan alone is 1,094 to
    1,381 characters and 1,310 to 1,606 once a point is decided, against a
    `writing_brief` cap that was 1,200. The engine answered 500 and the customer
    saw "Starling's AI isn't responding" for every reply to a question and
    every reply with a planned point.

    The brief's cap is now sized from its producer (`schemas.WRITING_BRIEF_CAP`).
    This is the guarantee behind it, for every payload and not just that one:
    each value is clipped to the cap the schema declares, so the constructor
    cannot raise whatever a producer does next. A clipped fact block admits it
    through `facts_truncated`, which should stay False; if it fires, that is a
    signal about the producer.

    Empty stays None: absent means unknown, never an empty string.
    """
    def fit(field: str, value) -> str | None:
        text = str(value or "")
        return text[:_value_cap(field)] or None

    facts = str(evidence or "")
    return {
        "facts": fit("facts", facts),
        "facts_truncated": len(facts) > _value_cap("facts"),
        "decided_point": fit("decided_point", point),
        "writing_brief": fit("writing_brief", plan),
        "shape_note": fit("shape_note", shape),
        "image_note": fit("image_note", image),
    }


def append_before_shape(direction: str, note: str, shape_text: str = "") -> str:
    """Add a late instruction without cutting the shape block off the end.

    THE SHAPE BLOCK LIVES AT THE END, so a plain `direction[:1000] + note` cut
    it off and an attached image silently reverted the post to the median
    silhouette. Lift it out, truncate the rest, put it back — the note and the
    shape instruction both survive, and the result is inside the cap.
    """
    body = direction
    if shape_text and body.endswith(shape_text):
        body = body[:-len(shape_text)].rstrip()
    reserve = (len(shape_text) + 2) if shape_text else 0
    room = DIRECTION_CAP - len(note) - reserve
    built = body[:max(0, room)] + note
    if shape_text:
        built = f"{built}\n\n{shape_text}"
    return built[:DIRECTION_CAP]
