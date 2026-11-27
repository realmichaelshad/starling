"""What this account writes about, read instantly and deterministically from its own texts.

API-NATIVE PASS (2026-09-22), the owner's rule: "the first press after someone adds writing
must already know their beat." The model-written voice card (`voice_card.py`) is the rich,
later read; it needs a background model call. This is the instant one: keyphrases from the
account's own posts and replies, its own-material voice items, the bio, the onboarding
pillars and the background notes, weighted and ranked with no model and no network, computed
and cached when material is added (`refresh`) and whenever the corpus changes (`current`).

It is used from the first press for:
  * topic research and the Create post / chat idea candidates (`topic_research.py`): which
    beats to look up, and how well a story fits;
  * relevance on the ✦ press: how close the post is to the writer's beat, given to the model
    as a prior (`main._decision_brief`), never as a rule.

PRIVACY. Background notes steer selection and never reach a drafting prompt
(`own_material`). Their words may rank phrases here, and a topic word from a note may become
part of a web-search QUERY for this account's beat, but only a short lowercase topic phrase
(never a sentence, never a capitalised name, never a number, handle or link): `query_terms`.

The phrase finder is plain: runs of 1-3 content words (no stopwords, no generic words such as
"people" or "week"), scored by how many of the writer's texts carry them (statements about
the beat — bio, pillars, notes — count three times), longer phrases preferred, and a word
already inside a chosen phrase not counted twice.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

STATEMENT_WEIGHT = 4.0          # bio, pillars and background notes say what the beat IS
MAX_BEATS = 4
MAX_TERMS = 16
MIN_TEXT_SUPPORT = 2            # a phrase from the writing alone needs two texts
_CACHE_TTL_S = 60.0
JOB_KEY = "niche_read"

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'+#&.-]*[A-Za-z0-9+#]|[A-Za-z0-9]")
_STOP = frozenset("""
a about above after again against all almost also am an and any are aren't as at be because been
before being below between both but by can can't cannot could couldn't did didn't do does doesn't
doing don't down during each either else enough etc even ever every few for from further had
hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself him himself
his how how's i i'd i'll i'm i've if in into is isn't it it's its itself just let's me might more
most much must mustn't my myself neither no nor not now of off on once only or other ought our
ours ourselves out over own per quite rather same shan't she she'd she'll she's should shouldn't
so some such than that that's the their theirs them themselves then there there's these they
they'd they'll they're they've this those though through thus to too under until up upon us very
via was wasn't we we'd we'll we're we've were weren't what what's whatever when when's where
where's whether which while who who's whom whose why why's will with within without won't would
wouldn't yet you you'd you'll you're you've your yours yourself yourselves
""".split())
# Words that are never a beat on their own: they occur in every niche.
_GENERIC = frozenset("""
actually again ago almost already always another anyone anything anyway around ask asked asks
away back bad best better big bit call came can come comes coming could day days did different
does doing done down easy else end ends enough ever every everybody everyone everything fact
fast feel feels find first five fix fixed four full get gets getting give given gives go goes
going gone good got great half happen happened happens happy hard help high hot hour hours idea
ideas instead keep keeps kind know knows last later least less let lesson lessons life like
likes little long look looks lot lots low made make makes making many maybe mean means month
months more most much need needed needs never new next nice nobody none nothing now number
often okay old once one ones part people person place plan point points post posted posts
probably problem problems put question questions quick rather real really reason right run
runs said same say says second see seen seems set shit short show sign signs since small
someone something sometimes soon start started starts still stop stuff sure take takes taking
talk tell ten than thing things think thinks thought three time times tip tips today told
tomorrow took top tried tries true try trying turn turns two use used uses using want wants
way ways week weeks well went whole wins work worked works world worth wrong year years yes
yesterday yet lol gm tbh imo honestly literally basically totally thread tweet tweets reply
replies hey guys folks amazing awesome cool crazy wild possible little beats beat counts count
matter matters build building built write writes writing wrote share sharing bust busting talk
talking teach teaching cover covering running ran post posting help helping learn learned
learning lose lost win won company companies thing stuff minute minutes seconds finally
""".split())
# Who the writer IS is not what they write about: a role at either end of a phrase is dropped
# ("ML infrastructure engineer" -> "ML infrastructure", "strength coach" -> "strength").
_ROLES = frozenset("""
engineer engineers founder founders cofounder coach coaches developer developers dev devs designer
designers marketer marketers consultant consultants ceo cto cfo manager managers writer writers
creator creators builder builders owner owners expert experts person student students teacher
teachers researcher researchers analyst analysts trainer trainers
""".split())
_CONNECTOR_BREAK = re.compile(r"[.!?;:,()\[\]{}\"“”—–|/\n]+")

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"key": None, "read": None, "at": 0.0}


def _stem(word: str) -> str:
    """A light plural fold for matching only ("evals" -> "eval", "beginners" -> "beginner")."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _root(word: str) -> str:
    """A rough root for RELEVANCE only: "progressing"/"progressive" -> "progress", "trained" -> "train"."""
    word = _stem(word)
    for suffix in ("ation", "ing", "ive", "ed", "ly", "er"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _roots(text: str) -> set[str]:
    """Every content word's root, with hyphenated words also split ("full-body" -> full, body)."""
    out: set[str] = set()
    for word in _words(text):
        parts = [word] + (word.split("-") if "-" in word else [])
        for part in parts:
            if _is_content(part) and part not in _GENERIC and part not in _ROLES:
                out.add(_root(part))
    return out


def _words(text: str) -> list[str]:
    return [token.lower().strip("'.-") for token in _TOKEN_RE.findall(text or "")]


def _is_content(word: str) -> bool:
    return bool(word) and word not in _STOP and not word.isdigit() and len(word) > 1 \
        and not word.startswith(("http", "www", "@")) and "'" not in word[-2:]


def _phrases(text: str) -> Counter:
    """Every run of 1-3 content words inside one clause, keyed by its stemmed form."""
    found: Counter = Counter()
    for clause in _CONNECTOR_BREAK.split(text or ""):
        words = _words(clause)
        run: list[str] = []
        for word in words + [""]:
            if _is_content(word):
                run.append(word)
                continue
            for size in (1, 2, 3):
                for start in range(0, max(0, len(run) - size + 1)):
                    gram = run[start:start + size]
                    if size == 1 and (gram[0] in _GENERIC or gram[0] in _ROLES):
                        continue
                    if size > 1 and (gram[0] in _GENERIC or gram[-1] in _GENERIC
                                     or gram[0] in _ROLES or gram[-1] in _ROLES):
                        continue
                    found[" ".join(_stem(w) for w in gram)] += 1
            run = []
    return found


def _surface_forms(texts: Iterable[str]) -> dict[str, Counter]:
    """How each stemmed phrase is actually written, to label a beat in the writer's words."""
    forms: dict[str, Counter] = defaultdict(Counter)
    for text in texts:
        for clause in _CONNECTOR_BREAK.split(text or ""):
            words = _words(clause)
            for size in (1, 2, 3):
                for start in range(0, max(0, len(words) - size + 1)):
                    gram = words[start:start + size]
                    if all(_is_content(word) for word in gram):
                        forms[" ".join(_stem(w) for w in gram)][" ".join(gram)] += 1
    return forms


def _proper_in(note: str) -> set[str]:
    """Stemmed words a note writes capitalised mid-sentence: names, never query words."""
    names: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n", note or ""):
        tokens = _TOKEN_RE.findall(sentence)
        for index, token in enumerate(tokens):
            if index > 0 and token[:1].isupper() and not token.isupper():
                names.add(_stem(token.lower()))
    return names


def read(texts: list[str], *, bio: str = "", pillars: Iterable[str] = (),
         notes: Iterable[str] = ()) -> dict:
    """The niche read of one account: ranked phrases, 2-4 beat labels, safe query terms."""
    own = [str(text) for text in texts if str(text or "").strip()]
    statements = [str(item) for item in ([bio] + list(pillars)) if str(item or "").strip()]
    note_texts = [str(item) for item in notes if str(item or "").strip()]
    support: Counter = Counter()          # weighted count of texts carrying the phrase
    in_own: Counter = Counter()           # how many of the writer's own texts carry it
    public: set[str] = set()              # phrases seen in own texts, the bio or the pillars
    for text in own:
        for phrase in set(_phrases(text)):
            support[phrase] += 1.0
            in_own[phrase] += 1
            public.add(phrase)
    for text in statements:
        for phrase in set(_phrases(text)):
            support[phrase] += STATEMENT_WEIGHT
            public.add(phrase)
    names: set[str] = set()
    for note in note_texts:
        names |= _proper_in(note)
        for phrase in set(_phrases(note)):
            support[phrase] += STATEMENT_WEIGHT
    import math
    scored: list[tuple[float, str]] = []
    for phrase, weight in support.items():
        own_count = in_own[phrase]
        stated_weight = weight - own_count          # what the bio, pillars and notes gave it
        if own_count < MIN_TEXT_SUPPORT and stated_weight <= 0:
            continue
        # Sublinear in the writing: a word in every post ("customers", "model") is part of the
        # beat, but a beat the writer STATED outranks it.
        base = stated_weight + ((1.0 + math.log2(own_count)) if own_count else 0.0)
        size = phrase.count(" ") + 1
        scored.append((base * (1.0, 1.5, 1.7)[size - 1], phrase))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[str, float]] = []
    covered: Counter = Counter()
    for score, phrase in scored:
        words = phrase.split()
        # A word already inside a stronger chosen phrase counts again only when it clearly
        # stands on its own as well ("latency" beside "p95 latency" in many texts).
        if len(words) == 1 and covered[phrase] and score < 2.0 * covered[phrase]:
            continue
        if any(phrase in other.split(" ") or phrase == other for other, _ in chosen):
            continue
        if len(words) > 1 and any(set(words) <= set(other.split()) for other, _ in chosen):
            continue
        chosen.append((phrase, round(score, 2)))
        for word in words:
            covered[word] = max(covered[word], score)
        if len(chosen) >= MAX_TERMS:
            break
    forms = _surface_forms(own + statements + note_texts)
    labels = {phrase: (forms.get(phrase) or Counter({phrase: 1})).most_common(1)[0][0] for phrase, _ in chosen}
    terms = [labels[phrase] for phrase, _ in chosen]
    public_words = {word for phrase in public for word in phrase.split()}
    query_terms = [labels[phrase] for phrase, _ in chosen
                   if all(word in public_words or word not in names for word in phrase.split())
                   and not re.search(r"(?<![a-z])\d+(?![a-z])", phrase) and len(phrase) <= 40][:MAX_TERMS]
    vocabulary: Counter = Counter()
    for text in own + statements + note_texts:
        vocabulary.update(_roots(text))
    return {
        "beats": query_terms[:MAX_BEATS] or terms[:MAX_BEATS],
        "vocabulary": dict(vocabulary.most_common(400)),
        "terms": terms,
        "query_terms": query_terms,
        "weights": {labels[phrase]: score for phrase, score in chosen},
        "stems": [phrase for phrase, _ in chosen],
        "texts": len(own),
    }


def _inputs() -> tuple[list[str], str, list[str], list[str]]:
    """The account's own texts, bio, pillars and background notes, as the niche read takes them."""
    texts: list[str] = []
    try:
        from .storage import performance_records
        texts += [str(row.get("text") or "") for row in performance_records()
                  if row.get("text") and row.get("visibility") != "protected"]
    except Exception:                                        # noqa: BLE001
        pass
    notes: list[str] = []
    try:
        from .own_material import background_lines, voice_texts
        texts += voice_texts(limit=200)
        notes = background_lines(limit=40)
    except Exception:                                        # noqa: BLE001
        pass
    bio, pillars = "", []
    try:
        from .storage import get_account_profile, list_profile_snapshots
        profile = get_account_profile() or {}
        bio = str(profile.get("bio") or "")
        if not bio.strip():
            latest = list_profile_snapshots(limit=1)
            bio = str((latest[0] if latest else {}).get("bio") or "")
        pillars = [str(item) for item in (profile.get("content_pillars") or []) if str(item or "").strip()]
        if profile.get("audience"):
            pillars.append(str(profile.get("audience")))
    except Exception:                                        # noqa: BLE001
        pass
    return texts, bio, pillars, notes


def _fingerprint(texts: list[str], bio: str, pillars: list[str], notes: list[str]) -> str:
    blob = "\x1e".join(sorted(texts)) + "\x1f" + bio + "\x1f" + "\x1e".join(pillars) + "\x1f" + "\x1e".join(notes)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def refresh() -> dict:
    """Recompute from the database and store it (called when material is added or removed)."""
    texts, bio, pillars, notes = _inputs()
    result = read(texts, bio=bio, pillars=pillars, notes=notes)
    result["fingerprint"] = _fingerprint(texts, bio, pillars, notes)
    result["at"] = time.time()
    try:
        from .storage import set_job_state
        set_job_state(JOB_KEY, result)
    except Exception:                                        # noqa: BLE001
        pass
    from . import storage
    with _LOCK:
        _CACHE.update(key=(str(storage.DB_PATH), storage.corpus_version()), read=result, at=time.monotonic())
    return result


def current() -> dict:
    """The niche read for this account, recomputed only when its inputs changed."""
    from . import storage
    key = (str(storage.DB_PATH), storage.corpus_version())
    now = time.monotonic()
    with _LOCK:
        if _CACHE["key"] == key and _CACHE["read"] is not None and now - float(_CACHE["at"]) < _CACHE_TTL_S:
            return _CACHE["read"]
    texts, bio, pillars, notes = _inputs()
    fingerprint = _fingerprint(texts, bio, pillars, notes)
    stored: dict = {}
    try:
        stored = storage.get_job_state(JOB_KEY) or {}
    except Exception:                                        # noqa: BLE001
        stored = {}
    if stored.get("fingerprint") == fingerprint and stored.get("terms") is not None:
        result = stored
        with _LOCK:
            _CACHE.update(key=key, read=result, at=now)
        return result
    return refresh()


def beats(limit: int = MAX_BEATS) -> list[str]:
    """The beats to look up: the voice card's when it has them, else this read's."""
    try:
        from .voice_card import beats as card_beats
        from_card = card_beats()
    except Exception:                                        # noqa: BLE001
        from_card = []
    if from_card:
        return from_card[:limit]
    return list(current().get("beats") or [])[:limit]


def fit(text: str, niche: dict | None = None) -> dict:
    """How close a text is to the writer's beat: {"score": 0..1, "matched": [terms]}."""
    niche = current() if niche is None else niche
    stems = list(niche.get("stems") or [])
    labels = list(niche.get("terms") or [])
    weights = niche.get("weights") or {}
    vocabulary = niche.get("vocabulary") or {}
    if not stems and not vocabulary:
        return {"score": 0.0, "matched": []}
    words = [_stem(word) for word in _words(text)
             if _is_content(word) and word not in _GENERIC and word not in _ROLES]
    joined = " " + " ".join(words) + " "
    roots = _roots(text)
    matched: list[str] = []
    total = 0.0
    for stem, label in zip(stems, labels):
        if f" {stem} " in joined:
            matched.append(label)
            total += float(weights.get(label) or 1.0)
    top = sum(sorted((float(value) for value in weights.values()), reverse=True)[:3]) or 1.0
    beat_score = min(1.0, total / top)
    # The rest of the writer's vocabulary: a post about "chunking" and "recall" is near a
    # writer who wrote about chunking once, even when no beat phrase is in it.
    distinct = roots
    shared = [word for word in distinct if word in vocabulary]
    word_score = min(1.0, len(shared) / max(4.0, len(distinct) * 0.5)) if distinct else 0.0
    for word in sorted(shared, key=lambda w: -int(vocabulary.get(w) or 0)):
        if word not in matched and len(matched) < 6:
            matched.append(word)
    return {"score": round(max(beat_score, 0.8 * word_score), 3), "matched": matched[:6]}


__all__ = ["beats", "current", "fit", "read", "refresh"]
