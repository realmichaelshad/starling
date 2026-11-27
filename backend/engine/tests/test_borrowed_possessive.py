"""A reply must not claim the author's things as its own.

Under "my agent opened 14 pull requests overnight and every single one renames a
variable", both live arena drafts came back saying "my agent" — true about Ben,
false about the account posting it, and grammatical, on-topic and fluent enough
that nothing downstream could see it. It was the real defect behind the arena's
`joke-post` case, whose own note calls it a REGISTER test; the register was fine.

`_BORROWED_LIFE_RE` should have caught it and could not: it is a closed noun list
(project, repo, repository, codebase, startup, company, team, cluster, server,
bill, invoice, subscription, side-project, saas, product, app) and `agent` is not
in it — in a product about agents, the one noun certain to recur.

WHY THIS IS A SCRUBBER AND NOT A NOUN ADDED TO THAT GATE. `_invented_experience`
rejects the candidate with `continue`, and a rejected candidate never enters
`best`, so `_retry_note` cannot name the fault. The writer would be retried blind,
produce the same phrase, and after four attempts the reply would be silence —
trading a one-word fix for nothing. Repairing it keeps the joke, the point and the
register, and moves only the ownership back.

"your" is correct here specifically because this is a reply. The scrub chain says
so already: a reply is a person answering a person, "where 'you' is the person
they are talking to and entirely correct". The same rewrite on a POST would be
wrong — he never addresses the reader — which is why this runs only under
`request.action == "reply"`.
"""
from __future__ import annotations

import pytest

from app.intelligence import _reclaim_borrowed_possessive


PARENT = ("my agent opened 14 pull requests overnight and every single one "
          "renames a variable")


# --------------------------------------------------------------------------- #
# the two drafts that actually shipped
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("draft, expected", [
    ("so now my agent renames 14 variables overnight just to pull requests again?",
     "so now your agent renames 14 variables overnight just to pull requests again?"),
    ("So 14 pull requests renamed variables overnight just because my agent wanted them opened again?",
     "So 14 pull requests renamed variables overnight just because your agent wanted them opened again?"),
])
def test_the_live_arena_drafts_are_repaired(draft, expected):
    assert _reclaim_borrowed_possessive(draft, PARENT) == expected


def test_sentence_case_is_preserved():
    assert _reclaim_borrowed_possessive("My agent does that too.", PARENT) == \
        "Your agent does that too."


def test_a_clean_reply_is_untouched():
    clean = "14 pull requests renamed 28 variables because each one renames two!"
    assert _reclaim_borrowed_possessive(clean, PARENT) == clean


# --------------------------------------------------------------------------- #
# it may only move a possession the PARENT claimed
# --------------------------------------------------------------------------- #

def test_it_does_nothing_when_the_parent_claims_nothing():
    """"my agent" is only appropriation if the author said it first. Without that,
    the account may genuinely own the thing and `_BORROWED_LIFE_RE` is the gate
    that decides — not this."""
    assert _reclaim_borrowed_possessive("my agent does that too",
                                        "agents open pull requests all night") == \
        "my agent does that too"


def test_it_does_nothing_without_a_parent():
    assert _reclaim_borrowed_possessive("my agent does that too", "") == \
        "my agent does that too"


def test_only_the_matching_noun_moves():
    """The parent owns a startup, not an agent. Rewriting "my agent" here would be
    inventing a correction nobody asked for."""
    assert _reclaim_borrowed_possessive("my agent does that too", "my startup shipped it") == \
        "my agent does that too"


def test_the_parents_own_noun_moves_even_when_others_are_present():
    parent = "my startup runs my agent all night"
    assert _reclaim_borrowed_possessive("my agent broke my laptop", parent) == \
        "your agent broke my laptop"


def test_empty_text_is_safe():
    assert _reclaim_borrowed_possessive("", PARENT) == ""


# --------------------------------------------------------------------------- #
# his own writing
# --------------------------------------------------------------------------- #

def test_a_prediction_in_his_voice_survives():
    """His seven "my <noun>" posts are predictions, and prediction in the first
    person IS his voice — `_BORROWED_LIFE_RE`'s own docstring says so. They are
    only reachable if a parent claims the same noun, which is why this is safe."""
    his = "My Gemini 4 prediction: October 12."
    assert _reclaim_borrowed_possessive(his, "Gemini 4 ships when it ships") == his


def test_his_own_post_is_never_borrowed_from():
    """A self-reply, or a quote of his own post: every rewrite is false there.

    Found 2026-08-06 while designing the quote ungate. The repair took only
    (text, parent_text) — no author — so it could not tell whose parent it was.
    Under HIS OWN "my agent opened 14 pull requests overnight", the draft
    "my agent has been doing this for a week now" shipped as "YOUR agent has been
    doing this for a week now": the product telling him his own agent is somebody
    else's. Worse, `reply_plan`'s `continue` brief instructs exactly the first
    person this was destroying — "continue it as a thread, in first person ...
    never address the author as 'you'". Two correct components, one wrong output.
    """
    parent = "my agent opened 14 pull requests overnight"
    draft = "my agent has been doing this for a week now."
    assert _reclaim_borrowed_possessive(
        draft, parent, parent_author="example_builder",
        own_handle="example_builder") == draft
    # ...and somebody else's post still gets repaired.
    assert _reclaim_borrowed_possessive(
        draft, parent, parent_author="benposts",
        own_handle="example_builder").startswith("your agent")


def test_a_padded_handle_still_matches_his_own():
    """One space and the self-post guard dies silently.

    `identity.own_handle` normalises `.strip().lstrip("@").strip().lower()`; the
    call site in `_assemble_candidates` must use the SAME order. `.lstrip("@")`
    strips nothing from "\\n@benposts" — the later `.strip()` then exposes an "@"
    that survives to the end, while `own_handle` has already removed it. The two
    strings never match, the guard never fires, and his own first person gets
    rewritten on his own thread: the exact defect the guard exists to prevent,
    reappearing over one character of whitespace and passing every test that
    only uses clean handles.

    Caught 2026-08-07 when session 1 fixed the same ordering in identity.py and
    the divergence became visible from the other side.
    """
    import inspect
    from app import intelligence
    source = inspect.getsource(intelligence._assemble_candidates)
    # This pinned the hand-rolled expression as a literal string. That
    # expression is gone: the call site calls `identity.normalize_handle`, which
    # IS own_handle's order by construction rather than by two copies matching.
    assert "normalize_handle(parent)" in source, (
        "the parent handle must go through identity.normalize_handle; a second "
        "hand-rolled copy is what diverged and killed this guard silently")

    # And the behaviour that order protects.
    parent = "my agent opened 14 pull requests overnight"
    draft = "my agent has been doing this for a week now."
    padded = "\n@Example_Builder "
    normalised = padded.strip().lstrip("@").strip().lower()
    assert normalised == "example_builder"
    assert _reclaim_borrowed_possessive(
        draft, parent, parent_author=normalised,
        own_handle="example_builder") == draft


def test_an_unknown_author_does_not_disable_the_repair():
    """The guard needs BOTH names, not just equality.

    An empty default on each would make `parent_author == own_handle` true
    whenever the caller knows neither, switching the repair off everywhere while
    every test that passes names kept passing. That is the shape of the trailing
    default which once made a model pin never-falsy and silently disabled two
    subsystems, so it is pinned here rather than trusted.
    """
    parent = "my agent opened 14 pull requests overnight"
    draft = "my agent has been doing this for a week now."
    assert _reclaim_borrowed_possessive(draft, parent).startswith("your agent")
    assert _reclaim_borrowed_possessive(
        draft, parent, parent_author="", own_handle="").startswith("your agent")
    # One name is not enough to conclude they match, so the repair still runs.
    assert _reclaim_borrowed_possessive(
        draft, parent, parent_author="benposts", own_handle="").startswith("your agent")


def test_an_enhance_keeps_his_own_words():
    """On an enhance the draft is HIS, and his first person is not ours to move.

    The reply branch of the scrub chain is a sibling of the enhancement guard, not
    nested inside it, so the first version of this fix reached an enhance.
    Measured: replying to "my agent opened 14 pull requests overnight", his own
    draft "my agent does the exact same thing every night" shipped as "your agent
    does the exact same thing every night" — the product editing his sentence
    about his own life.
    """
    from app import storage
    from app.intelligence import PersonalModel, _assemble_candidates
    from app.safety import assess_context
    from app.schemas import DraftRequest, PostContext

    storage.initialize()
    parent = "my agent opened 14 pull requests overnight"
    his = "my agent does the exact same thing every night"
    context = PostContext(author="Ben", author_handle="benposts", text=parent,
                          visibility="public")
    request = DraftRequest(action="reply", context=context, research_mode="off",
                           media_policy="no_media", grok_permission=False)
    request.direction = "Tighten it."
    request.enhancement_mode = True
    request.original_draft = his

    out = _assemble_candidates([{"text": his}], [], "not_needed", request, [],
                               PersonalModel(), {}, assess_context(context))
    if not out:
        pytest.skip("this enhance was rejected for an unrelated reason")
    assert out[0].text.startswith("my agent"), (
        f"his own words were rewritten on an enhance: {out[0].text!r}")


def test_it_never_runs_without_the_author_comparison():
    """Pinned at the source so the call site cannot drift.

    THE OLD INVARIANT WAS "reply only", AND IT DIED FOR A MEASURED REASON — it
    is recorded here rather than deleted, so the next reader does not assume it
    was dropped for being inconvenient. On the quote baseline (3 runs x 36 asks,
    2026-08-06/07) appropriation was the commonest defect class, 13 of 95
    drafts: "my agent" under someone else's joke, "runs on my CPU" under
    someone else's benchmark. A reply says that to one person; a QUOTE
    broadcasts it. The repair was gated to the milder surface.

    So the guard is no longer "which surface" but "do we know whose post this
    is". The call must sit behind BOTH the enhancement check (on an enhance the
    words are his) and an author comparison (on his OWN post every rewrite is
    false by construction), and it must actually pass both names — a call that
    reaches the function without them silently loses the self-post protection.
    """
    import inspect
    from app import intelligence
    source = inspect.getsource(intelligence._assemble_candidates)
    call = source.index("_reclaim_borrowed_possessive(")
    head = source[:call]
    assert 'request.action in ("reply", "quote")' in head, (
        "the call must stay behind an explicit surface list")
    assert "enhancement_mode" in head[head.rindex('request.action in ("reply", "quote")'):], (
        "the call has drifted outside the enhancement guard — on an enhance the words are his")
    # Search from the call to the END of the function, not a fixed window: the
    # first version sliced 400 characters and broke the moment a comment was
    # added between the call and its arguments. A test that measures distance
    # rather than structure fails on formatting and passes on nothing useful.
    invocation = source[call:]
    assert "parent_author=" in invocation and "own_handle=" in invocation, (
        "the call must pass BOTH names; without them the self-post guard cannot engage")
