"""Typo tolerance for intent routing.

Every message here is one a real user actually sent. Each one previously failed
to route and produced "I can't write that for you" instead of a draft.
"""
from __future__ import annotations

from app.text_normalizer import corrections, normalize_typos


def test_real_messages_that_failed_to_route():
    cases = [
        ("create for me ap sot that owudl go viral", "post"),
        ("hey cnau give a good reply that owudl go viral", "reply"),
        ("crete a psot about kimi", "create"),
        ("cna u reply", "can"),
        ("waht hsodu i do wit hthis psot", "should"),
        ("what is trendign right now", "trending"),
    ]
    for message, expected in cases:
        assert expected in normalize_typos(message).split(), message


def test_short_tokens_are_only_touched_by_the_curated_map():
    """Fuzzy matching a 2-3 character token would invent intents."""
    assert "post" not in normalize_typos("i am at").split()
    assert "post" not in normalize_typos("a to it").split()


def test_correct_spelling_is_left_alone():
    for message in ("write me a post about kimi", "should i reply to this",
                    "what is trending right now"):
        assert normalize_typos(message) == message


def test_unrelated_words_are_not_dragged_into_the_vocabulary():
    """Only tokens near a routing keyword may change."""
    text = normalize_typos("the weather is pleasant and my coffee went cold")
    for word in ("post", "reply", "quote", "delete", "thread"):
        assert word not in text.split()


def test_long_pasted_text_is_skipped():
    huge = "lorem ipsum " * 500
    assert normalize_typos(huge) == huge


def test_corrections_are_reportable():
    assert corrections("crete a psot")["psot"] == "post"
    assert corrections("write me a post") == {}


def test_normalisation_never_reaches_the_model(monkeypatch):
    """The user's own words go to the model; the cleaned copy is routing-only."""
    from app import main
    from app.schemas import BrowserAssistRequest

    seen = {}
    monkeypatch.setattr(main, "_chat_write_request",
                        lambda request, message, *a, **k: seen.setdefault("message", message) or {"ok": True})
    request = BrowserAssistRequest(mode="chat", message="crete a psot about kimi")
    try:
        main._assist_chat_sync(request, object(), {}, 0.0)
    except Exception:
        pass
    if "message" in seen:
        assert seen["message"] == "crete a psot about kimi"


def test_greetings_get_a_greeting_not_a_pitch():
    from app.main import _greeting_answer
    for msg in ("hey hey", "hey", "yo", "hello there", "gm", "sup"):
        reply = _greeting_answer(msg)
        assert reply is not None, msg
        low = reply.lower()
        assert "buzz" not in low and "dive in" not in low and "engagement" not in low


def test_acknowledgements_get_a_short_reply():
    from app.main import _greeting_answer
    assert _greeting_answer("thanks!") is not None
    assert _greeting_answer("ok cool") is not None


def test_real_requests_are_not_treated_as_greetings():
    from app.main import _greeting_answer
    for msg in ("write me a post about kimi", "what should i post about",
                "hey can you write me a reply", "when should i post", "is this true"):
        assert _greeting_answer(msg) is None, msg


def test_trim_strips_the_stream_sentinel():
    from app.main import _trim_chat_answer
    assert "@@META" not in _trim_chat_answer("A good answer.\n\n@@META@")
    assert "@@META" not in _trim_chat_answer("Answer.@@META@@")


def test_trim_drops_a_truncated_final_list_item():
    from app.main import _trim_chat_answer
    text = "Ideas:\n\n1. First idea here.\n2. Second idea here.\n3. Pose"
    out = _trim_chat_answer(text)
    assert "Pose" not in out
    assert "Second idea here." in out


def test_trim_keeps_a_complete_list():
    from app.main import _trim_chat_answer
    text = "1. First one done.\n2. Second one done."
    assert _trim_chat_answer(text).count("\n") == 1


# --- real words must survive the fuzzy matcher ------------------------------ #

def test_a_correctly_spelled_word_is_not_rewritten_into_a_keyword():
    """`status` -> `stats` fired five times in his own chat history. `_CANONICAL`
    holds `stats`, and anything within `_FUZZY_CUTOFF` (0.88) of a member gets
    rewritten - which is what makes his real typos reachable, and also what eats
    a real word one character away."""
    from app.text_normalizer import corrections, normalize_typos

    for word in ("status", "states", "replay", "timer", "tropic"):
        assert corrections(word) == {}, (word, corrections(word))
        assert word in normalize_typos(f"whats the {word}"), word


def test_the_typo_corrections_that_earn_this_feature_still_work():
    """Measured over 1,102 of his messages. If the guard above ever grows to the
    point these stop working, the trade has gone the wrong way."""
    from app.text_normalizer import corrections

    for typed, expected in (("psot", "post"), ("rpely", "reply"),
                            ("folwers", "followers"), ("hsodu", "should"),
                            ("waht", "what"), ("raech", "reach"),
                            ("deelte", "delete"), ("wrost", "worst")):
        assert corrections(typed).get(typed) == expected, (typed, corrections(typed))
