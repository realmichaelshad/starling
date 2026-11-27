"""The golden answers as stand-in rules (`E2E_AI_SCRIPT`, e2e/harness/fake_openai.py).

`build_rules()` turns `golden.GOLDEN` (per press) and `golden.VOICE_CARDS` (per persona) into the
list of `{"match": [...], "endpoint"?: ..., "answer": ...}` rules the stand-in consults first.
Each rule matches a call by substrings of the prompt the engine actually sends, so this file
follows the engine's prompt format (backend/engine/app/llm.py, voice_card.py, main.py); if a marker
below stops appearing in a prompt, the rule simply stops matching and the stand-in's own filler
answers instead, which the report makes obvious.

How a call is recognised (all substrings of the prompt text: the messages in order, each as
"<role>" on its own line and then its content):

* THE CUSTOMER: a sentence of their own writing, which the writer block (the second system
  message) always carries, or, for a brand-new account, the writer block's cold-start line. Chat
  has no writer block, so a chat is recognised by the account's own beat words, which the chat
  evidence carries (`post_ideas.writer_beats`, from the niche read).
* THE SURFACE: the task line (`THE TASK: one reply ...`, `... one standalone post ...`,
  `... improve the writer's own draft ...`), the ✦ decision block (`DECIDE FIRST, THEN WRITE`), the
  composer's streamed format (`Output format: first write one line starting POINT:`), the chat
  system prompt (`You are Starling`), and the voice card call (`VOICE CARD`).
* THE POST: the opening words of the post being answered, or of the draft being improved.

What each surface is answered with is the shape the engine asks for: the ✦ call a strict JSON
object (verdict, why, confidence, point, text); a composer reply a POINT line, the text
and the metadata line; a created post the text and a metadata line naming story 1; an enhance the
text and the metadata line (no POINT line, so the engine keeps the whole text); chat the answer;
the voice card a JSON object with the card's fields.
"""
from __future__ import annotations

import json
from typing import Any

from .golden import GOLDEN, VOICE_CARDS, Golden
from .personas import PERSONAS

# The engine's prompt markers (backend/engine/app/llm.py, voice_card.py).
SENTINEL = "@@META@@"
STAR = "DECIDE FIRST, THEN WRITE"
REPLY_TASK = "THE TASK: one reply from the writer"
POST_TASK = "THE TASK: one standalone post by the writer"
ENHANCE_TASK = "THE TASK: improve the writer's own draft"
COMPOSER_REPLY_FORMAT = "Output format: first write one line starting POINT:"
CHAT = "You are Starling"
CARD = "VOICE CARD"
COLD_START = "The writer has no posts on record yet"

# One sentence of each customer's own writing that the writer block always carries (the
# account has fewer texts than the block's fixed sample holds, so every sample is in it), and
# a beat word the chat evidence carries.
WRITER_MARK = {
    "terse": "turns out the bottleneck was the retry loop, not the model",
    "essayist": "The most expensive words in SaaS are",
    "coach": "You don't need a new program. You need to finish the one you started",
}
CHAT_MARK = {"terse": "inference cost", "essayist": "churn", "coach": "nutrition advice"}


def _opening(text: str, words: int = 7) -> str:
    """The first words of a post or draft, as the prompt carries them (JSON-escaped text).

    A post of a few words ("gm") is matched with its quotes, as the conversation block prints it,
    so it cannot match inside another word.
    """
    parts = str(text or "").split()
    if len(parts) <= 3:
        return json.dumps(" ".join(parts), ensure_ascii=False)
    return json.dumps(" ".join(parts[:words]), ensure_ascii=False)[1:-1]


def _star_answer(golden: Golden) -> dict[str, Any]:
    # The ✦ object has no "rationale": the card leads with "why" (backend/engine/app/llm.py).
    if golden.verdict == "wait":
        return {"verdict": "wait", "why": golden.why or "Nothing to add here.", "confidence": 0.9,
                "point": "", "text": ""}
    return {"verdict": "act", "why": "There is something worth adding here.", "confidence": 0.9,
            "point": golden.point or golden.text.split(".")[0], "text": golden.text}


def _stream(text: str, *, point: str = "", topic: bool = False) -> str:
    meta: dict[str, Any] = {"rationale": "The writer's own take, in their voice."}
    if topic:
        meta["topic"] = 1
    head = f"POINT: {point}\n" if point else ""
    return f"{head}{text}\n{SENTINEL}\n{json.dumps(meta, ensure_ascii=False)}"


def _card(persona: str) -> dict[str, Any]:
    read = VOICE_CARDS[persona]
    sentences = [part.strip() + "." for part in str(read["card"]).split(". ") if part.strip()]
    return {
        "beats": list(read["beats"]),
        "stances": sentences[1:3],
        "argues_and_jokes": sentences[-1] if sentences else "",
        "uses": [],
        "avoids": [],
        "shape": sentences[0] if sentences else "",
        "audience": sentences[0] if sentences else "",
    }


def build_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for persona in PERSONAS:
        writer = WRITER_MARK.get(persona.key, COLD_START)
        for press in persona.presses:
            golden = GOLDEN.get(press.id)
            if golden is None:
                continue
            if press.kind == "star" and press.post is not None:
                rules.append({"endpoint": "chat", "match": [STAR, REPLY_TASK, writer, _opening(press.post.text)],
                              "answer": _star_answer(golden)})
            elif press.kind == "create_reply" and press.post is not None:
                rules.append({"endpoint": "chat",
                              "match": [REPLY_TASK, COMPOSER_REPLY_FORMAT, writer, _opening(press.post.text)],
                              "answer": _stream(golden.text, point=golden.point)})
            elif press.kind == "create_post":
                rules.append({"endpoint": "chat", "match": [POST_TASK, writer, SENTINEL],
                              "answer": _stream(golden.text, topic=True)})
            elif press.kind == "enhance":
                rules.append({"endpoint": "chat", "match": [ENHANCE_TASK, writer, _opening(press.draft_text)],
                              "answer": _stream(golden.text)})
            elif press.kind == "chat" and persona.key in CHAT_MARK:
                rules.append({"endpoint": "chat", "match": [CHAT, press.message, CHAT_MARK[persona.key]],
                              "answer": golden.text})
        if persona.key in VOICE_CARDS:
            rules.append({"endpoint": "chat", "match": [CARD, writer], "answer": _card(persona.key)})
    # The brand-new account's chat last: it is the one chat with no beat words to match on.
    for persona in PERSONAS:
        for press in persona.presses:
            golden = GOLDEN.get(press.id)
            if golden is not None and press.kind == "chat" and persona.key not in CHAT_MARK:
                rules.append({"endpoint": "chat", "match": [CHAT, press.message], "answer": golden.text})
    return rules


__all__ = ["build_rules"]
