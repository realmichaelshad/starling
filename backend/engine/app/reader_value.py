"""Why would anyone read THIS instead of scrolling past?

The whole product exists to earn views and followers, and nothing in the
pipeline ever asked the reader's question. The scorer measured fit ("does this
look like the account"), voice, format and novelty — a draft could score well
by being perfectly in-voice and completely pointless. The one term aimed at
conversion was a word count worth 1 point out of 100.

Published 2026 ranking mechanics say where the follow actually comes from: a
reply that adds real value on a bigger account's post earns a PROFILE CLICK,
and the profile click is the step immediately before a follow. So the question
is not "is this engaging" (unmeasurable) but the concrete one a reader asks in
half a second:

    do I learn something, get a stake, or meet a position worth arguing with?

Everything here is text-measurable and explainable in one sentence, because a
score he cannot interrogate is a score he will not trust — and because a
number without a reason is what got us the slop in the first place.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# The signals a reader actually responds to
# --------------------------------------------------------------------------- #

# A concrete thing: a number with a unit, a version, a price, a measured claim.
# These are what make a post quotable — the reader can carry it away.
_CONCRETE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:%|x|GB|TB|MB|ms|s\b|k\b|K\b|M\b|B\b|tokens?|"
    r"hours?|mins?|minutes?|days?|weeks?|months?|years?|USD|\$)"
    r"|\$\s?\d|\b\d+(?:\.\d+)?\s?(?:per|/)\s?\w+"
    # A DATE IS A CARRYABLE SPECIFIC, and on this account it is the whole
    # product: "GPT-5.6 is expected on July 7" and "my Opus 5 prediction:
    # July 30" are checkable claims a reader can hold him to. Measured against
    # his archive, counting only units and currency scored fifteen of his own
    # published posts below the bar — nearly all of them dated calls.
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b(?:today|tomorrow|tonight|this (?:week|month|morning|evening)|"
    r"next (?:week|month)|by (?:the end of )?(?:the )?(?:week|month|year))\b",
    re.IGNORECASE)

# Consequence: what CHANGES for the reader. A fact with no consequence is
# trivia; a fact with one is news they can act on.
_STAKE_RE = re.compile(
    r"\b(means?|so you can|you can now|no longer|stops? working|breaks?|"
    r"costs?|saves?|cheaper|faster|slower|worse|better than|instead of|"
    r"which is why|the catch|the tradeoff|trade-?off|downside|risk|"
    r"if you|when you|before you|watch out|careful|"
    # CHANGE VERBS. A fixed vocabulary of consequence words missed "the
    # build-vs-buy maths FLIPS for small teams" — a real economic stake stated
    # in ordinary English — and rejected a good quote. An assertion that
    # something changes IS the stake, whatever verb carries it.
    r"flips?|shifts?|changes?|beats?|kills?|doubles?|halves?|wipes? out|"
    r"the part that matters|matters more|makes? it|turns? \w+ into|"
    # HIS OWN MONEY WHERE HIS MOUTH IS. "If this is true, I'm switching to
    # GPT-5.6" is the strongest stake a post can carry — he is telling the
    # reader what HE will do about it — and it registered as no stake at all.
    r"i'?m (?:switching|moving|cancelling|canceling|sticking|staying|done)|"
    r"i (?:switched|moved|cancelled|canceled|dropped|ditched)|"
    r"i'?d take|i'?ll take|worth (?:it|paying|switching))\b",
    re.IGNORECASE)

# BEING FIRST WITH A CHECKABLE STATUS. This account is built on reporting that
# something shipped, leaked, or moved before it is common knowledge: "GPT-5.6
# is already showing up for select users", "Grok 4.5 is now live for Heavy
# users", "Polymarket just nuked the odds". None of the signals above see it —
# there is no number, no consequence verb, no opinion — so fifteen of his own
# posts scored as having no reason to read. A verifiable status claim about a
# named thing IS the reason to read: the reader learns something they did not
# know, and can check it.
_SCOOP_RE = re.compile(
    r"\b(?:is (?:now )?(?:live|out|available|rolling out|shipping)|"
    r"just (?:announced|launched|shipped|dropped|released|nuked|added)|"
    r"(?:is|are) (?:already )?showing up|spotted in|appeared in|"
    r"entered (?:private )?(?:testing|beta)|has (?:started|begun) rolling|"
    r"quietly (?:added|shipped|updated|released)|"
    r"(?:expected|slated|due|set) (?:on|for|to launch))\b", re.IGNORECASE)

# FIRST-HAND OBSERVATION. "OpenAI's own AI just told me the official OpenAI
# account was fake" is the most valuable thing a small account can post: nobody
# else has it, and it cannot be fabricated by a reader. It carries no number, no
# consequence verb and no launch status, so every signal above missed it.
# Deliberately restricted to WITNESSING something ("it told me", "in my
# testing", "turns out") rather than holding a view — "I think this is huge"
# must still score as nothing, because it is.
_FIRSTHAND_RE = re.compile(
    r"\b(?:just told me|told me|showed me|i (?:just )?(?:saw|noticed|tested|tried|"
    r"ran|measured|hit|got)|in my (?:testing|experience|setup)|turns out|"
    r"happened to me|on my (?:box|machine|rig|account))\b", re.IGNORECASE)

# A position someone could disagree with — the thing that starts a reply chain.
_POSITION_RE = re.compile(
    r"\b(i think|i'd|i would|my bet|calling it|prediction|wrong|disagree|"
    r"overrated|underrated|actually|the real|nobody|everyone|most people|"
    r"unpopular|hot take|honestly|not convinced|doubt|bet|betting|"
    # "One thing I'm betting on" is as clear a position as any, and the
    # pattern knew "bet" but not "betting" — so a post with an explicit
    # wager in it read as having no view at all.
    r"i'?m calling|my read|i reckon|watch for|"
    # THE EVALUATIVE VERDICT, which is how this account actually takes a
    # position and was the one shape missing. "beating Fable 5 and GPT-5.6 Sol
    # already is kinda insane" and "this is the first time the 'Jarvis'
    # comparison feels legit" are as committed as writing gets, and both read
    # as having no view at all.
    #
    # Measured 2026-07-26 on his 23 short texts against the 26 baseline slop
    # drafts: the terms below fire on 4 of his and ZERO of the slop. The rest
    # of this pattern, on its own, fires 5 and 5 — no separation whatsoever —
    # so this is the part that actually distinguishes his voice.
    r"insane|wild|legit|nuts|brutal|absurd|ridiculous|bonkers|unreal|"
    r"overkill|underwhelming|gotta be|no way)\b", re.IGNORECASE)

# A MECHANISM or a correction: "the failure mode is state handling, NOT model
# quality". No number and no proper noun, but it is the most useful shape a
# post can have — it tells the reader how something actually works. An earlier
# version demanded digits or capitals and rejected exactly this.
_MECHANISM_RE = re.compile(
    # "X, not Y" in any grammar. An earlier version required a copula ("is X,
    # not Y") and so missed "agents fail on state handling, not model quality"
    # — the same correction, one verb different.
    r",\s*not\s+\w"
    r"|\b(?:is|are|was|were|it'?s)\s+[\w\s]{3,40}?\s+not\s+\w"
    r"|\brather than\b|\binstead of\b|\bthe reason\b|\bwhat actually\b"
    r"|\bcomes down to\b|\bboils down to\b|\bfailure mode\b|\bbottleneck\b"
    r"|\bthe catch\b|\bunder the hood\b|\bin practice\b", re.IGNORECASE)

# Tension: the shape of a surprise ("X, but Y" / "everyone says X, actually Y").
_TENSION_RE = re.compile(
    r"\b(but|however|yet|though|although|except|until|turns out|"
    r"in practice|on paper)\b", re.IGNORECASE)

# Specific named things: products, models, companies, versions. Position must
# NOT decide this — an earlier version ignored sentence-initial capitals and so
# scored "Opus 5 costs half what 4.5 did, but the rate limit is the catch" as
# naming nothing, rejecting a genuinely good post. Instead, exclude the common
# words that merely happen to open a sentence.
_NAMED_RE = re.compile(r"\b[A-Z][A-Za-z0-9.\-]{2,}\b")
_VERSION_RE = re.compile(r"\b[A-Za-z]+[- ]?\d+(?:\.\d+)?\b|\b\d+\.\d+\b")
# Sentence openers that are not names, however they are capitalised.
_NOT_A_NAME = frozenset("""
the this that these those there here it its they them we you your our their and
but for with from when what why how who all any some most more much many just
now today tomorrow yesterday local cloud people everyone someone something
nothing everything nobody anyone honestly actually really very still even also
one two three first last next new old good bad best worst big small real
""".split())

# Openers and fillers that signal "nothing here" to a scrolling reader.
#
# WHOLE PHRASES ONLY, and never a bare common word. This list once contained
# "this", matched as a substring, so every draft containing the word "this"
# silently lost points — it rejected every quote candidate in a live run. And
# "is here" is ordinary English ("Opus 5 is here" is the news itself), not
# filler. A false positive here is invisible: the draft just quietly dies.
_SCROLL_PAST = (
    "it's interesting", "interesting to see", "this is huge", "big if true",
    "we'll see", "time will tell", "thoughts?", "what do you think",
    "game changer", "changes everything",
    "excited to see", "can't wait", "love this", "so true",
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def reader_value(text: str, action: str = "post") -> dict:
    """Score and EXPLAIN a draft's reason to exist, from the reader's side.

    Returns {score 0..1, reason, signals, missing} where `reason` is the plain
    sentence he can check: "A builder learns the memory-bandwidth catch", or
    "Nothing — this is a reaction anyone could have posted."
    """
    body = " ".join(str(text or "").split())
    if not body:
        return {"score": 0.0, "reason": "There is nothing here.",
                "signals": {}, "missing": ["everything"]}
    lowered = body.lower()
    words = body.split()

    concrete = len(_CONCRETE_RE.findall(body))
    named_tokens = {match.group(0) for match in _NAMED_RE.finditer(body)
                    if match.group(0).lower() not in _NOT_A_NAME}
    named_tokens |= {match.group(0) for match in _VERSION_RE.finditer(body)
                     if any(char.isdigit() for char in match.group(0))
                     and match.group(0).split()[0].lower() not in _NOT_A_NAME}
    named = len(named_tokens)
    stake = bool(_STAKE_RE.search(body))
    position = bool(_POSITION_RE.search(body))
    tension = bool(_TENSION_RE.search(body))
    mechanism = bool(_MECHANISM_RE.search(body))
    scoop = bool(_SCOOP_RE.search(body))
    firsthand = bool(_FIRSTHAND_RE.search(body))
    filler = sum(1 for phrase in _SCROLL_PAST if phrase in lowered)

    signals = {
        "concrete_facts": concrete,
        "named_things": named,
        "has_stake": stake,
        "has_position": position,
        "has_tension": tension,
        "has_mechanism": mechanism,
        "has_scoop": scoop,
        "has_firsthand": firsthand,
        "filler_phrases": filler,
    }

    # Weights reflect what the reader actually stops for: something they can
    # carry away (a fact tied to a consequence) outranks a bare opinion, and a
    # bare opinion still beats a reaction with nothing in it.
    score = 0.0
    score += min(concrete, 2) * 0.18       # up to 0.36 — carryable specifics
    score += min(named, 3) * 0.07          # up to 0.21 — about real things
    score += 0.22 if stake else 0.0        # what changes for the reader
    score += 0.14 if position else 0.0     # something to argue with
    score += 0.07 if tension else 0.0      # a surprise worth a second read
    # A correction or mechanism ('X, not Y' / 'the bottleneck is…') teaches
    # the reader something on its own, so it can carry a post by itself.
    score += 0.26 if mechanism else 0.0
    # A checkable status about a named thing tells the reader something they
    # did not know. Weighted below a mechanism (which explains HOW something
    # works) but above a bare opinion, and it only pays when a named thing
    # is present — "it's live!" about nothing in particular is not news.
    score += 0.20 if (scoop and named >= 1) else 0.0
    # Something only he could report. Weighted like a scoop because that is
    # what it is — a scoop he witnessed rather than one he heard.
    score += 0.20 if firsthand else 0.0
    score -= filler * 0.15                 # the scroll-past tax
    # A reply's job is smaller (one sharp point in someone else's thread), so
    # judging it against a standalone post's bar would reject good replies. A
    # QUOTE borrows context the same way — the reader sees the quoted post
    # right there — and judging it as a standalone killed every quote candidate
    # in a live run, leaving the user with 38 seconds and no draft.
    if action in ("reply", "quote") and len(words) <= 30:
        score += 0.08
    # A STANDALONE POST CARRIES ITS OWN WEIGHT. A reply borrows the parent's
    # context; a post arrives cold in a stranger's feed. "Local models save
    # money compared to cloud costs. Cloud expenses grow quickly." scraped a
    # pass on consequence words alone while naming nothing and measuring
    # nothing — the exact generic post the user keeps rejecting. Without a
    # single concrete fact or named thing, a post has no business shipping.
    # A RECITATION IS NOT A POST. He flagged this one as unclear about what it
    # brings to the table:
    #
    #   "Grok 4.5 ships with token efficiency as its main claim. Anthropic
    #    launched Opus 5, which outperforms Fable 5 on benchmarks and costs
    #    less. Opus 5 came out two months after Opus 4.8, just a month after
    #    Mythos 5, Fable 5, and Sonnet 5."
    #
    # Twelve named things, five product names, and not one claim: no position,
    # no mechanism, no scoop, no measurement. It scored 0.43 and shipped,
    # because naming things is worth points and this named a great many.
    # Length matters to the rule — a SHORT line full of names is usually a
    # punchy call ("at this rate Grok 4.5 might launch before Opus 5"), while a
    # long one that never commits to anything is a list. Measured against his
    # archive: catches both drafts he rejected, flags none of his 34 posts.
    if (len(words) >= 25 and named >= 6 and concrete == 0
            and not position and not mechanism and not scoop
            and not firsthand and not tension):
        signals["recitation"] = True
        score = min(score, WEAK_THRESHOLD - 0.01)
    if (action == "post" and concrete == 0 and named < 2
            and not mechanism and not scoop and not firsthand):
        score = min(score, WEAK_THRESHOLD - 0.01)
    score = max(0.0, min(1.0, score))

    missing: list[str] = []
    if not concrete and not named:
        missing.append("anything specific — no numbers, no named things")
    if not stake:
        missing.append("what it changes for the reader")
    if not position and not tension and not mechanism:
        missing.append("a position worth arguing with")
    if filler:
        missing.append("filler that signals 'nothing here'")

    return {"score": round(score, 3), "reason": _reason(signals, score, body),
            "signals": signals, "missing": missing}


def _reason(signals: dict, score: float, body: str) -> str:
    """One sentence: what the reader gets. Honest when the answer is nothing."""
    if score < 0.25:
        if signals["filler_phrases"]:
            return ("Nothing — it is a reaction anyone could have posted, "
                    "in words that tell the reader to keep scrolling.")
        return "Nothing a stranger can use — no specifics, no stake, no position."
    gets = []
    if signals["concrete_facts"]:
        gets.append("a concrete number they can quote")
    if signals["named_things"] >= 2:
        gets.append("named things they can look up")
    if signals["has_stake"]:
        gets.append("what it changes for them")
    if signals["has_position"]:
        gets.append("a position they can agree or argue with")
    if signals["has_tension"]:
        gets.append("a twist they did not expect")
    if signals.get("has_mechanism"):
        gets.append("how the thing actually works")
    if not gets:
        return "Not much — it reads as an observation rather than a payoff."
    return "The reader gets " + ", ".join(gets[:3]) + "."


# Below this a draft is not worth the account's distribution: a reader has no
# reason to stop, and the post spends credibility to say nothing. Deliberately
# not a high bar — the goal is to catch emptiness, not to enforce a formula.
WEAK_THRESHOLD = 0.25


def is_worth_reading(text: str, action: str = "post") -> tuple[bool, str]:
    """(worth it, why). The gate the drafting pipeline can act on."""
    read = reader_value(text, action)
    return read["score"] >= WEAK_THRESHOLD, read["reason"]
