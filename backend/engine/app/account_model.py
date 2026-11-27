from __future__ import annotations

import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from statistics import median
from typing import Any

import numpy as np

from . import recency
from .storage import archive_texts, get_account_profile, performance_records, records


STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "because", "been", "but", "can", "could", "does", "for",
    "from", "have", "here", "into", "just", "like", "more", "not", "now", "one", "our", "out", "really", "that",
    "than", "the", "their", "there", "they", "this", "those", "too", "was", "what", "when", "where", "which", "who",
    "will", "with", "would", "you", "your", "https", "http",
    # A DATE IS NOT A SUBJECT, AND NEITHER IS A HEDGE. These reached the
    # dimension token cores from his own archive and made "july" and
    # "actually" look like things the account is ABOUT — enough for a
    # Spider-Man release date to share vocabulary with his AI beat.
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "today", "tomorrow",
    "yesterday", "week", "month", "year", "day", "days", "weeks", "months",
    "actually", "think", "thinking", "might", "maybe", "probably", "honestly",
    "basically", "literally", "feels", "feel", "seems", "guess", "pretty",
    "still", "already", "even", "every", "some", "much", "many", "make",
    "made", "get", "got", "going", "how", "why", "who's", "let",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z][a-z0-9_'-]{2,}", text.lower()) if token not in STOPWORDS]


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / max(1, denominator), 3)


def _shouted_words(text: str) -> list[str]:
    """Words in ALL CAPS that are EMPHASIS rather than initialisms.

    One source of truth for "is this account a shouter", shared with the
    scrubber that repairs a shouted draft (`intelligence.normalise_shouting`)
    so the measurement and the repair can never drift apart — the drift is
    what produced a directive inviting the model to shout.
    """
    from .intelligence import _REAL_INITIALISMS, _SHOUT_RE
    return [word for word in _SHOUT_RE.findall(text or "")
            if word not in _REAL_INITIALISMS]


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U00002B00-\U00002BFF]")


def _has_emoji(text: str) -> bool:
    return bool(_EMOJI_RE.search(text))


def _uniform(weights: list[float] | None) -> bool:
    """True when weighting cannot change any statistic.

    Uniform weights (an undated archive, or every post the same age bucket)
    take the exact unweighted code path, so 'weighted equals unweighted when
    nothing distinguishes the rows' holds by construction, not by rounding
    luck.
    """
    return weights is None or len(set(weights)) <= 1


def _pctile(values: list[float], q: float, weights: list[float] | None = None) -> float:
    if not values:
        return 0.0
    if _uniform(weights):
        ordered = sorted(values)
        return float(ordered[min(len(ordered) - 1, int(q * len(ordered)))])
    pairs = sorted(zip(values, weights))
    total = sum(weight for _, weight in pairs)
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= q * total:
            return float(value)
    return float(pairs[-1][0])


def _wmedian(values: list[float], weights: list[float] | None = None) -> float:
    """Weighted median that REDUCES to statistics.median at equal weights.

    Not `_pctile(values, 0.5, weights)`: that returns the first value whose
    cumulative weight crosses the halfway mark, while `statistics.median`
    averages the two middle values of an even sample. The two disagree by a
    lot on small samples (his six replies: crossing 10, averaging 13), so
    using the crossing rule for weighted data and the averaging rule for
    unweighted data meant weights a hair off equal could move a length
    target three words with no new information in the corpus.

    Averaging on an exact halfway tie fixes it: with equal weights the tie
    lands precisely on the boundary for an even sample (reproducing
    statistics.median) and never for an odd one (reproducing the middle
    value). Quartiles keep the crossing rule, which is what the unweighted
    `_pctile` already did.
    """
    if not values:
        return 0.0
    if _uniform(weights):
        return float(median(values))
    pairs = sorted(zip(values, weights))
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return float(median(values))
    # Each value sits at the CENTRE of its own weight block; the median is
    # the value interpolated where those centres cross the halfway mark.
    # At equal weights the crossing lands between the two middle centres of
    # an even sample (giving their average) and exactly on the middle centre
    # of an odd one — statistics.median in both cases.
    centres: list[float] = []
    cumulative = 0.0
    for value, weight in pairs:
        centres.append((cumulative + weight / 2.0) / total)
        cumulative += weight
    if 0.5 <= centres[0]:
        return float(pairs[0][0])
    if 0.5 >= centres[-1]:
        return float(pairs[-1][0])
    for index in range(len(centres) - 1):
        low, high = centres[index], centres[index + 1]
        if low <= 0.5 <= high:
            span = high - low
            fraction = 0.0 if span <= 0 else (0.5 - low) / span
            return float(pairs[index][0] + fraction * (pairs[index + 1][0] - pairs[index][0]))
    return float(pairs[-1][0])


def _wrate(flags: list[bool], weights: list[float] | None = None) -> float:
    flags = [bool(flag) for flag in flags]
    if _uniform(weights):
        return _rate(sum(flags), len(flags))
    total = sum(weights)
    if total <= 0:
        return 0.0
    return round(sum(weight for flag, weight in zip(flags, weights) if flag) / total, 3)


def _style_metrics(rows: list[dict[str, Any]], texts: list[str],
                   word_counts: list[int], total: int,
                   weights: list[float] | None = None) -> dict[str, Any]:
    """Measure the concrete, imitable shape of this account's posts.

    Everything here is derived from the user's OWN posts so the writer can match
    how they actually format, punctuate, capitalise, and pick words - the things
    that make a draft read like them and not like a generic bot.

    `weights` (aligned with `texts`) lets the live path lean toward the
    account's recent writing so the signature follows the author; uniform or
    absent weights take the exact unweighted code path.
    """
    if not texts:
        return {}
    if weights is not None and len(weights) != len(texts):
        weights = None
    lines = [text.count("\n") + 1 for text in texts]
    blocks = [len([part for part in re.split(r"\n\s*\n", text) if part.strip()]) for text in texts]

    def _slice(flags: list[bool]) -> list[float] | None:
        """Weights for the texts where a habit is present, kept aligned."""
        if weights is None:
            return None
        return [weight for flag, weight in zip(flags, weights) if flag]

    multiline_flags = ["\n" in text for text in texts]
    multiline_texts = [text for text in texts if "\n" in text]
    words = [w for text in texts for w in re.findall(r"[A-Za-z']+", text)]
    all_emoji = "".join(ch for text in texts for ch in text if _EMOJI_RE.match(ch))
    top_emoji = [e for e, _ in Counter(all_emoji).most_common(3)]
    # Counts WHEN the habit is present, so an account that stacks 3 emojis or 2
    # hashtags is described accurately, not capped at "one".
    emoji_flags = [_has_emoji(t) for t in texts]
    emoji_counts = [sum(1 for ch in t if _EMOJI_RE.match(ch)) for t in texts if _has_emoji(t)]
    hashtag_flags = ["#" in t for t in texts]
    hashtag_counts = [t.count("#") for t in texts if "#" in t]
    sentences_per = [max(1, len(re.findall(r"[.!?]+", t))) for t in texts]
    if _uniform(weights):
        avg_word_length = round(sum(len(w) for w in words) / max(1, len(words)), 1)
        long_word_rate = _rate(sum(1 for w in words if len(w) >= 8), len(words))
    else:
        # A word inherits its post's weight, so vocabulary follows the same
        # law as every per-post rate.
        length_sum = weight_sum = long_sum = 0.0
        for text, weight in zip(texts, weights):
            for word in re.findall(r"[A-Za-z']+", text):
                length_sum += len(word) * weight
                weight_sum += weight
                long_sum += weight if len(word) >= 8 else 0.0
        avg_word_length = round(length_sum / max(1e-9, weight_sum), 1)
        long_word_rate = round(long_sum / max(1e-9, weight_sum), 3)
    # image_rate reads `rows`, which may include text-less image posts the
    # text-aligned weights cannot cover; weight it only when they line up.
    image_flags = [bool(r.get("has_image")) for r in rows]
    image_weights = weights if weights is not None and len(image_flags) == len(weights) else None
    return {
        "sample": total,
        "weighted": bool(weights is not None and not _uniform(weights)),
        "effective_sample": round(sum(weights), 1) if weights is not None else total,
        "median_words": round(_wmedian([float(w) for w in word_counts], weights), 0) if word_counts else 0,
        "words_p25": round(_pctile([float(w) for w in word_counts], 0.25, weights)),
        "words_p75": round(_pctile([float(w) for w in word_counts], 0.75, weights)),
        "median_lines": round(_wmedian([float(v) for v in lines], weights)) if lines else 1,
        "median_blocks": round(_wmedian([float(v) for v in blocks], weights)) if blocks else 1,
        "multiline_rate": _wrate([l > 1 for l in lines], weights),
        # A line break and an empty spacer line are different habits. The old
        # formatter always invented blank lines for any multi-line writer,
        # which made compact reply writers look unlike themselves.
        "blank_line_rate": _wrate([bool(re.search(r"\n\s*\n", text)) for text in multiline_texts],
                                  _slice(multiline_flags)),
        "median_sentences": round(_wmedian([float(v) for v in sentences_per], weights)) if sentences_per else 1,
        "emoji_rate": _wrate(emoji_flags, weights),
        "emoji_per_post": round(_wmedian([float(v) for v in emoji_counts], _slice(emoji_flags))) if emoji_counts else 0,
        "top_emoji": top_emoji,
        "hashtag_rate": _wrate(hashtag_flags, weights),
        "hashtag_per_post": round(_wmedian([float(v) for v in hashtag_counts], _slice(hashtag_flags))) if hashtag_counts else 0,
        # Unweighted keeps the historical denominator (`total`, the text-bearing
        # count) even when rows include text-less image posts.
        "image_rate": (_wrate(image_flags, image_weights) if image_weights is not None
                       else _rate(sum(image_flags), total)),
        "mention_rate": _wrate([bool(re.search(r"@\w+", t)) for t in texts], weights),
        "link_rate": _wrate(["http" in t for t in texts], weights),
        # Casing: does the account write casual-lowercase, and does it shout a
        # word in ALL CAPS for emphasis (a real signature for many writers).
        "lowercase_start_rate": _wrate([t.strip()[:1].islower() for t in texts], weights),
        # EMPHASIS IS A SHOUTED WORD, NOT AN ACRONYM. This counted any 3+
        # letter capital run, so "AI", "API", "GPT", "MCP" and "SWE" all read
        # as shouting: the rate came out 0.34 on an archive whose ONE genuine
        # emphasis is a single "HALF" in 41 posts, the directive below then
        # told the writer "you MAY put one word in ALL CAPS, like they do",
        # and it shipped "cost efficiency is HALF PRICE for FABLE
        # performance" — three shouted words in eleven, one of them a model
        # name. The instruction was faithful to a measurement that was wrong,
        # which is the only kind of prompt bug worth fixing at the source.
        "allcaps_emphasis_rate": _wrate([bool(_shouted_words(t)) for t in texts], weights),
        # Punctuation habits. em-dash and semicolon are the classic AI-writing
        # tells; measured here so the writer is told to avoid them when this
        # account almost never uses them (and stripped deterministically after).
        "emdash_rate": _wrate(["—" in t or "–" in t or " - " in t for t in texts], weights),
        "semicolon_rate": _wrate([";" in t for t in texts], weights),
        "colon_rate": _wrate([":" in re.sub(r"https?://\S+", "", t) for t in texts], weights),
        "ellipsis_rate": _wrate(["..." in t or "…" in t for t in texts], weights),
        "exclaim_rate": _wrate(["!" in t for t in texts], weights),
        "question_rate": _wrate(["?" in t for t in texts], weights),
        # Vocabulary level.
        "avg_word_length": avg_word_length,
        "long_word_rate": long_word_rate,
        "contraction_rate": _wrate([bool(re.search(r"\w['’]\w", t)) for t in texts], weights),
    }


def _band(rate: float) -> str:
    """A rate (0..1) as a plain-language frequency word."""
    if rate >= 0.6:
        return "usually"
    if rate >= 0.3:
        return "often"
    if rate >= 0.12:
        return "sometimes"
    if rate >= 0.03:
        return "rarely"
    return "never"


# A per-action profile is only trusted with enough samples; below this, a
# couple of tagged replies is noisier than a sensible surface default (2 tagged
# replies gave question_rate 0.5 and length 17 — a coin-flip, not a habit). At
# or above it, the writer's real per-action style wins.
_MIN_ACTION_SAMPLES = 3

# Each surface is a DIFFERENT kind of writing — the user's rule: posts, replies
# and quotes should each have their own length, formatting AND tone. When there
# is not enough tagged data for an action, these anchor the general voice to the
# right shape for that surface instead of letting everything become post-shaped.
# THE POST SURFACE HAS TWO REGISTERS, and the tone above is only one of them.
# Measured on his 65 hand-read posts: 48% are shitposts (median 10 words, 26 of
# 31 exactly one sentence) and 22% are fact-stating. The product has produced
# 0% shitpost — every generated post is a bulletin, because the only tone this
# surface has ever offered is the bulletin's.
#
# ADDITIVE BY CONSTRUCTION. `_SURFACE_TONE` is untouched and is still the
# default for every caller; this table is consulted only when a register is
# passed. Nothing existing moves, so nothing existing can break.
_POST_REGISTER_TONE = {
    "shitpost": "One-line aside: say the funny or obvious thing straight out, in "
                "one sentence, and stop. No thesis, no build-up, no conclusion.",
    "facts": "Standalone take: lead with your own thesis or prediction and back it "
             "with one concrete fact. Declarative and opinionated.",
}

# NO FIXED VOICE OR LENGTH IN HERE (V1, 2026-09-21). The reply line used to end
# "conversational and punchy, one quick read. A question or a sharp reaction
# lands better than a mini-essay" and the post line "Declarative and
# opinionated": the original author's register, stated as every account's
# tone. What each surface IS stays; how long and how punchy comes from the
# measured Length/Shape lines and the writer's own examples.
_SURFACE_TONE = {
    "post": "Standalone post: it has to make sense to someone who sees nothing "
            "else, and carry the writer's own take, not a news summary.",
    "reply": "Reply to a person: address THEIR point directly, the way this "
             "writer talks to people.",
    "quote": "Commentary on the quoted post: your first line must stand on its own "
             "without the embed, then frame or push back on what they said — react "
             "to it, don't summarise it.",
}


def _surface_style(action: str, generic: dict[str, Any],
                   register: str | None = None) -> dict[str, Any]:
    """The general voice re-shaped for one surface: distinct length, formatting
    and tone for a post vs a reply vs a quote, when that action lacks samples.

    `register` is the POST surface's second axis (see `_POST_REGISTER_TONE`).
    Keyword-defaulted and consulted only when supplied, so every existing caller
    keeps the surface tone it has always had.
    """
    median = int(generic.get("median_words") or 0) or 30
    tone = _SURFACE_TONE.get(action)
    if action == "post" and register:
        tone = _POST_REGISTER_TONE.get(register, tone)
    # Length ladder — each surface visibly different: reply < quote < post.
    # A reply is a quick line; a quote is a compact take framing the source; a
    # standalone post is the longest.
    # `"sample": 0` IS THE POINT OF THESE TWO LINES. Both fallbacks spread
    # `**generic`, which carries the POST surface's sample count — 42 — into
    # a dict whose numbers are arithmetic on the post median, computed for a
    # surface with no samples at all. `style_directive` reads that count and
    # prints "MATCH THIS WRITER'S STYLE (measured from their own posts)" over
    # a fabricated quote length, and `_match_account_style` then enforces the
    # derived `words_p75` as a hard truncation bound, amputating quote drafts
    # to fit a number nobody measured. A derived shape must never inherit the
    # authority of the measurement it was derived from.
    if action == "reply":
        length = max(10, min(int(median * 0.55), 18))
        return {**generic, "median_words": length, "words_p25": max(8, length - 6),
                "words_p75": length + 6, "multiline_rate": 0.0, "median_lines": 1,
                "median_blocks": 1, "blank_line_rate": 0.0, "sample": 0,
                "tone_hint": tone, "reply_shape_fallback": True}
    if action == "quote":
        length = max(16, int(median * 0.8))
        return {**generic, "median_words": length,
                "words_p25": max(12, length - 8), "words_p75": length + 8,
                "median_blocks": min(2, int(generic.get("median_blocks") or 2)),
                "sample": 0,
                "tone_hint": tone, "quote_shape_fallback": True}
    # post — full length, roomy shape as measured.
    return {**generic, "tone_hint": tone}


def style_for_action(model: dict[str, Any] | None, action: str | None = None,
                     register: str | None = None) -> dict[str, Any]:
    """The measured writing signature for ONE surface — posts, replies and quotes
    are each a different kind of writing with their own length, formatting and
    tone. A per-action profile is used only when it has enough samples to be a
    real habit; otherwise the general voice is re-shaped for that surface."""
    voice = (model or {}).get("voice_profile") or {}
    generic_style = voice.get("style") or {}
    format_styles = voice.get("format_styles") or {}
    candidate = format_styles.get(str(action or "")) if isinstance(format_styles, dict) else None
    if isinstance(candidate, dict) and int(candidate.get("sample") or 0) >= _MIN_ACTION_SAMPLES:
        tone_hint = _SURFACE_TONE.get(str(action or ""))
        if action == "post" and register:
            tone_hint = _POST_REGISTER_TONE.get(register, tone_hint)
        return {**candidate, "tone_hint": tone_hint}
    if action in ("post", "reply", "quote") and generic_style:
        return _surface_style(action, generic_style, register)
    return generic_style


def style_directive(model: dict[str, Any] | None, action: str | None = None) -> str:
    """Render the style fingerprint as imperative instructions for the writer.

    A JSON blob of rates is not something a small model writes to. This turns the
    measured signature into short, concrete rules - "~28 words, 2-3 short lines,
    no hashtags, normal case but you may CAPS one word" - so the draft actually
    matches how the account writes.
    """
    voice = (model or {}).get("voice_profile") or {}
    format_styles = voice.get("format_styles") or {}
    candidate = format_styles.get(str(action or "")) if isinstance(format_styles, dict) else None
    has_measured_action_shape = isinstance(candidate, dict) and int(candidate.get("sample") or 0) >= 2
    style = style_for_action(model, action)
    if not style:
        return ""
    sample = int(style.get("sample") or 0)
    header = "MATCH THIS WRITER'S STYLE (measured from their own posts):"
    if style.get("quote_shape_fallback") or style.get("reply_shape_fallback"):
        # SAY WHICH NUMBERS ARE DERIVED. The flag has been set since this
        # function was written and read nowhere, so a length computed as 0.8x
        # the post median shipped under a claim of measurement.
        header = (f"MATCH THIS WRITER'S STYLE (measured on their posts and "
                  f"re-shaped for a {action or 'post'} — there are no "
                  f"{action or 'post'}s in the archive yet, so treat the "
                  "length below as a starting guide, not a measurement):")
    elif 0 < sample < 8:
        # Small archive => the signature is noisy; say so rather than assert it.
        header = (f"MATCH THIS WRITER'S STYLE (rough read - only {sample} posts to "
                  "go on, so follow the strong signals, not the fine detail):")
    lines: list[str] = [header]

    # Tone is per-surface: a post, a reply and a quote are different kinds of
    # writing, so the tone guidance leads (length/shape/punctuation follow).
    tone_hint = str(style.get("tone_hint") or "").strip()
    if tone_hint:
        lines.append(f"- Tone: {tone_hint}")

    median_w = int(style.get("median_words") or 0)
    lo, hi = int(style.get("words_p25") or 0), int(style.get("words_p75") or 0)
    if median_w:
        span = f" (usually {lo}-{hi})" if hi > lo else ""
        lines.append(f"- Length: about {median_w} words{span}. Do not pad past it.")
    med_lines = int(style.get("median_lines") or 1)
    med_blocks = int(style.get("median_blocks") or med_lines or 1)
    if action == "reply" and not has_measured_action_shape:
        lines.append("- Shape: no confirmed reply samples yet, so keep this as one compact paragraph; do not add blank spacer lines.")
    elif style.get("multiline_rate", 0) >= 0.4 and med_lines > 1:
        # `med_blocks > 1` OR THE SENTENCE CONTRADICTS ITSELF. The branch is
        # chosen from median_LINES and then filled with median_BLOCKS, two
        # independently measured numbers, so a surface measuring 2 lines and
        # 1 block printed "use ~1 short blocks with blank lines between" —
        # asking for one block and for gaps between blocks in the same
        # breath. It reached the reply prompt the moment the archive gained a
        # third tagged reply, replacing the honest "keep this as one compact
        # paragraph" guard with a coin flip on a two-sample rate.
        if style.get("blank_line_rate", 0) >= 0.5 and med_blocks > 1:
            lines.append(f"- Shape: use ~{med_blocks} short blocks with blank lines between, not one wall.")
        else:
            lines.append(f"- Shape: use ~{med_lines} short lines with one line break between; do not add blank spacer lines.")
    else:
        lines.append("- Shape: keep it tight, usually one short block.")

    emoji_b = _band(style.get("emoji_rate", 0))
    if emoji_b in ("never", "rarely"):
        lines.append("- Emoji: none (they almost never use them).")
    else:
        favs = " ".join(style.get("top_emoji") or [])
        count = int(style.get("emoji_per_post") or 1)
        howmany = "1-2" if count <= 2 else f"~{count}"
        lines.append(f"- Emoji: {emoji_b}, about {howmany} per post" + (f" (they like {favs})" if favs else "") + ".")
    if style.get("hashtag_rate", 0) < 0.15:
        lines.append("- Hashtags: none.")
    else:
        htc = int(style.get("hashtag_per_post") or 1)
        lines.append(f"- Hashtags: {_band(style['hashtag_rate'])}, {'1-2' if htc <= 2 else htc}.")
    # Images: a real habit the writer should honour by offering a visual.
    img = style.get("image_rate", 0)
    if img >= 0.5:
        lines.append(f"- Images: most of their posts include an image ({round(img*100)}%); plan to pair this with a visual.")
    elif img >= 0.25:
        lines.append(f"- Images: about {round(img*100)}% of their posts have an image; suggest one when it fits.")

    if style.get("lowercase_start_rate", 0) >= 0.5:
        case_rule = "- Case: casual, mostly lowercase."
    else:
        case_rule = "- Case: normal sentence case"
        if style.get("allcaps_emphasis_rate", 0) >= 0.15:
            case_rule += " - but you MAY put one word in ALL CAPS for emphasis, like they do"
        case_rule += "."
    lines.append(case_rule)

    punct = []
    # em-dash and semicolon are the loudest "AI wrote this" tells. State the
    # avoid-rule explicitly when the account rarely uses them — a small model
    # defaults to both unless told not to.
    if style.get("emdash_rate", 0) < 0.12:
        punct.append("do NOT use em-dashes (—); write two short sentences or use a comma instead")
    else:
        punct.append("an occasional em-dash")
    if style.get("semicolon_rate", 0) < 0.05:
        punct.append("never use semicolons")
    if style.get("colon_rate", 0) < 0.08:
        punct.append("avoid colons")
    if style.get("ellipsis_rate", 0) >= 0.12:
        punct.append("sometimes a trailing '...'")
    punct.append("almost never '!'" if style.get("exclaim_rate", 0) < 0.1 else "exclamation points are fine")
    lines.append("- Punctuation: " + ", ".join(punct) + ".")
    # THE REGISTER IS MEASURED, NOT ASSUMED (V1). This line said "sound like a
    # person firing off a quick take, not an essay" to every account, which is
    # one author's register; a long-form writer was told to stop being one.
    if median_w and median_w <= 20:
        lines.append("- Register: quick takes, the way they fire them off.")
    elif median_w >= 45:
        lines.append("- Register: they write developed posts. Keep the reasoning; "
                     "do not compress it into a one-liner.")
    lines.append("- No stock AI phrasing ('Let's be real', 'It's not just X, it's Y').")

    if style.get("long_word_rate", 0) < 0.13 and style.get("avg_word_length", 5) < 5:
        vocab = "- Words: plain and short"
    elif style.get("long_word_rate", 0) >= 0.2:
        vocab = "- Words: they use precise, technical vocabulary - don't dumb it down"
    else:
        vocab = "- Words: mostly plain, a few precise terms"
    if style.get("contraction_rate", 0) >= 0.3:
        vocab += "; use contractions (it's, don't, they're)"
    lines.append(vocab + ".")

    if style.get("mention_rate", 0) >= 0.3:
        # Never write a literal "@mention" here: the model copies the token
        # straight into drafts ("@mention Local models save money...").
        lines.append("- They usually address the person directly, by their real handle.")
    lines.append("Sound like them, not like a brand account.")
    return "\n".join(lines)


def infer_account_model(rows: list[dict[str, Any]] | None = None, *,
                        weighted: bool | None = None) -> dict[str, Any]:
    # Weight iff reading the live corpus: every caller that passes rows
    # explicitly keeps its historical, unweighted numbers.
    if weighted is None:
        weighted = rows is None
    # A deleted post must not keep teaching the voice — same stance
    # performance_records takes at the source.
    rows = rows if rows is not None else [r for r in records() if not r.get("deleted_at")]
    profile = get_account_profile()
    text_rows = [row for row in rows if row.get("text")]
    texts = [str(row.get("text") or "") for row in text_rows]
    liked_texts = archive_texts(("like",), 250)
    configured_pillars = [str(item) for item in profile.get("content_pillars", []) if str(item).strip()]
    interest_tokens = Counter(token for text in liked_texts for token in _tokens(text))

    # The recency law: voice and pillars follow the account, at the pace the
    # approach detector allows (90d half-life, 30d after a confirmed shift).
    approach_state_name = "stable"
    half_life = recency.DEFAULT_HALF_LIFE_DAYS
    style_weights: list[float] | None = None
    row_topic_weights: list[float] | None = None
    text_topic_weights: list[float] | None = None
    if weighted and text_rows:
        try:
            from .approach import approach_state  # local: approach imports this module
            state = approach_state()
            approach_state_name = str(state.get("state") or "stable")
            half_life = float(state.get("half_life_days") or recency.DEFAULT_HALF_LIFE_DAYS)
        except Exception:                            # noqa: BLE001
            pass
        style_weights = recency.voice_weights(text_rows, half_life_days=half_life)
        row_topic_weights = recency.topic_weights(rows, half_life_days=half_life)
        text_topic_weights = recency.topic_weights(text_rows, half_life_days=half_life)

    # Frequent vocabulary: weight-summed on the live path so an abandoned
    # beat fades below the floor while two recent posts' worth of evidence
    # (sum-weight >= 2.0, identical to count >= 2 when unweighted) clears it.
    if text_topic_weights is None:
        frequent = Counter(token for text in texts for token in _tokens(text)).most_common(12)
        frequent_pillars = [token for token, count in frequent[:4] if count >= 2]
    else:
        token_weight: dict[str, float] = {}
        for text, weight in zip(texts, text_topic_weights):
            for token in _tokens(text):
                token_weight[token] = token_weight.get(token, 0.0) + weight
        frequent = sorted(token_weight.items(), key=lambda kv: kv[1], reverse=True)[:12]
        frequent_pillars = [token for token, weight_sum in frequent[:4] if weight_sum >= 2.0]

    # Pillars MERGE, never replace: the base stays what it always was (the
    # user's configured pillars, else the frequent-token inference), so a
    # stable single-beat account keeps today's exact pillar list. An active
    # learned dimension that shares no vocabulary with the base is APPENDED —
    # a second beat becomes first-class instead of being blurred into one bag
    # or silently dropped.
    base_pillars = configured_pillars or frequent_pillars
    learned_labels: list[str] = []
    if weighted:
        try:
            dimensions = topic_dimensions()
        except Exception:                            # noqa: BLE001
            dimensions = []
        base_token_set = {token.rstrip("s") for pillar in base_pillars
                          for token in re.findall(r"[a-z0-9]{3,}", str(pillar).lower())}
        for dimension in dimensions:
            if not dimension.get("active"):
                continue
            label = str(dimension.get("display_label") or "").strip()
            if not label or label in learned_labels:
                continue
            # Dedupe on the LABEL, not the whole token core. A beat carries a
            # dozen tokens and one of them is usually generic, so testing the
            # full core let a single ambient word veto a real beat: on the live
            # archive the "opus anthropic" beat was suppressed because "model"
            # also appeared in the fallback pillar list. What matters is
            # whether the thing being ADDED is already said.
            label_tokens = {token.rstrip("s") for token in re.findall(r"[a-z0-9]{3,}", label.lower())}
            if base_token_set & label_tokens:
                continue
            learned_labels.append(label)
    inferred_topics = (base_pillars + learned_labels)[:6]

    word_counts = [len(re.findall(r"\S+", text)) for text in texts]
    post_types = Counter(str(row.get("post_type") or "post") for row in rows)
    total = len(texts)
    first_person_flags = [bool(re.search(r"\b(?:i|i'm|i’ve|ive|me|my|mine)\b", text, re.I)) for text in texts]
    question_flags = ["?" in text for text in texts]
    emoji_flags = [_has_emoji(text) for text in texts]
    mention_flags = [bool(re.search(r"@[A-Za-z0-9_]+", text)) for text in texts]
    link_flags = ["http://" in text or "https://" in text for text in texts]
    # Concrete style signature — the things that make a post read like THIS
    # writer: how it's shaped, punctuated, capitalised, and how plain the words
    # are. These were missing, so drafts didn't match the user's real formatting.
    style = _style_metrics(rows, texts, word_counts, total, weights=style_weights)
    format_styles: dict[str, dict[str, Any]] = {}
    for action in ("reply", "quote", "post"):
        accepted_types = {"post", "original", "poll", "thread"} if action == "post" else {action}
        selector = [str(row.get("post_type") or "post") in accepted_types for row in text_rows]
        format_rows = [row for row, keep in zip(text_rows, selector) if keep]
        format_texts = [str(row["text"]) for row in format_rows]
        format_weights = ([weight for weight, keep in zip(style_weights, selector) if keep]
                          if style_weights is not None else None)
        if format_texts:
            format_styles[action] = _style_metrics(
                format_rows, format_texts,
                [len(re.findall(r"\S+", text)) for text in format_texts], len(format_texts),
                weights=format_weights,
            )
    confidence_score = min(0.95, 0.15 + min(total, 200) / 300 + (0.18 if profile.get("audience") else 0) + (0.18 if configured_pillars else 0))
    confidence = "high" if confidence_score >= 0.75 else "moderate" if confidence_score >= 0.45 else "limited"
    audience = str(profile.get("audience") or "").strip()
    bio = str(profile.get("bio") or "").strip()
    positioning = bio or (f"Practical perspectives on {', '.join(inferred_topics[:3])}" if inferred_topics else "Positioning is not clear from the available profile and archive.")
    promise = (
        f"Useful, recognizable perspectives on {', '.join(inferred_topics[:3])} for {audience}."
        if inferred_topics and audience else
        "The account promise needs a clearer audience and repeatable topic payoff."
    )
    return {
        "handle": profile.get("handle"),
        "positioning": positioning,
        "intended_audience": audience,
        "audience": audience,
        "content_pillars": inferred_topics,
        "account_promise": promise,
        "avoidances": list(profile.get("constraints", [])),
        "recurring_beliefs": [],
        "audience_interest_signals": [token for token, count in interest_tokens.most_common(8) if count >= 2],
        "voice_profile": {
            "median_words": round(_wmedian([float(w) for w in word_counts], style_weights), 1) if word_counts else None,
            "question_rate": _wrate(question_flags, style_weights),
            "emoji_rate": _wrate(emoji_flags, style_weights),
            "mention_rate": _wrate(mention_flags, style_weights),
            "link_rate": _wrate(link_flags, style_weights),
            "first_person_rate": _wrate(first_person_flags, style_weights),
            "format_mix": ({kind: _rate(count, total) for kind, count in post_types.items()}
                           if row_topic_weights is None else
                           {kind: _wrate([str(row.get("post_type") or "post") == kind for row in rows],
                                         row_topic_weights)
                            for kind in post_types}),
            "frequent_vocabulary": [token for token, _ in frequent[:12]],
            # The concrete style signature used to write in this account's voice.
            "style": style,
            "format_styles": format_styles,
            # Self-naming (no silent behavior): how this profile was weighted.
            "recency": {
                "weighted": bool(style_weights is not None),
                "half_life_days": half_life if style_weights is not None else None,
                "floor": recency.WEIGHT_FLOOR,
                "starling_verified_factor": recency.STARLING_VERIFIED_VOICE_FACTOR,
                "approach_state": approach_state_name,
            },
        },
        "confidence": {"level": confidence, "score": round(confidence_score, 3), "post_count": total},
        "limitations": ["Performance forecasts stay qualitative until X analytics exports are imported.", "Recurring beliefs are left blank until repeated positions can be supported without guessing intent."],
    }


# --- Personal topic profile ------------------------------------------------
# WHAT the user posts about, joined with how each theme actually performed —
# "your model-comparison posts land at p78; your career posts at p31". This is
# the memory the radar and scorer use to prefer topics with proven pull for
# THIS account, always with sample sizes attached.

_CLUSTER_JOIN_COSINE = 0.55
_CLUSTER_JOIN_JACCARD = 0.2
_MIN_CLUSTER_SIZE = 3
# A dimension is ACTIVE while it carries a real slice of the account's
# recency-weighted output. Inactive dimensions stay listed (history is
# context) but stop feeding pillars.
#
# SHARE ONLY, never "posted recently too": on a dense account every cluster
# is recent, so an OR made everything active — the live archive turned a
# 4-post fringe group into the pillar "feels first" beside its real beat.
# A beat is a slice of what the account IS, and 0.15 means one part in
# six-or-fewer; a genuinely new beat crosses it as it grows (faster once
# the approach shift confirms and these weights speed up).
ACTIVE_DIMENSION_MIN_RECENCY_SHARE = 0.15
# ...or posted on inside this window. Matches approach.RECENT_WINDOW_DAYS:
# "recent" must mean the same number of days to the drift detector and to the
# beat map, or one can call a beat live while the other calls it history.
_ACTIVE_WINDOW_DAYS = 60.0
# TWO SIZES, BECAUSE THEY DO DIFFERENT JOBS. `tokens` is the discriminative
# core, sized like a configured pillar's token set because topics.score_topic
# divides by it — widening it there would quietly raise the relevance bar.
# `vocabulary` is the beat's fuller word set, used for count-based membership
# tests that have no denominator (subject.builder_fit's two-word rule). A
# 12-token core dropped "ottolenghi" from a food account's beat and rejected
# a bakery story; a 12-token denominator is still the right one for scoring.
DIMENSION_TOKEN_LIMIT = 12
DIMENSION_VOCABULARY_LIMIT = 40
_PROFILE_CACHE_TTL_S = 900.0
_PROFILE_LOCK = threading.Lock()
_PROFILE_CACHE: dict[str, Any] = {"key": None, "profile": None, "built_at": 0.0}


def _reach_percentiles(rows: list[dict[str, Any]]) -> dict[int, float]:
    """Index → reach percentile against the account's own history."""
    from .performance import normalized_objective  # local import: performance imports this module
    values: list[tuple[int, float]] = []
    for index, row in enumerate(rows):
        value = normalized_objective(row, "reach")
        if value is not None:
            values.append((index, float(value)))
    ranked = sorted(value for _, value in values)
    result: dict[int, float] = {}
    for index, value in values:
        below = sum(1 for other in ranked if other < value)
        result[index] = round(below / max(1, len(ranked)), 3)
    return result


def _cluster_label(texts: list[str]) -> str:
    counts = Counter(token for text in texts for token in _tokens(text))
    return " / ".join(token for token, _ in counts.most_common(3)) or "misc"


def _embed_rows(texts: list[str]) -> np.ndarray | None:
    try:
        from .embeddings import LocalOnnxEmbedder
        embedder = LocalOnnxEmbedder()
        if embedder.available:
            return embedder.encode(texts)
    except Exception:
        pass
    return None


def personal_topic_profile(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Cluster the user's own posted history by topic and attach honest
    per-cluster performance. Cached in-memory; rebuilt when the corpus grows."""
    from .storage import corpus_version
    # Cache check first; the corpus-wide load only happens on a miss. `len(rows)`
    # used to be in the key, so `performance_records()` ran even on a hit.
    # `corpus_version()` alone suffices: every writer that can change the row
    # count bumps it (`remove_posts_from_archive_sources` was the one exception
    # and now does too).
    cache_key = (corpus_version(),) if rows is None else None
    now = time.monotonic()
    if cache_key is not None:
        with _PROFILE_LOCK:
            cached = _PROFILE_CACHE
            if cached["profile"] is not None and cached["key"] == cache_key \
                    and now - cached["built_at"] < _PROFILE_CACHE_TTL_S:
                return cached["profile"]
    source_rows = rows if rows is not None else performance_records()
    usable = [(index, str(row.get("text") or "")) for index, row in enumerate(source_rows)
              if row.get("text") and len(str(row.get("text"))) >= 25]
    profile: dict[str, Any] = {"clusters": [], "sample_size": len(usable),
                               "note": "Themes from your own posted history with median reach percentiles "
                                       "against your own account — correlations with sample sizes, not promises."}
    if len(usable) < _MIN_CLUSTER_SIZE * 2:
        profile["note"] = "Not enough posted history to identify reliable topic themes yet."
        if cache_key is not None:
            with _PROFILE_LOCK:
                _PROFILE_CACHE.update(key=cache_key, profile=profile, built_at=now)
        return profile
    texts = [text for _, text in usable]
    embeddings = _embed_rows(texts)
    token_sets = [set(_tokens(text)) for text in texts]
    # Greedy leader clustering: cheap, deterministic, order = chronological.
    clusters: list[dict[str, Any]] = []
    for position, text in enumerate(texts):
        best, best_score = None, 0.0
        for cluster in clusters:
            if embeddings is not None:
                score = float(embeddings[position] @ cluster["centroid"] / max(1e-8, np.linalg.norm(cluster["centroid"])))
                threshold = _CLUSTER_JOIN_COSINE
            else:
                union = token_sets[position] | cluster["tokens"]
                score = len(token_sets[position] & cluster["tokens"]) / max(1, len(union))
                threshold = _CLUSTER_JOIN_JACCARD
            if score >= threshold and score > best_score:
                best, best_score = cluster, score
        if best is None:
            best = {"members": [], "tokens": set(), "centroid": np.zeros(embeddings.shape[1], dtype=np.float32) if embeddings is not None else None}
            clusters.append(best)
        best["members"].append(position)
        best["tokens"] |= token_sets[position]
        if embeddings is not None:
            best["centroid"] = best["centroid"] + embeddings[position]
    percentiles = _reach_percentiles(source_rows)
    summary = []
    for cluster in clusters:
        members = cluster["members"]
        if len(members) < _MIN_CLUSTER_SIZE:
            continue
        member_texts = [texts[position] for position in members]
        member_pcts = [percentiles[usable[position][0]] for position in members
                       if usable[position][0] in percentiles]
        entry = {
            "label": _cluster_label(member_texts),
            "size": len(members),
            "measured": len(member_pcts),
            "median_percentile": round(median(member_pcts), 3) if member_pcts else None,
            "evidence": "solid" if len(member_pcts) >= 8 else "limited" if member_pcts else "unmeasured",
            "tokens": sorted(cluster["tokens"])[:40],
        }
        if cluster.get("centroid") is not None:
            norm = float(np.linalg.norm(cluster["centroid"]))
            entry["_centroid"] = cluster["centroid"] / norm if norm > 0 else cluster["centroid"]
        summary.append(entry)
    summary.sort(key=lambda item: (item["median_percentile"] if item["median_percentile"] is not None else -1.0),
                 reverse=True)
    profile["clusters"] = summary
    if cache_key is not None:
        with _PROFILE_LOCK:
            _PROFILE_CACHE.update(key=cache_key, profile=profile, built_at=now)
    return profile


def topic_performance_for(text: str, profile: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Match a draft/trend/topic against the profile; the best matching cluster
    with measured history, or None when nothing matches confidently."""
    if not text:
        return None
    profile = profile if profile is not None else personal_topic_profile()
    clusters = [cluster for cluster in profile.get("clusters", []) if cluster.get("median_percentile") is not None]
    if not clusters:
        return None
    query_embedding = None
    if any("_centroid" in cluster for cluster in clusters):
        embedded = _embed_rows([text])
        query_embedding = embedded[0] if embedded is not None else None
    query_tokens = set(_tokens(text))
    best, best_score = None, 0.0
    for cluster in clusters:
        candidates = []
        if query_embedding is not None and "_centroid" in cluster:
            candidates.append((float(query_embedding @ cluster["_centroid"]), _CLUSTER_JOIN_COSINE))
        # Containment, not Jaccard: a 2-token trend name against a whole
        # cluster vocabulary should still match when both tokens land. Kept as
        # a fallback signal even with embeddings — short queries carry too
        # little text for a reliable cosine.
        overlap = len(query_tokens & set(cluster.get("tokens", [])))
        candidates.append((overlap / max(1, min(len(query_tokens), 12)), 0.34))
        for score, threshold in candidates:
            if score >= threshold and score > best_score:
                best, best_score = cluster, score
    if best is None:
        return None
    return {"label": best["label"], "median_percentile": best["median_percentile"],
            "size": best["size"], "measured": best["measured"], "evidence": best["evidence"],
            "match_score": round(best_score, 3)}


# Dimension grouping is TOKEN components, not the embedding clusters above.
# Measured 2026-07-31: the greedy 0.55-cosine clustering yields ONE cluster of
# 5 posts on the live 46-post archive — the hashed 96-dim embedder is a
# precision instrument for "does this trend resemble a measured theme", not a
# recall instrument for "what are this account's beats". Two posts belong to
# the same beat when they share >= 2 non-ubiquitous meaningful words — the
# exact standard topics.self_repetition already uses for "a real topical
# match" — and a token carried by more than half the posts ("model" on an AI
# account) is ambient vocabulary that cannot testify two posts share a beat.
_DIMENSION_JOIN_TOKENS = 2
_UBIQUITY_CAP = 0.5
# "Ubiquitous" needs a real sample behind it: in a 6-post corpus every beat
# token is in >50% of posts BY DEFINITION, and excluding them dissolves the
# only beat the account has. A token must be seen this many times before the
# share cap can call it ambient.
_UBIQUITY_MIN_SIGHTINGS = 8.0
_DIMENSION_MAX_ROWS = 500
_DIMENSION_CACHE_TTL_S = 900.0
_DIMENSION_LOCK = threading.Lock()
_DIMENSION_CACHE: dict[str, Any] = {"key": None, "dimensions": None, "built_at": 0.0}


def topic_dimensions(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The account's topic beats as first-class dimensions.

    His requirement: "I might talk about multiple topics — make sure we have
    a multi-dimension of topics embedded in." Each beat carries its share of
    the account (all-time and recency-weighted) and its token core, so
    relevance and standing can be judged against EACH beat instead of one
    blurred bag. Cold start (nothing groups yet) returns [] and every
    consumer falls back to today's exact behavior.
    """
    live = rows is None
    if live:
        from .storage import corpus_version
        # Cache check first; the corpus-wide load only happens on a miss.
        cache_key = (corpus_version(),)
        now_monotonic = time.monotonic()
        with _DIMENSION_LOCK:
            cached = _DIMENSION_CACHE
            if (cached["dimensions"] is not None and cached["key"] == cache_key
                    and now_monotonic - cached["built_at"] < _DIMENSION_CACHE_TTL_S):
                return cached["dimensions"]
        rows = performance_records()
    dimensions = _build_dimensions(rows)
    if live:
        with _DIMENSION_LOCK:
            _DIMENSION_CACHE.update(key=cache_key, dimensions=dimensions,
                                    built_at=time.monotonic())
    return dimensions


def _build_dimensions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = rows[-_DIMENSION_MAX_ROWS:]  # records() orders oldest-first
    usable = [(index, str(row.get("text") or "")) for index, row in enumerate(rows)
              if row.get("text") and len(str(row.get("text"))) >= 25]
    if len(usable) < _MIN_CLUSTER_SIZE:
        return []
    token_sets = [set(_tokens(text)) for _, text in usable]
    document_frequency = Counter(token for tokens in token_sets for token in tokens)
    ubiquity_cap = max(_UBIQUITY_CAP * len(usable), _UBIQUITY_MIN_SIGHTINGS)
    specific_sets = [{token for token in tokens if document_frequency[token] <= ubiquity_cap}
                     for tokens in token_sets]

    parent = list(range(len(usable)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            if len(specific_sets[i] & specific_sets[j]) >= _DIMENSION_JOIN_TOKENS:
                root_i, root_j = find(i), find(j)
                if root_i != root_j:
                    parent[root_j] = root_i
    components: dict[int, list[int]] = {}
    for position in range(len(usable)):
        components.setdefault(find(position), []).append(position)
    groups = [positions for positions in components.values()
              if len(positions) >= _MIN_CLUSTER_SIZE]
    if not groups:
        return []

    # Shares follow the same clock the voice does: after a confirmed shift a
    # new beat's slice rises at the 30-day half-life instead of the 90.
    try:
        from .approach import current_half_life
        half_life = current_half_life()
    except Exception:                                    # noqa: BLE001
        half_life = recency.DEFAULT_HALF_LIFE_DAYS
    row_weights = recency.topic_weights(rows, half_life_days=half_life)
    usable_weight_total = sum(row_weights[index] for index, _ in usable) or 1.0
    percentiles = _reach_percentiles(rows)
    dimensions: list[dict[str, Any]] = []
    for positions in groups:
        indices = [usable[position][0] for position in positions]
        member_texts = [usable[position][1] for position in positions]
        member_weight = sum(row_weights[index] for index in indices)
        member_when = [when for index in indices
                       if (when := recency.row_when(rows[index])) is not None]
        member_pcts = [percentiles[index] for index in indices if index in percentiles]
        token_weight: dict[str, float] = {}
        for position, index in zip(positions, indices):
            for token in token_sets[position]:
                token_weight[token] = token_weight.get(token, 0.0) + row_weights[index]
        # Occurrence counts break weight ties: a token used twice per post
        # ("tools") must outrank one mentioned once, or the display label
        # degrades to alphabetical noise.
        occurrence = Counter(token for text in member_texts for token in _tokens(text))
        ranked = sorted(token_weight.items(),
                        key=lambda kv: (-kv[1], -occurrence[kv[0]], kv[0]))
        last_active = max(member_when) if member_when else None
        recency_share = member_weight / usable_weight_total
        # A BEAT HE POSTED ON TODAY IS ACTIVE, whatever its share. Measured on
        # the live archive 2026-07-31: his one real beat (opus / anthropic /
        # fable / kimi / openai / codex) carries 28 of 498 recent posts — a
        # 0.062 share, because the rest of his timeline is one-off short
        # personal tweets that form no theme. A share-only rule marked that
        # beat inactive and every consumer (pillar merge, interests,
        # builder_fit's dimension admit, the topic-selection groups) fell
        # straight back to pre-dimension behaviour: the whole multi-topic
        # feature was inert on the only account it had ever run against. The
        # share test is what retires an ABANDONED beat; recency is what keeps
        # a live one — an account with six beats has none above 0.15.
        recently_active = bool(
            last_active
            and (_utc_now() - last_active).total_seconds() / 86400.0 <= _ACTIVE_WINDOW_DAYS)
        dimensions.append({
            "label": _cluster_label(member_texts),
            # Top-2 weighted tokens: the human-readable name a pillar list or
            # interests() can carry ("sourdough bread", not "bread / crumb / x").
            "display_label": " ".join(token for token, _ in ranked[:2]),
            "tokens": [token for token, _ in ranked[:DIMENSION_TOKEN_LIMIT]],
            "vocabulary": [token for token, _ in ranked[:DIMENSION_VOCABULARY_LIMIT]],
            "size": len(positions),
            "share": round(len(positions) / max(1, len(usable)), 3),
            "recency_share": round(recency_share, 3),
            "last_active": last_active.isoformat() if last_active else None,
            "median_percentile": round(median(member_pcts), 3) if member_pcts else None,
            "active": bool(recency_share >= ACTIVE_DIMENSION_MIN_RECENCY_SHARE
                           or recently_active),
        })
    dimensions.sort(key=lambda dim: (not dim["active"], -(dim["recency_share"] or 0.0)))
    # LANE-NAMING THIS LABEL WAS BUILT AND THEN CUT (R-SCOPE, 2026-08-21), deliberately and
    # not by omission. `lane.label_for_texts` exists and is tested; the reason it is not
    # called here is that it would have been INERT on the surface that justified it. The wait
    # card reads `subject.interests()`, and `interests()` now answers from the measured lane
    # split before it ever reaches a dimension label - verified: his account returns
    # ['frontier models', 'vibe coding'] at step 2, so step 3 never runs. Naming the label
    # would have improved a field no user-facing path reads on this account, at the cost of a
    # new code path landing the hour before a certification freeze.
    #
    # If a future account DOES fall through to the dimension path and shows token noise on
    # the card, the piece to add back is a call to `lane.label_for_texts(member_texts)` here,
    # guarded so only the highest-recency dimension per lane may take a lane name -
    # `subject._dimension_tokens` looks dimensions up BY `display_label`, so two dimensions
    # sharing a name silently return the wrong token set.
    return dimensions


def reset_topic_dimension_cache() -> None:
    """Test hook."""
    with _DIMENSION_LOCK:
        _DIMENSION_CACHE.update(key=None, dimensions=None, built_at=0.0)


def reset_topic_profile_cache() -> None:
    """Test hook."""
    with _PROFILE_LOCK:
        _PROFILE_CACHE.update(key=None, profile=None, built_at=0.0)


def growth_diagnoses(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = rows if rows is not None else records()
    model = infer_account_model(rows)
    texts = [str(row.get("text") or "") for row in rows if row.get("text")]
    total = len(texts)
    diagnoses: list[dict[str, Any]] = []

    def add(pattern: str, contributors: list[str], strength: str, objective: str, test: str) -> None:
        diagnoses.append({
            "observed_pattern": pattern,
            "likely_contributors": contributors,
            "evidence_strength": strength,
            "affected_objective": objective,
            "proposed_test": test,
        })

    if not model["intended_audience"] or not model["content_pillars"]:
        add(
            "The account's positioning is not yet specific enough to judge audience fit consistently.",
            ["The supplied audience or content pillars are missing."],
            "strong",
            "followers",
            "Define one intended audience and three durable pillars, then classify the next ten posts against them.",
        )
    if total >= 20:
        first_person_rate = float(model["voice_profile"]["first_person_rate"])
        if first_person_rate > 0.55:
            add(
                "A large share of posts is framed around the author rather than the reader's payoff.",
                [f"First-person language appears in {first_person_rate:.0%} of {total} posts."],
                "moderate",
                "followers",
                "For five comparable posts, keep the same ideas but state the audience payoff in the opening.",
            )
        mix = model["voice_profile"]["format_mix"]
        if mix and max(mix.values()) > 0.85:
            dominant = max(mix, key=mix.get)
            add(
                "The archive relies heavily on one format.",
                [f"{dominant.title()} posts make up {mix[dominant]:.0%} of the available history."],
                "strong",
                "reach",
                "Run one controlled format test while holding topic, opening strength, and timing as stable as possible.",
            )

    reply_rows = [row for row in rows if row.get("post_type") == "reply" and row.get("parent_text")]
    if len(reply_rows) >= 10:
        overlaps = []
        for row in reply_rows:
            own, parent = set(_tokens(str(row.get("text") or ""))), set(_tokens(str(row.get("parent_text") or "")))
            overlaps.append(len(own & parent) / max(1, len(own | parent)))
        average = sum(overlaps) / len(overlaps)
        if average < 0.06:
            add(
                "Replies often have little topical overlap with the visible parent context.",
                [f"Lexical overlap averages {average:.1%} across {len(reply_rows)} replies with parent text."],
                "limited",
                "conversation",
                "Test replies that name the exact claim being extended before adding a distinct point.",
            )
    return diagnoses
