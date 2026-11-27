"""Ground generative chat answers in the account's OWN observed signal.

The residual hallucinations after routing were all in free-form generation:
the model invented company attributions the account never mentions - "Microsoft's
Opus" (Opus is Anthropic's), "Moonshot AI's Kimi", "Claude developed by
Microsoft", "Meta's breakthrough decoding brain signals". Routing can't catch
these because they appear inside otherwise-reasonable prose.

The rule: an answer may only name a company/lab that appears in the account's
own signal - its niche pillars, its own posts, and the trends Radar observed.
A company outside that set is ungrounded, and the sentence naming it is removed.
This is a set-membership check against local data, so it adds no model call and
keeps the speed. It deliberately does NOT try to verify every fact - it targets
the one pattern that produced the confirmed, damaging hallucinations: naming an
outside company as if it were part of the account's world.
"""
from __future__ import annotations

import re

# Companies/labs we can DETECT by name. Whether each is ALLOWED depends on the
# account's own grounding set - the user's niche already includes OpenAI, Google,
# xAI, Anthropic, so those pass; Microsoft/Meta/Moonshot do not unless the
# account actually posts about them.
_KNOWN_COMPANIES = {
    "microsoft", "google", "meta", "amazon", "apple", "moonshot", "baidu",
    "alibaba", "tencent", "bytedance", "mistral", "cohere", "nvidia", "openai",
    "anthropic", "xai", "deepmind", "huggingface", "stability", "databricks",
    "perplexity", "inflection", "ibm", "oracle", "salesforce", "adobe",
    "samsung", "huawei", "nvidia", "intel", "qualcomm", "reka", "midjourney",
}

# Split on anything non-alphanumeric so "Microsoft's" -> {"microsoft", "s"} and
# "Moonshot-AI" -> {"moonshot", "ai"}. Keeping the apostrophe in the token made
# "meta's" not match "meta", so possessive company names slipped the guard.
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def grounding_set(*, pillars: list[str] | None = None,
                  own_texts: list[str] | None = None,
                  trend_names: list[str] | None = None) -> set[str]:
    """Every token the account has legitimately touched: pillars + posts + trends."""
    tokens: set[str] = set()
    for source in (pillars or []):
        tokens |= _tokens(str(source))
    for text in (own_texts or []):
        tokens |= _tokens(str(text))
    for name in (trend_names or []):
        tokens |= _tokens(str(name))
    return tokens


def ungrounded_companies(text: str, grounding: set[str]) -> set[str]:
    """Named companies in the answer that are NOT part of the account's signal."""
    named = _tokens(text) & _KNOWN_COMPANIES
    return {company for company in named if company not in grounding}


def ground_answer(text: str, grounding: set[str]) -> tuple[str, list[str]]:
    """Remove sentences that name an outside company. Returns (text, removed).

    Sentence-level so a single ungrounded name does not delete a whole useful
    answer - only the claim that carries it.
    """
    if not text:
        return text, []
    bad = ungrounded_companies(text, grounding)
    if not bad:
        return text, []
    kept, removed = [], []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if _tokens(sentence) & bad:
            removed.append(sentence)
        else:
            kept.append(sentence)
    return " ".join(kept).strip(), removed


def allowed_entities(grounding: set[str], limit: int = 24) -> list[str]:
    """The companies/labs the account may name, for steering the prompt upfront."""
    return sorted(grounding & _KNOWN_COMPANIES)[:limit]
