"""Who actually makes what — so a draft never hands a model to the wrong lab.

Two drafts generated live on 2026-07-24 said "Anthropic's Claude is finally
catching up to Fable" and "Anthropic's pricing for Opus 5 seems high compared
to OpenAI's Claude 3". Both are confident nonsense: Fable and Claude 3 are
Anthropic's own. Nothing caught them, because the claim gate checks NUMBERS
against evidence and these sentences contain none — the error is in who owns
what, which no amount of number-checking sees.

On an account whose entire beat is comparing labs, getting the lab wrong is
the most damaging mistake available: it is instantly visible to exactly the
readers he wants, and it cannot be defended as opinion.

This is deliberately small and certain. It knows only the products the account
actually writes about, and it only fires on an EXPLICIT ownership claim
("OpenAI's Claude", "Claude from OpenAI", "Google released Grok"). Comparisons
across labs — "Opus 5 vs Fable", "cheaper than GPT-5" — are the account's
bread and butter and must pass untouched.
"""
from __future__ import annotations

import re

# Product family -> the lab that makes it. Families only: version numbers move
# too fast to encode, and "Opus 5" matches on "opus".
PRODUCT_VENDOR: dict[str, str] = {
    "claude": "Anthropic",
    "opus": "Anthropic",
    "sonnet": "Anthropic",
    "haiku": "Anthropic",
    "fable": "Anthropic",
    "gpt": "OpenAI",
    "chatgpt": "OpenAI",
    "codex": "OpenAI",
    "sora": "OpenAI",
    "dall-e": "OpenAI",
    "gemini": "Google",
    "bard": "Google",
    "grok": "xAI",
    "llama": "Meta",
    "mistral": "Mistral",
    "mixtral": "Mistral",
    "deepseek": "DeepSeek",
    "kimi": "Moonshot",
    "qwen": "Alibaba",
}

# Lab names as they appear in writing, mapped to the canonical name above.
VENDOR_ALIASES: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "open ai": "OpenAI",
    "google": "Google",
    "deepmind": "Google",
    "xai": "xAI",
    "x.ai": "xAI",
    "meta": "Meta",
    "mistral": "Mistral",
    "deepseek": "DeepSeek",
    "moonshot": "Moonshot",
    "alibaba": "Alibaba",
}

_VENDORS = "|".join(sorted((re.escape(name) for name in VENDOR_ALIASES), key=len, reverse=True))
_PRODUCTS = "|".join(sorted((re.escape(name) for name in PRODUCT_VENDOR), key=len, reverse=True))

# Only forms that ASSERT ownership. Ordered vendor-first / product-first.
_CLAIMS: tuple[re.Pattern[str], ...] = (
    # "OpenAI's Claude 3", "Anthropic's GPT-5"
    re.compile(rf"\b({_VENDORS})['’]s\s+(?:new\s+|latest\s+|own\s+)?({_PRODUCTS})\b", re.I),
    # "Claude from OpenAI"
    re.compile(rf"\b({_PRODUCTS})\b[^.!?\n]{{0,12}}\bfrom\s+({_VENDORS})\b", re.I),
    # "Google released Grok", "Meta just launched Claude"
    re.compile(rf"\b({_VENDORS})\b\s+(?:just\s+|recently\s+)?"
               rf"(?:released|launched|shipped|announced|dropped|unveiled|makes|builds)\s+"
               rf"(?:the\s+|its\s+|their\s+|a\s+new\s+)?({_PRODUCTS})\b", re.I),
)
# Which capture group holds the product, per pattern above.
_PRODUCT_GROUP = (2, 1, 2)


def misattributions(text: str) -> list[tuple[str, str, str]]:
    """Ownership claims that name the wrong lab.

    Returns (claimed_vendor, product, true_vendor) for each. An empty list
    means the text makes no false ownership claim — which includes the common
    case of making no ownership claim at all.
    """
    if not text:
        return []
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern, product_group in zip(_CLAIMS, _PRODUCT_GROUP):
        vendor_group = 1 if product_group == 2 else 2
        for match in pattern.finditer(text):
            product = match.group(product_group).lower()
            claimed = VENDOR_ALIASES.get(match.group(vendor_group).lower())
            true_vendor = PRODUCT_VENDOR.get(product)
            if not claimed or not true_vendor or claimed == true_vendor:
                continue
            key = (claimed, product)
            if key not in seen:
                seen.add(key)
                found.append((claimed, product, true_vendor))
    return found


# A lab losing a race to its own model. "Anthropic's Claude is finally catching
# up to Fable" reads as analysis and is incoherent: Fable is Anthropic's. This
# is deliberately narrower than the comparisons he makes constantly — "Opus 5
# vs Fable 5" pits two PRODUCTS against each other and is exactly his beat, so
# the rule fires only when a LAB NAME is on one side and its own product on the
# other, with no rival lab named to explain it.
_RIVALRY = re.compile(
    rf"\b(?P<vendor>{_VENDORS})\b(?P<mid>[^.!?\n]{{0,60}}?)\s"
    rf"(?:catching up (?:to|with)|behind|trails?|racing (?:to beat|against)|"
    rf"competing with|losing to|can'?t keep up with|falling behind)\s+"
    rf"(?:the\s+)?(?P<product>{_PRODUCTS})\b", re.I)


def internal_rivalry(text: str) -> list[tuple[str, str]]:
    """Sentences that pit a lab against a model it makes. (vendor, product)."""
    if not text:
        return []
    found: list[tuple[str, str]] = []
    for match in _RIVALRY.finditer(text):
        vendor = VENDOR_ALIASES.get(match.group("vendor").lower())
        product = match.group("product").lower()
        if not vendor or PRODUCT_VENDOR.get(product) != vendor:
            continue
        # A rival lab named in between explains the sentence — leave it alone.
        if re.search(rf"\b({_VENDORS})\b", match.group("mid"), re.I):
            continue
        found.append((vendor, product))
    return found


def describe(text: str) -> str | None:
    """A sentence a human can act on, or None when the text is clean."""
    wrong = misattributions(text)
    if not wrong:
        rivalry = internal_rivalry(text)
        if not rivalry:
            return None
        vendor, product = rivalry[0]
        return (f"This has {vendor} losing ground to {product.title()} — but "
                f"{product.title()} is {vendor}'s own model, so the sentence "
                f"does not hold together.")
    claimed, product, true_vendor = wrong[0]
    article = product.title() if product not in {"gpt"} else product.upper()
    return (f"This calls {article} a {claimed} model. {article} is {true_vendor}'s. "
            f"Getting the lab wrong is the one mistake this audience always catches.")
