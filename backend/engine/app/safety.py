from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable
from urllib.parse import urlparse

from .schemas import PostContext


@dataclass
class SafetyAssessment:
    blocked: bool = False
    severity: str = "none"
    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    local_only: bool = False
    research_allowed: bool = True
    media_reuse_allowed: bool = True

    def model_dump(self) -> dict:
        return asdict(self)


# MENTIONING A CREDENTIAL TYPE IS NOT DISCLOSING ONE.
#
# The credentials rule read `\b(?:password|api key|social security|ssn)\b` and
# treated the PHRASE "api key" as private identifying information. Every
# critical-severity safety event in this product's history — all six of them —
# is that one false positive. They are his own CogniKernel posts:
#
#   "...That's another API key, per-session token cost, added latency."
#   "It stops burning another API key per session..."
#   "...lets agents cite facts forever without an API key."
#
# Five hard blocks inside 52 minutes on 2026-07-28, one more on the 29th, each
# telling him his post contained private information. It named a cost. Measured
# over all 5,000 stored safety-event texts, the rule fires exactly six times,
# every one of them on "api key", and `password`, `social security` and `ssn`
# have never fired at all.
#
# TWO TIERS, the same split [[a-gate-word-list-eats-his-register]] forced on the
# tragedy list: unambiguous words block alone; a word his own domain uses every
# day needs corroboration. Here the corroboration is a secret-SHAPED token in
# the same clause as the mention — `sk-…`, `API_KEY=…`, a long hex or base64
# run. Same clause, not the whole post: a commit SHA three sentences away is
# not a leaked key, and the whole reason this rule failed was treating
# proximity in vocabulary as proximity in fact.
#
# THE WINDOW IS 60 CHARACTERS AND STOPS AT A SENTENCE END, and it is that wide
# on purpose. A 4-character window was the first version; it passed every
# disclosure form written mention-first ("api key: sk-…") and let
# "ghp_1234567890abcdefghij is the api key" through — which the ORIGINAL rule
# blocked. A calibration that fixes six false positives by opening one true
# negative is not a calibration.
#
# The other three members are LEFT EXACTLY AS THEY WERE. "password" carries the
# same figurative risk ("password manager", "reset your password") and it has
# never once fired here — widening the fix to a member with no evidence behind
# it is how a calibrated gate becomes an uncalibrated one.
_SECRET_SHAPED = (
    r"(?:sk|pk|rk|ghp|gho|ghu|ghs|xox[abprs]|AKIA|ASIA|AIza|hf|glpat|npm)"
    r"[-_][A-Za-z0-9_-]{16,}"       # sk-…, ghp_…, xoxb-…
    r"|[A-Za-z0-9+/]{32,}={0,2}"    # a base64 secret
    r"|[A-Fa-f0-9]{32,}"            # a hex secret
)
_CREDENTIAL_TYPE = r"\bapi[ _-]?keys?\b"
_SAME_CLAUSE_CHARS = 60              # the window; it also stops at a sentence end


class _CredentialsRule:
    """The `credentials` rule, with the same meaning as the single expression it replaces and a
    running time that does not depend on what a stranger posts.

    It used to be one alternation:

        password|social security|ssn
        | api key, up to 60 same-clause characters, a secret-shaped token
        | a secret-shaped token, up to 60 same-clause characters, api key

    The third branch is quadratic. At every offset of a long run of `[A-Za-z0-9+/]` the run is
    matched greedily and then given back one character at a time, each time retrying the lazy
    window and the `api key` anchor: 3,200 characters took 13 seconds and 100,000 (the most this
    module scans) about four hours, with the interpreter lock held, so the customer's whole
    engine answered nothing. The text is exactly what the product reads: any post with a base64
    blob, a hash dump or a long URL path in it.

    Anchoring on the rare thing instead is linear: find each `api key`, then look for a secret in
    the bounded window either side of it.
    """

    _FLAT = re.compile(r"\b(?:password|social security|ssn)\b", re.I)
    _TYPE = re.compile(_CREDENTIAL_TYPE, re.I)
    _SECRET = re.compile(_SECRET_SHAPED, re.I)
    _CLAUSE_END = re.compile(r"[.\n]")
    _SECRET_REACH = 256               # how much of a long token is looked at beyond the window
    _MAX_MENTIONS = 400               # each costs a bounded look around it; past this, fail closed

    def search(self, text: str) -> "re.Match[str] | None":
        flat = self._FLAT.search(text)
        if flat is not None:
            return flat
        for seen, mention in enumerate(self._TYPE.finditer(text)):
            if seen >= self._MAX_MENTIONS:
                return mention  # hundreds of "api key"s is not a post anybody should be drafting from
            if self._secret_follows(text, mention.end()) or self._secret_precedes(text, mention.start()):
                return mention
        return None

    def _secret_follows(self, text: str, at: int) -> bool:
        """A secret STARTS within the window after `at`, before the clause ends."""
        window = text[at:at + _SAME_CLAUSE_CHARS]
        clause_end = self._CLAUSE_END.search(window)
        last_start = at + (clause_end.start() if clause_end else len(window))
        secret = self._SECRET.search(text, at, min(len(text), last_start + self._SECRET_REACH))
        return secret is not None and secret.start() <= last_start

    def _secret_precedes(self, text: str, at: int) -> bool:
        """A secret ENDS within the window before `at`, after the last clause end."""
        window_start = max(0, at - _SAME_CLAUSE_CHARS)
        breaks = [found.end() for found in self._CLAUSE_END.finditer(text, window_start, at)]
        first_end = breaks[-1] if breaks else window_start
        reach_from = max(0, first_end - self._SECRET_REACH)
        return any(secret.end() >= first_end and secret.end() <= at
                   for secret in self._SECRET.finditer(text, reach_from, at))


PII_PATTERNS = (
    ("doxxing", re.compile(r"\b(home|street) address\b|\bdoxx?\b|\bleak (?:their|his|her) (?:address|phone)\b", re.I)),
    ("credentials", _CredentialsRule()),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)")),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
)
INTIMATE_PATTERN = re.compile(r"\b(?:nudes?|intimate image|revenge porn|sexual image)\b", re.I)
THREAT_PATTERN = re.compile(r"\b(?:kill|hurt|attack|swat)\s+(?:him|her|them|this person|@[A-Za-z0-9_]+)\b", re.I)
HARASSMENT_PATTERN = re.compile(r"\b(?:go harass|pile on|mass report|ruin (?:him|her|them)|send hate)\b", re.I)
PROMPT_INJECTION_PATTERN = re.compile(r"\b(?:ignore (?:all |the )?(?:previous|system) instructions|system prompt|developer message|reveal your prompt)\b", re.I)
ENGAGEMENT_BAIT_PATTERN = re.compile(
    r"\b(?:follow me|like and repost|comment yes|drop a \w+ below|"
    r"tag (?:three|3) friends|"
    # The common forms this missed: "Like if you agree that AGI is coming in
    # 2027. Retweet if you disagree." matched none of the above, so a bait post
    # was treated as an ordinary reply target.
    r"(?:like|rt|retweet|repost|comment|reply) if you\b|"
    r"(?:like|rt|retweet|repost) (?:this )?if\b|"
    r"who(?:'s| is) with me\b|am i the only one who\b)", re.I)
DECEPTIVE_PATTERN = re.compile(r"\b(?:pretend this is verified|make up a source|fake screenshot|say it happened even if)\b", re.I)

# HUMAN HARM. Found by Sim Studio: on "At least 40 people have died after a
# bridge collapse", Starling scored the thread as a live room and recommended
# replying — a growth-optimised take on a fatal disaster. Reach is never the
# right frame here, so this is a hard stop rather than a penalty.
#
# TWO TIERS, because one flat word list does not survive his register. The
# flat list blocked 20 of 23 ordinary tech sentences — "the fatal flaw in this
# benchmark", "just rip out the old retrieval layer", "print media is dying",
# "shipping on a Friday is career suicide", "I'm shooting for sub-100ms p99" —
# each of them told him *People were hurt here. This is not a thread to work an
# angle in.* That is not one skipped reply; it is an accusation, on a post
# about latency.
#
# The earlier carve-out for bare "dead" (his Polymarket post: "the odds went
# from 87% to basically dead") found the rule this generalises. A tragedy word
# only counts when HUMAN CONTEXT is attached, because every one of these words
# has a live figurative sense in this domain: dead odds, a dying category, a
# fatal flaw, the first casualty, an earthquake of a release, feast or famine,
# ripping out a dependency. So:
#
#   TRAGEDY_EXPLICIT   — shapes that are never figurative (a count of bodies, a
#                        pronouncement, an event that is only ever this).
#                        Blocks on its own.
#   TRAGEDY_AMBIGUOUS  — words this domain uses figuratively every day. Blocks
#                        ONLY when HUMAN_HARM_CONTEXT also appears.
#
# Tightening the trigger does not soften the response: anything that clears the
# bar is still a hard block. See tests/test_tragedy_register.py — the bar is
# zero false positives on his archive AND on the figurative battery, with zero
# misses on the real-tragedy battery.
TRAGEDY_EXPLICIT = re.compile(
    # A death made specific: a count, a discovery, a pronouncement, a body.
    r"\b\d+\s+(?:people\s+|men\s+|women\s+|children\s+|others\s+|more\s+)?"
    r"(?:were\s+|are\s+|found\s+|left\s+)?dead\b|"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"dozens|scores|hundreds|thousands)\s+(?:of\s+(?:people|them|victims)\s+)?"
    r"(?:were\s+|are\s+|found\s+|left\s+|confirmed\s+)?dead\b|"
    r"\b(?:found|shot|left|pronounced|declared|confirmed|presumed)\s+dead\b|"
    r"\bdead\s+(?:bod(?:y|ies)|victims?)\b|\bdeath\s+toll\b|"
    # Killing with a human object. NOT a bare "killed" — "that update killed my
    # integration" and "the price killed the deal" are ordinary sentences here.
    r"\bkill(?:ed|ing|s)?\s+(?:at\s+least\s+|more\s+than\s+|nearly\s+|up\s+to\s+)?"
    r"(?:\d+|dozens|scores|hundreds|thousands)\s+(?:of\s+)?"
    r"(?:people|civilians|children|students|passengers|residents|troops|"
    r"soldiers|workers|men|women|victims)\b|"
    r"\bkill(?:ed|ing|s)?\s+(?:dozens|scores|hundreds|thousands)\b|"
    r"\bkill(?:ed|ing|s)?\s+(?:innocent\s+)?(?:people|civilians|children)\b|"
    # People as the subject of harm.
    r"\b(?:people|men|women|children|students|passengers|workers|civilians|"
    r"residents|soldiers|troops|victims|survivors)\s+"
    r"(?:have\s+|has\s+|had\s+|were\s+|was\s+|are\s+|been\s+|being\s+)*"
    r"(?:died|killed|injured|wounded|shot|murdered|massacred)\b|"
    r"\b(?:died|dying)\s+(?:by\s+suicide|of\s+(?:injuries|wounds|starvation))\b|"
    r"\b(?:lost\s+(?:his|her|their)\s+li(?:fe|ves)|"
    r"took\s+(?:his|her|their)\s+own\s+li(?:fe|ves))\b|"
    # Events with no second meaning.
    r"\b(?:massacre|genocide|war\s+crimes?|ethnic\s+cleansing|"
    r"terror(?:ist)?\s+attack|mass\s+shooting|school\s+shooting|mass\s+casualt|"
    r"plane\s+crash|bridge\s+collapse|building\s+collapse|tsunami|obituary|"
    r"condolences|passed\s+away|rest\s+in\s+peace|kidnapp?ed|held\s+hostage|"
    r"mourners?|vigil\s+for|memorial\s+service|gunman|gunfire|opened\s+fire|"
    r"airstrikes?|shelling)\b", re.I)

# The human context an ambiguous word needs before it means what it says.
# Deliberately NOT "N people" on its own: "at least 30 people asked me about
# the pricing change" is an ordinary sentence, and pairing it with the widened
# ambiguous list below ("the demo was a disaster") would re-create exactly the
# false positive this whole split exists to remove. Every alternative here has
# to be about a person being harmed, mourned, or dug out.
HUMAN_HARM_CONTEXT = re.compile(
    r"\b(?:victims?|injured|wounded|survivors?|death\s+toll|bereaved|grieving|"
    r"mourners?|condolences|obituary|coroner|morgue|casualties|"
    r"rescuers?|rescue\s+(?:crews?|workers?|teams?|operations?)|search\s+and\s+rescue|"
    r"first\s+responders|paramedics|emergency\s+services|hospitali[sz]ed|"
    r"intensive\s+care|evacuat(?:ed|ion|ing|ions)|"
    r"next\s+of\s+kin|(?:his|her|their)\s+famil(?:y|ies)|"
    r"crisis\s+(?:line|hotline)|suicide\s+(?:prevention|hotline|helpline)|"
    r"check\s+on\s+your\s+(?:friends|people))\b|"
    r"\b(?:police|authorities|officials|investigators)\s+"
    r"(?:said|say|says|confirmed|report(?:ed)?)\b|"
    r"\b(?:at\s+least|more\s+than|nearly|up\s+to)\s+\d+\s+"
    r"(?:dead|killed|injured|wounded|missing|dying)\b|"
    r"\b(?:people|men|women|children|residents|families|civilians)\s+"
    r"(?:are\s+|were\s+|have\s+been\s+|had\s+been\s+)?"
    r"(?:missing|trapped|hurt|displaced|evacuated|buried)\b", re.I)

# Words this domain uses figuratively every day. Meaningless without the above.
# "collapse", "attack" and "explosion" are deliberately absent: a security post
# ("officials said the attack affected 40,000 accounts") and a funding post
# ("officials said the merger collapsed") both carry a hard human-harm signal
# by coincidence, and blocking those would be the same bug in a new costume.
# The genuinely fatal forms of all three are spelled out in TRAGEDY_EXPLICIT.
TRAGEDY_AMBIGUOUS = re.compile(
    r"\b(?:deaths?|died|dying|fatal(?:it(?:y|ies))?|casualt(?:y|ies)|"
    r"shooting|stabbing|bombing|earthquake|wildfire|hurricane|tornado|"
    r"flood(?:s|ing)?|famine|overdose|suicide|hostage|funeral|rip|crash|"
    r"fire|blaze|rubble|missing|disaster|siege|drowned|refugees|"
    r"tragedy|tragic|devastating)\b", re.I)


def _is_tragedy(text: str) -> bool:
    """A real human tragedy, not the word for one used about software."""
    if TRAGEDY_EXPLICIT.search(text):
        return True
    return bool(TRAGEDY_AMBIGUOUS.search(text) and HUMAN_HARM_CONTEXT.search(text))


# FIRST-PERSON SELF-HARM INTENT. Nothing covered it: "i am going to kill
# myself" passed every gate here, and Starling would have drafted an
# engagement reply to it. The vocabulary above is built for the wrong
# grammatical person — TRAGEDY_EXPLICIT is third-person news register ("died by
# suicide", "took their own life"), and THREAT_PATTERN needs an object from
# (him|her|them|this person|@handle), so "myself" reads as no threat.
#
# The gap exists BECAUSE the register work was done right: the bare word
# "suicide" is deliberately ambiguous (see the "career suicide" note above) and
# needs HUMAN_HARM_CONTEXT to corroborate. These phrases need no corroboration,
# because none of them has a product or business reading. That is the whole
# reason this can be a flat match while the tragedy list cannot.
#
# DELIBERATELY EXCLUDED, and the omission is a decision rather than an
# oversight — `test_self_harm_is_blocked` pins it so it can be revisited:
#   "suicidal"      — "a suicidal pricing strategy" is his register exactly.
#   "want to die"   — ordinary hyperbole after a bad deploy.
#   "hurt myself" / "harm myself" — "hurt myself laughing". The obvious remedy,
#     routing them through HUMAN_HARM_CONTEXT the way _is_tragedy does, was
#     MEASURED and cannot fire: that context is news vocabulary (victims, death
#     toll, coroner, paramedics, "police said") and a person writing about
#     their own harm uses none of it. A branch that can never match is worse
#     than the gap, because it reports coverage.
# Calibrated on the 199 distinct post texts assess_context has actually seen —
# STRANGERS' text, not his archive, which is the wrong corpus for this gate.
# THE `-ing` FORMS ARE NOT UNIFORM AND THAT IS THE TRAP. `(?:ing)?` works on
# kill->killing and end->ending, and BREAKS on take->taking, because the stem
# loses its `e` — `take(?:ing)?` would match "takeing". The -ing form was
# therefore dropped from that branch rather than handled, giving a pattern that
# looks uniform and is not: shipped 2026-08-07 blocking "take my own life" and
# missing "thinking about taking my own life", which is the commoner phrasing
# of the two. `tak(?:e|ing)` spells both stems out.
#
# `own` stays MANDATORY on this branch and must not be relaxed: "taking my life
# in a new direction" is ordinary English, and `own` is the whole of what makes
# the phrase unambiguous.
SELF_HARM_PATTERN = re.compile(
    r"\b(?:kill(?:ing)?\s+myself|end(?:ing)?\s+my\s+(?:own\s+)?life|"
    r"tak(?:e|ing)\s+my\s+own\s+life)\b", re.I)

# The same intent aimed at the reader, for the OUTGOING gate. Starling is
# unlikely to write this; the gate whose job is to stop a harmful draft leaving
# should still cover the category. Coverage and likelihood are different
# claims. "hurt yourself" is NOT here — "you're only hurting yourself" is
# ordinary argumentative phrasing in exactly the register a reply lands in.
SELF_HARM_DIRECTED_PATTERN = re.compile(
    r"\b(?:kill\s+yourself|end\s+your\s+(?:own\s+)?life|kys)\b", re.I)


# A pile-on aimed at a PERSON: character attacks plus crowd language. Replying
# into one is both an ethics problem and a strategy problem (the ratio risk is
# entirely downside), and the analyzer happily called it "a live thread".
PILE_ON_PATTERN = re.compile(
    # A pile-on needs a PERSON on the receiving end. "is a scam" fired on
    # "This model is a scam at that price point" — a judgement about a product,
    # and squarely his register — so the subject now has to be a person or a
    # handle. A named individual ("Sam is a fraud") is not matched here on
    # purpose: the cost of blocking his product criticism is higher than the
    # cost of missing one shape the clauses below mostly catch anyway.
    # `@handle` cannot sit behind a \b — "@" is not a word character, so the
    # boundary fails at the start of a mention and "@someone is a scammer"
    # slipped through. The handle alternative carries its own anchor.
    r"(?:(?:\b(?:he|she|they|this (?:guy|founder|ceo|man|woman|person|dev|account))|@\w+)\s+"
    r"is a (?:fraud|scam(?:mer)?|liar|creep|predator|criminal)|\b"
    r"(?:everyone|anyone) who (?:worked with|supports|defends) (?:him|her|them)|"
    r"complicit|absolute scum|human garbage|cancel (?:him|her|them)|"
    r"expose (?:him|her|them)|deserves to (?:lose|be fired|be ruined)|"
    # Softer dogpiles read as ordinary criticism to the old pattern: a
    # condemnation aimed at one PERSON plus a call for the timeline to look.
    # "Absolutely disgraceful behaviour from this founder. Everyone should see
    # what he did." matched nothing and drew a real reply.
    r"(?:disgraceful|disgusting|shameful|vile|appalling)\b[^.!?\n]{0,40}\b"
    r"(?:from|by)\s+(?:this|that)\s+(?:founder|ceo|guy|man|woman|person|dev)|"
    r"everyone (?:should|needs to) (?:see|know) what (?:he|she|they) did)\b", re.I)


def _combined(context: PostContext) -> str:
    return "\n".join(filter(None, [context.text, context.parent_text, context.quote_text, *(context.thread_text or [])]))[:100_000]


def assess_context(context: PostContext) -> SafetyAssessment:
    text = _combined(context)
    result = SafetyAssessment()
    if context.visibility == "protected":
        result.local_only = True
        result.research_allowed = False
        result.media_reuse_allowed = False
        result.severity = "notice"
        result.categories.append("protected_context")
        result.reasons.append("Protected-post context is never sent for research or reused in media.")
    if context.sensitive:
        result.media_reuse_allowed = False
        result.categories.append("sensitive_media")
        result.reasons.append("Sensitive media is excluded from reuse recommendations.")
        result.severity = "warning"
    for category, pattern in PII_PATTERNS:
        if pattern.search(text):
            result.blocked = True
            result.local_only = True
            result.research_allowed = False
            result.media_reuse_allowed = False
            result.categories.append(category)
            result.reasons.append("The context may expose private identifying information, so drafting is blocked.")
            result.severity = "critical"
    if INTIMATE_PATTERN.search(text):
        result.blocked = True
        result.local_only = True
        result.research_allowed = False
        result.media_reuse_allowed = False
        result.categories.append("intimate_content")
        result.reasons.append("Potential non-consensual intimate content cannot be used for drafting or media reuse.")
        result.severity = "critical"
    if THREAT_PATTERN.search(text) or HARASSMENT_PATTERN.search(text):
        result.blocked = True
        result.categories.append("targeted_harm")
        result.reasons.append("Requests that encourage threats or targeted harassment are blocked.")
        result.severity = "critical"
    if _is_tragedy(text):
        result.blocked = True
        result.categories.append("human_harm")
        result.reasons.append("People were hurt here. This is not a thread to work an angle in — "
                              "if you want to say something, say it in your own words.")
        result.severity = "critical"
    if SELF_HARM_PATTERN.search(text):
        result.blocked = True
        result.categories.append("self_harm")
        # DECLINE, DO NOT COMPOSE. This renders on HIS card, so it speaks to
        # him. No crisis-line text anywhere a draft can carry it — a stranger
        # in crisis receiving a form letter with his name on it is a worse
        # outcome than the gap this closes.
        result.reasons.append("Someone is describing harm to themselves. This is not a "
                              "thread to reply in — if you want to reach out, do it in "
                              "your own words.")
        result.severity = "critical"
    if PILE_ON_PATTERN.search(text):
        result.blocked = True
        result.categories.append("pile_on")
        result.reasons.append("This is a pile-on aimed at a person. Adding to it risks the "
                              "account and helps nobody.")
        result.severity = "critical"
    if PROMPT_INJECTION_PATTERN.search(text):
        result.categories.append("prompt_injection")
        result.reasons.append("Instruction-like text in the page is treated as untrusted content and ignored.")
        result.severity = "warning" if result.severity in ("none", "notice") else result.severity
    result.categories = list(dict.fromkeys(result.categories))
    result.reasons = list(dict.fromkeys(result.reasons))
    return result


def assess_draft(text: str) -> SafetyAssessment:
    result = SafetyAssessment()
    for category, pattern in PII_PATTERNS:
        if pattern.search(text):
            result.blocked = True
            result.categories.append(category)
            result.reasons.append("Draft appears to contain private identifying information.")
            result.severity = "critical"
    if INTIMATE_PATTERN.search(text) or THREAT_PATTERN.search(text) or HARASSMENT_PATTERN.search(text):
        result.blocked = True
        result.categories.append("targeted_harm")
        result.reasons.append("Draft contains unsafe targeted or intimate content.")
        result.severity = "critical"
    if SELF_HARM_PATTERN.search(text) or SELF_HARM_DIRECTED_PATTERN.search(text):
        result.blocked = True
        result.categories.append("self_harm")
        result.reasons.append("Draft refers to self-harm. It is not sent.")
        result.severity = "critical"
    if DECEPTIVE_PATTERN.search(text):
        result.blocked = True
        result.categories.append("deception")
        result.reasons.append("Draft asks for fabricated or knowingly misleading content.")
        result.severity = "critical"
    if ENGAGEMENT_BAIT_PATTERN.search(text):
        result.categories.append("engagement_bait")
        result.reasons.append("The draft uses explicit engagement bait; prefer a substantive reason to respond.")
        result.severity = "warning"
    return result


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def _registrable(url: str) -> str:
    """The host, minus a leading `www.`. Deliberately not a full public-suffix
    parse — this compares a draft's links against links that were already on
    screen, and an exact host match is the honest bar for that."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def unvouched_urls(draft: str, allowed_sources: Iterable[str]) -> list[str]:
    """URLs in the draft whose host appears in NOTHING the model was shown.

    This is the one outcome a successful prompt injection can produce that the
    human gate does not catch. Everything else the architecture already holds:
    contexts are json.dumps-embedded so injected text cannot break out of its
    field, `x-action-guards` means model output can never reach a submit
    control, and the user presses Post themselves. But a draft that reads
    perfectly and carries an attacker's link is exactly what a person skims
    past — and it posts under their name, to their followers.

    So a link has to come from somewhere: the post being replied to, its
    parent or quote, its media, or a research source actually returned for this
    request. A host from none of those was invented somewhere between the page
    and the draft, and that is worth stopping to ask about.
    """
    vouched = {_registrable(str(source)) for source in allowed_sources if source}
    vouched.discard("")
    seen: list[str] = []
    for url in _URL_RE.findall(draft or ""):
        host = _registrable(url)
        if host and host not in vouched and url not in seen:
            seen.append(url)
    return seen


def safe_source_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.lower()
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False
