"""Decide what the reply SAYS BACK, before it is written.

`point.py` did this for posts and it is the change that fixed them: a writer
handed four facts writes four sentences, so the point is chosen first and the
draft is written to argue that one thing. Replies never got the equivalent, and
they need it more, because a reply has something a post does not — a person who
just said something, and a room that already read it.

Read what the reply path actually produced (live, 2026-07-26, `reply_arena.py`
and `generation_candidates`), because every rule below comes from one of these:

    @somedev "Been running Qwen locally for a month. The bottleneck was never
              the model, it was memory bandwidth."
      -> "So you're saying Qwen's not the bottleneck? What's the real issue
          then?"                      the parent ANSWERED that in its own
                                      second clause. The reply did not read it.

    @areader "This is the clearest take I've read on local inference. What rig
              are you running it on?"
      -> "You're running it on a GPU? I'm on a CPU and it's already faster."
                                      he was ASKED. The reply asks back, and
                                      invents a machine to do it.

    @sama    "...plan the best three options, make a full-stack site where the
              9 of us can coordinate..."
      -> "I can't wait to see how Fable 5 handles a trip plan with 8 friends."
                                      Fable appears nowhere in that post. It
                                      appears in HIS ARCHIVE.

    @swyx    "This is the part everyone is missing."
      -> "So you're saying the missing piece is the moon?"
                                      no subject in the parent, so one was
                                      invented.

Four different bugs with one shape: the draft was written from the account
rather than from the conversation. So this module answers, deterministically
and before any drafting:

  * WHAT KIND of thing am I replying to
  * WHAT MOVE does that kind deserve
  * WHAT AM I ALLOWED TO NAME (the post, its parent, its quote, its thread,
    and verified evidence — never the archive)

On the move, his instruction is the design: "replies are mainly about people
agreeing on what the post said." That is not a licence to write "so true" — the
quality gate kills bare agreement and should. It is a statement about which way
a reply that earns anything actually points. A reply gets liked when the people
who already read the parent see it and think *yes, and that is the part that
matters*. So AGREEMENT WITH A SPECIFIC ADDITION is the default, and
disagreement is a move you earn by having something that contradicts the post —
not the house style.

The old default was the opposite. The direction string asked for "a clear
opinion, comparison, correction" and the source rule demanded "a counter-
example, a specific disagreement", so the model argued with a man reporting his
own month of measurements. Nobody in that thread co-signs that reply.

WHAT IS PROMPTED AND WHAT IS CUT
--------------------------------
Half of this file is regexes that remove things after generation rather than
rules that ask for them, and that split is the most useful thing measured here.

    "So you're saying" opened 6 of 26 drafts. The plan then added an explicit
    "Never open with: 'So you're saying'". It opened 3 of 4.

    A register line reading `no "suggests", no "indicates"` produced two drafts
    using "suggests" in the very next run.

Naming a phrase to a small model is handing it the phrase; the verb in front
of it does not register. So the prompt carries only positive instructions and
never quotes a construction, and everything forbidden is taken out afterwards
(`strip_strawman_opener`, `strip_analyst_hedge`, and `intelligence.strip_
formulas`). Where variety is wanted, the habit is described by SHAPE — "your
last few opened with a question" — never by example.

Every threshold in this file was checked against his real archive before it
shipped, and the bar was the same each time: zero of his own 35 texts may be
flagged. A false positive in an integrity gate deletes a good reply, and when
it hits every candidate it becomes silence — which he has ruled out.

Measured over the fourteen surfaces in `tools/reply_arena.py`, scored by
`tools/reply_scoreboard.py`: clean replies went from 4/26 to 14/25, and drafts
that named something the conversation never mentioned, bounced the question
back, opened with a strawman, claimed experience the account does not have, or
sneered at the author all went to zero and stayed there.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field

# How long the point call may take before its answer is discarded. One short
# completion on the API model is well under a second (S5, 2026-09-21; this was
# 25 s for a local model). The drafting paths no longer make this call at all:
# the draft object carries its own point (S3); this remains for callers that
# still plan a point on its own.
_POINT_BUDGET_S = 4.0

from .subject import _NOT_A_NAME, _SUBJECT_ALIASES, _DOMAIN_TERMS, _DOMAIN_VERBS


# CAPITALISED IS NOT THE SAME AS NAMED. Anything at the start of a sentence is
# capitalised, and `subject._NOT_A_NAME` was written for topic labels ("Opus 5
# AI Model"), not for prose — so it does not carry the words that actually open
# sentences in replies. Left alone, "Maybe the cost is lower", "Either way",
# "Half the price" and "Then why's the bandwidth the issue" each registered as
# a company the conversation never mentioned.
#
# That matters more than tidiness: a name the conversation did not mention is
# an INTEGRITY failure and refuses the draft outright, so a false one here is a
# good reply deleted and, when it happens to every candidate, silence.
_NOT_A_NAME_IN_PROSE = frozenset("""
maybe either neither both half twice once again still then also though although
yes no sure honestly frankly turns look wait alright okay because since unless
while whether every each another other same different sorry thanks wow oh hey
guess seems feels sounds funny weird crazy insane wild fair true false right
wrong first second third last next best worst better worse
""".split())

# CATEGORIES, NOT NAMES. Written in capitals, but nobody's company or product: a SaaS founder
# answering a post about a payment provider may say "small SaaS teams" (the golden run: the reply
# was refused as naming "something the conversation never mentions"). A real name (Stripe,
# Goldman Sachs, Peloton) is still an intruder.
_CATEGORY_TERMS = frozenset("""
saas b2b b2c d2c smb smbs mrr arr ltv cac roi kpi kpis okr okrs mvp crm erp seo sem ppc
ceo ceos cto ctos cfo cfos coo vp vps vc vcs hr ops devops mlops qa ux ui cli ide sdk sdks
llm llms ai ml gpu gpus cpu cpus api apis oss faq faqs pdf csv url urls usd eur gbp
hiit bmi rpe amrap emom pr prs diy
""".split())


def named_things(text: str) -> set[str]:
    """Every product or company the text actually names, at PRODUCT level.

    `subject.subjects()` deliberately collapses "Claude", "Opus" and "Fable"
    onto one key, because for topic variety they are one subject — posting
    about Opus twice is repeating yourself whichever name you use.

    A reply needs the opposite resolution. Under a post about Opus 5, dropping
    in "Fable 5" is precisely the intrusion this is here to catch, and at org
    resolution the two are indistinguishable. So: the literal tokens, plus the
    organisation each one belongs to, so that answering a post about Claude by
    saying "Anthropic" is still inside the conversation while "Fable" is not.
    """
    found: set[str] = set()
    for word in re.findall(r"[a-z0-9][a-z0-9.\-]{1,}", (text or "").lower()):
        stem = word.strip(".-")
        if stem in _SUBJECT_ALIASES:
            found.add(stem)
            found.add(_SUBJECT_ALIASES[stem])
    # SENTENCE-INITIAL CAPITALS ONLY COUNT IF THEY ARE ACTUALLY NAMES. Every
    # sentence starts with a capital, so a positional rule is the only one that
    # separates "Serving a 30B is cheaper" from "Cohere cuts pricing" — and a
    # stoplist can never be finished, because English has more sentence
    # openers than anyone will enumerate. `radar._extract_subject` reached the
    # same rule for the same reason.
    #
    # It errs toward missing an unfamiliar company at the start of a sentence,
    # which is the right way round here: a missed name is a reply that names
    # one thing too many, while a false one refuses the draft outright.
    body = text or ""
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9.\-]{2,}\b", body):
        stem = match.group(0).lower().strip(".-")
        if (stem in _NOT_A_NAME or stem in _NOT_A_NAME_IN_PROSE
                or stem in _DOMAIN_TERMS or stem in _DOMAIN_VERBS or stem in _CATEGORY_TERMS):
            continue
        before = body[:match.start()].rstrip()
        opens_a_sentence = not before or before[-1] in ".!?\n:"
        if opens_a_sentence and stem not in _SUBJECT_ALIASES:
            continue
        found.add(stem)
        if stem in _SUBJECT_ALIASES:
            found.add(_SUBJECT_ALIASES[stem])
    return found


# ---------------------------------------------------------------- what it is
#
# Deliberately not a model call. The kind of post something is, is legible from
# its surface, and a classifier that costs two seconds and can hallucinate is a
# bad trade for something a regex gets right. The POINT still gets a model call
# (see `decide_reply_point`) because that is a judgement; this is a reading.

# First-person past tense plus a finding. "Been running X for a month", "we
# spent the week", "shipped my first". This is the one kind you must never
# argue with: it is their own measurement, and the room believes them.
# THE PRONOUN GETS DROPPED. "Spent the week replacing our retrieval stack"
# is first-hand experience with the "I" left off, which is ordinary on X
# and which the I/we requirement below misses entirely — that post fell
# through to the complaint branch instead.
_FIRSTHAND = re.compile(
    r"^(?:spent|ran|tried|tested|built|shipped|switched|moved|replaced|"
    r"measured|benchmarked|deployed)\b|"
    r"\b(?:i|we)\s+(?:ran|run|spent|tried|tested|built|shipped|switched|moved|"
    r"replaced|measured|benchmarked|deployed|found|noticed|have been|'ve been)\b"
    r"|\bbeen\s+(?:running|using|testing|building|shipping)\b"
    r"|\b(?:my|our)\s+(?:first|team|setup|rig|stack|agent|bill)\b", re.I)

# Something is broken and they are unhappy about it. The room is already
# nodding; a contrarian reply here reads as carrying water for the vendor.
# "down" ON ITS OWN IS NOT AN OUTAGE. It matched "Writing DOWN what actually
# mattered" — a post about a week of work, read as a complaint, which then
# took the complaint branch and produced the same analyst line in three
# consecutive runs. It also matches "calmed down", "narrowed down", "down the
# line". A service that is down says so with a verb in front of it.
_COMPLAINT = re.compile(
    r"\b(?:is|was|are|were|went|goes|going|still|been|stays?)\s+down\b|"
    r"\bdown\s+(?:again|for everyone|right now)\b|"
    r"\b(?:outage|broken|fails?|failing|failed|error|errors|502|503|529|"
    r"nobody(?:\s+\w+){0,3}\s+(?:has |have )?(?:answered|replied|responded)|"
    r"support (?:ticket|hasn'?t)|"
    r"third day|again today|unusable|regression)\b", re.I)

# A launch or a release. His account is built on these — "my account is based
# off of replying to bug updates and releases like this".
_ANNOUNCEMENT = re.compile(
    r"\b(?:is here|is now|now available|generally available|introducing|"
    r"announcing|we'?re (?:launching|shipping|releasing)|just (?:shipped|launched|released)|"
    r"rolling out|out now|available today|in beta|public preview)\b", re.I)

# Written like a person laughing, not a person analysing. Lowercase openings,
# absurd scale, "bro"/"lol". The register matters: an earnest three-clause
# analysis under a joke is the single most robotic thing the account can do.
_JOKE = re.compile(
    r"\b(?:literally just|somehow|apparently|my agent|every single one|"
    r"not a single|of course it|at this point)\b", re.I)
# An outright laugh needs no other evidence. Everything else has to arrive in
# lowercase, because that is what separates "my agent opened 14 PRs overnight"
# from "Our agent opened 14 pull requests" — same facts, different room.
_LAUGH = re.compile(r"\b(?:lol|lmao|lmfao|bro|i'?m done|send help)\b", re.I)

_QUESTION_TO_ME = re.compile(r"\?")

# A question that is rhetorical furniture, not an ask. "Why does this matter?"
# in the middle of an announcement is the writer setting up their own answer.
_RHETORICAL = re.compile(
    r"\b(?:why does (?:this|that) matter|guess what|right\?|no\?|"
    r"who (?:would have )?knew|what could go wrong)\b", re.I)


@dataclass
class Room:
    """What is in front of the writer, and what the room expects back."""

    kind: str = "unclear"
    move: str = "add_detail"
    asked: str = ""                          # the literal question put to him
    subject: list[str] = field(default_factory=list)
    allowed: str = ""                        # every text a name may come from
    # The conversation ALONE — post, parent, quote — with no author name or
    # handle appended. `allowed` has those bolted on so a reply may address the
    # person by name, which makes it the wrong thing to measure the post's
    # LENGTH against: it turned a five-content-word post into a seven-word one
    # and let a draft that swallowed the post whole slip under the copy bar.
    visible: str = ""
    author: str = ""
    reason: str = ""                         # why this move, for the log
    language: str = ""                       # the room's language, if not English
    correction: str = ""                     # the fact the post got wrong
    figures: list[str] = field(default_factory=list)   # numbers the post gave
    # WHAT THEY WROTE, AND WHAT THEY MERELY SHOWED YOU. On a quote-tweet the
    # author may have written four words on top of somebody else's forty, and
    # every layer used to arrive as one blob — so the figures, the names and
    # the "their sentence" the brief kept pointing at all belonged to a person
    # who is not in this conversation. See `reply_instruction`.
    quoted: str = ""                         # the post THEY quoted, alone
    own_text: str = ""                       # the words THEY actually wrote
    has_media: bool = False                  # a picture is a post too
    # THE POINT, AS A VALUE (OPTION-4). It is still appended to the instruction
    # string as well — both payloads stay live until the consumers read the
    # field — but a gate two modules away no longer has to split a 1200-char
    # direction on a marker to find out what the reply was supposed to argue.
    # `planned_point()` below documents why it was ever a substring: threading a
    # field through DraftRequest "would touch the schema, the browser contract
    # and every test that builds one". That is exactly what OPTION-4 did.
    decided_point: str = ""

    @property
    def has_subject(self) -> bool:
        return bool(self.subject)

    @property
    def quotes_someone(self) -> bool:
        return bool(self.quoted.strip())

    @property
    def own_figures(self) -> list[str]:
        """The figures in THEIR OWN words — never the quoted post's."""
        return _figures_in(self.own_text)


def _poll_options(context) -> list[str]:
    """The choices a poll offers, which are content the reader can see.

    Shaped defensively: `poll` arrives from the extension as whatever X gave
    it, and has been seen as a dict of options, a bare list, and absent.
    """
    poll = getattr(context, "poll", None)
    if not poll:
        return []
    options = poll.get("options", poll) if isinstance(poll, dict) else poll
    if isinstance(options, (str, bytes)):
        return [str(options)]
    if not isinstance(options, (list, tuple)):
        return []
    out: list[str] = []
    for option in options[:8]:
        label = (option.get("label") or option.get("text") or ""
                 ) if isinstance(option, dict) else option
        label = str(label or "").strip()
        if label:
            out.append(label)
    return out


def _visible(context) -> tuple[str, str]:
    """(the post itself, everything visible around it).

    The thread matters: `inesml` posted "writing down what actually mattered"
    and the four parts underneath carried every fact worth replying to. A reply
    written from part 1 alone has nothing to work with and invents something.
    """
    text = str(getattr(context, "text", "") or "").strip()
    around = [
        str(getattr(context, "parent_text", "") or ""),
        str(getattr(context, "quote_text", "") or ""),
        *[str(part) for part in (getattr(context, "thread_text", None) or [])],
        # A POLL'S OPTIONS ARE THE POST. "which one ships first?" with the
        # options [Opus 5, GPT-5.6] read as a conversation naming NOTHING, so
        # the brief said "name nothing" about a post whose whole content is two
        # product names — and the one reply that answers it ("Opus 5, and it is
        # not close") would then have been refused as an intrusion, because the
        # only place those names appear is the field nobody read.
        *_poll_options(context),
    ]
    # NEWLINE, NOT A SPACE. These are separate posts by separate people, and
    # joining them with a space ran the last word of one into the first word of
    # the next: a quote-tweet reading "this changes the maths completely" over a
    # post reading "Serving a 30B is now cheaper..." arrived as the single
    # sentence "this changes the maths completely Serving a 30B is now cheaper",
    # which is not a sentence anyone wrote. Everything downstream that splits on
    # sentences — the echo measures, the point gate, `_kind` — was reading
    # across a boundary that exists in front of the reader's eyes.
    return text, "\n".join(part.strip() for part in [text, *around] if part.strip()).strip()


# X reports the post's language as a BCP-47 tag. Only the ones worth naming to
# a small model — an unknown tag is better left unmentioned than turned into an
# instruction to reply in a language nobody can check.
_LANGUAGES = {
    "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German",
    "it": "Italian", "nl": "Dutch", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "ru": "Russian", "tr": "Turkish", "pl": "Polish",
    "id": "Indonesian", "hi": "Hindi", "ar": "Arabic", "fa": "Persian",
    "he": "Hebrew", "sv": "Swedish", "vi": "Vietnamese", "th": "Thai",
}


def _language_name(tag) -> str:
    """"es" -> "Spanish". Empty for English, empty for anything unrecognised."""
    code = str(tag or "").strip().lower().replace("_", "-").split("-")[0]
    if not code or code == "en":
        return ""
    return _LANGUAGES.get(code, "")


def _question_asked(text: str) -> str:
    """The question the writer is expected to answer, or ''.

    Only a real ask. A rhetorical flourish inside an announcement is not a
    question to him, and treating it as one produced replies that solemnly
    answered the vendor's own setup line.
    """
    if not _QUESTION_TO_ME.search(text):
        return ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        sentence = sentence.strip()
        if not sentence.endswith("?"):
            continue
        if _RHETORICAL.search(sentence):
            continue
        return sentence
    return ""


# `entity_truth.describe` is written for a HUMAN reading a warning card, so it
# closes with advice: "Getting the lab wrong is the one mistake this audience
# always catches." That sentence is guidance about the draft, not a fact about
# the world, and this string goes into the prompt as "here is the fact" — a
# small model handed it will write it into the reply. Keep the factual
# sentences and drop the coaching.
_COACHING = re.compile(r"\b(?:getting the lab wrong|this audience|the one mistake)\b", re.I)


def _just_the_fact(described: str) -> str:
    """The checkable part of a correction, without the advice attached to it."""
    kept = [s.strip() for s in re.split(r"(?<=[.!?])\s+", described or "")
            if s.strip() and not _COACHING.search(s)]
    return " ".join(kept)


def _kind(text: str, visible: str) -> str:
    lowered = text.lower()
    words = text.split()
    # REGISTER IS READ FIRST, because it outranks content. "my agent opened 14
    # pull requests overnight and every single one renames a variable" is a
    # joke about the writer's own week — read for content it is firsthand
    # experience, and the reply that came back was an earnest benchmark note.
    # The tell is the lowercase opening (or an outright laugh), which is what
    # separates that post from "Our agent opened 14 pull requests overnight".
    #
    # A FIGURE DOES NOT DISQUALIFY A JOKE either: the 14 is the joke. An
    # earlier version of this rule excluded any text containing a digit and
    # classified the funniest post in the corpus as a factual claim.
    if len(words) <= 40 and (_LAUGH.search(text)
                             or (text[:1].islower() and _JOKE.search(lowered))):
        return "joke"
    if _FIRSTHAND.search(text):
        return "firsthand"
    if _COMPLAINT.search(text):
        return "complaint"
    if _ANNOUNCEMENT.search(text):
        return "announcement"
    # Nothing named and barely any text: "This is the part everyone is missing."
    # There is no subject here, and pretending there is, is where "the missing
    # piece is the moon" came from.
    #
    # THE WHOLE VISIBLE UNIT, not just the post. "This is the number that
    # should be scaring people" over a quote about 30B serving costs was read
    # as subject-less and answered as if nothing had been said — but the reader
    # can see the quoted post, and so can the writer. Register above is read
    # from the post itself (that is whose voice it is); whether a subject
    # exists is read from everything on screen.
    named = named_things(visible)
    # Vague means there is NOTHING to engage: no name, no figure, and not
    # enough text for one to be implied. All three, because any one of them
    # alone is wrong — "This is the number that should be scaring people" over
    # a quote reading "Serving a 30B is now cheaper than the API call it
    # replaces" names nothing a name-detector will accept, and is obviously not
    # a post without a subject.
    if (len(words) <= 14 and not named and not re.search(r"\d", visible)
            and len(visible.split()) <= 25):
        return "vague"
    if re.search(r"\d", visible) or named:
        return "claim"
    return "unclear"


def read_the_room(context, *, evidence: str = "", own_handle: str = "",
                  already_replied: bool = False) -> Room:
    """What am I replying to, and what does it deserve back."""
    text, visible = _visible(context)
    author = (str(getattr(context, "author", "") or "")
              or str(getattr(context, "author_handle", "") or "")).lstrip("@")
    asked = _question_asked(text)
    kind = _kind(text, visible)

    # DID THE POST GET A LAB WRONG? "OpenAI's Fable 5 is cheaper than Claude"
    # is the one mistake this audience always catches, and correcting it is
    # among the highest-value replies this account can send — it is his beat,
    # it is checkable, and the author usually answers. `entity_truth` already
    # detects it on the account's OWN drafts; nothing was looking at the post
    # being replied to, so the `correct` move existed in this file and could
    # never be reached.
    #
    # Computed here, above `allowed`, because the true vendor is often a name
    # the post itself never uses — the correction has to be sayable.
    correction = ""
    try:
        from . import entity_truth
        correction = _just_the_fact(entity_truth.describe(text) or "")
    except Exception:                                       # noqa: BLE001
        correction = ""

    # Everything a name is allowed to come from. THE ARCHIVE IS NOT IN HERE and
    # that is the entire point: `_injected_entities` was passed the voice
    # examples as part of its corpus, so every lab in his 33 posts was
    # permanently whitelisted, and "Fable" could be dropped under a post about
    # a weekend trip with no gate objecting. Measured directly:
    #     _injected_entities(draft, parent)             -> ['fable']
    #     _injected_entities(draft, parent + examples)  -> []
    #
    # The AUTHOR belongs in here too. Addressing the person you are replying to
    # by name or handle is not an intrusion — it is the most ordinary thing a
    # reply does — and without them "@chartguy, the drop is steeper than that"
    # was rejected for naming somebody who is standing right there.
    #
    # EXCEPT ON HIS OWN THREAD, where the author is HIM. `_reply_constraints`
    # surfaces `allowed` verbatim, so on his one real self-reply the writer was
    # told "the only names you may use: anthropic, dario, fable" plus the
    # account's own name, while another line called the post someone else's —
    # an open invitation to write "<Name> points out that…" as a continuation
    # of his own thought. Third member of the appropriation family after the
    # possessive repair ("my agent" -> "your agent"): same defect, names channel
    # instead of pronoun.
    #
    # Only when it is HIS. When somebody else mentions him and he replies,
    # naming himself is ordinary and stays allowed.
    own_post = bool(getattr(context, "is_own_post", False))
    allowed = " ".join(part for part in (
        visible, evidence,
        "" if own_post else str(getattr(context, "author", "") or ""),
        "" if own_post else str(getattr(context, "author_handle", "") or ""),
        str(getattr(context, "parent_author", "") or ""),
        correction,
    ) if part).strip()

    # --- the move -----------------------------------------------------------
    # Order is precedence, and each branch is a failure that happened.
    if getattr(context, "is_own_post", False):
        move, reason = "continue", "his own post — a self-reply continues it"
    elif already_replied:
        move, reason = "follow_up", "he already replied in this thread"
    elif own_handle and str(getattr(context, "parent_author", "") or ""
                            ).lstrip("@").strip().lower() == own_handle.lower():
        move, reason = "host", "they replied to his post — he is the host"
    elif correction:
        # Ahead of `answer` deliberately: when a post asks something AND gets
        # the lab wrong, the correction IS the answer, and answering inside a
        # false premise is worse than not answering.
        move, reason = "correct", "the post attributes a model to the wrong lab"
    elif asked:
        # He was asked. The reply answers. Bouncing it back ("You're running it
        # on a GPU?") is the rudest thing a reply can do and it happened twice
        # in eight drafts.
        move, reason = "answer", "the post asks him a direct question"
    elif kind == "firsthand":
        move, reason = "agree_and_add", "they reported their own experience"
    elif kind == "complaint":
        move, reason = "agree_and_add", "they hit a real problem; the room agrees"
    elif kind == "joke":
        move, reason = "joke", "the register is a joke, so the reply is one"
    elif kind == "vague":
        move, reason = "name_it", "no subject in the post — say the specific thing"
    elif kind == "announcement":
        move, reason = "add_detail", "a launch — the room wants the implication"
    else:
        move, reason = "agree_and_add", "default: land with the room and add one thing"

    return Room(kind=kind, move=move, asked=asked,
                subject=sorted(named_things(visible)), allowed=allowed,
                visible=visible,
                quoted=str(getattr(context, "quote_text", "") or "").strip(),
                own_text=text,
                has_media=bool(getattr(context, "media", None)),
                author=author, reason=reason, correction=correction,
                figures=_figures_in(visible),
                language=_language_name(getattr(context, "language", None)))


# What the post MEASURED, not what it named. "Claude Opus 5 is here, at half
# the price of Opus 4.5" contains no measurements at all — 5 and 4.5 are
# version numbers, and handing the writer "use one of these figures: 5, 4.5"
# invites exactly the version noise the claim gate spends its time blocking.
# A number directly after a product or company name is part of the name.
# The UNIT is part of the figure. Without it "Latency went from 1.9s to 700ms"
# yielded "1" and "70" — two numbers that appear nowhere in the post, offered
# to the writer as the post's own measurements.
_FIGURE_RE = re.compile(
    r"(?<![A-Za-z.])[$€£]?\d+(?:[.,]\d+)?"
    r"(?:%|ms|s|x|k|m|b|gb|tb|mb|kb)?(?![A-Za-z])", re.IGNORECASE)
_NAME_BEFORE_RE = re.compile(r"([A-Za-z][A-Za-z.\-]*)\s*$")


def _figures_in(text: str) -> list[str]:
    body = text or ""
    seen: list[str] = []
    for match in _FIGURE_RE.finditer(body):
        value = match.group(0)
        if not any(ch.isdigit() for ch in value) or value in seen:
            continue
        before = _NAME_BEFORE_RE.search(body[:match.start()])
        if before:
            word = before.group(1).lower().strip(".-")
            if word in _SUBJECT_ALIASES or before.group(1)[:1].isupper():
                continue                    # "Opus 5", "GPT 4.5" — a name
        seen.append(value)
    # THE MEASUREMENTS FIRST, YEARS LAST. Only three are shown, and "2024 to
    # 2026: $12.40 down to $0.90" offered "2024, 2026, $12.40" — two dates and
    # one price, when the price collapse is the entire point of the post. A
    # bare four-digit year is a timestamp, not something worth quoting back.
    seen.sort(key=_figure_rank)
    return seen[:6]


def _figure_rank(value: str) -> int:
    if value.isdigit() and len(value) == 4 and value.startswith(("19", "20")):
        return 2                            # a year
    if value[:1] in "$€£" or value.endswith(("%", "x")) or not value.isdigit():
        return 0                            # money, a percentage, a unit
    return 1


# ------------------------------------------------------------- how to write it
#
# One block per move. They are short on purpose: a small model given eight
# obligations satisfies none of them, and the general rules in `llm.py`
# already cover voice, honesty and length. These say only what is different
# about THIS turn in THIS conversation.
_MOVES: dict[str, str] = {
    "answer": (
        "THEY ASKED YOU: \"{asked}\" "
        "Answer it in your first sentence, plainly. Never repeat the question "
        "back and never ask one instead. "
        # THE ANSWER MOVE INVITES FABRICATION AND HAS TO BE TOLD NOT TO TAKE
        # THE INVITATION. Asked "what rig are you running it on?", the drafts
        # came back "Running on a 2016 laptop" and "I'm on a CPU and it's
        # already faster" — a machine that does not exist, invented because
        # the brief said answer and the model had nothing true to answer with.
        # It cannot know his hardware, his week, or what he tried.
        "IF THE QUESTION IS ABOUT YOUR OWN SETUP, YOUR EXPERIENCE, OR ANYTHING "
        "ONLY THE WRITER COULD KNOW, you do not have that answer and must not "
        "invent one — no machine, no test, no measurement, no anecdote. Say "
        "the thing that is true regardless of whose setup it is: what actually "
        "decides the outcome they are asking about. Then at most one line more."),
    "agree_and_add": (
        "YOU ARE WITH THEM ON THIS. They are right, and the room already "
        "agrees. Do NOT argue and do NOT play devil's advocate. "
        # FINISH THEIR SENTENCE. "Say the specific thing that makes their point
        # land harder" produced the parent inverted — "Qwen's not the problem,
        # but the memory bandwidth is" under a post reading "the bottleneck was
        # never the model, it was memory bandwidth". Agreeing and restating are
        # the same sentence to a small model unless you tell it where to stand:
        # one step PAST what they wrote, not alongside it.
        # ...AND ASKING FOR AN IMPLICATION GETS ONE, PHRASED AS AN IMPLICATION.
        # "Say what follows from it" was answered literally: "The memory
        # bandwidth bottleneck shows the real limit of their setup", "Opus 5
        # costs half what 4.5 did, proving its value for money" — the post,
        # then a conclusion drawn from it out loud. Six of twenty-three drafts
        # in one run were that sentence. The content wanted was right; the
        # grammar was doing the damage, because a consequence stated as a
        # deduction has to name its premise, and their premise is their post.
        #
        # ASKING FOR A STANDALONE LINE BACKFIRED, MEASURED. The fix tried was
        # "write it so it would still stand up on its own with their post
        # deleted" — sound in theory, because a line that survives deleting the
        # premise cannot be a deduction from it. In practice the model made the
        # line self-contained the obvious way: by putting their subject back
        # into it. Copy rejections went from none to two of fourteen, both
        # cases returning NO DRAFT AT ALL, and the analyst count did not move
        # (3 before, 3 after). Reverted.
        #
        # "Flat statement" survives from that attempt — it is the half that
        # targets the grammar without inviting a restatement. The explicit
        # "not their point reworded" comes back too: the own-goal this file
        # keeps relearning is about naming a PHRASE the model can copy, and
        # this names a shape, not a phrase.
        "Start where their sentence STOPS. Say the next thing straight out, as "
        "your own flat statement — who it hits, what it costs, what breaks "
        "next. Never say their point back in different words. One concrete "
        "addition, not a question back."),
    "add_detail": (
        "They announced something and the room has read it. React the way you "
        "actually would, with the one specific reason behind it: the price, "
        "the limit, the number, or what it kills. Not a summary, not "
        "analysis, not scepticism for its own sake, not a question."),
    "name_it": (
        "This post names nothing specific, so the reply supplies it. Say the "
        # "IN YOUR OWN WORDS" REMOVED 2026-07-31. Three words asking for a
        # paraphrase, in the brief for the one post shape that has nothing to
        # paraphrase but its own vague sentence — so the reply came back as
        # that sentence, reworded. Same clause family as "or the question it
        # raises", and the same remedy: delete the request rather than gate
        # the output it asks for.
        "concrete thing they are gesturing at. "
        "NEVER invent a subject to fill the gap. "
        # "Write a short reaction that is true without one" was the fallback,
        # and it produced "It's the way we split the work?" — a reply with
        # nothing in it, under a post with nothing in it. Asking is better than
        # emptiness here and it is the one place a question is the right move:
        # the author answering is the strongest thing a reply can cause, and a
        # post this vague is an invitation to ask.
        "If you genuinely cannot name it, ask them the ONE specific question "
        "that would make them say it — a real question about the thing itself, "
        "not 'what do you mean?'"),
    "correct": (
        "THE POST GETS A FACT WRONG, and here is the fact: {correction} "
        "Say the correct version in one plain line, early. Correct the claim, "
        "never the person — no 'actually', no scoring points. Add nothing "
        "else; the correction is the whole reply."),
    "joke": (
        "This is a joke. Reply like a person laughing with them, not a "
        "consultant. Short, same register, land one line. No analysis, no "
        "benchmarks, no 'the real question is'."),
    "continue": (
        "This is YOUR OWN post and the reply continues it as a thread, in "
        "first person. Write the NEXT thought: the follow-up, the detail you "
        "left out, or the answer to what your post raised. Never @-mention "
        "yourself, never address the author as 'you', never argue with it."),
    "host": (
        "{author} replied to YOUR post{asked_clause}. You are the host of this "
        "thread. Answer what they actually said, specifically and first, then "
        "leave them an obvious opening to come back — a real question about "
        "their setup or their experience, not a rhetorical one. Address them, "
        "not an audience."),
    "follow_up": (
        "You have ALREADY replied in this thread. This is the next turn, not "
        "the first one: never restate your earlier point. React to what has "
        "happened since, add the new thing, or sharpen where you stood."),
}

# Tics measured across the live drafts. Every one of these opened a reply that
# either strawmanned the author or asked back a question they had just
# answered, and eight of sixteen drafts used one.
_BANNED_OPENERS = (
    "So you're saying", "So you are saying", "Are you saying",
    "What's the catch", "What is the catch", "So let me get this straight",
)


def reply_instruction(room: Room, *, description_obtained: bool = False) -> str:
    """The writing brief for this turn — the move, and what it may name.

    `description_obtained` — has the caller ALREADY put a description of the
    post's image into the direction? Named for what it means, never
    "vision_succeeded": `main.py:_post_image_line` falls back to the author's
    alt text, so a description can arrive with no vision model in the picture,
    and the earlier version of this reasoning missed exactly that.

    Default False on purpose. Absent information must mean "the writer was told
    nothing about the image", which keeps the blind-post denial below FIRING.
    The opposite default would silently delete a guard that exists because a
    draft invented what an image showed — the empty-default trap that once made
    a model pin never-falsy and switched off two subsystems.
    """
    block = _MOVES.get(room.move, _MOVES["agree_and_add"])
    # ON HIS OWN THREAD A QUESTION IS STILL A QUESTION. `host` outranks
    # `answer` in the move ladder — being the host of the thread changes who
    # you are addressing — but it must not lose the thing they asked. Live,
    # Dana asked "you got it running on a single 4090?" and the reply came back
    # "So you're saying the model's MoE only wakes two experts?", which
    # answers nobody.
    asked_clause = f' and asked: "{room.asked[:140]}"' if room.asked else ""
    block = block.format(asked=room.asked[:140], author=room.author or "They",
                         asked_clause=asked_clause,
                         correction=room.correction[:220])
    # THE REGISTER, on every move. His short writing is "at this rate Grok 4.5
    # might launch before Opus 5" and "Half the price and this close to Fable 5
    # is kinda insane". What the reply path produced, twice in two rounds, was
    # "The cost reduction suggests they're optimizing for smaller-scale
    # deployments rather than large-scale enterprise use."
    #
    # Not gated, on purpose. He uses "suggests" himself — in a long post,
    # under a bullet, with the evidence above it — so a rule that rejects the
    # word would reject his own writing in the one place it belongs. What is
    # wrong is a fourteen-word reply written as an analyst's note, and that is
    # a register instruction, not a pattern to ban.
    # POSITIVE ONLY. The first version of this line read: no "suggests", no
    # "indicates" — and the very next run produced "The cost reduction
    # suggests they're optimizing for lower-end hardware" twice. Exactly the
    # own-goal documented on `_BANNED_OPENERS`, committed again three hours
    # later. Quoting a word to a small model is handing it the word.
    # THE LANGUAGE GOES FIRST, not last. It sat at the end of a 900-character
    # brief and the Spanish replies came back as the parent translated —
    # mirroring 0.56 against the post, where a real Spanish reply scores 0.33.
    # Writing in a second language is the most structural thing the draft has
    # to do, and a constraint buried behind eight other sentences is one a
    # small model applies to the last clause it read.
    lead = f"THIS IS A REPLY. {block}"
    if room.language:
        lead = (f"THIS IS A REPLY, and it must be written in {room.language} — "
                f"the post is in {room.language} and so is the room. Do NOT "
                f"translate their sentence back at them; say your own thing, "
                f"in {room.language}. {block}")
    # "IN ONE FLAT LINE" WAS DOING TWO JOBS AND THE SECOND ONE WAS DAMAGE.
    #
    # It was written to kill the analyst register, and it did — but "flat" also
    # reads as "no reaction", and the drafts came back with none. Measured:
    # 43% of his own short writing carries a personality marker ("kinda
    # insane", "at this rate", "turns out", "bro is not saving the
    # multiverse"), against 0 of 27 generated replies. On his own numbers a
    # joke or a reference is worth 2.38x the engagement, so a register this
    # flat is not neutral — it is the shape that gets least read.
    #
    # The anti-hedge half is kept, because that is the half that was working.
    # The rest is permission: the voice examples are now his short writing, so
    # the register only has to stop being suppressed to come through.
    # LAND A VERDICT, not a description. Measured on his own writing: what
    # separates his short replies from the thin generated ones is that his
    # carry a judgement — the generated ones name a fact and stop. "half the
    # price? That's a discount." states what it IS and scores 0.08; "Half the
    # price and this close to Fable 5 is kinda insane" says what he MAKES of
    # it and scores 0.43.
    #
    # Phrased as a shape, never as his phrases: quoting "kinda insane" here
    # would produce "kinda insane" on every reply, which is the same own-goal
    # as the banned-opener list.
    parts = [lead,
             "Say it like you would to someone next to you — plain, with "
             "whatever reaction you actually have. Never hedge it. Land where "
             "you stand on it: naming the thing and stopping is half a reply."]

    # WHO SAID WHICH HALF. A quote-tweet puts two authors on the screen and the
    # brief used to describe them as one. Nothing in it said "quote", so every
    # instruction pointing at "their sentence", "their point" or "what they
    # said" resolved against a blob that was mostly somebody else's writing —
    # and `agree_and_add` in particular ("Start where their sentence STOPS")
    # aimed the whole reply at the wrong author.
    #
    # Said BEFORE the move block and the naming rule, because it changes what
    # every one of them refers to. Their own words go in verbatim when they are
    # short enough to fit, since on this surface the four words they added ARE
    # the post being replied to, and they are the easiest thing to lose.
    if room.quotes_someone:
        theirs = " ".join(room.own_text.split())
        said = f' They wrote: "{theirs[:180]}"' if theirs else (
            " They added no words of their own — the quote IS their whole post,"
            " so react to the fact they are passing it on.")
        parts.append(
            f"CAREFUL, MORE THAN ONE POST IS ON THE SCREEN. "
            f"{room.author or 'They'} is quoting somebody else's post, and you "
            f"are replying to {room.author or 'them'}, not to the person they "
            f"quoted.{said} Answer what THEY said about it. Never tell them "
            "what their own quoted post says, and never credit them with "
            "doing, building or measuring anything that happens in it.")

    # A POST CAN BE A PICTURE. With `text` empty the room reads as vague, the
    # move becomes `name_it`, and the brief then talks about "their words" and
    # "the phrase of theirs you have a view on" when the post contains no words
    # at all — an instruction that cannot be followed, about a post the writer
    # is not told exists. It is also the surface where a draft is likeliest to
    # invent what the image showed.
    # AND NOT WHEN THE WRITER HAS ALREADY BEEN TOLD WHAT THE IMAGE SHOWS.
    # `main.py` appends `_post_image_line` to the same direction string this
    # block joins — "The post has an IMAGE attached showing: … never contradict
    # what it shows" — and this block then said "you cannot see it … must not
    # describe it", denial last. Two instructions that were each written for a
    # real failure, in one prompt, contradicting each other. Neither knew the
    # other had run; the fix is the fact passed between them, not a reordering.
    if (not room.visible.strip() and getattr(room, "has_media", False)
            and not description_obtained):
        parts.append("THIS POST IS AN IMAGE WITH NO TEXT. You cannot see it, "
                     "so you do not know what is in it and must not guess, "
                     "describe it, or name anything it might contain. Reply to "
                     "the fact that they posted it, or say nothing at all.")

    # THE NAMING RULE. This is the reply-side half of the profile-leak fix. On
    # a post the subject is chosen, so the fix lives in the topic selector; on
    # a reply the subject is HANDED to you, which makes any other name an
    # objective intrusion rather than a judgement call.
    # Compressed hard on purpose. The direction is capped at 1200 characters
    # and the web facts share it: at full length this block plus the figures
    # line pushed the `numbers` case to 1287 and the evidence was dropped
    # silently. Every sentence here costs the reply its grounding.
    if room.subject:
        parts.append("Name only what is in this conversation: "
                     + ", ".join(room.subject[:8])
                     + ". No other company, model or product — the style "
                       "examples show HOW this account writes, not WHAT about.")
    else:
        parts.append("This conversation names nothing, so name nothing. Any "
                     "company or product you reach for came from elsewhere.")

    # NAME SOMETHING THEY SAID. Measured across a full run: eight of
    # twenty-seven replies engaged nothing the post actually put on the table —
    # under "Serving cost per million tokens, 2024 to 2026: $12.40 down to
    # $0.90", the reply was "The cost drop is a major shift in compute
    # efficiency", which ignored every figure in front of it. That is his
    # "no attention to detail" complaint in one draft.
    #
    # THEIR figures, never invented ones: the claim gate blocks any number the
    # evidence does not carry, so the only numbers available are the ones the
    # post handed over — which is exactly the ones worth using.
    #
    # NOT ON EVERY MOVE, and this is a self-inflicted lesson. The "pick one of
    # their words" block below was written for the nameless-post case and then
    # appended to every move that reached it, which put it on the two moves it
    # contradicts outright:
    #
    #   joke     "Reply like a person laughing with them, not a consultant.
    #             No analysis." ...followed by "say what you make of that one,
    #             why it is the one that counts, or what it would really take."
    #             That is the consultant, in the sentence after the ban.
    #   name_it  "Say the concrete thing they are gesturing at, IN YOUR OWN
    #             WORDS." ...followed by "write it EXACTLY AS THEY WROTE IT."
    #             Two opposite instructions about the same phrase.
    #
    # A brief that grants and forbids the same thing is not a brief — the same
    # fault this whole module keeps rediscovering, committed by the fix for it.
    if room.move == "joke":
        pass                      # the punchline needs no specifics rule
    elif room.move == "name_it":
        # Its own block already says what to name; all it was missing is that
        # a borrowed phrase has to be borrowed exactly.
        parts.append("If you use any phrase of theirs, use it exactly as they "
                     "wrote it, never a reworded version.")
    elif room.figures:
        # THE PERMISSION HAS TO BE EXPLICIT, because the general rules forbid
        # figures in the strongest terms — "Never state a percentage, multiple,
        # dollar figure, date... unless it appears in the evidence" — and the
        # post's own numbers are not what that block means by "the evidence".
        # So the writer was reading a blanket ban and dropping their figures:
        # handed "$12.40 down to $0.90", it returned "a major efficiency leap
        # in token processing". Quoting their number back is the opposite of
        # inventing one, and the prompt has to say so.
        # ...AND "THEIRS" HAS TO MEAN THEIRS. On a quote-tweet this line was
        # simply false. @dana writing "this changes the maths completely" over
        # somebody else's post produced `figures = 30B, $12.40, $0.90` — every
        # one of them measured by the quoted author, none of them dana's — and
        # then told the writer dana had PUBLISHED them and to use one. The
        # reply came back crediting dana with a measurement she never made,
        # which is the whole of "the replies just don't make sense" on this
        # surface. The figures are still usable; they just belong to someone
        # else, and a reply that says so is right where one that does not is
        # wrong.
        # ...AND THE TEST IS THE AUTHOR'S OWN TEXT, not which layer it came
        # from. The first version of this asked "is it a quote-tweet", which
        # fixed the quote surface and left the commonest one wrong: a reply
        # INTO a thread. @dana answering "yeah exactly" under @ben's
        # measurements still produced "THEIR OWN FIGURES, which they published:
        # $12.40, $0.90 ... Use one." — dana published nothing. Same false
        # sentence, one layer over.
        #
        # So the question is only ever: are these numbers in the words THEY
        # wrote? If they are, they published them and the writer should say so.
        # If they are not, they belong to somebody else in the conversation —
        # quoted, parent or thread, it makes no difference to the claim being
        # made — and the reply may use one without crediting anybody.
        own = room.own_figures
        if own:
            parts.append("THEIR OWN FIGURES, which they published: "
                         + ", ".join(own[:3])
                         + ". These count as evidence — repeating one of THEIRS is "
                           "not inventing a number, it is quoting them, and a reply "
                           "that drops their figures reads as unread. Use one. "
                           "Never add a figure they did not give.")
        else:
            parts.append("THESE FIGURES ARE IN THE CONVERSATION BUT NOT IN "
                         "THEIR POST: " + ", ".join(room.figures[:3])
                         + ". Somebody else put them there. Using one is right; "
                           "telling the person you are replying to that they "
                           "measured, published or found it is not. Never add a "
                           "figure nobody gave.")
    elif room.subject:
        # THEIR WORD, NOT YOUR WORDING OF IT. "Name one specific thing they
        # actually said" was read as "mention the topic again", and a reply
        # that half-rewords their phrase is worse than one that ignores it:
        # the reader sees their own sentence coming back wrong. Exactness is
        # the whole instruction — a borrowed phrase is either theirs or it is
        # yours, never a blend.
        parts.append("Name one specific thing they actually said, not a "
                     "summary of the topic. Use their word for it, exactly as "
                     "they wrote it — never your own rewording of their phrase.")
    else:
        # NOTHING NAMEABLE IS NOT NOTHING TO POINT AT — and this is the branch
        # that had no instruction at all.
        #
        # Measured 2026-07-31 on the live reply to @thsottiaux ("The day we
        # develop really good models. There will be signs. Reliability
        # increasing despite load going up and up. Sudden efficiency gains.
        # Things getting faster. Resets."):
        #
        #   room.subject = []   room.figures = []   kind = "unclear"
        #   -> move `agree_and_add`: "One concrete addition"
        #   -> naming rule:          "names nothing, so name nothing"
        #   -> this block:            SILENT, both branches above being empty
        #
        # So the brief demanded something concrete, forbade every new noun,
        # and named no third source of concreteness. The only vocabulary left
        # was the post's own, and the draft that came back was the post's five
        # nouns rearranged: "Reliability up under load means resets are
        # actually signs of progress not failure. Just sudden gains."
        #
        # That is a prompt bug, not model randomness — the instruction as
        # written has no compliant answer. The third source is their own
        # sentence: a reply can be perfectly concrete by picking ONE of the
        # things they listed and landing on it, which names nothing new.
        # His archive does exactly this, 4 of 48 posts, always a short
        # borrowed term and never a whole sentence: the link was "phishing",
        # the "Jarvis" comparison, the first time "AI agent" actually feels
        # like delegating work.
        parts.append("They named nothing you can look up, so the specific "
                     "thing is one of THEIR OWN WORDS. Pick the single word or "
                     "phrase of theirs you have a view on, write it exactly as "
                     "they wrote it, and say what you make of that one — why "
                     "it is the one that counts, or what it would really take. "
                     "You may put their word in quote marks. Leave everything "
                     "else they said alone: covering their whole point is how "
                     "a reply turns into their post said back.")

    # (The language instruction is in `lead` above — `PostContext.language` had
    # existed and been stored the whole time without ever reaching the writer.)
    # NO LIST OF FORBIDDEN OPENERS. There used to be one, naming "So you're
    # saying" first, and it is the clearest own-goal in this file: the phrase
    # opened 6 of 26 drafts before the list existed and 3 of 4 after. A small
    # model handed a phrase uses the phrase, whatever the verb in front of it
    # is. The tic is removed after generation now (`strip_strawman_opener`),
    # and what the prompt carries is the positive version.
    # The opener line is gone: `strip_strawman_opener` removes the tic
    # mechanically after generation, so spending 60 characters of a shared
    # budget asking for it a second time costs the reply its evidence.
    return " ".join(parts)


# How many recent replies count as "lately". Small on purpose: a habit is what
# the model is doing NOW, and a wide window averages a fresh tic away.
_RECENT_WINDOW = 6


_QUESTION_OPENER = re.compile(
    r"^\s*[\"“']?\s*(?:what|why|how|when|where|who|which|is|are|does|do|did|"
    r"can|could|would|should|will|so)\b", re.IGNORECASE)


def avoid_recent_openings(recent: list[str]) -> str:
    """Name the SHAPE the last few replies took, never the words they used.

    An earlier version listed the actual openings — "Your recent replies
    opened: 'So you're saying...'; 'What's the catch...'" — and it made things
    worse in the same way the static ban list did. Handed a phrase, a small
    model uses the phrase; the verb in front of it ("never", "avoid", "do not
    start") does not register. Measured across three rounds: "So you're saying"
    was suppressed and "What happens when" took its place at exactly the same
    rate, three times in one run across three unrelated posts.

    So this describes the habit instead of quoting it. "The last few opened
    with a question, open this one with a statement" is something a model can
    act on without being shown the words to reuse — and it generalises, which a
    phrase list never does.
    """
    openings = [str(t or "").strip() for t in recent if len(str(t or "").split()) >= 3]
    openings = openings[:_RECENT_WINDOW]
    if len(openings) < 3:
        return ""
    questions = sum(1 for text in openings
                    if _QUESTION_OPENER.match(text) or text.rstrip().endswith("?"))
    if questions * 2 >= len(openings):
        return (" Your last few replies all opened with a question. Open this "
                "one with a flat statement instead.")
    # Same first word over and over is the other habit worth naming, and the
    # word itself is safe to quote — it is one word, not a construction.
    firsts = Counter(text.split()[0].lower().strip('"“\'') for text in openings)
    word, count = firsts.most_common(1)[0]
    if count * 2 >= len(openings):
        return (f' Your last few replies all started with "{word}". '
                "Start this one some other way.")
    return ""


# ------------------------------------------------------------------ the point
# THE FRAMING IS PARAMETERISED BECAUSE ONE OF THESE POSTS IS HIS.
#
# `read_the_room` selects `continue` from `is_own_post` and the brief then says
# "This is YOUR OWN post… write the NEXT thought, in first person." This prompt
# used to answer it with "A person wrote this post: @example_builder… You are
# writing ONE reply to it" — and the point is chosen BEFORE the draft exists,
# so the stranger framing landed first and decided what the reply was about.
# Three components described a stranger's post and one described his own; the
# one that was right was the only one written for the case.
#
# Framing only, deliberately. The rules below still say "their post" where the
# referent is unambiguous from the two lines above, and rewriting them is a
# separate change with its own calibration.
_SELF_FRAMING = "This is YOUR OWN post, which you wrote:"
_OTHER_FRAMING = "A person wrote this post:"
_SELF_TASK = ("You are writing the NEXT post in your own thread — a continuation, "
              "not a reply to a stranger.")
_OTHER_TASK = "You are writing ONE reply to it."

_POINT_PROMPT = """{framing}

{author}: "{post}"
{extra}
{task} Your move is: {move}
{move_note}
{already_said}
Decide the SINGLE thing your reply says back. Not a summary of {whose} post —
the one thing you actually add to it, which a reader of the thread could
repeat afterwards.

Rules for your answer:
- ONE sentence, under 20 words.
- It must be about {whose_caps} post. Do not introduce a company, model or
  product it did not mention.
- It MUST reuse at least one concrete word or number from their post — the
  thing they named, the figure they gave, the term they used. A sentence that
  could have been written without reading their post is the wrong sentence.
- It must be a specific thing, not a description of what you will do.
- No numbers except ones their post or the evidence gives you. A figure you
  remember but cannot point to is a figure you do not use.
- No preamble, no quotes. The sentence only.

Your sentence names something from their post and says what follows from it,
with the reason attached. Two things that are not that: naming their point and
stopping, which is a summary; and describing what you intend to do, which is a
plan.
"""
# NO WORKED EXAMPLE LIVES HERE ANY MORE, and it cannot come back.
#
# The first version's examples were in this account's own domain and the model
# copied them into unrelated replies. The second version moved them to a bakery
# and added "write about THEIR post, never about bread" — which is the exact
# own-goal already recorded for banned phrases: naming the thing teaches it.
# Measured live 2026-07-27, both leaks verbatim from the Good lines:
#
#   post  : "This is the part everyone is missing."
#   reply : "The proving drawer is why their loaves beat the chain's."
#
#   post  : "What rig are you running it on?"
#   reply : "Their rig has one main oven and a smaller backup for extra batches"
#
# The first of those passed every gate in the product as CLEAN — it is
# concrete, it takes a position, it names no real company. Nothing downstream
# can see that the specifics came from the prompt instead of the post, which is
# why the fix has to be here: a sentence the model can lift is a sentence it
# will lift, whatever the surrounding instruction says. Describe the shape;
# never write a specimen.

_MOVE_NOTE = {
    "answer": "They asked you something. Your sentence must answer it.",
    "agree_and_add": "You agree with them. Your sentence is the addition, not the agreement.",
    "add_detail": "Say the consequence nobody in the thread has said yet.",
    "name_it": "Name the specific thing their post is gesturing at.",
    "correct": "State what is actually true, on the one point they got wrong.",
    "joke": "Match their joke. Your sentence is the punchline.",
    "continue": "This is your own post. Your sentence is the next thought in the thread.",
    "host": "They replied to you. Your sentence answers them and invites them back.",
    "follow_up": "You already replied. Your sentence is the new thing, not the old one.",
}

_MAX_POINT_WORDS = 22


def _clean(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = text.strip('"“”‘’').strip()
    for marker in ("Point:", "point:", "Answer:", "answer:", "Reply:", "reply:"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
    text = text.split("\n")[0].strip()
    words = text.split()
    if len(words) > _MAX_POINT_WORDS:
        text = " ".join(words[:_MAX_POINT_WORDS])
    return text


def _already_said(prior: list[str]) -> str:
    """The angle already offered for this exact post, for the point chooser.

    Quoting a previous draft here is the one place it is right to. Everywhere
    else in this file a phrase shown to the model is a phrase it reuses — but
    a retry is precisely the request "not that again", and the choice being
    made is which ANGLE to take, so the thing to avoid has to be nameable. It
    goes to the point call only, never to the writer, and the repeat gate is
    still behind it.
    """
    if not prior:
        return ""
    return ("\nYou already offered this angle on this same post, so pick a "
            f"different one:\n\"{prior[0][:200]}\"\n")


def decide_reply_point(llm, context, room: Room,
                       prior_for_this_post: list[str] | None = None) -> str | None:
    """One sentence: what this reply adds. None when nothing usable comes back.

    Same shape and same cost as `point.decide_point`, and rejected on the same
    grounds — plus one more that only replies need: a point that names
    something the conversation never mentioned is the leak arriving one stage
    early, and is thrown away here rather than gated later.
    """
    post = str(getattr(context, "text", "") or "").strip()
    if not post:
        return None
    extra = ""
    quoted = str(getattr(context, "quote_text", "") or "").strip()
    if quoted:
        extra = f'\nTheir post quotes: "{quoted[:400]}"\n'
    thread = [str(p) for p in (getattr(context, "thread_text", None) or [])][1:4]
    if thread:
        extra += "\nLater in their thread: " + " ".join(t[:160] for t in thread) + "\n"

    own = bool(getattr(context, "is_own_post", False))
    prompt = _POINT_PROMPT.format(
        framing=_SELF_FRAMING if own else _OTHER_FRAMING,
        task=_SELF_TASK if own else _OTHER_TASK,
        whose="your own" if own else "their",
        whose_caps="YOUR OWN" if own else "THEIR",
        author=room.author or "Someone", post=post[:700], extra=extra,
        move=room.move, move_note=_MOVE_NOTE.get(room.move, ""),
        already_said=_already_said(prior_for_this_post or []))
    try:
        raw = llm.router.chat(
            [{"role": "system",
              "content": "Return only the single sentence. No preamble."},
             {"role": "user", "content": prompt}],
            # WARMER THAN THE POST PLANNER (0.3) ON PURPOSE. The point is what
            # makes the draft deterministic: same room + same point + same
            # examples produced two byte-identical replies to @somedev in
            # consecutive rounds. That is fine for one draft and wrong for the
            # second press of the button, which has to offer him a different
            # angle or it is not a retry at all. The point is still bounded by
            # the same guards — under 22 words, about the parent, and rejected
            # outright if it names anything the conversation did not.
            options={"temperature": 0.7, "num_predict": 60, "timeout_s": 8.0},
            think=False)
    except Exception:                                       # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        from .llm import _strip_thinking
        raw = _strip_thinking(raw)
    except Exception:                                       # noqa: BLE001
        pass
    point = _clean(raw)
    return point if point_is_usable(point, room) else None


def point_is_usable(point: str, room: Room) -> bool:
    """The checks a reply's point has to pass, wherever it was decided.

    S3 (2026-09-21): the point used to be decided by its own model call in front
    of every reply (~0.7 s). The draft object now carries it (`point` before
    `text`), and the same checks run on what comes back.
    """
    point = _clean(point)
    if len(point.split()) < 4:
        return False
    # The point itself must not smuggle a name in. Same rule as the draft, one
    # stage earlier, because a leaked point produces a leaked reply that then
    # looks like it was "on plan".
    if intruders(point, room):
        return False
    # ...AND IT HAS TO BE ABOUT THEIR POST. The point is what the draft argues,
    # so a point that engages nothing they said produces a reply that engages
    # nothing they said — which was the last remaining fault class, at a third
    # of all drafts. Checking here is worth more than checking the draft: a bad
    # point poisons every candidate written from it, and rejecting it costs
    # nothing but the plan, which the writer can do without.
    if not engages_a_specific(point, room):
        return False
    # ...AND IT MUST NOT BE THEIR SENTENCE BACK. Asked to plan a reply to "This
    # is the part everyone is missing", the chooser answered "This is the part
    # everyone is missing." — which passes the engages-a-specific check
    # perfectly, because every word in it came from the post. A point that
    # restates cannot produce a reply that adds.
    # `_source_mirroring` is no use here — it needs four-plus tokens per
    # sentence and returns 0.0 for a line this short, which is exactly the
    # length a point is. The test that works at this size is simpler: does the
    # point contribute a single content word the post did not already have? A
    # sentence built entirely from their vocabulary is their sentence.
    if not (_shape_words(point) - _shape_words(room.allowed)):
        return False
    # ...AND IT MUST NOT BE THE PROMPT'S OWN WORDS. Whatever this file puts in
    # front of the model is text the model can hand back, and twice now it has:
    # a worked example written in this account's domain was copied into
    # unrelated replies, and after being moved to a neutral one it was copied
    # verbatim again ("The proving drawer is why their loaves beat the
    # chain's", under a post about AI). Nothing downstream catches it — that
    # line is concrete, takes a position, and names no real company, so it
    # scored CLEAN through every product gate.
    #
    # The examples are gone, so this guards against them coming back. It only
    # fires on words that are in the TEMPLATE and in neither the conversation
    # nor ordinary English, which is why it can afford to be a hard reject.
    if _speaks_the_templates_language(point, room):
        return False
    return True


# Content words that exist only in this module's instructions to the model. A
# point containing one did not come from the post, and the empty state is the
# correct one: with no worked examples left there is nothing here to leak.
_TEMPLATE_ONLY_WORDS: frozenset[str] = frozenset()


def _speaks_the_templates_language(point: str, room: Room) -> bool:
    """Did the plan borrow its specifics from the prompt instead of the post?"""
    if not _TEMPLATE_ONLY_WORDS:
        return False
    borrowed = _shape_words(point) & _TEMPLATE_ONLY_WORDS
    return bool(borrowed - _shape_words(room.allowed))


_POINT_LEAD = " THE POINT OF THIS REPLY, decided already: "


def planned_point(direction: str) -> str:
    """Pull the decided point back out of a direction string.

    The same trick `intelligence._planned_point` plays for posts, and for the
    same reason: the gate that checks a draft still argues its own point runs
    two modules away from the place the point was chosen, and threading a new
    field through `DraftRequest` for it would touch the schema, the browser
    contract and every test that builds one.
    """
    if _POINT_LEAD not in (direction or ""):
        return ""
    tail = direction.split(_POINT_LEAD, 1)[1].strip()
    return tail.split('"')[1] if tail.startswith('"') and tail.count('"') >= 2 else ""


def plan_reply(context, *, llm=None, evidence: str = "", own_handle: str = "",
               already_replied: bool = False,
               recent_replies: list[str] | None = None,
               description_obtained: bool = False) -> tuple[str, Room]:
    """The whole reply plan as one string to append to the direction.

    Returns (instruction, room). Best-effort throughout: a failure anywhere
    gives a shorter instruction, never an exception and never silence. The
    point call is skipped entirely when there is no model — the deterministic
    part of the plan is the part that stops the leak, and it must not depend on
    a generation succeeding.
    """
    room = read_the_room(context, evidence=evidence, own_handle=own_handle,
                         already_replied=already_replied)
    if recent_replies is None:
        try:
            from .storage import previously_drafted_replies
            recent_replies = previously_drafted_replies(limit=12)
        except Exception:                                   # noqa: BLE001
            recent_replies = []
    instruction = reply_instruction(room, description_obtained=description_obtained)
    instruction += avoid_recent_openings(recent_replies)

    # A SECOND PRESS ON THE SAME POST IS A RETRY. Same context, same room, same
    # move, same examples — so the second draft came back as the first one
    # reworded, which is not a retry at all. Seven of the nine drafts that
    # missed the clean bar in a full arena run were exactly this.
    prior_here: list[str] = []
    post_id = str(getattr(context, "post_id", "") or "").strip()
    if post_id:
        try:
            from .storage import replies_already_drafted_for
            prior_here = replies_already_drafted_for(post_id)
        except Exception:                                   # noqa: BLE001
            prior_here = []
    if prior_here:
        instruction += (" You have already drafted a reply to this exact post. "
                        "Take a genuinely different angle this time — a "
                        "different reason, a different consequence, or a "
                        "different part of what they said.")
    if llm is not None:
        # A BUDGET, because this is one extra call in front of the draft the
        # user is waiting for. Measured at 127 seconds while another process
        # had the local model busy — the provider's own request timeout is 120s
        # and nothing above it was watching the clock. When the machine is that
        # loaded the right answer is to write the reply without a planned
        # point, not to make him wait twice.
        started = time.monotonic()
        try:
            point = decide_reply_point(llm, context, room,
                                       prior_for_this_post=prior_here)
        except Exception:                                   # noqa: BLE001
            point = None
        if point and time.monotonic() - started > _POINT_BUDGET_S:
            point = None
        if point:
            # BOTH PAYLOADS. The string append is unchanged so every current
            # consumer behaves identically; the value is what P3 will read.
            # Removing the append is P5's job, after the readers have moved.
            room.decided_point = point
            instruction += (
                f'{_POINT_LEAD}"{point}". Say exactly that, in the writer\'s '
                "voice. Do not add a second idea.")
    return instruction, room


# ------------------------------------------------------------------- the gates
def intruders(draft: str, room: Room) -> list[str]:
    """Named things in the reply that this conversation never mentioned.

    The archive-blind twin of `intelligence._injected_entities`. That one takes
    a corpus which includes the voice examples, so his own past posts license
    every name in them; on a reply that is exactly backwards. Here the corpus
    is the conversation plus verified evidence, and nothing else.
    """
    allowed = named_things(room.allowed)
    return sorted({name for name in named_things(draft) if name not in allowed})


_SHAPE_STOP = frozenset("""a an and are as at be been but by can do does for from had has
have he her his how i if in is it its me my no not of on or our so than that the their them
then there these they this to too was we were what when which who will with would you your
it's i'm don't doesn't isn't that's you're we're just really very still even only also
about actually now""".split())


def _shape_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(w) > 2 and w not in _SHAPE_STOP}


def repeats_a_reply(draft: str, prior: list[str], *, source: str = "") -> str | None:
    """Has this reply already been written for this account?

    `_echoes_own_writing` exists and does not fire here. It wants two shared
    six-word runs or 45% Jaccard, and a twelve-word reply offers seven runs
    total — so these two, which are plainly the same reply, both passed it:

        "So Opus 5's half price? What's the catch?"
        "So Opus 5's half-price? That's a big deal. But how's it actually
         cheaper than 4.5?"

    Short text needs a lower bar and a shorter run, and the run has to be a
    CONTENT run: "what's the catch" is four words, three of them stopwords, and
    it appeared in four separate drafts. So: content-word overlap at 0.5, or a
    shared run of three content words.

    PROVENANCE, checked 2026-08-09: those four drafts carry FIXTURE ids — the
    calibration is sound and the motivating example is arena output. See
    `storage.previously_drafted_replies`. Separately measured: his one real
    restatement pair passes BOTH arms (overlap 0.300, no 3-content-word run),
    because his duplication is semantic and both arms are lexical. Do not
    retune the ratio to catch it — 0.300 also catches genuinely-different pairs
    at 0.154 and 0.143, on n=5.

    `source` is the post being answered, and a run drawn ENTIRELY from it is not
    evidence of anything. Two replies to the Opus 5 announcement — "It might be
    a scaled-down version of Opus 4.5" and "Half the price of Opus 4.5 means
    Opus 5 costs less than the previous tier" — make different points and were
    called repeats of each other because they both said "Opus 4.5", which is
    the one thing every honest reply to that post has to say. Naming the
    subject is not repeating yourself. Overlap is still judged on the whole
    text: two replies that really are the same are the same however much of the
    post they quote.
    """
    mine = _shape_words(draft)
    if len(mine) < 3:
        return None
    # Tokenised exactly like the runs below — `_shape_words` drops anything
    # under three characters, which would leave the "4" and the "5" of "opus
    # 4 5" looking like the reply's own invention.
    from_source = {w for w in re.findall(r"[a-z0-9']+", (source or "").lower())
                   if w not in _SHAPE_STOP}
    mine_seq = [w for w in re.findall(r"[a-z0-9']+", draft.lower()) if w not in _SHAPE_STOP]
    mine_runs = {" ".join(mine_seq[i:i + 3]) for i in range(max(0, len(mine_seq) - 2))}
    if from_source:
        mine_runs = {run for run in mine_runs
                     if not all(word in from_source for word in run.split())}
    for other in prior:
        theirs = _shape_words(other)
        if not theirs:
            continue
        union = mine | theirs
        if union and len(mine & theirs) / len(union) >= 0.5:
            return other
        other_seq = [w for w in re.findall(r"[a-z0-9']+", other.lower())
                     if w not in _SHAPE_STOP]
        other_runs = {" ".join(other_seq[i:i + 3]) for i in range(max(0, len(other_seq) - 2))}
        if mine_runs & other_runs:
            return other
    return None


_ASKS_BACK = re.compile(r"^[^.!?]*\?")


def strip_repeated_question(draft: str, room: Room) -> str:
    """Drop a leading verbatim re-statement of the question they asked.

    Live: "What rig are you running it on? The model's output is what decides
    it." The answer is right there in the second sentence — the reply just
    reads their own question back at them first, which is the single most
    inattentive thing a reply can open with and the reason
    `bounces_the_question` fired on an otherwise fine draft.

    Only when the opening really IS their question: two thirds of its content
    words, and the same shape. A reply that happens to start with a different
    question of its own is left alone.
    """
    body = (draft or "").strip()
    if not room.asked or not body:
        return draft
    first, sep, rest = body.partition("?")
    if not sep or not rest.strip():
        return draft
    theirs = _shape_words(room.asked)
    mine = _shape_words(first)
    if not theirs or not mine:
        return draft
    if len(mine & theirs) / len(mine) < 0.66:
        return draft
    rest = rest.lstrip()
    return (rest[:1].upper() + rest[1:]).strip()


def bounces_the_question(draft: str, room: Room) -> bool:
    """He was asked something and the reply asks something instead.

    Live: "What rig are you running it on?" -> "You're running it on a GPU?"
    and "the bottleneck was memory bandwidth" -> "What's the real issue then?".
    A reply whose FIRST sentence is a question, to a post that asked HIM a
    question, has not answered anybody.
    """
    # `host` counts too: being the host of the thread does not excuse leaving
    # the question they came to ask unanswered.
    if room.move not in ("answer", "host") or not room.asked:
        return False
    first = re.split(r"(?<=[.!?])\s+", draft.strip())[0] if draft.strip() else ""
    return bool(_ASKS_BACK.match(first.strip()))


# A reply that lists possibilities and picks none. Live, under a post about
# inference costs:
#
#   "So you're saying 30B is now cheaper than its own API? That's confusing.
#    Maybe the cost is lower when you use it directly instead of through the
#    API. Or maybe the API has extra fees that make it more expensive. Either
#    way, it's not clear how the pricing works."
#
# Forty-nine words announcing that the writer does not understand the post. The
# drafting rules already say "do not argue both sides" and "never end on a
# hedge", and nothing enforced either, so it shipped.
#
# Bare "maybe" is NOT the tell and must not be banned — "Maybe they're right.
# But where is the public proof?" is his own writing and takes a clear line.
# The tell is maybe-then-maybe, or an explicit surrender. Checked against all
# 36 posts in his archive: zero matches.
_NO_POSITION = re.compile(
    r"\bmaybe\b[^.!?\n]{0,60}[.!?]\s*(?:or\s+)?maybe\b"
    r"|\beither way\b|\bit'?s not clear\b|\bthat'?s confusing\b"
    r"|\bi'?m not sure (?:what|how|why|if)\b|\bhard to say\b", re.I)


def has_no_position(draft: str) -> bool:
    """The reply weighs two possibilities and commits to neither."""
    return bool(_NO_POSITION.search(draft or ""))


# An analyst's note instead of a person talking. Live, twice in one run:
#
#   "The cost reduction suggests they're optimizing for lower-end hardware
#    rather than top-tier performance."
#   "The half-price claim suggests Opus 5's cost efficiency is a key selling
#    point."
#
# Nobody replies like that, and nobody likes a reply that does.
#
# SCOPED TO REPLIES on purpose. He does use "suggests" — once, in a long post,
# under a bullet, with the evidence stacked above it, and that is a legitimate
# register for a post. Across his 24 texts of 36 words or fewer, which is what
# a reply actually is, this pattern hits ZERO. That asymmetry is the whole
# justification: it is not a bad word, it is the wrong form.
_ANALYST_VOICE = re.compile(
    r"\b(?:suggests?|indicates?|underscores?|demonstrates?|signals? that|"
    r"reflects? (?:a|the)|points? to a|appears? to be|is likely (?:to|a)|"
    r"could imply|may indicate|"
    # THE HEDGE THAT DECLINES TO FINISH. Seen live and clean through all
    # twelve checks: "Their claim about half the price of Opus 4.5 raises
    # questions about the actual cost savings." No hedging verb, so the
    # patterns above miss it; it names a specific, so the payoff floor passes
    # it at 0.44. It is still commentary about the post rather than a reply to
    # the person, and it lands nowhere on purpose. Measured over his 35
    # archive texts: zero hits.
    r"raises? (?:questions?|concerns?|doubts?)|"
    r"remains? to be seen|time will tell|has yet to be seen|"
    r"(?:will be |be )?interesting to see|"
    r"worth (?:watching|noting)|"
    # THE INFERENCE IT REFUSES TO MAKE. "Claude Opus 5's half-price claim could
    # mean it's cheaper than Opus 4.5's full cost" — half price DOES mean
    # cheaper; the sentence is a tautology wearing a hedge, and it passed every
    # other check. Note the shape being caught is "X could MEAN Y", not bare
    # "might": he writes "at this rate Grok 4.5 might launch before Opus 5" and
    # "Kimi K3 might be the reason Opus 5 gets delayed" — that is a prediction
    # with his name on it and it stays. Measured: 0 of his 35 archive texts,
    # 4 of 425 generated drafts, and three of those four are tautologies.
    # THE FAMILY, NOT THE PHRASES. Listing "could mean", then "could make", was
# losing a race: the next draft said "might BRING more users" and the gate
# missed it, so no retry fired and the hedge shipped. What every one of
# them shares is a modal in front of a CAUSATION verb — the writer
# gesturing at a consequence instead of claiming one.
    #
    # The verb class is what keeps his own voice out of it. "Kimi K3 might BE
    # the reason Opus 5 gets delayed" and "Grok 4.5 might LAUNCH before Opus
    # 5" are calls with his name on them, and they survive because `be` and
    # plain event verbs are not here. Measured: 0 of his 35 archive texts and
    # 0 of his 24 short ones, against 17 of 419 generated drafts.
    r"(?:could|might|may)\s+(?:make|bring|mean|cause|lead|drive|push|"
    r"allow|let|enable|affect|signal|indicate|imply|result|shift|turn|force|"
    r"help|create|spark|trigger|point)|"
    # `make` joined the list after "Opus 5's half-price offer might make it
    # more accessible" came back live. Note what is NOT here: a bare modal.
    # "could|might|may" followed by ANY verb fires on six of his own thirty-five
    # texts — "Kimi K3 might BE the reason Opus 5 gets delayed" is a call with
    # his name on it. Only the verbs that turn a claim into a deduction belong.
    # THE POST, THEN A CONCLUSION NOBODY ASKED FOR. `demonstrates` was already
    # here; these are the same sentence with a shorter verb, and every instance
    # measured is a restatement wearing a verdict:
    #
    #   "Claude Opus 5 costs half what Opus 4.5 did, proving its value for money."
    #   "The memory bandwidth bottleneck shows the real limit of their setup."
    #   "Three users in the first hour shows how quickly your feature resonates."
    #
    # All three scored CLEAN through every other check. Measured across his 35
    # archive texts: zero. Across 357 generated drafts: nine.
    r"prov(?:es|ing|en)\b|"
    r"shows? (?:that|the|how)\b|"
    r"value for money)\b", re.I)

# The same failure wearing nouns instead of verbs. Under a post from somebody
# celebrating their first three paying customers, the reply came back:
#
#   "Three users in the first hour means the product is hitting the right
#    notes with its value proposition."
#
# The hedge detector cannot see that — there is no hedging verb in it — and the
# general `abstract_jargon` check needs two hits from a different word list.
# Zero of his 35 archive texts contain any of these.
_CORPORATE_REGISTER = re.compile(
    r"\b(?:value proposition|key selling point|selling point|market "
    r"positioning|stakeholders?|leverage|ecosystems?|synergy|scalability|"
    r"utili[sz]e|paradigm|holistic|streamlin\w+|hitting the right notes|"
    r"best[- ]in[- ]class|end[- ]to[- ]end solution|"
    # THE STOCK PHRASE THAT SOUNDS LIKE A CONCLUSION. "529s in the morning, no
    # support? It's a wake-up call for the whole team" reads as a verdict and
    # is a stock one — nobody learns anything and it is not his register.
    # Deliberately excludes "the real win" and "at scale": both appear inside
    # replies that are otherwise good ("Reranking's the real win, but it's
    # still a hack over a fix"), and a gate that kills those costs more than
    # it saves. Measured across his 35 archive texts: zero hits.
    r"wake[- ]up call|break(?:ing|s)? the bank|in the dark|"
    r"tipping point|no[- ]brainer|under the hood|mov(?:ing|es|ed) the needle|"
    r"low[- ]hanging fruit|elephant in the room|sea change|step change|"
    r"rais(?:e|es|ed|ing) the bar|the bottom line|the real question)\b", re.I)


# HYPE THAT NEEDS A SUBJECT — split out of `_CORPORATE_REGISTER` 2026-08-07.
#
# `game[- ]chang\w+` sat in the list above and fired on his own published post:
# "Claude Code letting sessions talk to each other is an absolute game changer."
# The SAME phrase was independently listed in `intelligence._FILLER_RE`, where
# it deleted that first line outright — two gates, two files, two code paths,
# one sentence of his, and neither list knew about the other.
#
# Not a phrase removal. The eleven other entries here are empty in every
# context and measure zero across his archive; this one is empty only when it
# evaluates NOTHING. "It's a game changer for the whole space" names nothing;
# his sentence names Claude Code and a specific capability, and the evaluation
# IS the post. Same precedent as "fatal flaw" and "let it rip": split the gate,
# never trim the list, or the next hype noun is found by him on a live post.
_HYPE_NEEDS_SUBJECT = re.compile(r"\bgame[- ]chang\w+\b", re.I)


def sounds_like_an_analyst(draft: str) -> bool:
    """Hedged third-person commentary where a reply should be a person talking."""
    body = draft or ""
    if _ANALYST_VOICE.search(body) or _CORPORATE_REGISTER.search(body):
        return True
    # Corroborated by `named_things` — this module's own reader — rather than a
    # second word list, so a new hype noun inherits the test and a new product
    # name inherits the exemption without either list being maintained.
    return bool(_HYPE_NEEDS_SUBJECT.search(body) and not named_things(body))


# "<something> suggests (that) <claim>" -> "<claim>". The hedge is scaffolding;
# the claim underneath it is the reply.
#
#   "The cost reduction suggests they're optimizing for smaller-scale
#    deployments rather than large-scale infrastructure."
#        -> "They're optimizing for smaller-scale deployments rather than
#            large-scale infrastructure."
#
# Mechanical for the reason everything else here is mechanical: this survived
# a positive register instruction AND a gate, because when every candidate
# hedges the near-miss path has to ship one of them. Removing the hedge leaves
# a flat claim in his register, which is what was wanted all along.
_ANALYST_HEDGE = re.compile(
    r"^\s*[\"“']?\s*(?:the|this|that|a|an|these|those)?\s*[^.!?]{0,44}?"
    r"\b(?P<verb>suggests?|indicates?|signals?|implies|imply)\s+(?:that\s+)?",
    re.IGNORECASE)


def strip_analyst_hedge(draft: str) -> str:
    """Promote the claim out from under its hedge, or return the draft as-is."""
    text = (draft or "").strip()
    match = _ANALYST_HEDGE.match(text)
    if not match:
        return text
    body = text[match.end():].lstrip()
    # Only when a real clause survives. "Everything suggests so." leaves
    # nothing worth promoting, and half a sentence is worse than a hedge.
    if len(body.split()) < 4:
        return text
    # A NOUN PHRASE IS NOT A CLAUSE. "The cost drop suggests a major shift in
    # how models are trained and deployed" promotes to "A major shift in how
    # models are trained and deployed", which shipped and is not a sentence.
    #
    # A bare "does it start with a determiner" test was the first attempt and
    # it was too blunt: "Polymarket's update suggests the event's timing is now
    # uncertain" also starts with "the" and IS a clause, so the hedge stayed in
    # and shipped. And a bare "does it contain a verb" test fails the other
    # way — the fragment above contains "are".
    #
    # What separates them is WHERE the verb is. Cut at the first subordinator;
    # a clause has its verb before one ("the event's timing IS now uncertain"),
    # a noun phrase only has one after ("a major shift in HOW models are
    # trained"). When neither is found the hedge is left alone and the gate
    # handles it, which is the safe direction.
    if not _has_top_level_verb(body):
        # A NOUN PHRASE CAN STILL BE UNHEDGED — just not by promotion. Swap
        # the hedging verb for the copula and keep the subject, which is
        # grammatical and says the thing outright:
        #   "The 529 error suggests a network issue, not an API malfunction."
        #     -> "The 529 error is a network issue, not an API malfunction."
        # Before this, these were the whole of the remaining analyst register:
        # every one had a noun-phrase complement, so promotion was refused and
        # the hedge shipped.
        verb = match.group("verb")
        copula = "is" if verb.lower().endswith("s") else "are"
        head = text[:match.start("verb")].rstrip()
        return f"{head} {copula} {body}".strip()
    return (body[:1].upper() + body[1:]).strip()


_SUBORDINATOR = re.compile(
    r"\b(?:how|what|whatever|whichever|that|which|who|whom|whose|where|when|"
    r"why|because|while|whether|if|since|although|though)\b", re.I)
_FINITE_VERB = re.compile(
    r"\b(?:is|are|was|were|be|been|has|have|had|does|do|did|will|would|can|"
    r"could|should|may|might|must|'s|'re|'ve|'ll|'d)\b"
    r"|\b[a-z]+(?:s|ed)\b", re.I)


def _has_top_level_verb(body: str) -> bool:
    """Is there a finite verb before the first subordinating conjunction?"""
    cut = _SUBORDINATOR.search(body)
    head = body[:cut.start()] if cut else body
    return bool(_FINITE_VERB.search(head))


# A reply that DESCRIBES the post instead of answering it. Live, under "This is
# the part everyone is missing":
#
#   "The post is gesturing at the overlooked efficiency of sparse activation
#    in large models."
#
# Nobody replies to a person by narrating what they wrote. Note the exclusion:
# "Your take on local inference is the clearest I've read" addresses the PERSON
# and is fine — it is the third-person artefact ("the post", "the author",
# "OP") that never appears in his 35 archive texts and never should.
_TALKS_ABOUT_THE_POST = re.compile(
    r"\b(?:the|this|that|their)\s+(?:post|tweet|thread|reply|comment)\b"
    r"|\bthe author\b|\bthe poster\b|\bOP\b", re.I)


def talks_about_the_post(draft: str) -> bool:
    """Narrates the post in the third person instead of replying to a person."""
    return bool(_TALKS_ABOUT_THE_POST.search(draft or ""))


# THE EIGHT-WORD RULE, WHICH NOTHING ENFORCED AGAINST THE PARENT.
#
# `BASE_DRAFT_RULES` has said "Never copy more than eight words in a row from
# any reference" since it was written. Nothing checked it against the post
# being replied to, and `restates_the_source` is a similarity average, which
# cannot tell these two apart — both score 0.60:
#
#   parent: "Honestly the pricing page is the only benchmark that has ever
#            mattered to me."
#   copy  : "The pricing page is the only benchmark that has ever mattered to
#            me because it reflects real-world value."     <- 10-word run
#   good  : "Pricing is the only benchmark with a receipt attached."
#                                                          <- 3-word run
#
# The RUN separates them cleanly. Measured across the live drafts: copies score
# 10-11, every genuinely different reply scores 1-3. Eight is the documented
# rule and has enormous margin either side.
_COPY_RUN = 8


# WHAT THE POST ACTUALLY SAID, as opposed to what it was vaguely about.
#
# The Tibo case is the whole argument for this: "Unbelievable excited for what's
# coming together. Tomorrow is feeling codexy" got back a reply about a 2019
# Netflix miniseries. It was topically adjacent to a word in the post and
# engaged nothing that was in it. His verdict — "no attention to detail like
# when tibo said codexy" — is the requirement.
#
# A post's SPECIFICS are the things a reader would point at: names, figures,
# and the unusual words. Common vocabulary is not a specific — "model",
# "price", "the API" are shared by every post on this beat, so matching them
# proves nothing.
_COMMON_ON_THIS_BEAT = frozenset("""
model models price prices pricing cost costs api apis token tokens context
window release released launch launched new better faster cheaper thing things
people time times work working good great real actually really still even just
more most much many company companies team teams user users product
""".split())


_NUMBER_WORDS = frozenset("""
one two three four five six seven eight nine ten dozen half twice double
first second third
""".split())

# FOUR LETTERS, with a stoplist of its own. At five, the key phrase of "Claude
# Opus 5 is here, at half the price of Opus 4.5" was invisible — "half" is four
# characters and "price" is on the beat-common list — so a reply reading "half
# the price means more people can try it before they commit", which is engaging
# the post about as directly as possible, scored as ignoring it.
#
# `_NOT_A_NAME_IN_PROSE` is the wrong list to reuse here: it answers "is this a
# company", and "half" correctly is not. This answers "is this a detail of the
# post", and "half" plainly is. Function words only.
_FUNCTION_WORDS = frozenset("""
this that these those here there what when where which who whom whose
with from into onto upon over under about above below after before since until
while your their ours mine they them then than also just only even much many
more most less been being have has had was were will would could should shall
must does did done some such very well back down out off own same each both
all any one two how why yet its it's is are am be do go get got now new old
like need want know say said see look
""".split())


def post_specifics(room: Room) -> set[str]:
    """The concrete things this conversation put on the table.

    Five characters, not six. At six, "Three users bought it in the first hour"
    offered only `bought/feature/shipped/shaking/stopped` as specifics, so a
    reply opening "Three users in the first hour..." — which is engaging the
    post about as directly as a reply can — was scored as ignoring it. The
    words that carry a post are often short.

    Spelled-out numbers count as figures, for the same reason: "three users"
    and "3 users" are the same detail.
    """
    text = room.allowed
    found = set(named_things(text))
    found.update(re.findall(r"\b\d+(?:[.,]\d+)?\b", text))
    for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text):
        stem = word.lower()
        if stem in _NUMBER_WORDS:
            found.add(stem)
        elif (stem not in _COMMON_ON_THIS_BEAT and stem not in _SHAPE_STOP
                and stem not in _FUNCTION_WORDS):
            found.add(stem)
    # Known domain terms count whatever their length. "rig" is three characters
    # and was invisible, so the rig/hardware synonym below could never fire on
    # the one post it was written for.
    for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text):
        stem = word.lower()
        if stem in _SAME_THING:
            found.add(stem)
    return found


# SAME SUBSTANCE, NO SHARED LETTERS. Asked "what rig are you running it on?"
# the reply answered "The model decides, not the hardware" — engaging the
# question exactly, and scoring as off-post because a prefix match cannot see
# that a rig IS hardware. Deliberately small and one-directional: these are
# pairs from this account's own beat where the two words denote the same thing,
# not a general thesaurus. A loose synonym list would let anything match.
_SAME_THING: dict[str, tuple[str, ...]] = {
    "rig": ("hardware", "gpu", "card", "machine", "setup", "box", "build"),
    "hardware": ("rig", "gpu", "card", "machine"),
    "bandwidth": ("throughput",),
    "latency": ("speed", "lag"),
    "inference": ("serving", "runtime"),
    "locally": ("local", "on-device", "offline"),
    "local": ("locally", "on-device", "offline"),
    "cheaper": ("cheap", "price", "cost"),
    "quarter": ("q1", "q2", "q3", "q4"),
}


def engages_a_specific(draft: str, room: Room) -> bool:
    """Does the reply pick up something the post actually said?

    Deliberately not the same question as `_shows_context_connection`, which is
    a floor satisfied by any shared stem OR any digit anywhere — a reply full
    of its own unrelated numbers clears it. This asks the narrower thing his
    complaint was about: is there a specific FROM THE POST in here.

    Cheap and forgiving on purpose: one specific is enough, matched on a
    five-character stem so inflections and possessives count. Its job is to
    catch the reply that could have been written without reading the post, not
    to score how deeply it engaged.
    """
    specifics = post_specifics(room)
    # A CONTENTLESS POST OFFERS CONTENTLESS SPECIFICS. "This is the part
    # everyone is missing" yields {part, missing, everyone} — and demanding the
    # reply contain one of those is demanding an echo, not attention to detail.
    # There is only something to engage when the post named a thing, gave a
    # figure, or used a word distinctive enough to be worth picking up.
    if not _has_anything_worth_engaging(room, specifics):
        return True                     # nothing to engage; not the reply's fault
    # FOUR-CHARACTER STEM, and matched both ways. At five, "dropped" became
    # "dropp" and a reply reading "the cost drop makes large models viable"
    # scored as ignoring a post about costs dropping — the inflection was the
    # only difference. Prefix matching in both directions covers drop/dropped,
    # scale/scaling, price/pricing without needing a stemmer.
    lowered = (draft or "").lower()
    # Currency and percent signs are stripped from BOTH sides before matching.
    # The post's figures are stored bare ("0.90") while a reply writes "$0.90",
    # so a draft using their own number — the most on-point thing it can do —
    # failed the prefix test on the dollar sign alone.
    words = {_bare(word) for word in re.findall(r"[a-z0-9$€£.,%]+", lowered)}
    words.discard("")
    for spec in specifics:
        bare = _bare(spec)
        if len(bare) < 3:
            continue
        for candidate in (bare, *_SAME_THING.get(bare, ())):
            stem = candidate[:4]
            if any(word.startswith(stem) or candidate.startswith(word[:4])
                   for word in words if len(word) >= 3):
                return True
    return False


def _bare(token: str) -> str:
    return token.strip("$€£%,.").lower()


# Words a post can be "about" without being about anything. Matching one of
# these proves the reply reused a word, not that it read the post.
_CONTENTLESS = frozenset("""
part parts missing everyone everybody nobody somebody thing things point points
piece pieces stuff idea ideas take takes reason reasons problem problems
question questions answer answers story stories moment side sides
""".split())


def _has_anything_worth_engaging(room: Room, specifics: set[str]) -> bool:
    if room.figures or room.subject:
        return True
    return any(spec not in _CONTENTLESS and not spec.isdigit()
               and spec not in _NUMBER_WORDS
               and spec.lower() != (room.author or "").lower()
               and len(spec) >= 6
               for spec in specifics)


def copies_the_post(draft: str, room: Room) -> bool:
    """A verbatim stretch lifted straight out of the post being replied to.

    Handed back under the post it came from, this is the most embarrassing
    thing the account can send — which is why it refuses rather than shipping
    with a note. Everything softer stays a `restates_the_source` clarity issue.
    """
    from .intelligence import _longest_run, _words_only
    source = _words_only(room.allowed)
    # A SHORT POST USED TO BE UNCOPYABLE BY DEFINITION. The rule was "give up
    # if the post is shorter than the run we look for", which is backwards:
    # copying a seven-word post in full is more obviously a copy than lifting
    # eight words out of forty. Live 2026-07-27, under "This is the part
    # everyone is missing." (seven words), the reply came back "This is the
    # part everyone is missing: the unspoken assumption that context length is
    # free" — the whole post, verbatim, as an opener — and scored CLEAN.
    #
    # So when the post is short the bar becomes the post itself: only a run
    # covering ALL of it counts, which leaves a reply free to pick up their
    # phrase ("the part everyone is missing is bandwidth") while catching the
    # draft that swallows the post whole. Under five words there is nothing
    # here worth calling a copy.
    # The author's name and handle are appended to `allowed`, and counting
    # them made a five-content-word post look like a seven-word one — enough
    # to keep the bar above the whole post and let the copy through again.
    core = _words_only(room.visible) or source
    needed = _COPY_RUN if len(core) >= _COPY_RUN else len(core)
    if needed < 5:
        return False
    words = _words_only(draft)
    # THE POST, NOT THE PAGES IT LINKS. `allowed` also carries the evidence (the
    # linked page, pool stories), and measuring against it refused a good reply
    # under "This study of 400 SaaS pricing pages is worth your time." for
    # quoting one finding of that study: "...said what happens when a customer
    # outgrows their tier" is eight words of the PAGE, cited, with the reply's
    # own point after it (golden run, 2026-09-22). Citing a source is the job;
    # handing the post back is the fault. A long lift from a page is still a
    # copy, at twice the bar: that is pasting the article, not quoting it.
    if _longest_run(words, core) >= needed:
        return True
    return _longest_run(words, source) >= 2 * _COPY_RUN


def says_it_back(draft: str, room: Room) -> bool:
    """Their post rearranged — the echo `_source_mirroring` cannot see.

    `growth_scorer._source_mirroring` scores each draft SENTENCE against its
    closest single SOURCE SENTENCE and averages. Two structural blind spots
    follow from that, and the live reply to @thsottiaux walked through both:

        post   "...There will be signs. Reliability increasing despite load
                going up and up. Sudden efficiency gains. Things getting
                faster. Resets..."
        draft  "Reliability up under load means resets are actually signs of
                progress not failure. Just sudden gains."

        _source_mirroring -> 0.222        (the gate needs 0.5, the penalty 0.3)

      * FUSION IS INVISIBLE. That first draft sentence is four of the post's
        sentences folded into one, so it is measured against whichever single
        one it matches best — and matching four sentences a little scores
        LOWER than matching one of them a lot. Swallowing more of the post
        makes the number go DOWN.
      * A SHORT SENTENCE IS DROPPED ENTIRELY. "Just sudden gains." is their
        "Sudden efficiency gains." with a word taken out, and it carries two
        content tokens against a filter that wants four, so it never reached
        the average at all. Same bug `copies_the_post` already had to fix from
        the other side, where a short POST was uncopyable by definition.

    So this pools the source instead of picking one sentence, and weights each
    draft sentence by its own length instead of dropping the short ones. It is
    an ADDITION to the existing check, never a replacement: the sentence-level
    measure is what protects a reply that legitimately has to say their subject
    ("A single 4090 handles it because MoE only wakes two experts" scores 0.22
    on both and passes), and this catches the shape that goes around it.

    CALIBRATED 2026-07-31, and the threshold is where it is because of what it
    must NOT flag. Against his own three archive replies that carry a parent:

        0.375  "Grok 4.5 on Copilot is faster, but it doesn't solve the cost
                of running frontier models."                          (passes)
        0.000  "Honestly, that gap is getting scary fast."             (passes)
        0.600  the @thsottiaux draft above                            (caught)

    and against the shape he actually asked for, which stays well clear
    because it borrows ONE of their words rather than all of them:

        0.143  'The "resets" one is the tell. Nobody reboots a fleet that is
                coping.'

    On the 166 reply candidates in the local database it fires on 9 that the
    current gate misses and none that it should not — every one hand-read, and
    every one their post handed back: "Even with 16GB being mediocre, local
    models still need catching up as everyone says.", "Mark Zuckerberg's
    question shows whether superintelligence empowers everyone or concentrates
    power", "So Opus 5 is half the price of 4.5? What's the catch?". Two of the
    nine are the fault he complained about — their sentence returned with the
    terms swapped, which makes it WRONG as well as empty: "Every feature I
    trust takes longer to build than the eval it measures" under a post reading
    "Every eval I trust took longer to build than the feature it measures".
    """
    source = _shape_words(room.visible or room.allowed)
    if not source:
        return False
    sentences = [words for words in
                 (_shape_words(s) for s in re.split(r"(?<=[.!?])\s+|\n+", draft or ""))
                 if words]
    if not sentences:
        return False
    borrowed = sum(len(words & source) for words in sentences)
    total = sum(len(words) for words in sentences)
    if not total:
        return False
    # 0.55, not 0.5: "Qwen's not the problem, but the memory bandwidth is."
    # sits at 0.500 and the sentence-level gate already refuses it, so the
    # lower bar would only buy a second flag on a draft that is already dead.
    return borrowed / total >= 0.55


def opens_with_a_tic(draft: str) -> str | None:
    """The strawman openers, which are a house style nobody chose."""
    head = draft.strip().lstrip('"“').strip()
    for opener in _BANNED_OPENERS:
        if head.lower().startswith(opener.lower()):
            return opener
    return None


_STRAWMAN_PREFIX = re.compile(
    r"^[\"“']?\s*(?:so|and)?\s*(?:you'?re|you are|are you)\s+saying\s+(?:that\s+)?",
    re.IGNORECASE)
_STANDALONE_TIC = re.compile(
    r"^[\"“']?\s*(?:what'?s the catch|what is the catch|so let me get this "
    r"straight)\s*[?.!]\s*", re.IGNORECASE)


_DEDUCTION_TAIL = re.compile(
    r",\s*(?:prov|show|demonstrat|indicat|signal|suggest|highlight|"
    r"underscor|reflect|making|leaving|creating|turning|causing|forcing)\w*\s+(?:that|how|why|the|a|an|its?|early|real)"
    r"\b[^.!?]*", re.I)


def strip_deduction_tail(draft: str, source: str = "") -> str:
    """Cut the ", proving that ..." clause and keep the claim in front of it.

    The writer's most persistent hedge is a concrete claim with a deduction
    bolted on: "Chunking strategy's impact outshone the 1.9s latency drop,
    proving that focused optimization beats brute force." The head is a good
    reply — specific, carries a figure — and the tail is the analyst voice the
    gate rejects. Retrying regenerates the whole line and the writer bolts the
    tail on again 2-3 attempts running, so the repair has to be mechanical,
    like the tics and the strawman openers.

    Measured before adding: 0 of his 35 archive texts carry the shape, 6 of
    446 generated drafts do, and in every one of the six the head stands alone.
    The cut only happens when what remains is a sentence with at least four
    words — half a sentence is worse than a hedge, and anything shorter is
    left for the gate.
    """
    text = (draft or "").strip()
    match = _DEDUCTION_TAIL.search(text)
    if not match:
        return text
    head = text[:match.start()].strip()
    tail_after = text[match.end():].strip()          # usually just the period
    if len(head.split()) < 4 or len(_shape_words(head)) < 2:
        return text
    # A HEAD THAT MIRRORS THE POST IS NOT A RESCUE. Three runs measured the
    # conversion: analyst went to zero while echo rose, because cutting
    # ", proving …" off a restatement leaves the restatement. Shipping that
    # trades one fault for another; leaving the draft flagged keeps it in the
    # retry ladder, which can actually replace it.
    if source:
        # Content-word share, not `_source_mirroring` — that needs four-plus
        # tokens per sentence and returns 0.0 for a line this short (same
        # reason the point gate uses shape words).
        head_words = _shape_words(head)
        if head_words:
            share = len(head_words & _shape_words(source)) / len(head_words)
            if share >= 0.7:
                return text
    closing = tail_after if tail_after in (".", "!", "?") else "."
    remainder = tail_after.lstrip(".!? ").strip()
    # A second sentence after the deduction survives on its own.
    rebuilt = head + closing + ((" " + remainder) if remainder else "")
    return rebuilt.strip()


def strip_copied_opener(draft: str, room: Room) -> str:
    """Drop a first sentence that is the post handed back, keep the reply.

    THE COPY IS OFTEN ONLY THE OPENER. Two of the most stubborn failures in the
    whole run are this shape, and in both the second sentence is a perfectly
    good reply wearing the post as a hat:

        post  : "Claude Opus 5 is here. Our most capable model yet, at half
                 the price of Opus 4.5."
        draft : "Claude Opus 5 is here. Half the price, but still way expensive."
                                        ^ this half is the reply

        post  : "This is the part everyone is missing."
        draft : "This is the part everyone is missing: the unspoken assumption
                 that context length is free."

    `copies_the_post` refuses both, correctly — handing someone their own
    sentence back is the worst thing this account can send — and the writer is
    then out of attempts and the user gets nothing. Cutting the hat off is
    strictly better than refusing, and it is the same mechanical repair the
    tics and the hedges already get.

    Deliberately narrow. The opener goes only when EVERY content word in it
    came from the conversation (so it introduces nothing of its own), and only
    when what follows still stands up as a sentence. Anything less certain is
    left alone for the gate to judge.
    """
    from .intelligence import _words_only
    text = (draft or "").strip()
    match = re.match(r"^([^.!?:]{4,}[.!?:])\s+(.{10,})$", text, re.S)
    if not match:
        return text
    head, rest = match.group(1), match.group(2).strip()
    source = set(_words_only(room.visible or room.allowed))
    head_words = set(_words_only(head))
    if not source or len(head_words) < 3:
        return text
    if head_words - source:                 # the opener says something of its own
        return text
    if len(_shape_words(rest)) < 3:         # nothing worth keeping underneath
        return text
    return rest[0].upper() + rest[1:]


def strip_strawman_opener(draft: str) -> str:
    """Remove the tic and keep the sentence, rather than argue with the model.

    TELLING IT NOT TO MADE IT WORSE. Before the reply plan existed, "So you're
    saying" opened 6 of 26 drafts. After the plan added an explicit "Never open
    with: 'So you're saying'", it opened 3 of 4. Naming a phrase to a small
    model puts that phrase in front of it; the instruction primed the exact
    thing it forbade. That is why this is mechanical and why the prompt no
    longer mentions the phrase at all.

    "So you're saying the model's not the issue? Then why's the memory
    bandwidth the real pain?" becomes "The model's not the issue. Then why's
    the memory bandwidth the real pain?" — the strawman framing goes, the point
    the draft was making stays. Meaning is preserved because the tic carries
    none: it is throat-clearing that attributes a position to the author
    instead of stating one.
    """
    text = (draft or "").strip()
    if not text:
        return text
    stripped = _STANDALONE_TIC.sub("", text, count=1)
    if stripped != text:
        return stripped.strip()
    match = _STRAWMAN_PREFIX.match(text)
    if not match:
        return text
    body = text[match.end():].lstrip()
    if not body:
        return text
    # The question mark belonged to the strawman, not to what follows. Only the
    # FIRST sentence is converted — a genuine question later in the reply is the
    # writer's own and stays.
    head, sep, tail = body.partition("?")
    if sep and head and len(head.split()) >= 3:
        body = f"{head.rstrip()}.{(' ' + tail.lstrip()) if tail.strip() else ''}"
    return (body[:1].upper() + body[1:]).strip()
