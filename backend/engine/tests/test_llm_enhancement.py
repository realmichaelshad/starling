from app.llm import ModelClient
from app.schemas import DraftRequest, PostContext


def test_unavailable_model_returns_no_generic_candidate_for_native_enhancement():
    request = DraftRequest(
        action="quote",
        context=PostContext(author="A", text="The source post should not replace the user's thought.", visibility="public"),
        enhancement_mode=True,
    )
    assert ModelClient._fallback_candidates(request) == []


def test_offline_drafting_fabricates_nothing():
    """No draft beats a fabricated one when the model is unavailable.

    The offline path used to splice the first five non-stopword tokens of the
    source post into a fixed sentence frame. Replying to "I was so inspired
    reading all the DMs..." shipped "Otherwise was inspired reading all the
    stays a good-sounding idea instead of becoming useful" as a real draft —
    ungrammatical by construction, and invisible to the quality gates because a
    template is not model output. It even satisfied the context-connection
    check, since it had copied the source's own words.
    """
    for action in ("post", "reply", "quote"):
        request = DraftRequest(
            action=action,
            context=PostContext(author="Someone",
                                text="I was so inspired reading all the DMs on how folks use it",
                                visibility="public"),
        )
        assert ModelClient._fallback_candidates(request) == []


def test_strip_thinking_removes_the_stream_sentinel():
    from app.llm import _strip_thinking
    assert _strip_thinking("A real answer.\n\n@@META@") == "A real answer."
    assert _strip_thinking("Answer.@@META@@") == "Answer."
    assert "@@" not in _strip_thinking("text @@META@ more")
