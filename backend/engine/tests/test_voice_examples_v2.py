"""V2 + V4: the draft is shown the writer's own most similar writing, and nothing when there is none.

The old pool was chosen to AVOID the subject (an on-topic past post was an "answer key"),
ordered by one author's slang, and a new customer got `[]` under "these are the writer's
PAST posts". These pin what replaced it: reply PAIRS ranked by word similarity to the post
being answered, the account's protected posts never sent, and an honest cold-start line.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.intelligence import PersonalModel, _voice_examples
from app.llm import draft_messages
from app.schemas import DraftRequest, PostContext
from app.storage import upsert_post
from app.voice_examples import bm25_scores, own_writing_count

NOW = datetime.now(timezone.utc)


def _seed_replies() -> None:
    pairs = [
        ("GPU prices are finally dropping for inference boxes.",
         "cheaper cards help but the bandwidth ceiling is still the real limit"),
        ("We moved our CI to self-hosted runners.",
         "self-hosted runners are great until the one box running them dies at 2am"),
        ("Postgres handled everything we threw at it.",
         "postgres is the answer to about 80 percent of queue questions"),
        ("Memory bandwidth is the bottleneck for local models, not compute.",
         "bandwidth is the whole game for local inference, a bigger card with the same memory just idles"),
    ]
    for index, (parent, reply) in enumerate(pairs):
        created = NOW - timedelta(days=3 * index + 1)
        upsert_post({"post_id": f"r{index}", "text": reply, "post_type": "reply", "parent_text": parent,
                     "created_at": created.isoformat(), "visibility": "public",
                     "metrics": {"views": 400 + index, "likes": 3 + index},
                     "metrics_updated_at": (created + timedelta(days=1)).isoformat()})
    upsert_post({"post_id": "p0", "text": "shipping the boring version first is still undefeated",
                 "post_type": "post", "created_at": (NOW - timedelta(days=2)).isoformat(),
                 "visibility": "public"})


def _reply_request(text: str) -> DraftRequest:
    return DraftRequest(action="reply", research_mode="off", media_policy="no_media", grok_permission=False,
                        context=PostContext(author="Some Dev", author_handle="somedev", text=text,
                                            visibility="public"))


def test_a_reply_is_shown_the_most_similar_reply_pair_first():
    _seed_replies()
    request = _reply_request("Been running Qwen locally for a month. The bottleneck was memory bandwidth.")
    examples = _voice_examples(PersonalModel(), request)
    assert examples and examples[0].startswith("bandwidth is the whole game"), examples
    assert examples[0].parent.startswith("Memory bandwidth is the bottleneck")
    assert all(example.kind == "reply" for example in examples), "their replies, not their posts"
    prompt = draft_messages(request, examples, {})[-1]["content"]
    assert 'Replying to: "Memory bandwidth is the bottleneck' in prompt
    assert prompt.index("bandwidth is the whole game") < prompt.index("self-hosted runners are great")


def test_the_accounts_protected_posts_never_reach_the_prompt():
    _seed_replies()
    upsert_post({"post_id": "secret", "text": "bandwidth bandwidth bandwidth private note about memory",
                 "post_type": "reply", "parent_text": "memory bandwidth", "visibility": "protected",
                 "created_at": NOW.isoformat()})
    request = _reply_request("memory bandwidth for local models")
    examples = _voice_examples(PersonalModel(), request)
    assert all("private note" not in str(example) for example in examples)


def test_a_post_is_shown_similar_posts_and_top_performers():
    for index in range(6):
        created = NOW - timedelta(days=index + 2)
        upsert_post({"post_id": f"post{index}", "post_type": "post", "created_at": created.isoformat(),
                     "text": (f"agent evals keep missing the third failure mode, note {index}" if index < 3
                              else f"woodworking note {index}: sharp chisels beat expensive routers"),
                     "metrics": {"views": 50_000 if index == 5 else 300, "likes": 90 if index == 5 else 2},
                     "metrics_updated_at": (created + timedelta(days=1)).isoformat()})
    request = DraftRequest(action="post", research_mode="off",
                           context=PostContext(author="You", text="", visibility="public"),
                           direction="Write about agent evals and failure modes.")
    examples = _voice_examples(PersonalModel(), request)
    assert "agent evals" in examples[0]
    assert any("woodworking note 5" in example for example in examples), "the top performer rides along"


def test_a_new_account_is_never_sent_an_empty_example_list():
    request = _reply_request("Anything at all.")
    examples = _voice_examples(PersonalModel(), request)
    assert examples == []
    # API-native pass: the cold-start line is the writer block (the stable prefix), not the user part.
    prompt = "\n".join(m["content"] for m in draft_messages(request, examples, {}))
    assert "no posts on record yet" in prompt
    assert ": []" not in prompt and "PAST posts" not in prompt
    assert own_writing_count(PersonalModel()) == 0


def test_bm25_prefers_shared_rare_words():
    scores = bm25_scores("memory bandwidth local models",
                         ["the memory bandwidth ceiling", "shipping boring software", "local cafe opens"])
    assert scores[0] == 1.0 and scores[1] == 0.0 and 0.0 < scores[2] < 1.0
