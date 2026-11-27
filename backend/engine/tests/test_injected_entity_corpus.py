"""His archive vouches for a FIGURE. It never vouches for a NAME.

`_injected_entities` was judged against a corpus that, for a from-scratch post,
included his own past posts. His archive names GPT, Fable, Claude and OpenAI
constantly, so any lab he has ever written about was permitted in a post about
anything at all.

Measured over ten live posts on 2026-07-27, 2 of 10 named a model the evidence
never mentions and passed for that reason alone — and they were two of the
three worst drafts in the run:

    topic "Belay: A local firewall for AI coding agents"
      -> "What's the real difference between GPT-5.6 and Fable 5?"
    topic "A Conversation Between Two Coding Agents"
      -> "If GPT-5.6 is as good as they say, I'm switching to Claude."

The gate's own docstring calls the archive "the archive bias in its last hiding
place". This is that place. Figures keep the archive defence — he knows his own
beat and a number he has used before is fair. A lab is different: naming one the
evidence never put on the table is a claim he would have to defend.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import _injected_entities            # noqa: E402

# The evidence a Belay post was actually given, in substance.
BELAY_EVIDENCE = (
    "Belay: A local firewall for AI coding agents. Belay is self-hostable, "
    "runs no LLM in its decision path, and is AGPL-3.0 licensed.")
ARCHIVE = (
    "Opus 5 might actually be delayed. Polymarket just nuked the odds. "
    "I'd take Fable 5 over every GPT model available right now. "
    "OpenAI's own AI just told me the official OpenAI account was fake.")


def test_a_lab_the_evidence_never_mentions_is_caught():
    draft = "What's the real difference between GPT-5.6 and Fable 5? Belay matters here."
    assert _injected_entities(draft, BELAY_EVIDENCE), (
        "a lab absent from the evidence must not pass")


def test_the_archive_alone_must_not_rescue_it():
    """The exact regression: adding his past posts made the same draft legal."""
    draft = "What's the real difference between GPT-5.6 and Fable 5? Belay matters here."
    strict = _injected_entities(draft, BELAY_EVIDENCE)
    lenient = _injected_entities(draft, BELAY_EVIDENCE + " " + ARCHIVE)
    assert strict, "sanity: the strict corpus objects"
    assert not lenient, "sanity: the archive is what used to rescue it"
    # The pipeline must use the strict one. Guarded behaviourally below.


def test_comparing_labs_stays_legal_when_the_evidence_names_them():
    """This is the account's whole beat and must not be collateral damage."""
    evidence = ("Claude Opus 5 narrowly tops the Artificial Analysis Intelligence "
                "Index, matching Fable 5 at 26% lower cost per task.")
    draft = ("Claude Opus 5 tops the index and matches Fable 5 at 26% lower "
             "cost per task.")
    assert not _injected_entities(draft, evidence)


def test_the_pipeline_passes_the_evidence_only_corpus():
    """Behavioural: the draft dies even though the archive contains the name."""
    from app.intelligence import _assemble_candidates, get_personal_model
    from app.safety import assess_context
    from app.schemas import DraftRequest, PostContext

    request = DraftRequest(
        action="post",
        direction=f"Write about Belay. USE THESE FACTS, and only these: {BELAY_EVIDENCE}",
        context=PostContext(author="a", text="", visibility="public"))
    raw = [{"text": "What's the real difference between GPT-5.6 and Fable 5? "
                    "Belay is the local firewall that matters here."}]
    # `examples` is his archive — the thing that used to vouch for the name.
    result = _assemble_candidates(raw, [], "unavailable", request, [ARCHIVE],
                                  get_personal_model(), {"objective": "reach"},
                                  assess_context(request.context))
    for candidate in result:
        assert "Fable 5" not in candidate.text, (
            "his archive rescued a lab the evidence never mentioned")


def test_it_does_not_fire_across_his_own_posts_paired_with_themselves():
    """A post is always consistent with its own text — a floor sanity check
    that the alias matching is not simply always-on."""
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    hits = [p for p in posts if _injected_entities(p, p)]
    assert not hits, f"self-paired post flagged: {hits[0][:80]!r}"
