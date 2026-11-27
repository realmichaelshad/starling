"""The API-native pass end to end (2026-09-22), against a scripted stand-in for the services proxy.

  * ✦ on a relevant post 2.5 hours old stays a REPLY: the analyzer's timing read is a prior the
    model is given, never a rule that turns the press into a standalone post;
  * Create post never dead-ends: stored stories for the account's beat are offered (numbered,
    with their pages on the card, the chosen one first); with nothing stored one press-time
    search runs; with nothing found the model writes from the beat and the card says so;
  * a quiet hour is a note on a drafted post, not a WAIT;
  * "What should I post about today?" reaches the model with the account's ideas;
  * integrity still blocks (an invented figure is refused and the retry is told why), while a
    style finding ships in one call with at most one note;
  * background work never runs on the press path.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import background, cloud, main, own_material, providers, topic_research
from app.schemas import BrowserAssistRequest, ComposerEnhancement, PostContext

NOW = datetime.now(timezone.utc)
REPLY = "so the upgrade path for local qwen is faster memory, not a bigger model"


@pytest.fixture()
def proxy(monkeypatch):
    """Records every call; answers any strict schema from `script`, streams `script['stream']`."""
    calls: list[dict] = []
    script: dict = {"text": REPLY, "verdict": "act", "confidence": 0.9, "topic": 1,
                    "stream": "benchmarks are the easy half. the useful tools ship the failure cases too"}

    def chat(model, messages, *, options=None, fmt=None):
        calls.append({"messages": messages, "fmt": fmt, "stream": False})
        props = ((fmt or {}).get("json_schema") or {}).get("schema", {}).get("properties", {}) \
            if isinstance(fmt, dict) else {}
        if not props:
            return "Two ideas from your beat: the protein review, and the new gym numbers.", {"total_ms": 5.0}
        answer = {}
        for name in props:
            if name in script:
                answer[name] = script[name]
            elif name == "why":
                answer[name] = "a concrete point to add"
            elif name == "point":
                answer[name] = "Faster memory, not a bigger model, is the local upgrade path."
            else:
                answer[name] = "adds the consequence"
        texts = script.get("texts")
        if texts and "text" in props:
            answer["text"] = texts.pop(0) if len(texts) > 1 else texts[0]
        return json.dumps(answer), {"total_ms": 5.0}

    def chat_stream(model, messages, *, options=None, stop_event=None):
        calls.append({"messages": messages, "fmt": None, "stream": True})
        user = messages[-1]["content"]
        meta = {"rationale": "lands"}
        if '"topic":1' in user:
            meta["topic"] = script.get("topic", 1)
        body = script["stream"] if "@@META@@" in user else "Two ideas from your beat, from the stories found."
        text = f"{body}\n@@META@@\n{json.dumps(meta)}" if "@@META@@" in user else body
        for piece in text.split(" "):
            yield {"delta": piece + " "}
        yield {"done": True, "timing": {"total_ms": 5.0}}

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:9")
    cloud.set_services_token("t" * 43)
    monkeypatch.setattr(cloud, "chat", chat)
    monkeypatch.setattr(cloud, "chat_stream", chat_stream)
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (None, "unavailable"))
    providers.reset_router()
    yield calls, script
    cloud.reset()
    providers.reset_router()


def _ai(calls):
    return [call for call in calls]


def _context(**extra) -> PostContext:
    base = dict(post_id="1790000000000000888", author="Some Dev", author_handle="somedev",
                text="Been running Qwen locally for a month. The bottleneck was never the model, "
                     "it was memory bandwidth.",
                post_created_at=(NOW - timedelta(minutes=18)).isoformat(), visibility="public",
                visible_metrics={"views": 5200, "likes": 140, "replies": 18},
                thread_scanned=True, visible_reply_count=18, visible_reply_ages_minutes=[2, 5, 9])
    base.update(extra)
    return PostContext(**base)


def _press(context: PostContext) -> dict:
    return asyncio.run(main.browser_assist(BrowserAssistRequest(mode="context", context=context)))


def _create_post(message: str = "") -> dict:
    return asyncio.run(main.browser_assist(BrowserAssistRequest(
        mode="enhance_composer", message=message or None,
        composer=ComposerEnhancement(action="post", draft_text="", intent="create"))))


def _coach() -> None:
    for text in ("Protein at breakfast is the easiest win nobody takes.",
                 "Beginners: three full-body days a week beats six days of chaos.",
                 "Creatine is boring, cheap, and works."):
        own_material.add("voice", text)
    own_material.add("background", "I coach strength training and nutrition for beginners.")
    main._refresh_niche()


def test_a_post_two_and_a_half_hours_old_stays_a_reply_with_the_prior_in_the_brief(proxy, monkeypatch):
    calls, _ = proxy
    original = main.decide_action

    def lean_post(*args, **kwargs):
        card = original(*args, **kwargs)
        card.action = "post"          # what the timing windows used to turn a 150-minute post into
        return card

    monkeypatch.setattr(main, "decide_action", lean_post)
    result = _press(_context(post_created_at=(NOW - timedelta(minutes=150)).isoformat(),
                             visible_reply_ages_minutes=[20, 35, 60]))
    decision = result["decision"]
    assert decision["action"] == "reply" and decision["draft"] == REPLY
    user = calls[-1]["messages"][-1]["content"]
    assert "Starling's timing read (a prior, not a rule): lean toward a post" in user
    assert "THE TASK: one reply from the writer" in user


def test_create_post_offers_the_stored_stories_and_lists_the_chosen_page_first(proxy, monkeypatch):
    calls, script = proxy
    _coach()
    findings = topic_research.findings_from({"sources": [
        {"title": "Protein review for beginners", "url": "https://news.example.com/protein",
         "snippet": "A review this week finds total protein matters more than timing for beginners."},
        {"title": "Gyms report a January surge", "url": "https://daily.example.com/gyms",
         "snippet": "Example Daily reports gyms saw a record number of new members this week."}]})
    import time
    topic_research._store(findings, "strength training", "background", time.time() - 60)
    monkeypatch.setattr(topic_research, "_pool_stories", lambda: [])
    script["topic"] = 2
    script["text"] = script["stream"] = "new members every january. the ones who stay picked a plan they can repeat"
    result = _create_post("post it anyway")
    decision = result["decision"]
    assert decision["draft"].startswith("new members every january")
    user = calls[-1]["messages"][-1]["content"]
    assert "[1] " in user and "[2] " in user and "Pick the ONE this writer" in user
    urls = [source["url"] for source in decision.get("sources") or []]
    chosen = topic_research.candidates(3)
    assert urls and urls[0] == "https://daily.example.com/gyms", decision
    assert "https://daily.example.com/gyms" not in [item["url"] for item in chosen], "the used story rests"


def test_create_post_with_nothing_stored_searches_once_then_writes_from_the_beat(proxy, monkeypatch):
    calls, script = proxy
    script["text"] = "most beginners need a plan they can repeat more than a better plan"
    _coach()
    searches = []
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (searches.append(kw) or ({"answer": "", "sources": []}, "ok")))
    monkeypatch.setattr(topic_research, "_pool_stories", lambda: [])
    result = _create_post("post it anyway")
    decision = result["decision"]
    assert searches == [{"purpose": "topics", "background": False}], "one press-time search, inside its caps"
    assert decision.get("draft"), "never a dead end"
    assert "nothing new in your beat today" in str(decision.get("search_note") or ""), "a search ran and found nothing"
    user = calls[-1]["messages"][-1]["content"]
    assert "There is no fresh news for this post" in user
    assert [call for call in calls if call["fmt"]], "the model was asked"


def test_a_refused_topic_search_is_not_called_nothing_new(proxy, monkeypatch):
    """Product review U6: a capped search says which cap and when it opens, never "nothing new"."""
    _, script = proxy
    script["text"] = "most beginners need a plan they can repeat more than a better plan"
    _coach()
    capped = cloud.SearchStatus("capped", "plan_share", 3 * 86_400)
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (None, capped))
    monkeypatch.setattr(topic_research, "_pool_stories", lambda: [])
    decision = _create_post("post it anyway")["decision"]
    note = str(decision.get("search_note") or "")
    assert decision.get("draft"), "never a dead end"
    assert "nothing new" not in note, note
    assert "news searches on your plan are used up; they come back on " in note, note
    late = cloud.SearchStatus("late", "timeout")
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (None, late))
    note = str(_create_post("post it anyway")["decision"].get("search_note") or "")
    assert "did not come back in time" in note and "nothing new" not in note, note


def test_create_post_about_a_subject_the_writer_named_skips_the_story_pick(proxy, monkeypatch):
    """The website's writing page asks "What's it about?": that subject is the ask, and nothing is
    searched or picked from the beat (`ComposerEnhancement.topic`)."""
    calls, script = proxy
    script["text"] = script["stream"] = "tracking sleep did more for my lifts than any program change"
    _coach()
    searches, picks = [], []
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (searches.append(kw) or (None, "unavailable")))
    monkeypatch.setattr(topic_research, "candidates", lambda *args, **kwargs: picks.append(args) or [])
    result = asyncio.run(main.browser_assist(BrowserAssistRequest(
        mode="enhance_composer", message="post it anyway",
        composer=ComposerEnhancement(action="post", draft_text="", intent="create",
                                     topic="  why beginners should\n track sleep "))))
    decision = result["decision"]
    assert decision["action"] == "post" and decision["draft"].startswith("tracking sleep"), decision
    assert searches == [] and picks == [], "the writer chose the subject: no search, no story pick"
    user = calls[-1]["messages"][-1]["content"]
    assert ("What the writer asked for: A post about this subject, which the writer chose: "
            "why beginners should track sleep.") in user
    assert "There is no fresh news for this post" not in user and "Pick the ONE this writer" not in user


def test_a_subject_is_ignored_on_a_reply_and_on_an_enhance(proxy):
    calls, script = proxy
    script["text"] = script["stream"] = REPLY
    for composer in (ComposerEnhancement(action="reply", draft_text="", intent="create", topic="ignored subject"),
                     ComposerEnhancement(action="post", draft_text="memory bandwidth is the real limit for local models",
                                         intent="enhance", topic="ignored subject")):
        asyncio.run(main.browser_assist(BrowserAssistRequest(
            mode="enhance_composer", composer=composer, context=_context() if composer.action == "reply" else None)))
        assert "ignored subject" not in calls[-1]["messages"][-1]["content"]


def test_a_quiet_hour_is_a_note_on_a_drafted_post(proxy, monkeypatch):
    _, script = proxy
    script["text"] = "most beginners need a plan they can repeat more than a better plan"
    _coach()
    monkeypatch.setattr(topic_research, "_pool_stories", lambda: [])
    from app import posting_hours
    monkeypatch.setattr(posting_hours, "hour_verdict",
                        lambda: {"dead": True, "reason": "You rarely post at 4am.", "until_local": "08:00"})
    decision = _create_post()["decision"]
    assert decision.get("draft") and decision.get("quiet_hour") is True
    assert "You rarely post at 4am." in decision.get("timing_line", "")
    assert decision.get("resume_local") == "08:00"


def test_what_should_i_post_reaches_the_model_with_the_accounts_ideas(proxy, monkeypatch):
    calls, _ = proxy
    _coach()
    import time
    topic_research._store(topic_research.findings_from({"sources": [
        {"title": "Protein review for beginners", "url": "https://news.example.com/protein",
         "snippet": "A review this week finds total protein matters more than timing for beginners."}]}),
        "strength training", "background", time.time() - 60)
    monkeypatch.setattr(topic_research, "_pool_stories", lambda: [])
    result = asyncio.run(main.browser_assist(BrowserAssistRequest(mode="chat",
                                                                  message="What should I post about today?")))
    assert calls, "the question reached the model"
    evidence = " ".join(str(m["content"]) for m in calls[-1]["messages"] if m["role"] == "system")
    assert "post_ideas" in evidence and "Protein review for beginners" in evidence
    assert result["research"]["sources"][0]["url"] == "https://news.example.com/protein"


def test_an_invented_figure_is_refused_and_the_retry_is_told_why(proxy):
    calls, script = proxy
    script["texts"] = ["local qwen got 71% faster the day they upgraded the memory",
                       REPLY]
    decision = _press(_context())["decision"]
    assert decision["draft"] == REPLY
    drafts = [call for call in calls if call["fmt"]]
    assert len(drafts) == 2
    assert "which is in neither the conversation nor the facts" in drafts[1]["messages"][-1]["content"]


def test_a_style_finding_ships_in_one_call(proxy):
    calls, script = proxy
    # "is a big deal" is a style finding (empty_big_deal): noted at most, never another call.
    script["text"] = "memory bandwidth being the limit is a big deal for anyone running qwen locally"
    decision = _press(_context())["decision"]
    assert decision["draft"] == script["text"]
    assert len([call for call in calls if call["fmt"]]) == 1
    assert not decision.get("needs_review")


def test_background_work_never_runs_on_the_press_path(proxy, monkeypatch):
    ran = []
    monkeypatch.setattr(background, "run_due_jobs", lambda **kw: ran.append(threading.current_thread().name))
    _press(_context())
    assert ran == [], "disabled outside a hosted worker"
    monkeypatch.setenv("STARLING_BACKGROUND_JOBS", "1")
    monkeypatch.setattr(background, "AFTER_PRESS_DELAY_S", 0.0)
    background._STATE.update(scheduled_at=0.0, running=False)
    seen = {}

    def job(**kwargs):
        from app import request_context
        seen["request_id"] = request_context.request_id()
        seen["thread"] = threading.current_thread().name

    monkeypatch.setattr(background, "run_due_jobs", job)
    assert background.schedule_after_press() is True
    for thread in threading.enumerate():
        if thread.name == "starling-background":
            thread.join(timeout=5)
    assert seen == {"request_id": None, "thread": "starling-background"}, "its own thread, no press context"


def test_a_chat_list_keeps_its_last_idea_unless_the_answer_was_cut():
    """Golden run: three good "what should I post about?" lists lost their last item, which a
    list does not end with a full stop. It is dropped only when the answer hit the token ceiling."""
    answer = ("Three that fit you today:\n- prompt caching and time to first token\n"
              "- how your eval set leaks into your prompts\n- p95 versus the median")
    assert main._trim_chat_answer(answer, truncated=False).endswith("- p95 versus the median")
    assert not main._trim_chat_answer(answer, truncated=True).endswith("- p95 versus the median")


def test_a_category_is_not_a_name_a_reply_can_leak():
    """Golden run: "small SaaS teams" under a post about a payment provider was refused as naming
    something the conversation never mentions. A real outside name still is."""
    from app.reply_plan import intruders, read_the_room
    context = PostContext(author="Fintech Daily", author_handle="fintechdaily",
                          text="Stripe now lets businesses accept payments in more than 135 currencies.")
    room = read_the_room(context, evidence="")
    assert intruders("Pricing in it is where small SaaS teams and B2B shops quietly lose margin.", room) == []
    assert intruders("Adyen did this years ago.", room) == []  # opens a sentence: not counted
    assert intruders("Honestly, this is what Adyen shipped years ago.", room) == ["adyen"]


def test_an_improve_close_to_the_writers_own_post_says_so(monkeypatch):
    """Golden run: an Improve that came out close to one of the writer's OWN earlier posts was
    labelled "almost the same as one Starling suggested before". It now says what it is close to."""
    from app import intelligence
    monkeypatch.setattr(intelligence, "_DRAFTED_THIS_SESSION", [])
    own = "We stopped offering custom contracts to small customers. Sales got faster and nobody left."
    assert intelligence._enhance_repeat_note(own) == intelligence.ENHANCE_OWN_POST_NOTE
    monkeypatch.setattr(intelligence, "_DRAFTED_THIS_SESSION", [own])
    assert intelligence._enhance_repeat_note(own) == intelligence.ENHANCE_REPEAT_NOTE


@pytest.mark.parametrize("text, checked", [
    ("The WHO recommends at least 150 minutes of moderate aerobic activity per week for adults.", True),
    ("Stripe now lets businesses accept payments in more than 135 currencies.", True),
    ("The EU AI Act's obligations for general-purpose AI models started applying in August 2025.", True),
    ("Freemium is dead for B2B. Sales-led or bust.", False),          # an opinion, and B2B is no name
    ("Shipping 40 features this quarter was a mistake.", False),      # an opener, not a name
    ("We moved our RAG pipeline to 300-token chunks and recall went from 62% to 81%.", False),  # their own
])
def test_the_claim_check_fires_on_public_claims_only(text, checked):
    """Review D: the eval's three claim posts ran no web check (a name that opens the sentence, an
    acronym after "The", one multiword subject); an opinion about "B2B" must not spend one."""
    from app.radar import claim_to_check
    assert bool(claim_to_check(text)) is checked, text
