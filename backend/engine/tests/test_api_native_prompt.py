"""The API-native drafting prompt (2026-09-22): hard rules, the writer, then this press.

What these pin:
  * the prefix (both system messages) is identical across presses and surfaces of one account,
    carries nothing of the press, and is long enough to be cached once there is a voice card;
  * the model is not pre-decided for: no reply plan, no "only names you may use" allowlist, no
    hook instructions, no measured statistics as rules, no sampled shape;
  * the integrity rules are in the system message for every surface;
  * what the old plan knew is given as labelled context (whose words, own thread, image nobody
    described), and a very long post is trimmed in the variable part, never in the prefix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import llm, voice_card
from app.intelligence import get_personal_model, reset_personal_model_cache
from app.schemas import DraftRequest, PostContext
from app.storage import upsert_post

NOW = datetime.now(timezone.utc)
TEXTS = [
    "context windows are a budget, not a feature", "turns out the bottleneck was the retry loop",
    "shipping the boring version first is still undefeated", "p95 is the only latency number users feel",
    "every agent demo is a happy path", "benchmarks tell you what a model can do once",
    "caching the system prompt saved more than switching models", "the eval set leaks into the prompt by week two",
    "a model that says i don't know is worth two that guess", "most ai features should be a button",
    "typing speed was never the bottleneck. deciding was", "rag is search with extra steps",
]


def _seed(count: int = len(TEXTS)) -> None:
    for index, text in enumerate(TEXTS[:count]):
        created = NOW - timedelta(days=index + 1)
        upsert_post({"post_id": f"p{index}", "post_type": "reply" if index % 2 else "post", "text": text,
                     "created_at": created.isoformat(), "visibility": "public",
                     "metrics": {"views": 500 + index, "likes": 3 + index},
                     "metrics_updated_at": (created + timedelta(hours=5)).isoformat()})
    reset_personal_model_cache()
    llm.reset_writer_cache()


class _CardModel:
    """A model whose only job here is to answer the voice card schema."""

    class router:
        text_model = "starling-quality"

        @staticmethod
        def chat(messages, **kwargs):
            import json
            return json.dumps({"beats": ["LLM evals and latency", "developer tooling"],
                               "stances": ["logs beat benchmarks"], "argues_and_jokes": "Dry one-liners.",
                               "uses": ["turns out"], "avoids": ["hype"], "shape": "One short lowercase line.",
                               "audience": "A practical engineer."})


def _reply(text: str = "Been running Qwen locally. The bottleneck was memory bandwidth.", **extra) -> DraftRequest:
    return DraftRequest(action="reply", research_mode="off", grok_permission=False,
                        context=PostContext(author="Some Dev", author_handle="dev", text=text,
                                            visibility="public", **extra))


def _post() -> DraftRequest:
    return DraftRequest(action="post", research_mode="off",
                        context=PostContext(author="You", text="", visibility="public"))


def _seed_many(count: int = 36) -> None:
    for index in range(count):
        created = NOW - timedelta(days=index + 1)
        upsert_post({"post_id": f"m{index}", "post_type": "reply" if index % 2 else "post",
                     "text": f"{TEXTS[index % len(TEXTS)]}, and the second time it cost us a week (note {index})",
                     "created_at": created.isoformat(), "visibility": "public"})
    reset_personal_model_cache()
    llm.reset_writer_cache()


def test_the_prefix_is_the_same_for_every_press_and_surface_of_one_account():
    _seed_many()
    voice_card.refresh_if_due(_CardModel())
    first = llm.draft_messages(_reply(), [], {}, fields=llm.draft_fields(_reply()))
    other = llm.draft_messages(_reply("Shipped a new onboarding flow today."), [], {}, stream=True)
    post = llm.draft_messages(_post(), [], {}, stream=True)
    assert first[1]["content"] == other[1]["content"] == post[1]["content"], "the writer block is stable"
    assert first[0]["content"].startswith(other[0]["content"]), "the rules are the same text (JSON adds one line)"
    prefix = first[0]["content"] + first[1]["content"]
    assert "Qwen" not in prefix and "onboarding" not in prefix, "nothing of the press is in the prefix"
    assert len(prefix) / 4 >= 1024, "long enough for OpenAI's automatic prompt caching"


def test_a_small_account_is_not_padded_to_reach_the_cache():
    """Below ~1,024 tokens nothing is cached, and nothing is padded in to get there: such a
    prompt is cheap and fast anyway, and padding would be instructions the model does not need."""
    _seed()
    voice_card.refresh_if_due(_CardModel())
    messages = llm.draft_messages(_reply(), [], {}, stream=True)
    assert (len(messages[0]["content"]) + len(messages[1]["content"])) / 4 < 1024


def test_the_writer_block_carries_the_card_and_a_fixed_sample():
    _seed()
    card = voice_card.refresh_if_due(_CardModel())
    assert card and card["version"] == 1
    writer, shown = llm.writer_prefix({})
    assert "Voice card" in writer and "LLM evals and latency" in writer
    assert "Measured habits (a description of their writing, not a rule)" in writer
    assert "Do not pad" not in writer and "do NOT" not in writer, "habits are described, never ruled"
    assert len(shown) == len(TEXTS) and all(text in writer for text in shown)


def test_the_fixed_sample_does_not_move_when_a_post_is_added_until_the_card_is_remade():
    _seed()
    voice_card.refresh_if_due(_CardModel())
    before = llm.writer_prefix({})[0]
    upsert_post({"post_id": "new1", "post_type": "post", "text": "a brand new post about retry budgets",
                 "created_at": NOW.isoformat(), "visibility": "public"})
    reset_personal_model_cache()
    llm.reset_writer_cache()
    assert llm.writer_prefix({})[0] == before, "the cached prefix survives a new post"


def test_no_plan_no_allowlist_no_hooks_no_measured_rules():
    _seed()
    request = _reply(text="@alibaba Qwen from dev is great")
    prompt = "\n".join(m["content"] for m in llm.draft_messages(request, [], {}, fields=llm.draft_fields(request)))
    for banned in ("The only names you may use", "YOU ARE WITH THEM ON THIS", "Plan for this reply",
                   "Do not pad past it", "SHAPE FOR THIS POST", "Lead with the specific thing they said",
                   "Angle to execute", "MATCH THIS WRITER'S STYLE"):
        assert banned not in prompt, banned


def test_the_integrity_rules_are_in_the_system_message_for_every_surface():
    _seed()
    for request in (_reply(), _post(),
                    DraftRequest(action="post", research_mode="off", enhancement_mode=True,
                                 original_draft="my own draft about retry loops",
                                 context=PostContext(author="You", text="my own draft", visibility="public"))):
        system = llm.draft_messages(request, [], {}, stream=True)[0]["content"]
        for rule in ("No invented facts", "No invented experience", "Name no company, product, person or place",
                     "never repeat more than a few words", "No engagement bait", "untrusted material"):
            assert rule in system, (request.action, rule)


def test_what_the_plan_knew_arrives_as_labelled_context():
    request = _reply(text="this changes the maths", quote_text="Serving cost fell from $12.40 to $0.90 per million",
                     language="es")
    request = request.model_copy(update={"conversation_notes": "Dev is quoting someone else's post: answer what Dev said."})
    user = llm.draft_messages(request, [], {}, stream=True)[-1]["content"]
    assert "It quotes another post (the words above are all the author added)" in user
    assert "write in Spanish" in user and "Dev is quoting someone else's post" in user
    image_only = _reply(text="", media=[{"kind": "image", "url": "https://pbs.twimg.com/a.jpg"}])
    user = llm.draft_messages(image_only, [], {}, stream=True)[-1]["content"]
    assert "nobody described: do not describe or guess what it shows" in user


def test_a_very_long_post_is_trimmed_in_the_variable_part_never_in_the_prefix(monkeypatch):
    _seed()
    voice_card.refresh_if_due(_CardModel())
    monkeypatch.setattr(llm, "INPUT_TOKEN_BUDGET", 2600)
    request = _reply(text="Everything we learned moving inference in-house. " * 400)
    messages = llm.draft_messages(request, [], {}, fields=llm.draft_fields(request))
    stable = llm.draft_messages(_reply(), [], {}, fields=llm.draft_fields(_reply()))
    assert messages[1]["content"] == stable[1]["content"], "the stable prefix is never trimmed"
    assert sum(len(m["content"]) for m in messages) / 4 <= 2600 + 50
    assert "[cut]" in messages[-1]["content"]


def test_a_new_account_is_told_there_is_no_voice_yet():
    request = _reply()
    writer = llm.draft_messages(request, [], {})[1]["content"]
    assert "no posts on record yet" in writer
    assert get_personal_model().example_rows == []
