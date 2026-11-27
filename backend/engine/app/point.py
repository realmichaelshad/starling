"""Decide what the post SAYS before writing it.

His words, reading ten drafts: "as a reader I'm not getting what the post is
trying to say — and prompting isn't what's going to fix it."

He is right about the cause. Look at what the drafts do:

    "Hugging Face uses GLM-2 to fight OpenAI, but Chinese models are outpacing
     them. U.S. Policy is stuck between tech and trade. The real question isn't
     who makes the model, it's who controls the data."

Three sentences, three unrelated claims. That is not bad writing — it is
exactly what happens when you hand a writer four disconnected facts and say
"write a post". It writes one sentence per fact. The result is a collage ABOUT
a topic rather than an argument, and no amount of "make one clear point" in the
prompt changes it, because the model has never been asked to choose the point.
It is choosing four.

So the choice is made first, in its own call, and the answer is a single
sentence: subject + claim. The draft is then written to argue that one thing,
and checked against it afterwards. A post that no longer matches its own point
is the collage coming back, and it is rejected rather than shipped.

This is deliberately one extra small call. It costs a couple of seconds and it
is the difference between "here are some facts about X" and "here is what I
think about X".
"""
from __future__ import annotations

import re

_MAX_POINT_WORDS = 22

_PROMPT = """You are planning ONE post for an account that writes about AI models and coding tools.

FACTS:
{facts}

Decide the SINGLE point the post will make. Not a summary of the facts — the
one thing the writer actually thinks, which a reader could repeat afterwards.

Rules for your answer:
- ONE sentence, under 20 words.
- It must name the specific subject from the facts.
- It must be a CLAIM someone could disagree with, not a description.
- No preamble, no quotes, no explanation. The sentence only.

Bad:  "Midjourney released v8.2 with new aesthetics."      (that is the news)
Good: "Midjourney is optimising for looks while builders need a stable API."

Bad:  "There are questions about open-weight models."      (says nothing)
Good: "The open-weight fight is about who controls the data, not who trains."
"""


def _clean(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = text.strip('"“”‘’').strip()
    # A model that explains itself before answering: keep the last real line.
    for marker in ("Point:", "point:", "Answer:", "answer:"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
    text = text.split("\n")[0].strip()
    words = text.split()
    if len(words) > _MAX_POINT_WORDS:
        text = " ".join(words[:_MAX_POINT_WORDS])
    return text


def decide_point(llm, facts: str, *, topic: str = "") -> str | None:
    """One sentence: what this post argues. None when nothing usable comes back."""
    body = str(facts or "").strip()
    if not body:
        return None
    prompt = _PROMPT.format(facts=body[:900])
    try:
        raw = llm.router.chat(
            [{"role": "system",
              "content": "Return only the single sentence. No preamble."},
             {"role": "user", "content": prompt}],
            options={"temperature": 0.3, "num_predict": 60, "timeout_s": 8.0},
            think=False)
    except Exception:                                   # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        from .llm import _strip_thinking
        raw = _strip_thinking(raw)
    except Exception:                                   # noqa: BLE001
        pass
    point = _clean(raw)
    if len(point.split()) < 4:
        return None
    # It has to be about the subject it was given, or it is a generic aphorism
    # dressed as a plan — which is the failure this exists to prevent.
    if topic and not (_words(point) & _words(topic)):
        if not (_words(point) & _words(body)):
            return None
    return point


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 3}


def serves_the_point(draft: str, point: str) -> bool:
    """Does the finished draft still argue the thing it set out to argue?

    Cheap and deliberately forgiving: the draft has to carry the point's
    distinctive words. It cannot verify reasoning, but it catches the failure
    that actually happens — the model drifting off its own plan and producing
    the collage again.
    """
    if not point:
        return True
    subject = _words(point)
    if len(subject) < 2:
        return True
    shared = subject & _words(draft)
    return len(shared) >= min(2, len(subject))

_CHECK_PROMPT = """A draft post, and the facts it was written from.

FACTS:
{facts}

DRAFT:
{draft}

Does the draft state anything the facts do not support, or contradict itself?

Answer with ONE line:
OK
or
BAD: <the exact phrase that is unsupported or contradictory>

Judge only what is asserted. An opinion is fine. A prediction is fine. A claim
about what something IS or DOES, when the facts say otherwise or say nothing,
is not.
"""


def unsupported_phrase(llm, draft: str, facts: str) -> str | None:
    """NOT WIRED. Measured and it does not work — recorded so it is not retried.

    Tested 2026-07-26 against the three failures it was built for. It answered
    OK to ALL of them, including the plain self-contradiction:

        "My project has 400 stars but UCP killed it. It is a sign people are
         building with it."                                 -> OK
        "GPT-5.6 API is already too slow for that." (facts silent)  -> OK

    Latency was 3.4s, 20.4s and 97.7s on three consecutive calls — the model
    was not reliably resident even immediately after generating, so the "it is
    already warm" assumption behind this was wrong too.

    The conclusion is about the model, not the prompt: a 9B asked to judge
    whether a fluent sentence is supported says yes. A verifier has to be
    better than the writer, and here it is the same model. Closing this gap
    needs a stronger judge — the 30b-a3b escalation route, or an embedding
    check of draft claims against evidence — not another prompt.

    One cheap pass: does the draft say something the evidence cannot back?

    This is the gap nine pattern gates cannot close. Measured live, all of
    these passed every one of them — real subject, one point, no template, no
    invented number:

        "GPT-5.6's API is already too slow for that."   (facts say nothing)
        "400 stars, but UCP's release killed it. It's a sign people are
         building with it."                              (contradicts itself)
        "RelativeDB's 30-day window for revenue minus cost is a big deal."
                        (RelativeDB is a query engine, not a finance tool)

    Each is a wrong inference from correct facts, which is a comprehension
    failure, not a pattern one.

    It runs AFTER generation on purpose. An earlier attempt put a planning call
    BEFORE it, which forced a cold model load and pushed the whole request past
    the timeout — 128s, and the composer got nothing. By this point the model
    is already resident, and the same call costs about a second.
    """
    body, evidence = (draft or "").strip(), (facts or "").strip()
    if not body or not evidence:
        return None
    try:
        raw = llm.router.chat(
            [{"role": "system", "content": "Reply with OK or BAD: <phrase>. Nothing else."},
             {"role": "user", "content": _CHECK_PROMPT.format(
                 facts=evidence[:700], draft=body[:500])}],
            options={"temperature": 0.0, "num_predict": 40, "timeout_s": 8.0},
            # S9 (2026-09-22): a yes/no check against the facts, on the fast lane.
            think=False, lane="fast")
    except Exception:                                   # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        from .llm import _strip_thinking
        raw = _strip_thinking(raw)
    except Exception:                                   # noqa: BLE001
        pass
    answer = re.sub(r"\s+", " ", str(raw)).strip()
    if not answer or answer.upper().startswith("OK"):
        return None
    match = re.search(r"BAD\s*:\s*(.+)", answer, re.IGNORECASE)
    if not match:
        return None
    phrase = match.group(1).strip().strip('"“”').strip()
    # A verifier that quotes the whole post is not identifying anything.
    if not phrase or len(phrase) > 160 or len(phrase.split()) > 24:
        return None
    return phrase
