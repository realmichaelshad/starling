"""What a topic is ABOUT, separated from what the account has written before.

The account kept producing the same post. Measured on this machine, the last
six from-scratch topics were:

    Grok 4.5 Efficiency Leader
    Anthropic's Opus 5 Restrictions Removal
    Opus 5 AI Model
    Anthropic's Long-standing Line of Heavyweight AI Models
    Claude Opus 5 Value Proposition
    Opus 5 Cybersecurity Safeguards

Five of six are Opus 5. The variety guard was running the whole time. Two
mistakes, stacked, and neither is about Anthropic — any account would collapse
onto whatever its first posts happened to cover:

  1. RELEVANCE WAS SCORED ON PAST ENTITIES. "Is this topic mine?" was answered
     by overlap with the entity names in his archive, where the Anthropic
     family is 55% of all mentions. A topic could only look relevant by naming
     something he had already written about, so the system could not recommend
     anything new — it is circular by construction, and it tightens every time
     it is used.

  2. VARIETY WAS CHECKED ON LABEL WORDING. `_repeats_recent` needed two shared
     tokens, and "Opus 5 AI Model" shares exactly one with "Claude Opus 5 Value
     Proposition". The same subject, rephrased, read as a different subject.

The separation this module draws:

    DOMAIN    the field he works in — model, pricing, benchmark, latency,
              context, launch. Stable, broad, and the honest answer to "is
              this his kind of thing".
    SUBJECT   the specific entity a topic is about — Opus, Grok, Gemini. The
              thing that must CHANGE between posts.

Voice still comes from his archive. Only topic selection is cut loose from it.
"""
from __future__ import annotations

import re

# Product and organisation names. Deliberately a fixed list rather than
# something learned from his archive: learning it from what he writes is the
# circularity this module exists to break.
_SUBJECT_ALIASES: dict[str, str] = {
    "anthropic": "anthropic", "claude": "anthropic", "opus": "anthropic",
    "sonnet": "anthropic", "haiku": "anthropic", "fable": "anthropic",
    "openai": "openai", "gpt": "openai", "chatgpt": "openai",
    "codex": "openai", "sora": "openai", "dall-e": "openai",
    "google": "google", "gemini": "google", "deepmind": "google", "bard": "google",
    "xai": "xai", "grok": "xai",
    "meta": "meta", "llama": "meta",
    "mistral": "mistral", "mixtral": "mistral",
    "deepseek": "deepseek", "kimi": "moonshot", "moonshot": "moonshot",
    "qwen": "alibaba", "alibaba": "alibaba",
    "cursor": "cursor", "perplexity": "perplexity", "midjourney": "midjourney",
    "nvidia": "nvidia", "amd": "amd", "apple": "apple", "microsoft": "microsoft",
}

# The FIELD, not the players in it. A topic is his kind of thing when it is
# about these, whoever happens to be involved — which is what lets a lab he has
# never mentioned still register as relevant.
_DOMAIN_TERMS: frozenset[str] = frozenset("""
model models llm llms inference training finetune fine-tuning weights
benchmark benchmarks eval evals leaderboard score scores
pricing price cost costs cheaper expensive subscription tier tiers free paid
latency throughput tokens token context window speed fast slow
launch launches launched launching release releases released releasing
rollout preview beta
api sdk agent agents tool tools coding developer developers terminal
gpu gpus vram compute cluster datacenter memory bandwidth
open-source open source closed proprietary local self-hosted
rate limits quota cap usage
safety guardrails alignment jailbreak refusal
funding valuation revenue users adoption
""".split())


# Verbs that ANY news headline uses. They belong to the field only in company
# with a field noun: "unveils Doctor Doom" and "unveils a new GPU" share the
# verb and nothing else, so these count for less.
_DOMAIN_VERBS: frozenset[str] = frozenset("""
ship ships shipped shipping drop drops dropped announce announces announced
unveil unveils unveiled cut cuts raises rolls launches adds adding
""".split())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9.\-]{1,}", (text or "").lower())


# Capitalised words that open a sentence without being anybody's name.
#
# THIS LIST DECIDES WHAT "NAMES SOMETHING" MEANS. The fallback below treats any
# capitalised token as a subject, and its own comment claims to skip sentence
# openers — it does not, because a real organisation often IS the first word
# ("Cohere cuts inference pricing" was the case it was written for). So the
# only thing separating a name from an ordinary capitalised word is this list,
# and it was short enough to let these through, measured live on 2026-07-27:
#
#   "Did you think the model was smart?"        -> subject "did"
#   "Bringing Nunchaku 4-bit Inference..."      -> subject "bringing"
#   "Newer Models, Same Advantage"              -> subject "advantage"
#   "Model Routing Is Simple. Until It Isn't."  -> subject "isn"
#   "Coding Has Agents. Trading Has One"        -> subject "one"
#
# Each junk key costs twice. `names_nothing` reads it as "this post names a
# thing" and waves an empty draft through, and the variety rule reads two junk
# keys as two different subjects, so a repeat looks like rotation. Extended
# with the ordinary openers, gerunds and comparatives a headline actually uses.
_NOT_A_NAME = frozenset("""
the this that these those there here it its they them we you your our their and
but for with from when what why how who all any some most more much many just
now today tomorrow yesterday a an is are was were be been has have had will
would can could should new old good bad big small real running local open
did does do done doesn didn isn aren wasn weren won don can't cannot
one two three four five six seven eight nine ten first second next last
same other another every each both few less least best worst better worse
newer older faster slower cheaper bigger smaller higher lower
bringing building making using running getting taking giving having doing
coming going looking working trying keeping turning moving adding
advantage lesson point thing stuff way ways idea ideas case cases
still even also then than about into over under after before while
maybe perhaps actually really honestly finally already almost
routing trading pricing scaling testing training tuning serving
simple simpler complex easy hard cheap expensive fast slow
until unless because since though although whether
alright crazy half heavy look near people work voice honestly literally
ok okay yeah nope wow lol damn
show ask tell launch net desktop native online free
""".split())
# Deliberately NOT solved with Title-Case detection. "Bringing Nunchaku 4-bit
# Diffusion Inference to Diffusers" is title case and Nunchaku is a real
# product, so skipping capitalised words in title-cased headlines would lose
# exactly the names this fallback exists to find. A stop list is dumber and
# keeps the names.


def subjects(text: str) -> set[str]:
    """The entities a topic is about, collapsed to one key per organisation.

    "Claude Opus 5" and "Anthropic's guardrails" are the same subject. That is
    the whole point: variety means a different SUBJECT, not different wording.
    """
    found: set[str] = set()
    for word in _words(text):
        stem = word.strip(".-")
        if stem in _SUBJECT_ALIASES:
            found.add(_SUBJECT_ALIASES[stem])
    if found:
        return found
    # AN ORGANISATION NOBODY ADDED TO THE LIST IS STILL A SUBJECT. Without
    # this, "Cohere cuts inference pricing" has no subject at all, so the
    # variety rule and the crowding penalty simply do not apply to it — the
    # spiral test picked the same Cohere headline five times in a row while
    # reporting perfect subject spread. The alias list exists to GROUP related
    # names (Claude and Opus are one company); it was never meant to decide
    # which companies exist. Any capitalised name that is not a sentence-opener
    # becomes its own subject key.
    for token in re.findall(r"\b[A-Z][A-Za-z0-9.\-]{2,}\b", text or ""):
        stem = token.lower().strip(".-")
        if stem not in _NOT_A_NAME and stem not in _DOMAIN_TERMS \
                and stem not in _DOMAIN_VERBS:
            found.add(stem)
    return found


def _names_an_organisation(text: str) -> bool:
    """Does this name a company or product at all — known to us or not?

    Structural, not a lookup. A hand-written list would score Cohere below
    Anthropic for the identical story, which is a familiarity bias by another
    route: every organisation that has not been added yet gets marked down for
    being unfamiliar, and a new lab could never break through.
    """
    for token in re.findall(r"\b[A-Z][A-Za-z0-9.\-]{2,}\b", text or ""):
        if token.lower().strip(".-") not in _NOT_A_NAME:
            return True
    return False


def domain_fit(text: str) -> float:
    """Is this his FIELD? 0..1, and entity names contribute nothing.

    A launch from a lab he has never mentioned scores exactly as well as one
    from a lab he writes about weekly, which is the behaviour that was missing.
    """
    words = [w for w in _words(text) if len(w) >= 3]
    if not words:
        return 0.0
    strong = sum(1 for word in words if word.strip(".-") in _DOMAIN_TERMS)
    weak = sum(1 for word in words if word.strip(".-") in _DOMAIN_VERBS)
    # A field noun is worth two and a news verb a half: "unveils Doctor Doom"
    # and "unveils a new GPU" share only the verb, and only one of them is his.
    score = strong * 1.0 + weak * 0.25
    # Naming an ORGANISATION is field evidence too — "Anthropic ships Opus 5"
    # is obviously a launch story and carries no field noun at all.
    #
    # This deliberately does NOT ask whether the name is in the alias list. It
    # used to, and the audit caught the consequence: the identical story scored
    # 0.36 for "Anthropic cuts inference pricing" and 0.28 for "Cohere cuts
    # inference pricing", because Anthropic is in the list and Cohere is not.
    # A hand-written list of companies is a familiarity bias wearing a
    # different hat — every name not yet on it is penalised for being new,
    # which is the exact circularity this module exists to remove. The alias
    # list's only job is GROUPING (Claude and Opus are one organisation); it
    # has no business deciding what counts as a company.
    #
    # But a NAME ALONE IS NOT A FIELD. Recognising any capitalised word as an
    # organisation made "Marvel unveils Doctor Doom casting" score as his
    # subject, which the tests caught immediately. The name only counts when
    # the sentence carries field evidence with it, OR when the name is
    # VERSIONED — "Opus 5", "Grok 4.5", "GPT-5.6" — because a version number is
    # what a software release looks like and Doctor Doom does not have one.
    versioned = re.search(r"\b[A-Z][A-Za-z.\-]{1,}\s?\d+(?:\.\d+)?\b", text or "")
    if _names_an_organisation(text) and (strong >= 1 or versioned):
        score += 1.0
    return min(1.0, score / max(2.0, min(len(words), 8.0)))


# THE USER PROFILE, AS THEMES RATHER THAN COMPANIES.
#
# `account_profile` on this machine holds no pillars, no niche and no topics —
# nothing at all about what he is into. With no profile of interests, the
# system substituted the only thing it could count: which company names appear
# most in his archive. That is how "who is this account" became "Anthropic,
# xAI, OpenAI", which describes nobody. He is interested in AI models and
# vibe-coding; Anthropic is just whoever happened to ship this week.
#
# Themes are the right grain: they are stable (his interest in pricing does not
# change when the company does), they transfer (a pricing story from a lab he
# has never named still lands), and they are what a person would actually say
# when asked what they post about.
_THEMES: dict[str, tuple[str, ...]] = {
    "model releases": ("launch", "launches", "release", "releases", "shipped",
                       "rollout", "preview", "beta", "version"),
    "pricing": ("pricing", "price", "cost", "costs", "cheaper", "expensive",
                "subscription", "tier", "tiers", "free", "paid", "budget"),
    "benchmarks": ("benchmark", "benchmarks", "eval", "evals", "leaderboard",
                   "score", "scores", "outperforms", "beats"),
    "performance": ("latency", "throughput", "speed", "fast", "slow", "tokens",
                    "context", "window", "memory", "bandwidth"),
    "running models locally": ("local", "locally", "self-hosted", "open-source",
                               "open", "weights", "gpu", "vram", "inference"),
    "coding tools": ("coding", "code", "agent", "agents", "terminal", "developer",
                     "cursor", "ide", "api", "sdk", "tool", "tools"),
    "limits and access": ("rate", "limits", "quota", "cap", "restricted",
                          "guardrails", "refusal", "safety"),
}


def interests(texts: list[str], limit: int = 5) -> list[str]:
    """What this account is ABOUT, in themes a person would recognise.

    Company names are deliberately not counted. "AI topics and vibe coding" is
    a profile; "Anthropic, xAI, OpenAI" is a list of press releases.

    When the archive is rich and its posts group into two or more active
    dimensions, the account's own measured beats ARE the themes — the fixed
    vocabulary below is a coder-world cold-start, and a food blogger's
    Starling must never inherit it. (The dimension path reads the same
    corpus `texts` was drawn from; the argument still drives the cold path.)
    """
    # PRECEDENCE, AND THE POINT OF IT IS DETERMINISM.
    #
    #   1  lanes the USER declared          - inference must never overrule a stated beat
    #   2  lanes MEASURED in his archive    - NEW
    #   3  active dimension labels          - the old path, now reached only when 1 and 2 fail
    #   4  curated _THEMES                  - unchanged cold start
    #
    # The old code went straight to 3, guarded by `_rich_archive() and len(labels) >= 2`, and
    # `account_model` records that his archive has ONE real beat. So `interests()` returned
    # curated `_THEMES` names when one dimension was active and two-token noise when two or
    # more were - both observed live, from the same account, depending on clustering. Steps 1
    # and 2 are what make the answer the same every time.
    #
    # `_rich_archive()` SURVIVES UNTOUCHED. Three tests assert it is True and it is
    # load-bearing for `builder_fit`'s dimension override - the property that lets a car
    # reviewer's "Tesla" be their lane. This narrows its REACH by putting two steps above it,
    # rather than weakening the function.
    #
    # `lane` is imported inside the function on purpose: `lane` imports THIS module for
    # `_GENRE_KILL` and `_PARAMETER_COUNT_RE`, so a module-level import here is a cycle.
    try:
        from . import lane as lane_mod
        declared = [name for name in lane_mod.declared_lanes()]
        if declared:
            return declared[:limit]

        # A lane needs >= 3 classified posts AND >= 10% share before it names the account:
        # one post about a thing is not a beat, which is the same floor `builder_fit`'s
        # dimension override already uses for exactly this reason.
        verdicts = [lane_mod.classify(str(text or "")).lane for text in texts]
        landed = [name for name in verdicts if name]
        if landed:
            share_floor = max(3, int(0.10 * len(texts)))
            measured = [name for name in lane_mod.LANES
                        if landed.count(name) >= 3 and landed.count(name) >= share_floor]
            measured.sort(key=lambda name: -landed.count(name))
            if measured:
                return measured[:limit]
    except Exception:                                    # noqa: BLE001
        pass

    if _rich_archive():
        try:
            from .account_model import topic_dimensions
            labels = [str(dim.get("display_label") or "").strip()
                      for dim in topic_dimensions() if dim.get("active")]
            labels = [label for label in labels if label]
        except Exception:                                # noqa: BLE001
            labels = []
        if len(labels) >= 2:
            return labels[:limit]
    words: list[str] = []
    for text in texts:
        words.extend(_words(text))
    if not words:
        return []
    counts: dict[str, int] = {}
    for theme, terms in _THEMES.items():
        hits = sum(1 for word in words if word.strip(".-") in terms)
        if hits:
            counts[theme] = hits
    ranked = sorted(counts.items(), key=lambda pair: -pair[1])
    return [theme for theme, _ in ranked[:limit]]


def interest_fit(text: str, themes: list[str]) -> float:
    """How well a candidate topic matches the themes he actually writes about.

    Falls back to plain domain fit when there are no themes yet, so a new
    account is not locked out of everything on day one.
    """
    if not themes:
        return domain_fit(text)
    words = {word.strip(".-") for word in _words(text)}
    best = 0.0
    for theme in themes:
        # A theme is either one of the fixed cold-start buckets or a learned
        # dimension's display label; both resolve to a term set and score
        # through the identical formula.
        terms = set(_THEMES.get(theme, ())) or _dimension_tokens(theme)
        if terms and words & terms:
            best = max(best, len(words & terms) / max(2.0, min(len(words), 8.0)))
    return max(best, domain_fit(text) * 0.6)


def repeats_recent_subject(candidate: str, recent: list[str]) -> bool:
    """Would this be the same subject as one of the last few posts?

    ONE shared subject is enough. The old check wanted two shared word tokens,
    so five consecutive Opus 5 posts all read as different topics.
    """
    mine = subjects(candidate)
    if not mine:
        return False
    for previous in recent:
        if mine & subjects(previous):
            return True
    return False


def subject_pressure(recent: list[str]) -> dict[str, float]:
    """How heavily each subject already dominates the recent run, 0..1.

    Used to push the next pick away from a subject rather than only blocking an
    exact repeat — the difference between "not the same post twice" and "this
    account is about more than one company".
    """
    counts: dict[str, int] = {}
    for topic in recent:
        for subject in subjects(topic):
            counts[subject] = counts.get(subject, 0) + 1
    total = sum(counts.values())
    if not total:
        return {}
    return {subject: count / total for subject, count in counts.items()}

# WHAT THIS ACCOUNT IS ACTUALLY ABOUT: building things with AI. His words —
# "I post about vibe coding, not this" — after being handed a Salesforce
# Slackbot post.
#
# A strength score cannot make this distinction. Measured live, "AI Leaders
# Push Open Models" scored 0.45 and "Grok 4.5 Efficiency Lead" scored 0.15, so
# any threshold that admits his beat also admits policy news. The difference is
# not how much AI a topic contains — it is WHAT KIND of thing it is about. A
# model or a tool a builder uses, or not.
_BUILDER_THINGS = re.compile(
    # Version-glued names must match: "Qwen3.5" never matched `qwen` because
    # the group's trailing \b fails between "n" and "3" — found 2026-07-29
    # when "Qwen3.5 tops SWE-bench" rated off-beat. Digit/dot suffixes are
    # part of the name.
    r"\b(?:opus|sonnet|haiku|claude|fable|gpt|chatgpt|codex|grok|gemini|llama|"
    r"mistral|mixtral|deepseek|kimi|qwen|midjourney|dall-?e|sora|"
    r"cursor|copilot|cline|aider|windsurf|zed|replit|vscode|jetbrains|"
    r"hugging ?face|vllm|llamacpp|llama\.cpp|"
    r"cli|terminal|ide|sdk|api|repo|git|npm|docker|compiler|editor|agent|"
    r"agents|model|models|llm|llms|token|tokens|context window|benchmark|"
    r"benchmarks|swe-?bench|mmlu|evals?|leaderboard|"
    r"gpu|gpus|vram|cuda|"
    r"fine-?tune|inference|latency|prompt|prompts|vibe cod|"
    r"open-?weights?|open-?source|self-?host|local model|coding)[\d.]*\b",
    re.IGNORECASE)

# Things that are AI-adjacent but not his beat: enterprise rollouts, policy,
# funding, culture pieces. Each of these produced a live draft he rejected.
_NOT_HIS_BEAT = re.compile(
    # "teams" meant MICROSOFT Teams and killed "governance controls for
    # Claude Code teams" — a product feature about teams of people. Name the
    # product, not the plural (found by the 2026-07-29 distrust audit).
    r"\b(?:slackbot|salesforce|workday|hubspot|zoom|microsoft teams|ms teams|"
    r"sharepoint|crm|"
    r"enterprise|workplace|hiring|recruit|billboard|layoff|layoffs|"
    r"white house|policymak|regulat|senate|congress|lawsuit|antitrust|"
    r"burnout|society|philosoph|wellness|mindful|"
    # Observability and incident work. Adjacent to AI, not adjacent to HIM.
    # "AI Root Cause Analysis Shifts from Model Reasoning to Context
    # Engineering" reached a live draft because it names models and
    # engineering; it is about triaging 502s.
    # Cars are not coding tools. "Tesla to Make Design and Software for Model S
    # and X open source" reached a live draft on 2026-07-27 — it clears
    # `_BUILDER_THINGS` on the phrase "open source" alone, and nothing here
    # objected. He writes about building software with models, not about
    # vehicles that happen to ship code. 0 of his 35 posts match, and Grok/xAI
    # (the same founder, and squarely his beat) are untouched.
    r"tesla|model s\b|model x\b|roadster|cybertruck|autopilot|robotaxi|"
    r"vehicles?|cars?\b|self-?driving|\bfsd\b|"
    r"root cause|incident|observability|chaos mesh|on-?call|postmortem|"
    r"downtime|outage|sre\b|monitoring|telemetry|dashboards?|"
    r"series [abc]\b|raises \$|funding round|valuation|ipo|"
    # ACQUISITIONS AND CAPITAL ARE NOT HIS BEAT — measured 2026-07-28, the
    # live pool served "Cyera agrees to acquire Oasis Security for $1B" and
    # "Fund Momentum MCP: Live VC Fund Data" with builder_fit=True. COMPANY
    # acquisitions only: the first version banned bare "acquir\w+" and killed
    # "Claude Code acquires new skills through plugins" — a feature story.
    # Same lesson as the tragedy word list: the shape, not the word.
    r"agrees to acquire|acquisition|acquires \w+(?: \w+)? for \$|\bmerger\b|"
    r"buys \w+ for|\bvc\b|venture capital|fund data|deal size|"
    r"lands \$\d+[mbk]?|secures (?:a )?series|series [d-f]\b|"
    # Policy papers and frameworks-about-openness are philosophy, not tools.
    # BARE "governance" killed "Anthropic ships governance controls" — a
    # product feature; the paper shapes carry their own tells.
    r"toward an? framework|unpacking|policy brief|governance framework|"
    r"ai governance|regulation|"
    # Domains that merely run inference are not coding: geospatial imaging
    # reached the pool because "inference" cleared the domain terms.
    r"geospatial|planetary|satellite|earth observation)\b",
    re.IGNORECASE)

# The GENRE subset of `_NOT_HIS_BEAT`: story TYPES (capital flows, policy,
# incident ops, culture pieces) as opposed to wrong-world SUBJECTS (tesla,
# salesforce, geospatial). The split exists for the rich-archive path: an
# account's own durable subject overrides a subject-kill — a car reviewer's
# "Tesla" is their lane — but never a genre-kill, because "OpenAI secures
# Series F" is a funding story even for an account that writes about OpenAI
# daily. Every alternative here is copied verbatim from `_NOT_HIS_BEAT`
# (test_account_topic_fit pins the subset relation) — edit them together.
_GENRE_KILL = re.compile(
    r"\b(?:enterprise|workplace|hiring|recruit|billboard|layoff|layoffs|"
    r"white house|policymak|regulat|senate|congress|lawsuit|antitrust|"
    r"burnout|society|philosoph|wellness|mindful|"
    r"root cause|incident|observability|chaos mesh|on-?call|postmortem|"
    r"downtime|outage|sre\b|monitoring|telemetry|dashboards?|"
    r"series [abc]\b|raises \$|funding round|valuation|ipo|"
    r"agrees to acquire|acquisition|acquires \w+(?: \w+)? for \$|\bmerger\b|"
    r"buys \w+ for|\bvc\b|venture capital|fund data|deal size|"
    r"lands \$\d+[mbk]?|secures (?:a )?series|series [d-f]\b|"
    r"toward an? framework|unpacking|policy brief|governance framework|"
    r"ai governance|regulation)\b",
    re.IGNORECASE)


# A HEADLINE IS NOT AUTOMATICALLY A SUBJECT. Measured over ten live posts on
# 2026-07-27, six of the eight that shipped were defective, and the fault was
# almost never the writing — it was that the topic handed to the writer was not
# an event at all. Read what each kind of title produced:
#
#   "Show HN: LLM-spend - Audit your ... spend locally"
#       -> "Show HN: LLM-spend lets you track OpenAI costs without a full key."
#          The site's submission prefix went out in the post.
#   "Show HN: How I built a debugging tool, and the agent ... found bugs in it"
#       -> "Show HN's tool lets agents add or remove trace points in seconds."
#          "Show HN" read as the name of a company.
#   "Why My Open-Source Project Hasn't Done Better"
#       -> "My open-source project has 400 stars, but it's not the LLMs ..."
#          The author's first person became HIS first person, and a number he
#          has never had became a claim in his voice. This is the worst of them:
#          every other fault is a bad post, this one is a false statement about
#          his own life.
#   "Will it be possible to do LLM Pool Training?"
#       -> "LLM Pool Training's overhead is a killer."
#          A question answered as though it were a finding.
#
# So the rule is about what KIND of thing the title is, the same shape of
# distinction `builder_fit` makes about domain. A launch is an event and keeps
# its subject; a discussion prompt, an open question and a personal essay are
# not events and have no subject to report.
_SHOW_HN = re.compile(r"^\s*show\s+hn\s*[:\-–—]\s*", re.IGNORECASE)
_DISCUSSION_HN = re.compile(r"^\s*(?:ask|tell)\s+hn\s*[:\-–—]\s*",
                            re.IGNORECASE)
# A title that is simply a question. "Will it be possible to do LLM Pool
# Training?" has no answer in it, so a post built on it must invent one.
_BARE_QUESTION = re.compile(r"\?\s*$")
# The author's own story. Adopting it puts words in his mouth about his own
# work — "My open-source project has 400 stars" — which no later gate can
# repair, because the sentence is grammatical, on-topic and simply untrue.
_FIRST_PERSON_TITLE = re.compile(
    r"^\s*(?:why|how|what|when)\s+(?:i|we|my|our)\b"
    r"|^\s*(?:i|we)\s+\w+"
    # ANY title that OPENS with My/Our is the author's own story. The noun
    # list below was the hole: "My local LLM scored 6/6. It was wrong every
    # time" isn't a project/startup/app, so it reached the pool and the draft
    # came back in the first person — "My local LLM scored 6/6" — as HIS
    # claim, twice on 2026-07-28. The opener is the tell, not the noun.
    r"|^\s*(?:my|our)\b"
    r"|\b(?:my|our)\s+(?:open-?source\s+)?(?:project|startup|company|app|blog|team)\b"
    r"|\bwhat\s+(?:building|i|we)\b.{0,40}\btaught\s+(?:me|us)\b",
    re.IGNORECASE)


def event_subject(title: str) -> str | None:
    """The postable subject inside a headline, or None if it is not an event.

    Returns the title with a submission prefix removed, so "Show HN: Termic -
    desktop app for running CLI coding agents" becomes a real tool launch
    rather than a post about a website's section header.
    """
    body = " ".join(str(title or "").split())
    if not body:
        return None
    if _DISCUSSION_HN.search(body):
        return None                     # a question to a forum, not a release
    body = _SHOW_HN.sub("", body).strip()
    if not body:
        return None
    if _BARE_QUESTION.search(body):
        return None
    if _FIRST_PERSON_TITLE.search(body):
        return None
    return body


# HIS LANE, PRECISELY — stated by him 2026-07-29: "I don't mainly speak
# about LLMs, I speak about Claude Code and the vibe coding world." CORE is
# what he'd actually post: coding agents and assistants, frontier-lab model
# news (those models are what he codes with), coding benchmarks, pricing and
# limits of coding tools, running models locally to build with. Adjacent AI
# (OCR models, diffusion, background removal, memory research) clears
# `builder_fit` but is NOT his voice — it may serve only as a last resort,
# and the card says so. Calibrated: every one of his archive subjects
# (anthropic 24, openai 10, xai 7, moonshot 6, cursor) is core-shaped.
_VIBE_CORE = re.compile(
    r"\b(?:claude code|cursor|copilot|aider|cline|windsurf|goose|codex|"
    r"zed|replit|devin|jetbrains|vscode|"
    r"coding agents?|coding assistants?|coding models?|code review|"
    r"agentic coding|vibe cod\w*|pair programm|"
    r"swe-?bench|terminal|ide\b|mcp\b|"
    r"opus|sonnet|haiku|fable|claude|gpt|chatgpt|grok|gemini|kimi|qwen|"
    r"deepseek|llama|mistral|nouscoder|"
    r"anthropic|openai|xai|moonshot|"
    r"rate limits?|pricing|subscription|"
    r"local (?:model|llm|inference))[\d.]*\b",
    re.IGNORECASE)


def vibe_core(text: str) -> bool:
    """Is this squarely the lane he posts in, not merely AI-adjacent?"""
    return bool(_VIBE_CORE.search(text or "")) and builder_fit(text)


# THE LANE IS MEASURED, NOT ASSUMED — his requirement, 2026-07-29: "if it was
# a different user we will adapt to his archive, especially by the topic."
# The account's OWN posts define its core: the subjects it actually posts
# about (alias-grouped) plus the capitalized product names in its writing.
# `_VIBE_CORE` above is only the cold-start seed for an archive under eight
# posts — a food blogger's Starling must never inherit a coder's lane.
_LANE_CACHE: tuple[tuple[int, int], set[str]] | None = None


def _lane_posts() -> list[str]:
    """Non-deleted archive texts — a deleted post is not this account's lane."""
    try:
        from .storage import connect
        with connect() as conn:
            return [r[0] for r in conn.execute(
                "select text from posts where text is not null and deleted_at is null") if r[0]]
    except Exception:                                    # noqa: BLE001
        return []


def _lane_key(posts: list[str]) -> tuple[int, int]:
    # corpus_version catches same-count edits and deletions the bare count
    # missed; the count still guards direct-sqlite seeding in tests.
    try:
        from .storage import corpus_version
        return (corpus_version(), len(posts))
    except Exception:                                    # noqa: BLE001
        return (-1, len(posts))


def account_core_tokens() -> set[str]:
    """Lowercased tokens that mark this account's own lane, from its archive."""
    global _LANE_CACHE
    posts = _lane_posts()
    cache_key = _lane_key(posts)
    if _LANE_CACHE is not None and _LANE_CACHE[0] == cache_key:
        return _LANE_CACHE[1]
    tokens: set[str] = set()
    for post in posts:
        tokens |= subjects(post)
        for name in re.findall(r"\b[A-Z][A-Za-z0-9.\-]{2,}\b", post):
            stem = name.lower().strip(".-")
            if stem not in _NOT_A_NAME and len(stem) >= 3:
                tokens.add(stem)
    _LANE_CACHE = (cache_key, tokens)
    return tokens


def notable_subjects() -> set[str]:
    """The things this account writes about REPEATEDLY, plus the names any
    reader would recognise.

    Measured 2026-07-29 on a live pool of 22 topics: only 5 named anything
    from his archive, 17 named nothing recognisable — and seven of those
    seventeen passed `core_lane` anyway, because a Show HN blurb like
    "OpenLore: local-first memory and guardrails for AI coding agents"
    contains the domain words. The picker then chose OpenLore over "Claude
    Code costs $200 a month, Goose does it free" and "Anthropic launches
    Cowork", because the rotation rule pushes AWAY from a subject it used
    recently — straight into obscurity. Being on the beat is not the same as
    being worth posting about.

    Durable means twice. A single mention is a one-off (his Marvel joke gave
    the archive "thor" and "avengers"), and the capitalised-word fallback
    also drags in sentence openers like "alright" and "people". Requiring two
    posts keeps the subjects he returns to and drops the noise; the alias
    list covers the labs and tools everyone knows even if he has not
    written about them yet.
    """
    return _durable_archive_subjects() | set(_SUBJECT_ALIASES) \
        | set(_SUBJECT_ALIASES.values()) | _KNOWN_TOOLS


def _durable_archive_subjects() -> set[str]:
    """The subjects this account RETURNS to (seen >= 2) — notable_subjects
    without the seeded alias/tool vocabulary, i.e. the part that is genuinely
    this account's own data. Split out so builder_fit can let an account's
    own repeated subjects override the seed kill-list."""
    counts: dict[str, int] = {}
    for post in _lane_posts():
        for token in subjects(post):
            counts[token] = counts.get(token, 0) + 1
        for name in re.findall(r"\b[A-Z][A-Za-z0-9.\-]{2,}\b", post):
            stem = name.lower().strip(".-")
            if stem not in _NOT_A_NAME:
                counts[stem] = counts.get(stem, 0) + 1
    return {token for token, seen in counts.items() if seen >= 2}


def _rich_archive() -> bool:
    """Enough measured lane to speak for itself — core_lane's exact floor."""
    return len(account_core_tokens()) >= 8


def _account_dimension_sets() -> list[set[str]]:
    """Vocabulary of the account's ACTIVE topic beats ([] cold).

    The fuller `vocabulary`, not the 12-token scoring core: this test counts
    shared words and has no denominator to protect.
    """
    try:
        from .account_model import topic_dimensions
        return [set(dim.get("vocabulary") or dim.get("tokens") or [])
                for dim in topic_dimensions() if dim.get("active")]
    except Exception:                                    # noqa: BLE001
        return []


def _dimension_tokens(label: str) -> set[str]:
    """The token core behind one dimension's display label ({} if unknown)."""
    try:
        from .account_model import topic_dimensions
        for dim in topic_dimensions():
            if str(dim.get("display_label") or "") == label:
                return set(dim.get("tokens") or [])
    except Exception:                                    # noqa: BLE001
        pass
    return set()


# RECOGNISABLE IS NOT ONLY LABS. The alias list is model families, so a
# rotation probe on 2026-07-29 rated "Goose agent benchmarks" and a runtime's "
# context window update" unnotable and never picked either — both are
# squarely this lane, and Goose is in his archive as the free alternative to
# Claude Code. A reader in this world knows these names the way they know
# Anthropic; they are seed knowledge, not this account's data, and an account
# in another world simply never sees a topic containing them.
_KNOWN_TOOLS = frozenset("""
goose aider cline windsurf replit zed devin copilot vscode jetbrains
vllm llamacpp langchain langgraph huggingface together groq fireworks
perplexity openrouter modal runpod lmstudio nous exa cohere
""".split())


# A MODEL RELEASE IS NOTABLE WHOEVER SHIPPED IT. "NousCoder-14B lands within
# two points of closed models" is exactly this account's beat — open weights,
# a parameter count, a benchmark claim — even though Nous Research is not in
# the alias list and appears once in the archive. The marker is a parameter
# count attached to a name (14B, 70B, 4B, 26B), which a tool launch never has
# and which no amount of Show HN blurb produces by accident.
_PARAMETER_COUNT_RE = re.compile(r"\b\d{1,3}\s?[bB]\b(?!\w)")


def names_notable(text: str) -> bool:
    """Does this topic name something the reader — and he — would recognise?"""
    body = text or ""
    if subjects(body) & notable_subjects():
        return True
    return bool(_PARAMETER_COUNT_RE.search(body))


def core_lane(text: str) -> bool:
    """Is this topic in THIS account's measured lane?

    THEIR ARCHIVE IS THE AUTHORITY. With enough posts, a topic sharing a
    subject or name with what the account actually writes about is core —
    without asking `builder_fit`, whose noun list is itself a coder's frame
    ("Vercel raises deployment pricing" fails it, and that IS a web-dev
    account's lane; found by the adaptation test, 2026-07-29). The generic
    frame gates only the cold-start path, where it is all there is.
    """
    tokens = account_core_tokens()
    if len(tokens) >= 8:
        topic_subjects = subjects(text)
        topic_words = set(_words(text))
        if (topic_subjects & tokens) or (topic_words & tokens):
            return True
    return builder_fit(text) and vibe_core(text)


def builder_fit(text: str) -> bool:
    """Is this topic in territory THIS account can credibly speak to?

    Cold start (archive under eight lane tokens) is the original coder-frame
    rule, unchanged: a topic must name a model, a dev tool or the act of
    building with one — and must not be an enterprise/policy/funding story
    that merely mentions AI. "Salesforce rolls out new Slackbot AI agent"
    satisfies the first test through "agent" and fails the second, which is
    correct: it is a workplace story, and he does not write those.

    With a rich archive the account's own MEASURED BEATS speak first (same
    authority rule as core_lane): a topic sharing two meaningful words with
    an active topic dimension is this account's territory, and that
    overrides the seed kill-list's subject kills — a car reviewer's "Tesla"
    is their lane — but never its GENRE kills (`_GENRE_KILL`: a funding or
    policy story stays killed whoever it names).

    A BEAT, NOT A NAME. The override was first written against durable
    archive subjects (anything named twice) and measured on the live pool
    2026-07-31: it admitted Spider-Man, Dragon Ball, Avengers and a DOGE
    story, because his archive contains one Marvel joke and the word
    "july". A dimension is >= 3 posts that group together AND >= 15% of the
    account's recent output, so a one-off cannot become a lane. Two shared
    words is the topics.self_repetition standard for a real topical match,
    deliberately NOT core_lane's one-word test, which the ambient token
    "model" poisons ("Model Routing…" in his archive must never admit
    "Tesla Model S").

    The generic rule stays as the floor, so big-lab model news passes for
    every account and a cold install behaves exactly as it always has.
    """
    body = str(text or "")
    if not body.strip():
        return False
    if _rich_archive():
        topic_words = {word.strip(".-") for word in _words(body)}
        on_a_measured_beat = any(len(topic_words & tokens) >= 2
                                 for tokens in _account_dimension_sets())
        if on_a_measured_beat and not _GENRE_KILL.search(body):
            return True
        if _NOT_HIS_BEAT.search(body):
            return False
        return bool(_BUILDER_THINGS.search(body))
    if _NOT_HIS_BEAT.search(body):
        return False
    return bool(_BUILDER_THINGS.search(body))
