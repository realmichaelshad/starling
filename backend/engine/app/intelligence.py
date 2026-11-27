from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from datetime import datetime
from uuid import uuid4

import numpy as np
from typing import TYPE_CHECKING

if TYPE_CHECKING:                     # sklearn is imported where it is used (cold start)
    from sklearn.feature_extraction.text import TfidfVectorizer

from . import calibration, deadline
from .embeddings import LocalOnnxEmbedder
from .growth_scorer import (_anchors, _source_mirroring, band_for,
                            empty_hype_hits, score_candidate)
from .llm import ModelClient, fit_to_limit, variation_note
from .performance import ChronologicalPerformanceModel, normalized_objective, plural_post_type
from . import entity_truth, topics
from .safety import assess_context, assess_draft
from .schemas import DraftCandidate, DraftRequest, PostContext
from .storage import (
    corpus_version,
    learning_examples,
    previously_drafted_replies,
    previously_drafted_texts,
    load_cached_embeddings,
    performance_records,
    save_safety_event,
    store_cached_embeddings,
)
from .strategy import strategy_brief

# Kept in step with main._EVIDENCE_LEAD: the phrase the grounding
# facts are introduced with inside a drafting direction.
_EVIDENCE_MARKER = "USE THESE FACTS, and only these:"
from .text_normalizer import fold_punctuation, split_sentences


def analyse_context(context: PostContext) -> tuple[bool, list[str]]:
    assessment = assess_context(context)
    if assessment.categories:
        _record_safety("context", assessment.severity, assessment.blocked, assessment.reasons, context.model_dump(mode="json"))
    return assessment.blocked, assessment.reasons


def feature_vector(text: str, created_at: str | None = None, media_count: int = 0) -> list[float]:
    """Legacy nine-feature interface retained for callers and old tests."""
    hour = 12.0
    if created_at:
        try:
            hour = datetime.fromisoformat(created_at.replace("Z", "+00:00")).hour
        except ValueError:
            pass
    return [
        min(len(text), 500) / 500,
        text.count("?") / 3,
        text.count("!") / 3,
        len(re.findall(r"#[\w]+", text)) / 4,
        len(re.findall(r"@[\w]+", text)) / 4,
        sum(ord(char) > 10000 for char in text) / 8,
        1.0 if "\n" in text else 0.0,
        media_count / 4,
        hour / 24,
    ]


class PersonalModel:
    def __init__(self) -> None:
        self.rows = performance_records()
        learned = learning_examples()
        # `kind`, `parent` and `created_at` travel with every row (V2): a reply is
        # shown as the pair it was, and similarity is helped by recency.
        self.example_rows = [{"text": row["text"], "performance": self._row_strength(row),
                              "shareable": row.get("visibility") != "protected",
                              "kind": str(row.get("post_type") or "post"),
                              "parent": str(row.get("parent_text") or ""),
                              "created_at": row.get("created_at")}
                             for row in self.rows if row.get("text")]
        # Every archive example remains on this computer and can be used for local retrieval.
        self.example_rows.extend({"text": text, "performance": 0.7, "shareable": True,
                                  "kind": "edited"} for text in learned if text)
        # The strategy manual's archive-proven exemplars (his best replies plus
        # WHY they worked) join the voice pool at high weight — ON HIS MACHINE.
        #
        # `exemplars_for_this_install`, never `get_strategy().exemplars`. Those
        # three replies are baked into the shipped manual, and on an empty
        # database they were the ONLY rows in this pool, so a new user's first
        # draft was written in another person's voice, with his opinions and his
        # @-mentions of real accounts. The accessor resolves ownership per call
        # against the account handle; the owner's pool is unchanged.
        try:
            from .strategy_v2 import exemplars_for_this_install
            self.example_rows.extend({"text": exemplar["text"], "performance": 0.9, "shareable": True,
                                      "kind": str(exemplar.get("format") or "reply")}
                                     for exemplar in exemplars_for_this_install()
                                     if exemplar.get("text"))
        except Exception:
            pass
        # WRITING HE HANDED THE ACCOUNT ON PURPOSE, which until now there was no
        # way to do: the voice pool was the archive he imported once plus the
        # drafts he happened to edit. Weighted at 0.95 — above a median archive
        # post and above the strategy exemplars, because an example he chose and
        # typed is the most direct statement of "write like this" the product
        # can receive, and there will only ever be a handful of them.
        #
        # ADDED HERE, in the pool itself, rather than at one of the read sites.
        # `_voice_examples` has five different return paths (reply-by-length,
        # enhancement, assigned-subject, off-topic, backfill) and appending to
        # one of them would have been a setting that works on some surfaces and
        # silently does nothing on the rest — this module's recurring failure.
        # Everything downstream reads `example_rows`, so one insert covers all
        # five. `tests/test_own_material.py` pins that it reaches the writer.
        try:
            from .own_material import voice_texts
            self.example_rows.extend({"text": text, "performance": 0.95, "shareable": True,
                                      "kind": "material"}
                                     for text in voice_texts() if text)
        except Exception:
            pass
        # Deleted posts are STYLE-ONLY negatives for the scorer: wording he
        # rejected fast. Never engagement labels, never few-shot examples.
        #
        # BOTH KINDS OF DELETION COUNT. This read only the archive export's
        # `deleted_tweet` records — history from the once-ever zip — so a post
        # he published and then took down (marked here the moment he says "but
        # i deleted it", or by a later re-import) taught the scorer nothing.
        # A post deleted while it was live is the STRONGER signal of the two:
        # he saw it on his timeline and pulled it.
        try:
            from .storage import archive_texts, deleted_post_texts
            deleted = list(archive_texts(["deleted_tweet"], limit=120))
            seen = {text.strip() for text in deleted}
            deleted.extend(text for text in deleted_post_texts(limit=120)
                           if text.strip() and text.strip() not in seen)
            self.deleted_texts = deleted
        except sqlite3.OperationalError:
            self.deleted_texts = []
        self._voice_band: tuple[float, float] | None = None
        self.embedder = LocalOnnxEmbedder()
        self.embedding_matrix: np.ndarray | None = None
        self.vectorizer: TfidfVectorizer | None = None
        self.matrix = None
        if len(self.example_rows) >= 2:
            texts = [row["text"] for row in self.example_rows]
            # Retrieval runs in process on the bundled random projection: no
            # archive text leaves the engine to be embedded.
            if self.embedder.available:
                self.embedding_matrix = _cached_encode(texts, "local-projection", lambda batch: self.embedder.encode(batch))
            else:
                # Source-only fallback: character n-grams preserve voice across slang and languages.
                # Imported here, not at module level: sklearn is about half of the engine's
                # import time, and the embedder path above never needs it (cold start).
                from sklearn.feature_extraction.text import TfidfVectorizer
                self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), max_features=7000)
                self.matrix = self.vectorizer.fit_transform(texts)
        self.performance_model = ChronologicalPerformanceModel(self.rows)
        self.action_performance_models = {
            action: ChronologicalPerformanceModel(self.rows, action)
            for action in ("reply", "quote", "post")
        }

    @staticmethod
    def _row_strength(row: dict) -> float:
        values = [normalized_objective(row, objective) for objective in ("reach", "conversation", "resonance")]
        available = [value for value in values if value is not None]
        return float(np.mean(available)) if available else 0.0

    @staticmethod
    def _normalised_embeddings(values: list[list[float]]) -> np.ndarray:
        matrix = np.asarray(values, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-8)

    def similar_examples(self, text: str, limit: int = 6, remote_safe: bool = False) -> list[str]:
        if not text:
            return [row["text"] for row in self.example_rows if not remote_safe or row["shareable"]][-limit:]
        if self.embedding_matrix is not None:
            query = self.embedder.encode([text])[0]
            scores = self.embedding_matrix @ query
        elif self.vectorizer and self.matrix is not None:
            from sklearn.metrics.pairwise import cosine_similarity
            scores = cosine_similarity(self.vectorizer.transform([text]), self.matrix)[0]
        else:
            return [row["text"] for row in self.example_rows if not remote_safe or row["shareable"]][-limit:]
        # Similarity leads; historical strength is only a small tie-breaker.
        combined = [(float(scores[index]) + min(0.15, self.example_rows[index]["performance"] / 50), index)
                    for index in range(len(scores)) if not remote_safe or self.example_rows[index]["shareable"]]
        combined.sort(reverse=True)
        return [self.example_rows[index]["text"] for score, index in combined[:limit] if score > 0.02]

    def winning_examples(self, post_type: str = "post", limit: int = 2) -> list[str]:
        """The user's own TOP-performing posts of one format, outcome-ranked.

        similar_examples keeps voice continuity around the current topic; these
        show the generator what WINNING looks like in this voice — the hooks
        and shapes that actually earned reach on this account."""
        scored = []
        for row in self.rows:
            text = str(row.get("text") or "").strip()
            if not text or len(text) < 25:
                continue
            row_type = str(row.get("post_type") or "post")
            if row_type != post_type and not (post_type == "post" and row_type == "original"):
                continue
            strength = self._row_strength(row)
            if strength > 0:
                scored.append((strength, text))
        scored.sort(reverse=True)
        return [text for _, text in scored[:limit]]

    def predict(self, text: str, media_count: int = 0, post_type: str = "post") -> tuple[dict[str, float | str | None], list[str]]:
        action_model = self.action_performance_models.get(post_type)
        if action_model and action_model.fits:
            prediction, reasons = action_model.predict(text, media_count, post_type)
            return prediction, [f"Forecast uses {post_type}-specific historical comparables rather than mixing formats.", *reasons]
        if action_model is not None:
            return ({"reach": None, "conversation": None, "resonance": None, "confidence": "limited"},
                    # Plain language: "will not borrow numerical forecasts from a
                    # different action type" is internal vocabulary, and it was
                    # being shown to the user verbatim under the draft.
                    [f"You don't have enough {plural_post_type(post_type)} yet for me to predict how this "
                     f"one will do, and I won't guess from your other post types."])
        return self.performance_model.predict(text, media_count, post_type)

    def voice_band(self) -> tuple[float, float]:
        """How similar his OWN posts are to each other — the reachable maximum.

        Measured leave-one-out on this account: median 0.457, best 0.670. Raw
        cosine was being fed straight into an 11-point term as though 1.0 were
        attainable, so a draft that sounded exactly like him earned about 5 of
        11 and the other 6 went nowhere. Generated drafts actually scored 0.486
        — slightly MORE in-voice than his own posts are to one another — while
        the rubric recorded them as barely half marks.

        Returns (floor, ceiling) — BOTH ends have to be measured. The bundled
        96-dimension projection is coarse: text with nothing to do with this
        account still scores about 0.39 against it, while his own posts reach
        only about 0.46. Dividing by the ceiling alone scored a cake recipe at
        8.2 of 11, because almost the whole 0..1 range is noise floor. The
        usable signal is the narrow band between "unrelated text" and "his own
        writing", so the score is where a draft sits IN THAT BAND.
        """
        if self._voice_band is not None:
            return self._voice_band
        band = (0.0, 0.65)                  # neutral default when unmeasurable
        matrix = self.embedding_matrix
        if matrix is not None and matrix.shape[0] >= 5:
            try:
                similarities = []
                for index in range(matrix.shape[0]):
                    row = matrix @ matrix[index]
                    row[index] = -1.0       # never match a text against itself
                    similarities.append(float(np.max(row)))
                ceiling = float(np.percentile(similarities, 75))
                floor = self._out_of_domain_floor()
                # Keep a real gap between the ends, or a uniform corpus turns
                # every draft into full marks.
                if ceiling - floor >= 0.05:
                    band = (floor, ceiling)
                else:
                    band = (max(0.0, ceiling - 0.15), ceiling)
            except Exception:
                pass
        self._voice_band = band
        return band

    # Text from outside this account's world. Embedded once to find out what
    # "nothing like him" actually scores, because on a 96-dimension projection
    # that is nowhere near zero.
    _OUT_OF_DOMAIN = (
        "Preheat the oven to 180C and butter a nine inch cake tin.",
        "The quarterly synergy alignment framework drives stakeholder value.",
        "He scored twice in the second half and the crowd went home happy.",
        "Rainfall this week will ease the drought across the eastern counties.",
    )

    def _out_of_domain_floor(self) -> float:
        try:
            queries = self.embedder.encode(list(self._OUT_OF_DOMAIN))
            best = [float(np.max(self.embedding_matrix @ query)) for query in queries]
            return float(np.median(best)) if best else 0.0
        except Exception:
            return 0.0

    def voice_similarity(self, text: str) -> float | None:
        """How close a draft is to his voice, as a share of what is reachable."""
        if not text or self.embedding_matrix is None:
            return None
        try:
            query = self.embedder.encode([text])[0]
            scores = self.embedding_matrix @ query
            if not len(scores):
                return None
            raw = float(np.clip(np.max(scores), 0.0, 1.0))
            floor, ceiling = self.voice_band()
            span = max(1e-6, ceiling - floor)
            return float(np.clip((raw - floor) / span, 0.0, 1.0))
        except Exception:
            return None

    def performance_hint(self) -> str:
        report = self.performance_model.report()
        if not report:
            return "Personal analytics evidence is limited. Optimize for a clear, original point and gather outcomes before claiming a pattern."
        summaries = [f"{name}: {item['sample_size']} chronological samples" for name, item in report.items()]
        return "Personal forecasts use action-specific, post-age/follower-normalized chronological holdouts where enough comparable examples exist (" + "; ".join(summaries) + "). They are directional, not promises."

    def grok_rate(self) -> float:
        if not self.rows:
            return 0.0
        return sum("@grok" in row["text"].lower() for row in self.rows) / len(self.rows)


def _cached_encode(texts: list[str], model_name: str, encode_fn) -> np.ndarray | None:
    """Encode texts with a persistent per-text cache; only new rows hit encode_fn."""
    hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    try:
        cached = load_cached_embeddings(list(set(hashes)), model_name)
    except sqlite3.OperationalError:
        cached = {}
    missing_indexes = [index for index, text_hash in enumerate(hashes) if text_hash not in cached]
    fresh: np.ndarray | None = None
    if missing_indexes:
        fresh = encode_fn([texts[index] for index in missing_indexes])
        if fresh is None:
            return None
        fresh = np.asarray(fresh, dtype=np.float32)
        try:
            store_cached_embeddings(
                [(hashes[index], fresh[position].tobytes(), int(fresh.shape[1]))
                 for position, index in enumerate(missing_indexes)], model_name)
        except sqlite3.OperationalError:
            pass
    dim = int(fresh.shape[1]) if fresh is not None else next((entry[1] for entry in cached.values()), 0)
    if not dim:
        return None
    matrix = np.zeros((len(texts), dim), dtype=np.float32)
    fresh_position = 0
    for index, text_hash in enumerate(hashes):
        entry = cached.get(text_hash)
        if entry is not None and entry[1] == dim:
            matrix[index] = np.frombuffer(entry[0], dtype=np.float32)[:dim]
        elif fresh is not None and fresh_position < len(missing_indexes):
            matrix[index] = fresh[fresh_position]
            fresh_position += 1
        else:
            return None  # stale cache row with a different dim: rebuild next pass
    return matrix


_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE: dict = {"model": None, "version": -1, "built_at": 0.0}
_MODEL_CACHE_TTL_S = 900.0


def get_personal_model() -> PersonalModel:
    """Reuse the PersonalModel across requests; rebuild on new data or TTL.

    Rebuilding on every click re-read the whole archive, re-encoded the corpus
    and re-fit four sklearn models — measurable dead time before the LLM even
    started. The corpus version (storage.mark_corpus_dirty) invalidates this.
    """
    from . import storage as _storage
    # Keyed on (corpus version, DB path) so isolated test databases and any
    # runtime data-dir switch can never serve a stale model.
    version = (corpus_version(), str(_storage.DB_PATH))
    now = time.monotonic()
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE
        if cached["model"] is not None and cached["version"] == version and now - cached["built_at"] < _MODEL_CACHE_TTL_S:
            return cached["model"]
    model = PersonalModel()
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.update(model=model, version=version, built_at=time.monotonic())
    return model


def reset_personal_model_cache() -> None:
    """Test hook."""
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.update(model=None, version=-1, built_at=0.0)


# Best-of-N plan: the MOST attempts one request may make, never the usual
# number. Attempts are sequential and share a byte-identical prompt prefix (the
# angle and retry notes ride at the tail), so the provider's prompt cache makes
# calls 2..N cheap. Enhance stays N=1: never replace the user's wording with a
# different candidate.
#
# A later attempt is generated only when the earlier ones produced nothing
# that passed the gates, or a draft with a detected weakness worth another shot
# (`_first_draft_that_passes`, S1). A draft that lands first time ends the run.
# Each attempt takes a different angle (`_candidate_angles`), so a retry is a
# different approach rather than another roll of the same dice.
# FOUR SHOTS ON A REPLY, not three: for a writer that lands 40% of the time the
# capture rate goes 1 - 0.6^n, three tries 78%, four 87%. Only a draft already
# judged weak spends them.
CANDIDATE_PLAN = {"reply": 4, "quote": 2, "post": 3}
# RANKING BANDS ONLY, never stop rules (S1, 2026-09-21). EARLY_EXIT_SCORE was a
# door no reply ever reached (best measured 86.6), and ACCEPT_SCORE made every
# post under 78 buy another attempt whatever its gates said. The loop now stops
# on the first draft that passes the gates (`_first_draft_that_passes`).
EARLY_EXIT_SCORE = 88.0
ACCEPT_SCORE = 78.0

# THE ATTEMPT LOOP STOPS ON THE FIRST DRAFT THAT PASSES THE GATES (S1).
#
# It used to stop on ELAPSED TIME: a soft budget (8 s on a reply, 20 s on posts
# and quotes) that ended the run once `best` held something, plus a score no
# draft reached. Those numbers were calibrated for a local model that took 7-40
# seconds per attempt, where the first attempt alone overran the budget. On an
# API model an attempt takes ~1.5 s, so the same rule made a FASTER model make
# MORE calls: a ✦ reply always ran all four attempts even when attempt one
# passed every gate (measured: 6.3 s and 5 AI calls where 1.8 s and 2 would do).
#
# What still buys another attempt is unchanged in spirit: a draft the gates
# flagged (`needs_review`, the near-miss path), and the detected weaknesses in
# `_reply_needs_another_attempt` (an echo, a thin or repeated reply, a quote that
# only asks, a post that missed its planned shape). A score is not one of them.
#
# The hard ceilings below are SAFETY NETS for a slow provider, not pacing: they
# only end a run that already holds something to ship.
REPLY_HARD_CAP_SECONDS = 20.0
HARD_CAP_SECONDS = 25.0


def _hard_cap_for(action: str | None) -> float:
    return (REPLY_HARD_CAP_SECONDS if action == "reply"
            else HARD_CAP_SECONDS)

# THIS MODULE COULD NOT SEE THE CLOCK. `deadline` reaches five call sites in
# three modules through a contextvar precisely so it can be read anywhere, and
# every one of them was in the transport layer — `providers.py` capped how long
# a single call could run and nothing capped how many more the pipeline would
# start. So a request could cap each call correctly and still walk past its own
# wire deadline one well-behaved call at a time, which is the shape of every
# "Starling could not finish that draft" that had no cause in it.
#
# What one extra model call costs HERE: on the API model a rewrite or a support
# check is one short completion, ~1 s, under an 8 s auxiliary timeout (S5,
# 2026-09-21; these were 40 s and 30 s when a local model decoded at 5-9 tok/s).
#
# Spending that with less than this left does not buy a better draft — it
# spends the entire remaining budget on a rewrite and leaves nothing for the
# gates, the scoring, the storage read-back and the response, so a request
# holding a shippable draft returns none at all. Below the bar the draft ships
# with its weakness NAMED instead (the quality-only path below), which is the
# degradation this pipeline already knows how to do. Both stay above
# `deadline.MIN_USEFUL_S`, so an optional call always stops before the draft
# itself would.

# The same reasoning for the support check's extra call.
_SUPPORT_BUDGET_S = 6.0


def _affordable(seconds: float) -> bool:
    """Is there room for a call of roughly `seconds` inside this request?

    True when there is no budget at all — the arena, the learning loop and
    every tool run unbudgeted and must keep their full behaviour, exactly as
    `deadline.remaining()` documents.
    """
    left = deadline.model_time()
    return left is None or left >= seconds


# Deliberate hook variation across best-of-N: the bandit varies WHAT the
# candidate argues; these vary how it OPENS, so candidates don't share one
# weak first line.
#
# THE QUESTION HOOK IS GONE FROM POSTS TOO. Measured 2026-07-28 (17:40
# batch): five of nine drafts opened by addressing the reader — "You think
# AI coding agents are helping?", "Do you want open-source memory without
# keys?" — and two contorted into @-mentions ("@Writekin you strip…",
# "@HN SOTA…") to satisfy a "question opening" hook title. His archive
# addresses the reader in 0 of 35 posts. The instructed question turns into
# a question AT someone; the organic ones (his closer questions) arise
# without being asked for, and those still pass every gate. Same defusal the
# quote surface got, same reasoning, one surface later.
HOOK_STYLES = (
    "Open with your flat verdict — one declarative sentence a reader could "
    "disagree with. Never address the reader.",
    "Open with a sharp contrast or contradiction against what everyone assumes.",
    "Open with one concrete number or named specific — no warm-up sentence.",
)

# THE SHITPOST REGISTER GETS ITS OWN HOOKS, for the same reason the quote and
# reply surfaces did: the post hooks above are written for a bulletin. "Open
# with your flat verdict" and "open with one concrete number" are instructions
# to write a thesis, and 48% of his real posts are not theses — median 10 words,
# 26 of 31 exactly one sentence, and his two longest are a Thor joke and a
# sustained bit about potatoes.
#
# NOT SELECTED FROM YET (step 8 wires the register into hook selection). It
# lives here rather than in `post_plan` so both post tables stay in one file —
# splitting them across modules is how two tables drift apart, and this file
# already documents two surfaces that took the wrong hooks for months.
SHITPOST_HOOK_STYLES = (
    "Open on the joke. The first line IS the post. No set-up, no framing "
    "sentence before it.",
    "Open on the named absurdity: the specific thing that is funny, said "
    "plainly, without explaining why it is funny.",
    "Open mid-thought, lowercase, as if continuing a conversation the reader "
    "was already in.",
)


# A QUOTE NEVER GETS THE QUESTION HOOK. The reply lane already caught this
# exact own-goal — HOOK_STYLES written for standalone posts being cycled into a
# surface whose brief forbids the shape — and gave replies their own hooks.
# Quotes kept taking the post path, so attempt 1 of EVERY quote was literally
# instructed to "open with a question" while SOURCE_ADDITION_RULE says "do not
# write a rhetorical question". The prompt argued with itself, and the strong
# side of the argument was the angle instruction.
#
# Measured before this existed: quotes shipped as bare questions in 6/10, 3/10,
# 5/10 across three cycles, and with the soft budget ending most runs after one
# attempt, the question angle WAS the run. A quote's whole job is the one line
# the reader would not have got from the post on screen — a claim, not a
# question the post already provokes.
QUOTE_HOOK_STYLES = (
    "Open with your flat verdict on it — one declarative sentence a reader "
    "could disagree with.",
    "Open with a sharp contrast or contradiction against what everyone assumes.",
    "Open with the consequence: what changes for people who build, because of "
    "this. No warm-up sentence.",
)


# A REPLY GETS ITS OWN HOOKS, and finding out why is the most useful thing
# measured on this surface.
#
# `HOOK_STYLES` above is written for a standalone POST, where opening with a
# question or a contradiction is a legitimate way to stop a stranger's scroll.
# It was being cycled into every reply prompt too, so two of the three angles
# generated for a reply said, verbatim:
#
#   "Open with a question that carries its own premise"
#   "Open with a sharp contrast or contradiction against what everyone assumes"
#
# — alongside a `reply_plan` brief saying "not a question back" and "do NOT
# argue and do NOT play devil's advocate". The prompt was arguing with itself,
# and the question-opener tic and the contrarian replies I kept chasing
# downstream were being ASKED FOR upstream. The bandit's angle title
# ("Contradiction or hypocrisy") pulls the same way.
#
# These three are all compatible with every reply move. They vary the
# SUBSTANCE — which part of the post to build on — instead of varying the
# rhetorical device, because on a reply the move brief already decided the
# device.
REPLY_HOOK_STYLES = (
    "Lead with the specific thing they said, and what follows from it.",
    "Lead with the consequence they stopped short of naming.",
    "Lead with the case where it bites hardest, concretely.",
)


def _prior_reply_count(request: DraftRequest) -> int:
    """How many replies already exist for this exact post. 0 on the first press."""
    post_id = str(getattr(request.context, "post_id", "") or "").strip()
    if not post_id:
        return 0
    try:
        from .storage import replies_already_drafted_for
        return len(replies_already_drafted_for(post_id, limit=6))
    except Exception:                                       # noqa: BLE001
        return 0


def _candidate_angles(request: DraftRequest, count: int) -> list[dict]:
    if request.enhancement_mode or count <= 1:
        return [{}]
    if request.action == "reply":
        # No bandit angle title either: those are post angles, and "Angle to
        # execute: Contradiction or hypocrisy" is the same own-goal in a
        # different sentence.
        #
        # THE ROTATION SHIFTS ON A RETRY. The angle sequence is otherwise
        # identical every time, so pressing the button twice on the same post
        # ran attempt 1 against the same hook as before and came back with the
        # same thought reworded — three of fourteen drafts in one run. Offset
        # by how many replies already exist FOR THIS POST and the second press
        # starts from a different angle, with no dependence on the point call
        # surviving a busy model.
        offset = _prior_reply_count(request)
        return [{"instruction": REPLY_HOOK_STYLES[(offset + index)
                                                  % len(REPLY_HOOK_STYLES)]}
                for index in range(count)]
    if request.action == "quote":
        hooks = QUOTE_HOOK_STYLES
    elif getattr(request, "post_register", None) == "shitpost":
        # The post hooks tell the writer to open with a verdict or a number —
        # instructions to write a thesis, on the register whose median is ten
        # words and one sentence.
        hooks = SHITPOST_HOOK_STYLES
    else:
        hooks = HOOK_STYLES
    try:
        from .bandit import choose_angles
        from .strategy_v2 import angle_title
        seed = request.context.context_id or request.context.post_id or request.context.text[:80]
        chosen = choose_angles(request.action, count, seed=seed)
        angles = []
        for index, item in enumerate(chosen):
            title = str(angle_title(item["angle_id"]) or "")
            # THE BANDIT'S TITLES ARE POST ANGLES TOO. "Angle to execute:
            # Question with a premise" reached quote prompts through this
            # prefix even after the hooks were split — the exact own-goal the
            # reply comment above records ("No bandit angle title either").
            # For a quote the question-shaped arm keeps its bandit slot (the
            # observation stays comparable) but stops instructing the shape;
            # the claim-first hook stands alone.
            # ...and for POSTS since 2026-07-28: the same title produced
            # "Question opening asking if..." hooks and the 17:40 batch's
            # reader-address epidemic (five of nine drafts).
            if request.action in ("quote", "post") and re.search(r"\bquestion\b", title, re.IGNORECASE):
                angles.append({"angle_id": item["angle_id"],
                               "instruction": hooks[index % len(hooks)]})
            else:
                angles.append({"angle_id": item["angle_id"],
                               "instruction": f"Angle to execute: {title}. "
                                              f"{hooks[index % len(hooks)]}"})
        return angles or [{}]
    except Exception:
        return [{"instruction": hooks[index % len(hooks)]} for index in range(count)]


# WHICH OWN WRITING THE DRAFT IS SHOWN (V2, 2026-09-21): `voice_examples.select`.
#
# This used to EXCLUDE every past post on the subject in front of it (an
# on-topic post was treated as an answer key), take replies by LENGTH (4-36
# words, closest to 18: one author's reply length) and ORDER them by one
# author's slang ("kinda", "honestly", "bro", "lol"). So each customer was shown
# the posts least like the one it had to write, sorted by how much they sounded
# like somebody else. The copy guards that made the exclusion feel necessary
# still run on every draft (`copied_voice_example`, `copies_the_post`), so the
# most similar writing can now be shown, which is what shows a voice best.


def _reply_voice_examples(model: PersonalModel, request: DraftRequest,
                          *, exclude: set[str] | None = None) -> list[str]:
    """The writer's own replies for a reply draft, as (parent, reply) pairs, most similar first."""
    from .voice_examples import select
    reply_request = request if request.action == "reply" else request.model_copy(update={"action": "reply"})
    return select(model, reply_request, exclude=exclude)


def _voice_examples(model: PersonalModel, request: DraftRequest,
                    *, exclude: set[str] | None = None) -> list[str]:
    """The writer's own most similar writing for this draft (see `voice_examples`).

    API-native pass: these are the NEAREST examples (4-8, in the variable part of the
    prompt); the writer block's fixed sample carries the representative ones in the
    cached prefix, and `exclude` keeps a text from being shown twice. Order is stable per
    request, so every attempt of one press shares the same prompt.
    """
    from .voice_examples import select
    return select(model, request, exclude=exclude)


class _GateExamples(list):
    """Every own text the model was shown (nearest + the fixed sample), for the copy gates."""


def _examples_for(model: PersonalModel, request: DraftRequest,
                  brief: dict | None) -> tuple[list[str], list[str]]:
    """(the nearest examples for the prompt, every own text the prompt shows for the gates)."""
    try:
        from .llm import writer_prefix
        shown = list(writer_prefix(brief)[1])
    except Exception:                                        # noqa: BLE001
        shown = []
    nearest = _voice_examples(model, request, exclude=set(shown))
    gate = _GateExamples(list(nearest) + [text for text in shown if text not in set(nearest)])
    return nearest, gate


class Drafts(list):
    """The ranked candidates, plus the model's verdict when the call decided (D1).

    A plain `list` everywhere it is used; `verdict` is only read by the ✦ path.
    Callers and test doubles that return a bare list simply carry no verdict.
    """

    verdict: dict | None = None


def _verdict_of(raw_candidates: list) -> dict | None:
    """The decision fields of the first draft object, when the call carried them."""
    from .llm import verdict_confidence
    first = next((raw for raw in raw_candidates if isinstance(raw, dict)), None)
    if not first or "verdict" not in first:
        return None
    return {"verdict": str(first.get("verdict") or "").strip().lower(),
            "why": str(first.get("why") or "").strip()[:200],
            "confidence": verdict_confidence(first)}


def generate_drafts(request: DraftRequest, llm: ModelClient,
                    *, brief: dict | None = None) -> list[DraftCandidate]:
    context_safety = assess_context(request.context)
    if context_safety.blocked:
        return []
    model = get_personal_model()
    # The caller has usually built this already (`main.py` needs it for
    # `create_generation`), and building it twice in one request is pure cost:
    # `strategy_brief` is a read over the whole corpus. It is a pure function of
    # one DB snapshot, and `_experiment_assignment` is seeded deterministically
    # from the context id, so the second call provably returns the same dict.
    brief = brief if brief is not None else strategy_brief(request)
    examples, gate_examples = _examples_for(model, request, brief)
    hint = model.performance_hint()
    plan = 1 if request.enhancement_mode else CANDIDATE_PLAN.get(request.action, 1)
    # NO ANGLE INSTRUCTIONS (API-native pass): the model chooses the move and the angle.
    # A later attempt exists only because every draft of the one before failed an
    # integrity check, and it is told which (`_retry_note`).
    angles = [{} for _ in range(plan)]
    started = time.monotonic()
    best: list[DraftCandidate] = []
    # Every draft the writer produced for this request, kept or not, and why the
    # rejected ones were rejected: a retry is told both.
    tried: list[str] = []
    rejections: list[str] = []
    verdict: dict | None = None
    # D1: only the FIRST attempt decides. Once the model has said "act" (or a
    # "wait" too unsure to refuse the press), every later attempt is a plain
    # draft of the same post.
    drafting_request = (request.model_copy(update={"decide": False, "decision_brief": None})
                        if getattr(request, "decide", False) else request)
    for attempt, angle in enumerate(angles):
        attempt_request = request if attempt == 0 else drafting_request
        raw_candidates, sources, research_status = llm.generate(
            attempt_request, examples, hint, brief,
            angle_instruction=angle.get("instruction", ""), attempt=attempt,
            retry_note=_retry_note(request, best, attempt, tried=tried, rejections=rejections))
        if attempt == 0 and getattr(request, "decide", False):
            verdict = _verdict_of(raw_candidates)
            from .llm import WAIT_CONFIDENCE
            if verdict and verdict["verdict"] == "wait" and verdict["confidence"] >= WAIT_CONFIDENCE:
                # A confident "nothing worth adding" is the answer: no draft is
                # written, gated or charged as a product.
                out = Drafts()
                out.verdict = verdict
                return out
        tried.extend(str(raw.get("text") or "") for raw in raw_candidates if isinstance(raw, dict))
        if angle.get("angle_id"):
            for raw in raw_candidates:
                raw.setdefault("angle_id", angle["angle_id"])
        assembled = _assemble_candidates(raw_candidates, sources, research_status, drafting_request,
                                         gate_examples, model, brief, context_safety, llm,
                                         rejections=rejections)
        best = _merge_ranked(best, assembled, action=request.action)
        # S1: THE FIRST DRAFT THAT PASSES THE GATES SHIPS.
        if _first_draft_that_passes(request, best, time.monotonic() - started):
            break
        # A draft that passed integrity but repeats an earlier reply buys ONE more attempt
        # (told what it repeated), never the whole plan: only integrity failures, which
        # leave `best` empty, may spend the rest.
        if best and attempt >= 1:
            break
        # The safety net: a slow provider cannot keep buying attempts once
        # there is something to ship.
        if best and (time.monotonic() - started > _hard_cap_for(request.action)
                     or not _affordable(_RETRY_ATTEMPT_S)):
            break
        if attempt + 1 >= plan:
            break
    out = Drafts(_support_checked(best, request, llm))
    out.verdict = verdict
    return out


def _first_draft_that_passes(request: DraftRequest, best: list, elapsed: float) -> bool:
    """Is the best draft so far good enough to stop the attempt loop?

    API-NATIVE PASS (2026-09-22): yes whenever there is a draft that passed every
    INTEGRITY check (they reject before a draft reaches `best`) and is not a labelled
    repeat of something already written. Style findings are notes on the draft, never a
    reason for another call: the length, shape and register are the writer's voice, and
    the model was shown it. The one quality reason left is a reply that repeats one this
    account was already given (`_repeats_an_earlier_reply`), which is copying its own
    past writing.
    """
    del elapsed
    if not best:
        return False
    top = best[0]
    if getattr(top, "needs_review", False) and "written this before" in str(getattr(top, "review_note", "") or ""):
        return False
    if request.action == "reply" and not getattr(request, "enhancement_mode", False):
        return not _repeats_an_earlier_reply(top.text)
    return True


def _vouched_sources(request: DraftRequest) -> list[str]:
    """Every URL a link in this draft could legitimately have come from.

    URLs, not the text that contains them: `safety.unvouched_urls` reduces
    each entry to a registrable host, and a whole sentence reduces to nothing
    — so passing the post's TEXT vouched for no host at all and the post's own
    link would have been flagged as invented.
    """
    context = getattr(request, "context", None)
    blobs: list[str] = []
    for field in ("post_url", "text", "parent_text", "quote_text", "thread_text",
                  "page_url"):
        value = getattr(context, field, None) if context else None
        if value:
            blobs.append(str(value))
    # HIS OWN DRAFT VOUCHES FOR ITS OWN LINKS. On a POST enhance this is already
    # covered, because the synthetic context's text IS the draft — but on a REPLY
    # or QUOTE enhance `context.text` is somebody else's post, so a link he typed
    # himself would have been read as invented the moment enhance started
    # checking links at all.
    original = getattr(request, "original_draft", None)
    if original:
        blobs.append(str(original))
    for group in ("links", "media"):
        for item in (getattr(context, group, None) or []) if context else []:
            blobs.append(str(item))
    # The direction carries the evidence sources the radar actually returned,
    # and since W2 the facts travel in their own field.
    blobs.append(str(getattr(request, "direction", "") or ""))
    blobs.append(str(getattr(request, "facts", "") or ""))
    urls: list[str] = []
    for blob in blobs:
        if blob.startswith(("http://", "https://")):
            urls.append(blob)
        urls.extend(re.findall(r"https?://[^\s\"'<>)\]]+", blob))
    return urls


def _support_evidence(request: DraftRequest) -> str:
    """What the top draft's claims are allowed to rest on, per surface.

    A POST is grounded in the web evidence carried inside `direction`; the
    lead-in instruction text around it is stripped so the model cannot quote
    the scaffolding back as support. A QUOTE is judged against the tweet on
    screen — the same rule the figure gate already follows, because the reader
    sees only that tweet.
    """
    if request.action == "quote":
        parts = [request.context.text or "",
                 getattr(request.context, "parent_text", "") or "",
                 getattr(request.context, "quote_text", "") or ""]
        return " ".join(p for p in parts if p).strip()
    # OPTION-4: the captured facts as a VALUE, through a type that cannot
    # carry the image note. Until now this property was held by ORDERING — the
    # caption happens to sit before the marker in every real direction — and
    # `_SCAFFOLD_TAILS` never enforced it on this path at all: its "There is an
    # IMAGE attached" is the composer's wording, while reply/quote emits "The
    # post has an IMAGE attached showing:". The two never matched. A type
    # cannot be defeated by moving a block.
    from .schemas import SupportEvidence
    supported = SupportEvidence.of(request)
    if supported.facts is not None:
        return supported.facts.strip()
    # None means UNKNOWN — a legacy row or a pre-P2 producer — so fall through
    # to the string exactly as before. Never treat None as "no evidence".
    direction = str(getattr(request, "direction", "") or "")
    marker = " USE THESE FACTS, and only these: "
    if marker not in direction:
        return ""
    facts = direction.split(marker, 1)[1].strip()
    # ONLY THE FACTS, NOT EVERYTHING PRINTED AFTER THEM. The docstring above
    # has always claimed the scaffolding is stripped; only the LEADING
    # scaffolding ever was. Everything appended later rode along as quotable
    # evidence: the harness's own "Mention at least one specific thing from
    # these facts…" instruction, the sampled shape block, the thread line,
    # and — the dangerous one — the VISION MODEL'S description of an attached
    # image. That last is model output, so a hallucinated reading of a
    # screenshot became promotable to verified fact: the draft repeats the
    # invented number, `_find_support` finds that sentence verbatim in the
    # "evidence", and it ships unflagged. Evidence must be the captured
    # facts and nothing this process wrote itself.
    for tail in _SCAFFOLD_TAILS:
        cut = facts.find(tail)
        if cut > 0:
            facts = facts[:cut]
    return facts.strip()


# Everything the harness itself prints after the captured facts. Shared with
# `_direction_without_scaffolding` so the two callers cannot drift: one decides
# what a claim may REST on, the other decides whether a figure was INVENTED, and
# they have to agree about what counts as evidence.
_SCAFFOLD_TAILS = ("Mention at least one specific thing",
                   "SHAPE FOR THIS POST",
                   "There is an IMAGE attached",
                   "The user asked for a thread",
                   "Every sentence must say something")


def _direction_without_scaffolding(request: DraftRequest) -> str:
    """`direction` with the harness's own trailing instructions removed.

    THE SHAPE BLOCK CARRIES NUMBERS, and this string is the corpus the figure
    gate checks a draft's figures against. `shape.py:554` always appends "no
    sentence past 20 words" and `shape.py:528` writes "Write it as N blocks
    separated by blank lines, N sentences total" — so 20, the block count and the
    measured word target were permanently present in the "evidence". A draft
    saying "Claude Code is 20% cheaper than Cursor", "$20", "20x" or "4x" was
    vouched for by Starling's own instruction to keep sentences under 20 words.
    Verified by running `_unsupported_claims`: the same draft against the same
    evidence returns ['20%'] without the shape block and [] with it.

    Deliberately NOT `_support_evidence`, which additionally requires the
    "USE THESE FACTS" marker and returns "" without it. That is right for
    deciding what a claim may rest on, and wrong here: a reply's direction has no
    such marker, so using it would empty the corpus for every reply and reject
    figures that the visible post does support. This only removes what the
    harness wrote itself, and keeps everything the user and the web put there.
    """
    direction = str(getattr(request, "direction", "") or "")
    for tail in _SCAFFOLD_TAILS:
        cut = direction.find(tail)
        if cut > 0:
            direction = direction[:cut]
    # W2: the facts left the direction for `DraftRequest.facts`. They are still
    # what a figure may rest on, so they rejoin here, AFTER the scaffolding cut
    # (the shape block now sits before where the facts used to be).
    facts = str(getattr(request, "facts", "") or "").strip()
    if facts and _EVIDENCE_MARKER not in direction:
        direction = f"{direction.strip()} {_EVIDENCE_MARKER} {facts}"
    return direction.strip()


def _support_checked(best: list[DraftCandidate], request: DraftRequest,
                     llm) -> list[DraftCandidate]:
    """Run the any-machine support check on the ONE draft that will ship.

    Top candidate only — it is the draft the composer receives, and the check
    costs ~10s per claim. Posts and quotes only: a reply's evidence IS the
    conversation and its dedicated gates already police it. Enhancement is his
    own text and is never second-guessed. See `support_check` for why the
    model fetches and the harness judges.
    """
    if (not best or request.action not in ("post", "quote")
            or getattr(request, "enhancement_mode", False)):
        return best
    evidence = _support_evidence(request)
    if not evidence:
        return best
    # A MANGLED NAME IS REPAIRED, NOT FLAGGED. "NoBg makes training it easy"
    # shipped beside evidence that only ever says FeyNoBg — the entity gate
    # passes substrings, so the tear was invisible. Deterministic, and it runs
    # BEFORE the support check so claims are verified against the real name.
    mended = repair_name_fragments(best[0].text, evidence)
    if mended != best[0].text:
        best[0] = best[0].model_copy(update={"text": mended})
    try:
        from .support_check import apply_support_check
        text, note = apply_support_check(best[0].text, evidence, llm)
    except Exception:                                    # noqa: BLE001
        return best
    if note:
        # A SUPPORTED RUNNER-UP OUTRANKS AN UNSUPPORTED WINNER. Flag-shipping
        # is honest but it does not change what the reader sees — measured in
        # the first 20-post phase batch, the KBlip draft carried its "I can't
        # back ..." note and the unsupported sentence right below it. When the
        # run produced a second gate-clean candidate, checking IT costs one
        # more fetch (~10-20s, only on this already-flagged path) and a
        # supported draft beats a labelled unsupported one every time.
        # ONE runner, not two — the second re-check helped rarely and cost
        # 10-30s of model time on an already-slow path (see support_check's
        # _MAX_CLAIMS note for the measured pile-up it fed).
        #
        # And not even that one when the clock is nearly out. The check above
        # already ran, so the winner is either supported or carries its "I
        # can't back ..." note; promoting a supported runner-up is a real
        # improvement but an OPTIONAL one, and it is the last thing standing
        # between a flagged-but-shippable draft and a request that ends with
        # nothing. The winner's own check is never skipped — that one is what
        # keeps an unsupported claim from shipping unlabelled.
        runner_range = range(1, min(len(best), 2)) if _affordable(_SUPPORT_BUDGET_S) else range(0)
        for runner_index in runner_range:
            try:
                alt_text, alt_note = apply_support_check(
                    best[runner_index].text, evidence, llm)
            except Exception:                            # noqa: BLE001
                break
            if alt_note:
                continue
            promoted = best[runner_index]
            if alt_text != promoted.text and alt_text.strip():
                # THE LAST WORD STAYS WITH THE SCRUBBER (the shaper rule, one
                # layer later). This check runs AFTER every scrub, and cutting
                # a sentence out of a multi-block draft leaves doubled blank
                # lines at the splice — live 2026-07-31, three of six batch
                # posts. Any text this function rewrites gets its spacing
                # re-tidied on the way out; same at both sites below.
                promoted = promoted.model_copy(
                    update={"text": tidy_whitespace(alt_text)})
            demoted = best[0].model_copy(update={
                "needs_review": True, "review_note": note,
                "rationale": [note, *list(best[0].rationale or [])]})
            best = [promoted, demoted,
                    *[c for i, c in enumerate(best) if i not in (0, runner_index)]]
            return best
        # The checker may have EDITED the text as well as flagging it — the
        # hedge transform turns "outperforms competitors" into "claims to
        # outperform competitors". Shipping the note with the un-hedged text
        # would name the problem while still asserting it.
        update: dict = {"needs_review": True, "review_note": note,
                        "rationale": [note, *list(best[0].rationale or [])]}
        if text != best[0].text and text.strip():
            update["text"] = tidy_whitespace(text)
        best[0] = best[0].model_copy(update=update)
    elif text != best[0].text and text.strip():
        best[0] = best[0].model_copy(update={"text": tidy_whitespace(text)})
    # A CUT THAT GUTS THE PLANNED SIZE MUST SAY SO. Live 2026-07-31 morning:
    # a draft passed every gate at full length, this check amputated the
    # unsupported sentences, and a 7-word survivor shipped against a 47-word
    # plan with a rationale still describing the pre-cut draft — needs_review
    # False, nothing on the card. The gates cannot re-run back here, but the
    # size floor can: whatever this function ships, an under-plan result
    # carries the same note the near-miss path would have attached.
    if best and not best[0].needs_review:
        try:
            from .shape import size_was_delivered
            direction = str(getattr(request, "direction", "") or "")
            if direction and not size_was_delivered(best[0].text, direction):
                note = _WEAKNESS_NOTES.get("under_planned_size")
                if note:
                    best[0] = best[0].model_copy(update={
                        "needs_review": True, "review_note": note,
                        "rationale": [note, *list(best[0].rationale or [])]})
        except Exception:                                # noqa: BLE001
            pass
    return best


def generate_drafts_streaming(request: DraftRequest, llm: ModelClient, emit, stop_event=None,
                              emit_status=None, *,
                              brief: dict | None = None) -> list[DraftCandidate]:
    """Streamed generation: emit(delta) previews tokens live.

    The streamed text is display-only. The complete draft still runs through
    the same safety + quality gates before it becomes a returned candidate.
    Create mode streams one angle attempt at a time and moves to the next
    angle only when the previous attempt did not pass the gates (S1: the same
    stop rule as `generate_drafts`, `_first_draft_that_passes`).
    """
    context_safety = assess_context(request.context)
    if context_safety.blocked:
        return []
    model = get_personal_model()
    # See generate_drafts: the caller already has this, and it is a corpus-wide
    # read. The extension prefers the streaming endpoint, so this is the path
    # the button actually hits.
    brief = brief if brief is not None else strategy_brief(request)
    examples, gate_examples = _examples_for(model, request, brief)
    hint = model.performance_hint()
    plan = 1 if request.enhancement_mode else CANDIDATE_PLAN.get(request.action, 1)
    angles = [{} for _ in range(plan)]      # the model chooses the angle (see generate_drafts)
    started = time.monotonic()
    best: list[DraftCandidate] = []
    tried: list[str] = []
    rejections: list[str] = []
    for attempt, angle in enumerate(angles):
        if stop_event is not None and stop_event.is_set():
            return []
        if emit_status is not None:
            emit_status("writing")          # every attempt (the stages: main.STREAM_STAGES)
        raw = llm.generate_stream(request, examples, hint,
                                  brief, emit, stop_event,
                                  angle_instruction=angle.get("instruction", ""),
                                  attempt=attempt,
                                  retry_note=_retry_note(request, best, attempt, tried=tried,
                                                         rejections=rejections))
        if stop_event is not None and stop_event.is_set():
            return []
        if isinstance(raw, dict) and raw.get("text"):
            tried.append(str(raw["text"]))
        if emit_status is not None:
            emit_status("checking")
        raw_candidates = [raw] if raw else llm._fallback_candidates(request)
        if angle.get("angle_id"):
            for item in raw_candidates:
                item.setdefault("angle_id", angle["angle_id"])
        assembled = _assemble_candidates(raw_candidates, [], "unavailable", request, gate_examples,
                                         model, brief, context_safety, llm,
                                         rejections=rejections)
        # Ranked the same way as the non-streaming path.
        best = _merge_ranked(best, assembled, action=request.action)
        # THIS IS THE PATH THE BUTTON ACTUALLY USES, so it stops by the same
        # rule as `generate_drafts` (S1): the first draft that passes the gates
        # ships. A HIGH SCORE IS STILL NOT A REASON TO STOP ON A REPLY — a
        # fluent echo scores well and says nothing, which is why the detected
        # weaknesses, not the score, decide whether another attempt is worth it.
        # Streamed tokens are display-only: the composer takes the `final`
        # event, and `emit_status` narrates each new attempt.
        if request.enhancement_mode and best:
            break
        if _first_draft_that_passes(request, best, time.monotonic() - started):
            break
        # A repeat of an earlier reply buys ONE more attempt, never the plan (see generate_drafts).
        if best and attempt >= 1:
            break
        # The safety net (see REPLY_HARD_CAP_SECONDS). `best` guards it: running
        # out of time with nothing to show is still worth one more try.
        if best and (time.monotonic() - started > _hard_cap_for(request.action)
                     or not _affordable(_RETRY_ATTEMPT_S)):
            break
    return _support_checked(best, request, llm)


# The repeat note for an ENHANCE: the text is the customer's own, so "you have written this before"
# reads as an accusation about their own words. What repeated is Starling's rewrite.
ENHANCE_REPEAT_NOTE = ("This rewrite is almost the same as one Starling suggested before. Press again, "
                       "or say what to change, for a different one.")
# ...and when what it comes close to is one of the writer's OWN earlier posts, it says that instead:
# the idea is theirs, and repeating themselves may be deliberate.
ENHANCE_OWN_POST_NOTE = ("This comes out close to one of your earlier posts. Fine if you mean to say it "
                         "again; otherwise tell me what is new this time.")


def _enhance_repeat_note(text: str) -> str:
    """Which of the two it is: close to a Starling draft of this session, or to the writer's own post."""
    try:
        if _echoes_own_writing(str(text or "").lower(), [], corpus=list(_DRAFTED_THIS_SESSION)):
            return ENHANCE_REPEAT_NOTE
    except Exception:                                        # noqa: BLE001
        return ENHANCE_REPEAT_NOTE
    return ENHANCE_OWN_POST_NOTE


# Why a rejected draft was rejected, as the next attempt is told it (`_retry_note`).
_REJECTION_SENTENCES = {
    "unsupported_claim": "it stated {x}, which is in neither the conversation nor the facts",
    "invented_experience": "it claimed a personal experience the writer never described ({x})",
    "injected_entity": "it named {x}, which is not in the conversation or the facts",
    "leaked_into_reply": "it named something the conversation never mentions",
    "copies_the_post": "it handed the post back instead of answering it",
    "copied_voice_example": "it repeated one of the writer's past posts",
    "engagement_bait": "it asked for engagement",
    "instruction_leak": "it talked about the instructions instead of being the draft",
    "unvouched_link": "it included a link nobody gave",
    "misattributed_model": "it named the wrong company behind a model",
    "left_the_point": "it lost the writer's own point",
    "repeated_reply": "it repeated a reply this account was already given",
}


def _rejection_sentence(reason: str) -> str:
    code, _, detail = str(reason or "").partition(":")
    template = _REJECTION_SENTENCES.get(code.strip())
    if not template:
        return ""
    detail = " ".join(detail.split())[:60]
    return template.format(x=f'"{detail}"' if detail else "something")


def _retry_note(request: DraftRequest, best: list, attempt: int,
                *, tried: list[str] | None = None, rejections: list[str] | None = None) -> str:
    """What the next attempt is told about the ones before it (empty on the first).

    API-native pass: an attempt after the first exists because the earlier drafts failed
    an integrity check (or repeated a reply already given), so the note says exactly
    that, in plain words, and shows how the earlier ones opened so this one differs. It
    rides at the tail of the prompt, after everything the attempts share.
    """
    if attempt <= 0:
        return ""
    reasons: list[str] = []
    for reason in rejections or []:
        sentence = _rejection_sentence(reason)
        if sentence and sentence not in reasons:
            reasons.append(sentence)
    earlier = [text for text in (tried or []) if str(text or "").strip()] or [
        str(getattr(candidate, "text", "") or "") for candidate in best]
    note = "\nAN EARLIER ATTEMPT WAS NOT USED"
    note += (": " + "; ".join(reasons[-3:]) + "." if reasons else ".")
    if earlier:
        note += (" It opened: " + "; ".join(f'"{_opening(text)}"' for text in earlier[-2:])
                 + ". Write a genuinely different draft that keeps every hard rule.")
    else:
        note += " Write a genuinely different draft that keeps every hard rule."
    return note


def _opening(text: str, count: int = 8) -> str:
    return " ".join(str(text or "").split()[:count])


def _below_reply_payoff(text: str) -> bool:
    """Does this reply give the reader nothing? See `_reply_ranking_key`."""
    from .reader_value import reader_value
    return reader_value(text or "", "reply")["score"] < _payoff_floor("reply")


# How long the extra attempts may go on costing him. Measured over a full run:
# letting a weak reply use all three attempts moved the MEDIAN wait barely at
# all (30s -> 36s, because most replies land first time) but took the p90 from
# 38s to 222s and the worst case to 356s. Nearly six minutes staring at a
# spinner is not a fair price for a better draft, and he has to press this
# button many times a day.
#
# 150s fits three ordinary attempts (~36s each, plus the plan call) and cuts
# off only the pathological case where the machine is loaded and each attempt
# is already taking minutes — which is exactly when a fourth minute buys least.
# 240s was the local-model number (~40-60 s per attempt, four attempts). On the
# API model an attempt is ~1.5 s, so 30 s is still room for the full plan of
# four with a slow provider and refuses to let one press run away (S5,
# 2026-09-21). The ✦ request itself has 50 s in all (`deadline.DEFAULT_BUDGET_S`).

# What one more attempt costs before it can be delivered: one draft call
# (~1.5 s on the API model, capped by its own 20 s timeout) plus the assembly
# and gates. Rounded up, because a retry that lands one second after the wire
# deadline bought nothing and cost everything. Was 45 s on the local model.
_RETRY_ATTEMPT_S = 6.0


def _drop_own_record(prior: list[str], text: str) -> list[str]:
    """Everything drafted before, minus THIS draft's own row.

    A draft is recorded before it is judged, so one equal row has to come out
    or it matches itself — but only one. Removing every equal row is what made
    an exact duplicate the single thing the repeat gate could not see.
    """
    body, out = (text or "").strip(), list(prior)
    for index, seen in enumerate(out):
        if seen.strip() == body:
            out.pop(index)
            break
    return out


def _repeats_an_earlier_reply(text: str) -> bool:
    """Has this exact reply already been handed over this session?

    Detecting a repeat was worth nothing on its own: `repeated_reply` carries a
    weakness note and nothing else, so the duplicate shipped anyway with a
    label on it. The second press of the button is a request for a different
    angle, and the retry path already rotates the hook and tells the point
    planner what was said last time — so the useful response to a duplicate is
    to spend another attempt, not to apologise for it.
    """
    body = (text or "").strip()
    if not body:
        return False
    try:
        from .reply_plan import repeats_a_reply
        prior = _drop_own_record(_recent_replies(), body)
        return bool(prior and repeats_a_reply(body, prior))
    except Exception:                                       # noqa: BLE001
        return False


def _weak_reply(text: str) -> bool:
    """The two ways a reply says nothing, which are one thing to the reader.

    Thinness is the obvious one and the payoff floor measures it. The other is
    a draft that carries specifics and still lands nowhere — "Claude Opus 5's
    half-price claim could mean it's cheaper than Opus 4.5's full cost" names
    two products, clears the floor at 0.44, and is a tautology. Measured over
    the 18 drafts of one run, these two accounted for 8 of the 9 faults.

    Used for all three reply decisions that ask "is this one good enough":
    which candidate wins, which near miss gets shown, and whether a second
    attempt is worth its ~60s.
    """
    from .reply_plan import sounds_like_an_analyst
    # Each half counts only where it means something for THIS writer (V3): a
    # payoff floor their own replies miss half the time is not a floor for them.
    thin = _below_reply_payoff(text) and not calibration.note_only("no_reader_payoff", "reply")
    return thin or (sounds_like_an_analyst(text or "")
                    and not calibration.note_only("analyst_voice", "reply"))


def _same_draft_key(text: str) -> str:
    """Two drafts that differ only in case, spacing or punctuation are one draft."""
    return re.sub(r"[\W_]+", " ", str(text or "").lower()).strip()


def _merge_ranked(current: list[DraftCandidate], fresh: list[DraftCandidate],
                  *, action: str | None = None) -> list[DraftCandidate]:
    # IDENTICAL DRAFTS ARE ONE CANDIDATE. A model that ignores sampling can hand
    # back the same text on a retry, and keeping both would fill the ranked list
    # with one draft twice and push a genuinely different one out of it.
    merged: list[DraftCandidate] = []
    seen: set[str] = set()
    for candidate in [*current, *fresh]:
        key = _same_draft_key(getattr(candidate, "text", ""))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(candidate)
    if action == "reply":
        # Same tier as the final ranking, for the same reason: GrowthScore
        # cannot see a thin reply, so without this a second attempt's real
        # reply merges in BELOW the empty one the first attempt produced and
        # the extra work is thrown away.
        merged.sort(key=lambda candidate: (not _weak_reply(candidate.text),
                                           candidate.growth_score if candidate.growth_score is not None else -1.0),
                    reverse=True)
    else:
        merged.sort(key=lambda candidate: (candidate.growth_score if candidate.growth_score is not None else -1.0),
                    reverse=True)
    return merged[:3]


# Issues that mean the draft must NOT ship, even with a caveat: they are about
# honesty and safety, not polish. Everything else is a quality bar the writer
# can judge for himself once he can see the draft.
# THE POST HAS TO LAND. He flagged this draft as not delivering its point:
#
#   "Opus 5 is cheaper and less restrictive than Fable 5. It launched two
#    months after Opus 4.8. Why does this matter? Because Anthropic's
#    guardrails are finally MATCHING the speed of their releases. They're NOT
#    KEEPING UP with the demand for more open models."
#
# Two separate faults, both measurable, and neither fires on any of his 34
# published posts:
#
#   * explainer scaffolding — "Why does this matter? Because..." is blog
#     structure. He writes "Why?" and then evidence; he never writes the
#     question-and-answer form, because it reads like a model narrating.
#   * self-contradiction — the post affirms an idea and then negates it. A
#     reader cannot take away a point the post argues with itself about.
#
# A first attempt measured whether the last line shared vocabulary with the
# opening. That was wrong: his own posts score 0.00 on it, because a punchy
# closing line that introduces new words is exactly his style.
_EXPLAINER_SCAFFOLD = re.compile(
    r"\b(?:why does this matter|what does this mean|why is this important|"
    r"here'?s why(?: it matters)?|the takeaway is|in other words)\b", re.I)

_KEEPING_UP = r"(?:keeping up|keep up|matching|match|catching up|catch up|on par)"
_AFFIRMS = re.compile(rf"\b(?:finally |now |already )?{_KEEPING_UP}\b", re.I)
_NEGATES = re.compile(
    rf"\b(?:not|n'?t|never|failing to|struggling to)\s+\w*\s*{_KEEPING_UP}\b"
    rf"|\bfalling behind\b|\bcan'?t keep up\b", re.I)


def _contradicts_itself(text: str) -> bool:
    """Affirms an idea and then denies it, so there is no point to carry away.

    The affirmation has to be a REAL one. "not keeping up" contains "keeping
    up", so a single one-sided claim matched both halves and read as a
    contradiction with itself — which would have flagged the perfectly coherent
    sentence "Anthropic is not keeping up with OpenAI".
    """
    if not _NEGATES.search(text):
        return False
    for match in _AFFIRMS.finditer(text):
        preceding = text[max(0, match.start() - 24):match.start()]
        if not re.search(r"\b(?:not|n'?t|never|failing to|struggling to)\s*\w*\s*$",
                         preceding, re.I):
            return True          # an affirmation that is not itself negated
    return False


_INTEGRITY_ISSUES = frozenset({"engagement_bait", "copied_voice_example",
                               "misattributed_model",
                               # `cut_off` DELIBERATELY DOES NOT BELONG HERE.
                               # It sat in this set to stop the live post that
                               # reached the composer ending on "If you're
                               # using it," — but an integrity issue is one no
                               # note can make sendable, and truncation is not
                               # that: it is the one fault the writer can see
                               # and fix in a second. Listing it here also
                               # broke the registry's NEVER1 invariant, since
                               # it has a _WEAKNESS_NOTES entry, and an issue
                               # in both sets means a QUALITY miss vanishes
                               # into silence instead of shipping named.
                               # Truncation is handled where it should be:
                               # `drop_trailing_fragment` repairs it before any
                               # gate sees it, and what survives that ships
                               # with the last sentence flagged.
                               # Naming a company the conversation never
                               # mentioned is a claim he would have to defend,
                               # not a wording problem. "I can't wait to see
                               # how Fable 5 handles a trip plan with 8
                               # friends" cannot be revised into correctness —
                               # the sentence is about the wrong thing.
                               "leaked_into_reply",
                               # A verbatim stretch of the post, handed back
                               # under the post it came from. "The pricing page
                               # is the only benchmark that has ever mattered
                               # to me because it reflects real-world value"
                               # shipped twice to the man who wrote that first
                               # clause. No "Heads up" makes that sendable, and
                               # the similarity average cannot see it — a good
                               # reply to the same post scores identically.
                               "copies_the_post"})


# What to tell the writer when a draft ships despite missing a bar. Each names
# the fault in his terms and implies the fix, because "needs review" alone is
# just a shrug.
_WEAKNESS_NOTES = {
    "explainer_scaffolding": "Heads up: it explains itself like a blog "
                             "(\"Why does this matter?\"). Cut straight to the point.",
    "contradicts_itself": "Heads up: this argues both sides of the same "
                          "idea, so there is nothing to take away. Pick one.",
    "restates_the_source": "Heads up: this leans on saying the post back. "
                           "Swap the opening for your own take before posting.",
    "no_reader_payoff": "Heads up: there is no concrete hook in this yet. "
                        "Add a number, a name, or what it changes.",
    "missing_context_connection": "Heads up: this drifts off the post it answers.",
    "generic_agreement": "Heads up: this only agrees. Add the thing you would add.",
    "says_nothing": "Heads up: this says very little. Worth a rewrite.",
    # THE SIX THAT FELL THROUGH TO THE SHRUG. Every one of these can be
    # `issues[0]` on the near-miss path, where the lookup falls back to "It is
    # a bit weak, so read it before posting." — which is the exact "needs
    # review alone is just a shrug" this registry exists to replace. Found
    # 2026-07-30 by counting raised issues against the registry.
    "sentence_too_long": "Heads up: one sentence runs past 30 words. Break it "
                         "in two before posting.",
    "dense_phrasing": "Heads up: the sentences average too long to scan on a "
                      "phone. Cut each one under 20 words.",
    "tangled_clauses": "Heads up: one sentence stacks too many clauses to "
                       "follow in a single read. Split it.",
    "abstract_jargon": "Heads up: this stacks abstract nouns nobody says out "
                       "loud. Name the concrete thing instead.",
    "empty_filler": "Heads up: there is a filler phrase carrying no meaning. "
                    "Delete it and the post loses nothing.",
    "loses_the_thread": "Heads up: this drifts away from the post it answers "
                        "partway through. Keep it on their point.",
    "asks_without_saying": "Heads up: this only asks a question, so it lands "
                           "no point of your own. Say what you actually think.",
    "talks_to_the_reader": "Heads up: this tells the reader about themselves "
                           "rather than saying a thing. Name what actually changed.",
    "parrots_headline": "Heads up: this opens by saying the headline back. "
                        "Lead with your read on it instead.",
    "sweeping_claim": "Heads up: it claims something about everyone that the "
                      "evidence can't back. Say what you actually saw.",
    "empty_big_deal": "Heads up: \"is a big deal\" says nothing — say what it "
                      "changes, costs, or breaks instead.",
    # "Him" was the original author (V9); the card speaks to the customer.
    "off_register": "Heads up: this doesn't sound like you. Drop the dashes, "
                    "keep the sentences as short as yours usually are, and use "
                    "plain words.",
    "dangling_subject": "Heads up: this never names what it's about — a "
                        "stranger scrolling past can't tell. Put the name in "
                        "the first sentence.",
    # `no_position` was defined TWICE in this dict — here and ~30 lines below.
    # Python keeps the last, so this one never shipped a single note. Removed
    # rather than merged: the lower one is what users have actually been
    # reading, and changing which text ships would be an unrequested behaviour
    # change smuggled into a bug fix. Its wording is kept here for whoever wants
    # it back: "this weighs both sides and lands on neither. Pick one and say
    # it." Found 2026-08-07 while auditing the registry against every raisable
    # key.
    "describes_the_source": "Heads up: this describes the article it came from, "
                            "and nobody reading it has seen that. Just say the thing.",
    # A note, not a rejection: a wrong name is a five-second fix once pointed
    # at, and a false positive here must cost one caution line, never a
    # deleted draft. Deliberately not revised either — the model that garbled
    # the name is the wrong tool to fix it, and quoting the bad form back
    # would teach it (see naming-a-banned-phrase).
    "mangled_name": "Heads up: a product name in this doesn't match the "
                    "source — check the names against the article before "
                    "posting.",
    "under_planned_size": "Heads up: this came out well under the size the "
                          "material supports. Post it if it lands — or hit "
                          "retry for the fuller take.",
    # Deliberately NOT in _CLARITY_ISSUES: the repair for "name something" is
    # to supply a product, version or figure, and a reviser asked for one will
    # invent it. Naming the gap is the honest move; filling it is his.
    "names_nothing": "Heads up: this doesn't name anything specific — no "
                     "product, version or number. Say which one you mean.",
    "too_short": "Heads up: this is shorter than your usual.",
    "explains_the_joke": "Heads up: the line lands and then gets explained. "
                         "Cut everything after it.",
    "overwrites_the_bit": "Heads up: this is an essay where a line would do. "
                          "Say it once and stop.",
    "cut_off": "Heads up: the last sentence looks unfinished.",
    "ai_tell": "Heads up: one phrase reads like stock AI copy.",
    "bounced_question": "Heads up: they asked you something and this doesn't "
                        "answer it. One line with the actual answer first.",
    "strawman_opener": "Heads up: it opens by putting words in their mouth "
                       "(\"So you're saying...\"). Say your point straight.",
    "repeated_reply": "Heads up: you have been shown a reply close to this one "
                      "before. Worth changing the angle.",
    "no_position": "Heads up: this lists possibilities without picking one. "
                   "Say which way you actually lean.",
    "analyst_voice": "Heads up: this reads like an analyst's note, not you. "
                     "Say it flat, the way you would out loud.",
    "talks_about_the_post": "Heads up: this describes their post instead of "
                            "replying to them. Talk to the person.",
    "ignores_the_post": "Heads up: this doesn't pick up anything they actually "
                        "said. Name one thing from their post.",
}


def _assemble_candidates(raw_candidates: list[dict], sources, research_status: str, request: DraftRequest,
                         examples: list[str], model: PersonalModel, brief: dict,
                         context_safety, llm: ModelClient | None = None,
                         rejections: list[str] | None = None) -> list[DraftCandidate]:
    """Gate, scrub and score the raw drafts of one call.

    `revise_budget` ({"left": n}) is shared by every call of one request (S7): the
    clarity rewrite runs at most that many times per request, on the draft that is
    about to ship, never once per candidate or per thread part. None = no cap
    (callers outside the generators).
    """
    result: list[DraftCandidate] = []
    rejected = rejections if rejections is not None else []
    # Read ONCE per assembly, not once per candidate: `own_handle` consults the
    # account profile and, when that is unset, derives the handle from the
    # archive. Cheap, but not free, and the loop below runs per draft.
    from .identity import normalize_handle, own_handle
    account_handle = own_handle()
    objective = str(brief.get("objective") or "conversation")
    if objective not in ("reach", "conversation", "resonance", "followers"):
        objective = "conversation"
    timing = brief.get("timing", {})
    first_window = (timing.get("windows") or [None])[0]
    repeats: list[str] = []
    recent_angles = [str(angle) for angle in brief.get("recent_angles_to_avoid_repeating", [])]
    experiment = brief.get("experiment") or {}
    for raw in raw_candidates[:6]:
        reply_point = ""
        # Cap BEFORE the gates so safety and quality judge the text that will
        # actually be posted, not a longer draft X would refuse.
        text = fit_to_limit(str(raw.get("text", "")).strip())
        # Cut the stock constructions BEFORE anything judges the draft. Asking
        # the model not to write them failed three times: it produced one in
        # every candidate, so every candidate was flagged, and the near-miss
        # path shipped a flagged one rather than answer with silence. Removing
        # them here means the gates below see what the reader will see.
        # Enhancement mode is exempt — those are his words, not the model's.
        is_reply = request.action == "reply"
        # THE MODEL'S WORDS ARE KEPT (API-native pass, 2026-09-22). ~15 English-regex
        # scrubbers used to rewrite every draft here: dashes removed, "shouting" lowered,
        # the text reshaped into measured blocks, cut to one point, hedges, reader
        # address, stock formulas and closers stripped. They were a small local model's
        # crutches and they rewrote the customer's voice as one author's. What is left are
        # the repairs of INTEGRITY faults and of what X itself refuses:
        #   * `fit_to_limit` (above): X's length limit;
        #   * `drop_trailing_fragment`: a sentence cut mid-way;
        #   * `strip_copied_opener`: a first sentence that is the post handed back (the
        #     rest is kept, the copy is not);
        #   * `_sanitize_reply_mentions` and `_reclaim_borrowed_possessive` below: an
        #     invented @-recipient, and the writer claiming the author's things as theirs.
        # Everything a scrubber used to fix is still DETECTED by `_draft_quality_issues`
        # and becomes a note on the card when it is not this writer's habit.
        if not getattr(request, "enhancement_mode", False):
            text = drop_trailing_fragment(text)
            if is_reply or request.action == "quote":
                try:
                    from .reply_plan import read_the_room, strip_copied_opener
                    text = strip_copied_opener(text, read_the_room(request.context))
                except Exception:                           # noqa: BLE001
                    pass
            text = tidy_whitespace(text)
        if request.action == "reply":
            text = _sanitize_reply_mentions(text, getattr(request.context, "author_handle", None)
                                            or getattr(request.context, "author", None))
            # Before the gates, so what they judge is what the reader will see —
            # and so the ownership is already right rather than merely flagged.
            #
            # CREATE ONLY. This branch is a SIBLING of the enhancement guard
            # above, not nested inside it, so without this condition the rewrite
            # reached an enhance — and on an enhance the words are HIS. Measured:
            # replying to "my agent opened 14 pull requests overnight", his own
            # draft "my agent does the exact same thing every night" shipped as
            # "your agent does the exact same thing every night". He may well own
            # an agent, and either way his first person is not this function's to
            # correct. The whole point of the gate exemptions above is that an
            # enhance preserves the user's draft; a scrubber that quietly edits it
            # breaks the same promise from the other side.
        # THE POSSESSIVE REPAIR IS NOT REPLY-ONLY ANY MORE, and the reason is a
        # measurement: on the quote baseline (36 asks x3, 2026-08-06/07) the
        # commonest defect class was appropriation — 13 of 95 drafts, four of
        # them "my agent" under Ben's joke and "runs on my CPU" under
        # ggerganov's post. On a REPLY that is one person misspeaking to one
        # person; on a QUOTE it is broadcast to the whole timeline as a claim
        # about hardware the account does not own. Same defect, worse surface,
        # and the repair that already fixes it was gated to the milder one.
        #
        # `_sanitize_reply_mentions` above stays reply-only on purpose: it is
        # about @-mentions in a threaded conversation, which a quote does not
        # have.
        #
        # HIS OWN POST IS NEVER BORROWED FROM. `own_handle()` and the parent's
        # handle are compared so a self-reply or a quote of his own post is left
        # alone — there every rewrite is false by construction, and `reply_plan`'s
        # `continue` brief is simultaneously telling the writer to continue his
        # own thread "in first person ... never address the author as 'you'".
        # Both names are required, never equality alone: empty defaults would
        # make them "match" whenever the caller knows neither and switch the
        # repair off everywhere.
        if (request.action in ("reply", "quote")
                and not getattr(request, "enhancement_mode", False)):
            parent = (getattr(request.context, "author_handle", None)
                      or getattr(request.context, "author", None))
            text = _reclaim_borrowed_possessive(
                text, _conversation_text(request),
                # STRIP THE SPACE FIRST, and in this exact order, because
                # `identity.own_handle` does — the two strings are compared and a
                # difference of one space silently un-matches them. X's
                # `User-Name` node yields "\n@benposts"; `.lstrip("@")` strips no
                # "@" from that, the later `.strip()` then exposes one, and the
                # "@" survives to the end while `own_handle` has already removed
                # it. The guard would never fire and his own first person would
                # be rewritten on his own threads again, silently.
                parent_author=normalize_handle(parent),
                own_handle=account_handle)
        if not text:
            continue
        # THE REST OF A THREAD IS BUILT AND SCRUBBED HERE, NOT 170 LINES DOWN.
        #
        # On a thread request `llm.py` sets `candidate["text"] = parts[0]` and
        # hands the whole thread over in `thread_parts`. Both of the things below
        # only ever saw `text`, i.e. the hook:
        #
        #   * the five hard integrity gates that follow — unsupported figures,
        #     invented experience, leaked instructions, unvouched URLs, injected
        #     entities. Verified with a spy on `_unsupported_claims`: exactly one
        #     call, carrying part 1. A part 2 reading "It cut inference spend by
        #     62% on a 750,000 line refactor" shipped with needs_review False and
        #     no note, and so did "I ran it on my own repo for a week",
        #     "Microsoft ships the same terminal agent next month" and a link to
        #     an unvouched host.
        #   * the whole scrub chain above, which is why a banned construction in
        #     part 2 reached the composer intact.
        #
        # `main.py` joins these parts with "\n---\n" into the answer and the
        # composer, so they are exactly as public as the hook.
        #
        # `tests/test_threads.py::test_thread_candidates_gate_every_part` passes
        # today and hid this: engagement_bait is caught by `assess_draft` INSIDE
        # the per-part loop, so the test's premise held for the one gate it used
        # while the five above stayed blind. The name is what made it look done.
        #
        # Part 1 stays exactly as it is — `text` has already been through the
        # full chain including the shaper. Parts 2..N get the post-path scrubbers
        # (a thread is always an `action == "post"`, never a reply and never an
        # enhancement). The shapers are deliberately NOT applied per part:
        # `_match_account_style` reflows into blocks, which is meaningful for one
        # standalone post and wrong for a numbered part of a thread.
        def _scrub_thread_part(part: str) -> str:
            # The same repairs as part 1 (see above): length and a cut-off sentence.
            part = fit_to_limit(part)
            part = drop_trailing_fragment(part)
            return tidy_whitespace(part)

        _raw_parts = [str(part).strip()
                      for part in (raw.get("thread_parts") or []) if str(part).strip()]
        if len(_raw_parts) >= 2:
            thread_parts = [text] + [
                cleaned for cleaned in (_scrub_thread_part(part) for part in _raw_parts[1:])
                if cleaned]
        else:
            thread_parts = [fit_to_limit(part) for part in _raw_parts]
        # What every gate from here down judges. A thread sinks as a unit.
        gate_texts = thread_parts if len(thread_parts) >= 2 else [text]
        # No draft — post, reply, OR quote — may state a specific figure or a
        # named benchmark its evidence doesn't contain. This used to run for
        # posts ONLY, so a reply could invent "leads on TerminalBench 2.1 with
        # 88.8%" freely. The corpus is everything the draft is legitimately
        # allowed to draw from: the visible post + its parent, the grounding
        # evidence carried in `direction`, and the account's own past posts.
        # Enhancement preserves the user's own draft, so it is exempt.
        if not getattr(request, "enhancement_mode", False):
            corpus_parts = [
                request.context.text or "",
                getattr(request.context, "parent_text", "") or "",
                getattr(request.context, "quote_text", "") or "",
            ]
            # A QUOTE IS JUDGED AGAINST THE TWEET ON SCREEN. Web grounding is
            # legitimate backing for a post a reader arrives at cold; it is not
            # backing for a claim ABOUT someone else's tweet, because the reader
            # sees only that tweet. Live, a quote of "Claude Code now runs 2x
            # faster" asserted "5-hour limits doubled" — true somewhere on the
            # web, absent from the post, and read by anyone looking at it as a
            # claim about what is in front of them.
            if request.action != "quote":
                # NOT the raw direction — the harness's own shape block is full
                # of numbers and was vouching for invented ones. See
                # `_direction_without_scaffolding`.
                corpus_parts.append(_direction_without_scaffolding(request))
            # HIS ARCHIVE VOUCHES FOR A POST, NOT FOR A QUOTE. On a from-scratch
            # post, a figure he has used before is fair game — he knows his own
            # beat. Replying to or quoting someone else is different: the reader
            # is looking at the original, and a number that is not in it reads as
            # a claim about THAT post. Live, a quote of "Claude Code now runs 2x
            # faster" came back asserting "5-hour limits doubled" and "the Max
            # plan's 20x usage" — neither in the post, both waved through because
            # his archive mentions those figures.
            # THE ARCHIVE VOUCHES FOR A FIGURE, NEVER FOR A NAME. Everything
            # above is about numbers, where "he has used this figure before" is
            # a fair defence. It is not a defence for dragging a LAB into a
            # post about something else, and that is where the archive bias was
            # still living. Measured over ten live posts on 2026-07-27, 2 of 10
            # named a model the evidence never mentions and were waved through
            # for no reason except that his archive mentions it too:
            #
            #   topic "Belay: A local firewall for AI coding agents"
            #     -> "What's the real difference between GPT-5.6 and Fable 5?"
            #   topic "A Conversation Between Two Coding Agents"
            #     -> "If GPT-5.6 is as good as they say, I'm switching to Claude."
            #
            # Both are about the wrong thing entirely, and both were two of the
            # three worst drafts in the run. `_injected_entities` therefore
            # judges against the EVIDENCE only; comparing labs stays fair game
            # whenever the evidence actually put them on the table, which is
            # what "on the record" was always supposed to mean.
            entity_corpus = " ".join(corpus_parts)
            if request.action not in ("reply", "quote"):
                corpus_parts.extend(examples)
            corpus = " ".join(corpus_parts)
            # EVERY PART, and the failing part is what gets recorded — a reason
            # naming the hook when the fault is in part three is a reason nobody
            # can act on.
            bad_part, invented = next(
                ((part, found) for part in gate_texts
                 if (found := _unsupported_claims(part, corpus))), (None, None))
            if invented:
                _record_safety("draft", "high", True, [f"unsupported_claim:{invented[0]}"],
                               {"text": bad_part[:200]})
                rejected.append(f"unsupported_claim:{invented[0]}")
                continue
            bad_part, fabricated = next(
                ((part, found) for part in gate_texts
                 if (found := _invented_experience(part, corpus))), (None, None))
            if fabricated:
                _record_safety("draft", "high", True,
                               [f"invented_experience:{fabricated}"], {"text": bad_part[:200]})
                rejected.append(f"invented_experience:{fabricated}")
                continue
        # WHAT THE MODEL *ADDED* IS CHECKED ON AN ENHANCE TOO.
        #
        # Everything above is exempt for a reason that holds: a figure or an
        # experience claim in an enhance is HIS, and "enhancement preserves the
        # user's own draft" is a fair defence for those. The block then grew to
        # swallow three more gates where that defence does not apply at all,
        # because an enhance still asks a model to emit new text — and it can add
        # things his draft never contained.
        #
        # Measured, same rewrite through both modes:
        #
        #   ENHANCE  stray attacker URL  -> SHIPPED, url in the posted text
        #   ENHANCE  injected lab name   -> SHIPPED, name in the posted text
        #   CREATE   stray attacker URL  -> url scrubbed out before shipping
        #   CREATE   injected lab name   -> blocked
        #
        # So the one outcome `unvouched_urls` exists to stop — "a draft that reads
        # perfectly and carries an attacker's host... posts under his name" — was
        # reachable on the surface where the scrubbers are also switched off, which
        # is the surface where nothing else would remove it either.
        #
        # `_vouched_sources` now includes `original_draft`, so a link HE typed
        # still passes; and `entity_corpus` includes it below for the same reason.
        leaking = next((part for part in gate_texts if leaks_instructions(part)), None)
        if leaking:
            _record_safety("draft", "high", True, ["instruction_leak"],
                           {"text": leaking[:200]})
            rejected.append("instruction_leak")
            continue
        from .safety import unvouched_urls as _unvouched_urls
        bad_part, stray = next(
            ((part, found) for part in gate_texts
             if (found := _unvouched_urls(part, _vouched_sources(request)))), (None, None))
        if stray:
            _record_safety("draft", "high", True,
                           [f"unvouched_link:{stray[0][:80]}"], {"text": bad_part[:200]})
            rejected.append("unvouched_link")
            continue
        if getattr(request, "enhancement_mode", False):
            # His draft vouches for its own names. Without it, every lab HE named
            # in his own words would read as an intrusion the moment this ran.
            own_entities = " ".join([
                str(getattr(request.context, "text", "") or ""),
                str(getattr(request, "original_draft", "") or ""),
                str(getattr(request, "direction", "") or ""),
            ])
            intruder = _injected_entities(text, own_entities)
            if intruder:
                _record_safety("draft", "high", True,
                               [f"injected_entity:{intruder[0]}"], {"text": text[:200]})
                rejected.append(f"injected_entity:{intruder[0]}")
                continue
            planned_own = _planned_point(str(getattr(request, "direction", "") or ""))
            # S8: when no point was decided beforehand, the draft call named his
            # point in the same object as the rewrite. It counts only when it is
            # HIS (`enhance_read.point_is_his`, the test the separate read's answer
            # had to pass); a point about something else decides nothing.
            if not planned_own:
                offered = str(raw.get("point") or "").strip()
                if offered:
                    from .enhance_read import point_is_his
                    if point_is_his(offered, str(getattr(request, "original_draft", "") or "")):
                        planned_own = offered
            if planned_own:
                from .point import serves_the_point as _serves
                if not _serves(text, planned_own):
                    # A rewrite that lost his point is not an enhance. Rejecting
                    # it here is what makes the composer keep HIS words.
                    _record_safety("draft", "medium", True, ["left_the_point"],
                                   {"text": text[:200]})
                    rejected.append("left_the_point")
                    continue
        if not getattr(request, "enhancement_mode", False):
            # names_nothing IS NOT CHECKED HERE. It used to `continue` from
            # this spot, and a hard reject at this stage is silence: the
            # candidate is gone before the near-miss path can see it. One gate
            # took a whole generation to zero that way — every candidate in the
            # API lifecycle test ("The useful test is whether the workflow
            # changes a real decision.") names nothing, so the endpoint
            # returned three drafts' worth of nothing with no reason attached.
            # A vague post is WEAK, not dishonest, so it belongs in
            # `_draft_quality_issues` with the rest of the quality bars, where
            # a better candidate still outranks it and the writer still gets
            # something with the fault named when it is all there is.
            #
            # `leaks_instructions` and `unvouched_urls` USED TO LIVE HERE and now
            # run above for both modes — an enhance can add a link or spill the
            # prompt exactly like a creation can. Only the two checks whose
            # defence is "these are his own words" stay create-only, plus the
            # entity and point checks, which run above with an enhance-specific
            # corpus instead.
            bad_part, stray = next(
                ((part, found) for part in gate_texts
                 if (found := _injected_entities(part, entity_corpus))), (None, None))
            if stray:
                _record_safety("draft", "high", True,
                               [f"injected_entity:{stray[0]}"], {"text": bad_part[:200]})
                rejected.append(f"injected_entity:{stray[0]}")
                continue
            # DID IT STAY ON ITS OWN POINT? The point was chosen from the
            # evidence before drafting; a draft that no longer carries it has
            # gone back to listing facts, which is the collage this whole
            # mechanism exists to stop.
            planned = _planned_point(str(getattr(request, "direction", "") or ""))
            if planned:
                from .point import serves_the_point
                if not serves_the_point(text, planned):
                    _record_safety("draft", "medium", True, ["left_the_point"],
                                   {"text": text[:200]})
                    continue
            # S3: A REPLY'S POINT ARRIVES IN THE SAME OBJECT AS ITS TEXT. It used
            # to cost its own model call in front of every reply. The checks that
            # call's answer had to pass now run on the returned point, and the
            # point is kept only when the text actually carries it
            # (`serves_the_point`). A point that fails either is dropped, never
            # the draft: the old flow drafted without a point whenever its point
            # failed, and the text's own reply gates (a name the conversation
            # never mentioned, engaging nothing they said, saying it back) still
            # judge the text on its own.
            if request.action == "reply":
                offered = str(raw.get("point") or "").strip()
                if offered:
                    try:
                        from .point import serves_the_point
                        from .reply_plan import point_is_usable, read_the_room
                        point_room = read_the_room(request.context, evidence=_evidence_of(request))
                        if point_is_usable(offered, point_room) and serves_the_point(text, offered):
                            reply_point = offered
                    except Exception:                        # noqa: BLE001
                        reply_point = ""
            # parrots_headline IS NOT CHECKED HERE ANY MORE. It was a hard
            # reject, and a hard reject at this point is silence — the
            # candidate is gone before the near-miss path can see it. Measured
            # live on 2026-07-27: two of ten post requests returned NO DRAFT,
            # and in both cases every candidate had been thrown away for this
            # one reason (three drafts about two coding agents talking, all
            # parroting). It fired on 4 of 16 consecutive drafts.
            #
            # Saying the headline back is weak, not dishonest — the same
            # judgement `restates_the_source` already gets, which is a
            # revisable quality issue for exactly this reason. So it moves to
            # `_draft_quality_issues`: a better candidate still outranks it,
            # the reviser gets one attempt, and if it is all that came back he
            # gets it with the fault named instead of an empty composer.
        # A user-requested thread gates EVERY part: one bad part sinks the
        # whole candidate rather than slipping through behind a clean hook.
        #
        # `thread_parts` and `gate_texts` are built far above now, before the
        # integrity gates, so those gates see every part too — and so the parts
        # are the SCRUBBED ones. Rebuilding them here from `raw` is what used to
        # happen, and it silently threw the scrubbing away: the composer posted
        # the model's untouched part 2.
        blocked = False
        # INTEGRITY BLOCKS; STYLE IS A NOTE (API-native pass, 2026-09-22). Every finding of
        # `_draft_quality_issues` used to block the draft unless nothing better came back,
        # which bought another model call and a clarity rewrite whatever the model wrote,
        # on English heuristics tuned to one author. Now only `_INTEGRITY_ISSUES` (copying
        # the post or the writer's past writing, a wrong lab, a name the conversation
        # never mentions) and the safety verdict (bait, unsafe text) reject. Every other
        # finding is kept as a note, and only when it is not this writer's own habit
        # (`calibration.split_issues`: a gate that fires on their own posts says nothing).
        # A thread sinks as a unit: every part is judged.
        seen_issues: list[str] = []
        account_notes: list[str] = []
        for gate_text in gate_texts:
            draft_safety = assess_draft(gate_text)
            quality_issues = _draft_quality_issues(gate_text, request, examples) if gate_text == gate_texts[0] else \
                [issue for issue in _draft_quality_issues(gate_text, request, examples) if issue != "missing_context_connection"]
            integrity = [issue for issue in quality_issues if issue in _INTEGRITY_ISSUES]
            style = [issue for issue in quality_issues if issue not in _INTEGRITY_ISSUES]
            flagged, _habit = calibration.split_issues(style, request.action)
            account_notes.extend(issue for issue in flagged if issue not in account_notes)
            if draft_safety.blocked or "engagement_bait" in draft_safety.categories or integrity:
                _record_safety("draft", draft_safety.severity, True,
                               [*draft_safety.reasons, *integrity], {"text": gate_text[:500]})
                blocked = True
                seen_issues.extend(integrity)
                rejected.extend(integrity or (["engagement_bait"] if "engagement_bait" in draft_safety.categories
                                              else ["unsafe"]))
                break
        if blocked:
            if "copied_voice_example" in seen_issues and text.strip():
                # A repeat must never ship as a new idea, nor become silence: held aside,
                # and if it is all there is, it goes out LABELLED as already written.
                repeats.append(text)
            continue
        draft_safety = assess_draft(text)
        post_type = "reply" if request.action == "reply" else "quote" if request.action == "quote" else "post"
        prediction, reasons = model.predict(text, len(request.context.media), post_type)
        grok_suggested = bool(raw.get("grok_suggested")) and request.grok_permission and _grok_allowed(request.context, model.grok_rate())
        rationale = [str(raw.get("rationale", "")).strip() or reasons[0], *reasons]
        # A NOTE, NOT A VERDICT: at most one, and never one this writer's own posts trip.
        rationale.extend([_WEAKNESS_NOTES[issue] for issue in account_notes
                          if issue in _WEAKNESS_NOTES][:1])
        if grok_suggested:
            rationale.append("@grok is suggested only because this draft asks for public analysis; remove it if that is not your intent.")
        novelty = _novelty_score(text, examples)
        fatigue = _fatigue_score(text, recent_angles)
        candidate_objective = raw.get("target_objective") if raw.get("target_objective") in ("reach", "conversation", "resonance", "followers") else objective
        prediction_value = prediction.get(candidate_objective if candidate_objective != "followers" else "reach")
        account_model = brief.get("account_model") or {}
        growth_score, score_terms = score_candidate(
            text,
            action=request.action,
            objective=str(candidate_objective),
            context_text=request.context.text or "",
            # THE EVIDENCE, so a correctly grounded figure stops being read as an
            # invented one. `context_text` for a created post is the topic
            # grounding line, not the captured facts — those live in `direction` —
            # so every faithful figure took a flat -10 and ranked BELOW a draft
            # that mentioned no numbers at all.
            #
            # Same helper the figure gate uses, so the two agree about what
            # evidence is: the raw direction ends with the harness's own shape
            # block, whose numbers would otherwise vouch for anything.
            grounded_text=_direction_without_scaffolding(request),
            # When Radar has already assigned the subject, pillar overlap must
            # not be able to stand in for addressing it: `_topic_fit` takes the
            # MAX of the two, so an off-brief draft about the account's usual
            # topics scored as well as an on-brief one — and selection then
            # preferred it. Credibility was already settled when the topic was
            # chosen, so counting pillars again here is double-counting.
            pillars=([] if topics.is_assigned_subject(request.context.text or "")
                     else [str(p) for p in (account_model.get("content_pillars") or [])]),
            angle_id=raw.get("angle_id"),
            voice_similarity=model.voice_similarity(text) if hasattr(model, "voice_similarity") else None,
            novelty=novelty,
            fatigue=fatigue,
            predicted_percentile=float(prediction_value) if isinstance(prediction_value, (int, float)) else None,
            deleted_texts=getattr(model, "deleted_texts", []),
            thread_hook=len(thread_parts) >= 2,
        )
        remember_draft(text)
        result.append(DraftCandidate(
            id=str(uuid4()), text=text, sources=sources, retrieved_examples=examples[:3],
            voice_score=_voice_score(text, examples), performance=prediction, rationale=list(dict.fromkeys(filter(None, rationale))),
            grok_suggested=grok_suggested, research_status="unavailable" if context_safety.local_only else research_status,
            strategic_role=str(raw.get("strategic_role") or _strategic_role(request.action, objective)),
            hook=str(raw.get("hook") or text.splitlines()[0][:120]), target_objective=candidate_objective,
            novelty_score=novelty, fatigue_risk=fatigue, recommended_window=first_window,
            experiment_id=experiment.get("experiment_id"), safety=draft_safety.model_dump(),
            growth_score=growth_score, score_band=band_for(growth_score), score_terms=score_terms,
            angle_id=raw.get("angle_id"),
            thread_parts=thread_parts if len(thread_parts) >= 2 else [],
            point=reply_point or None,
            topic=_chosen_topic(raw, request),
        ))
        if len(result) == 3:
            break
    # GrowthScore leads; the legacy rank stays as the tie-breaker for
    # candidates the scorer could not separate.
    #
    # ON REPLIES, GROWTHSCORE CANNOT SEE A THIN ONE. Measured over a 22-reply
    # run: drafts below the reader-payoff floor averaged 67.2 and drafts above
    # it averaged 68.0 —eight tenths of a point apart on a hundred-point scale.
    # The worst draft in the run, "The 529 error persists for three days
    # without support response" (a straight paraphrase of the post, payoff
    # 0.08), scored 71.1 and beat "The MoE routing's not the bottleneck"
    # (payoff 0.41) at 66.6. Three candidates were generated and the ranker
    # picked the empty one. The scorer's terms are about shape — format fit,
    # alignment, hook — and a fluent restatement has all of them.
    #
    # So for replies the floor becomes the first sort key: any candidate that
    # gives the reader something outranks every candidate that does not.
    # Within each tier the existing order is untouched, so this only ever
    # changes the pick when the candidates actually differ on the floor.
    if request.action == "reply":
        result.sort(key=lambda candidate: _reply_ranking_key(candidate, objective), reverse=True)
    else:
        # THE SAME BLINDNESS, ON THE QUOTE SURFACE. GrowthScore judges shape,
        # and a rhetorical question has excellent shape — a hook, a format fit,
        # an alignment — while asserting nothing. Measured live on 2026-07-27,
        # quoting @simonw's Claude Code pricing post, two candidates came back:
        #
        #   [0] "Why's Goose free when it's doing the same thing as Claude?"
        #   [1] "Goose is free, but it's not like you're getting a free ride."
        #
        # and the ranker chose the question. It was not the only one: six of
        # ten quotes in that run were questions. So a candidate that COMMITS to
        # something outranks every candidate that only asks, and GrowthScore
        # orders within each tier — which leaves the pick unchanged except in
        # exactly the case above, where the old key chose the empty one.
        result.sort(key=lambda candidate: (not asks_without_saying(candidate.text),
                                           (candidate.growth_score if candidate.growth_score is not None else -1.0),
                                           _candidate_rank(candidate, objective)), reverse=True)
    # (No near-miss path any more: style never blocks, so no draft is held back as one.)
    if not result and repeats:
        note = (_enhance_repeat_note(repeats[0]) if getattr(request, "enhancement_mode", False) else
                "You have written this before — Starling drafted almost exactly "
                "this text earlier and it was never posted. Here it is again so "
                "you are not left with nothing, but it is not a new idea. Ask for "
                "a different angle if you want one.")
        remember_draft(repeats[0])
        result.append(DraftCandidate(
            id=str(uuid4()), text=repeats[0], sources=[], retrieved_examples=[],
            performance={}, rationale=[note], research_status=research_status,
            safety={"reviewed": True}, needs_review=True, review_note=note))
    return result


def _chosen_topic(raw: dict, request: DraftRequest) -> int | None:
    """The story a created post says it chose (1-based), when it was offered several."""
    try:
        from .llm import topic_choices
        offered = topic_choices(request)
        value = int(raw.get("topic")) if offered and raw.get("topic") is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and 1 <= value <= offered else None


def _candidate_rank(candidate: DraftCandidate, objective: str) -> float:
    performance = candidate.performance.get(objective)
    return float(candidate.voice_score or 0.4) * 0.35 + float(performance if isinstance(performance, (float, int)) else 0.4) * 0.35 + float(candidate.novelty_score or 0.5) * 0.2 - float(candidate.fatigue_risk or 0.0) * 0.1


# S7: the faults a clarity rewrite is asked to fix. It may LEGITIMATELY fix any
# of `_CLARITY_ISSUES` below (they are about how, not what), but only these two
# are ones a rewrite fixes reliably enough to be worth a call before the next
# attempt: an unfinished sentence and a sentence with too many clauses.


# Faults a rewrite may legitimately fix, because they are about HOW the point
# is said. Everything else (unsafe, off-topic, repeats a past post, too short)
# is about WHAT it says and must reject rather than be reworded.
# Issues the revise-once pass may attempt to repair. RESTATEMENT belongs here:
# a small model echoes the source verbatim into a quote, and killing every such
# candidate outright left the user waiting 38 seconds for "nothing usable came
# back". Naming the exact fault and asking once turns a dead end into a draft —
# and the gate still runs afterwards, so a bad repair dies anyway.
_CLARITY_ISSUES = frozenset({"sentence_too_long", "dense_phrasing",
                             "tangled_clauses", "abstract_jargon", "cut_off",
                             "ai_tell", "no_reader_payoff", "restates_the_source",
                             "explainer_scaffolding", "contradicts_itself",
                             "empty_filler", "loses_the_thread",
                             # A rewrite CAN fix an opening. "So you're saying
                             # X?" says the same thing as "X" without the
                             # strawman, and that is a wording change. Cutting
                             # the both-sides tail is one too, and so is
                             # rewriting a hedged analyst line as a flat one.
                             "strawman_opener", "no_position", "analyst_voice",
                             "talks_about_the_post",
                             # A rewrite CAN turn "What's the catch with Goose
                             # if it's beating Claude?" into the take it is
                             # dancing around. The answer is already in the
                             # post being quoted — the draft just declined to
                             # commit to it. The gate runs again afterwards, so
                             # a rewrite that is still only a question dies.
                             "asks_without_saying",
                             # Same reasoning: "You're spending like a king"
                             # can be rewritten as what actually happened
                             # without inventing anything.
                             "talks_to_the_reader",
                             # And the same again for the headline echo — the
                             # facts are already in hand, only the opening
                             # sentence has to stop repeating them.
                             "parrots_headline",
                             # A universal claim can be narrowed to what the
                             # evidence actually shows without inventing.
                             "sweeping_claim",
                             # And the article narration can simply be dropped —
                             # the fact it was reporting is already in hand.
                             "describes_the_source",
                             # A rewrite genuinely can cut an explanation or an
                             # over-long bit: both faults are surplus text, and
                             # removing text invents nothing.
                             "explains_the_joke",
                             "overwrites_the_bit",
                             # "Is a big deal" rewrites into what it changes —
                             # the facts carrying the specifics are in hand.
                             "empty_big_deal",
                             # Register is pure wording: same meaning, his
                             # punctuation and vocabulary.
                             "off_register",
                             # Picking a side is a rewrite, not new evidence.
                             "no_position",
                             # The missing name is IN the topic — the reviser
                             # only has to move it into the first sentence.
                             "dangling_subject",
                             # NAMING NOTHING WAS FLAGGED BUT NEVER REPAIRED.
                             # It was moved out of the hard-reject path above
                             # (see the note there) so a vague draft ships with
                             # its fault named rather than as silence — and
                             # then nobody put it here, so it flagged and shipped
                             # with no second attempt. Both defects in the
                             # 2026-07-30 arena run were exactly that: the gate
                             # saw them, said so, and had no way to fix them.
                             # The name or the number is sitting in the
                             # evidence; a rewrite is all it takes.
                             "names_nothing",
                             # A rewrite CAN name one of their specifics — the
                             # post is right there and the guidance says which.
                             "ignores_the_post"})

# The user's PERSONAL banned register — collected from drafts he rejected as
# "so random"/"AI shit" (registry G81). Each is a construction he has never
# once used and hates on sight; the Tibo draft's "the real drama's already been
# told" is the exact family. Grows with every rejection he reports.
_AI_TELLS = (
    "let's be real", "lets be real", "it's not just", "its not just",
    "isn't just", "isnt just", "is not just",
    "not just about", "the real story", "the real drama", "already been told",
    "flash in the pan", "game changer", "game-changer", "here's the thing",
    "the truth is", "at the end of the day", "when it comes to",
    "in today's world", "let that sink in", "the future of", "a new era",
    "rewriting the script", "leading the charge", "buckle up",
    # Measured 2026-07-26 against his 34 published posts: each of these appears
    # ZERO times in his writing and in most generated drafts. They are the
    # model's stock skeleton, filled with whatever the topic happens to be —
    # which is the "technical slop, gives the reader 0% reason to read"
    # complaint in its exact form.
    "if you're building with it", "if youre building with it",
    "you're stuck with", "youre stuck with", "no real progress",
    "just more of the same", "ahead of the curve", "the next big thing",
    "getting left behind",
)

# "THAT'S NOT AN X. IT'S A Y." Three of four live drafts used it in a single
# run — "That's not a failure. It's just another reason to keep building",
# "That's not a number. It's a sign", "That's not a feature. It's a bug". Same
# machine as _NOT_JUST_RE and _ABOUT_TEMPLATE_RE: a shape that sounds like a
# point and asserts nothing. Zero occurrences in 34 published posts.
# TWO WAYS OUT OF THIS PATTERN, both taken live on 2026-07-26:
#
#   "Three users? That's not a feature, that's a leak."
#   "Half the price? You're not selling Opus 5, you're selling a discount
#    sticker."
#
# The first swapped the full stop for a COMMA and the old pattern required
# `[.!?]` between the halves. The second changed the subject to "you're" and
# the shape to a verb, which the old subject list did not contain. Both are the
# same template the rules already ban, and both went out as replies — the first
# under a post from somebody celebrating their first paid customer.
#
# Checked against all 36 posts in his archive: ZERO matches, and an ordinary
# sentence like "This is not the model I expected but the price is right"
# still passes.
_THATS_NOT_RE = re.compile(
    r"\b(?:that'?s|this is|it'?s|you'?re|we'?re|they'?re)\s+not\s+(?:just\s+)?"
    r"(?:an?|the)?\s*[a-z]+\b[^.!?\n]{0,34}[.!?,;—-]\s*"
    r"(?:it'?s|that'?s|they'?re|you'?re|we'?re)\b",
    re.IGNORECASE)

# The "not just X, it's Y" template survives any wording of its halves — match
# the construction, not the strings ("Apple's not just fast, it's got...").
# THE CONTRACTION WALKED STRAIGHT THROUGH IT. `\bnot just\b` cannot match
# "isn't just" — there is no word boundary inside "isn't" — so the single most
# banned shape in the drafting rules shipped as a reply on 2026-07-26:
#
#   "This isn't just a price drop. It's a shift in how people actually use
#    these tools now."
#
# under a post carrying two real figures. `_THATS_NOT_RE` missed it for the
# same reason: it wants "this is not", and this was "this isn't". Sentence
# boundary allowed between the halves too, since that is how it was written.
# Checked against all 36 posts in his archive: zero matches, and "not just
# anyone can do that" still passes.
_NOT_JUST_RE = re.compile(
    r"\b(?:not|isn'?t|aren'?t|ain'?t|wasn'?t|weren'?t)\s+just\b"
    r"[^.!?\n]{0,45}[.!?,;]?\s*\b(?:it'?s|its|they'?re|you'?re|we'?re|that'?s|but)\b",
    re.IGNORECASE)

# HIS VOICE USES THE BARE FORM. "That filter isn't just safety. It's
# insurance." is a real archive post — short noun payoff, no "about" — and on
# 2026-07-27 the whole-text construction scan deleted it, which the archive
# test (byte-identical, both floors) caught the same evening. Measured against
# every rewrite the scan was built for, the TEMPLATE always carries a marker
# the voice form never does: "about" somewhere in the construction ("not just
# about speed. It's about who…"), a gerund straight after "just" ("not just
# writing code…"), or "anymore". No marker, no deletion — the register gates
# may still FLAG it, but the scrubber leaves his own shape alone.
_TEMPLATE_MARKER_RE = re.compile(
    r"\babout\b|\bjust\s+\w+ing\b|\banymore\b", re.IGNORECASE)

# "ABOUT" ALONE IS NOT THE TELL — narrowed 2026-08-04, on his instruction.
#
# He published this and confirmed it as his voice when the archive test caught
# the scrubber eating it:
#
#     It's not just about who builds the smartest model.
#     It's about who people trust enough to hand their code, data, and
#     entire workflow to.
#
# The marker rule above saw "about", called it a template, and deleted BOTH
# halves — which is the whole argument, leaving a hook and a conclusion that no
# longer connect. Calibrated at 0/35, his archive is now 95 and contained one.
#
# What separates them is not the keyword but what the keyword TAKES. The
# template names an abstract noun and moves on ("not just about speed", "not
# just about the price"); his takes a CLAUSE, and the clause carries the point
# ("about who builds the smartest model"). This is the same discrimination
# `_ISNT_JUST_SENTENCE_RE` already makes one screen down, where "isn't just A
# buzzword" dies and his bare "isn't just safety" lives: the complement's shape
# is the tell.
#
# A gerund or "anymore" still convicts on its own. Those have no voice form to
# protect — the archive has none of either — so only the "about" marker can be
# redeemed.
#
# The residual is a model draft that writes "not just about who wins", which
# this now spares. That is deliberate and it is the cheap direction to be wrong
# in: the register gates still SEE the construction anywhere in a sentence and
# will FLAG it, so it ships named rather than silently rewritten. Deleting his
# own argument has no such recovery.
#
# Measured before shipping: all four documented template forms still deleted,
# his bare form still spared, and false positives across his 95 posts 1 -> 0.
_ABOUT_CLAUSE_RE = re.compile(
    r"\bjust\s+about\s+(?:who|whom|what|which|how|why|whether|where|when)\b",
    re.IGNORECASE)
_HARD_MARKER_RE = re.compile(r"\bjust\s+\w+ing\b|\banymore\b", re.IGNORECASE)


def _not_just_is_template(window: str) -> bool:
    if not _TEMPLATE_MARKER_RE.search(window):
        return False
    if _HARD_MARKER_RE.search(window):
        return True
    return not _ABOUT_CLAUSE_RE.search(window)


# ---- register: the measured vocabulary and punctuation of THIS account ----
#
# NOTHING USER-SPECIFIC IS HARDCODED HERE — his requirement, 2026-07-29: "if
# it was a different user we will adapt to his archive." The first version
# froze HIS 26 long words into a constant; a web-dev user whose posts say
# "authentication" and "deployment" daily would have been flagged for their
# own vocabulary. The whitelist and the sentence ceiling are measured from
# whatever archive exists, cached per process, and refreshed with the
# personal-model cache. An empty archive measures to an empty whitelist and
# the 30-word default ceiling — stricter, and revise-only, so a cold account
# degrades to caution rather than to someone else's voice.
_REGISTER_CACHE: dict | None = None


def _account_register() -> dict:
    """{'long_words': set, 'max_sentence_words': int} measured from the
    account's own archived posts."""
    global _REGISTER_CACHE
    if _REGISTER_CACHE is not None:
        return _REGISTER_CACHE
    long_words: set[str] = set()
    max_sentence = 30
    try:
        from .storage import connect
        with connect() as conn:
            posts = [r[0] for r in conn.execute(
                "select text from posts where text is not null") if r[0]]
        for post in posts:
            for word in re.findall(r"[a-zA-Z']+", post.lower()):
                if len(word) >= 10:
                    long_words.add(word)
            for sentence in split_sentences(post):
                max_sentence = max(max_sentence, len(sentence.split()))
    except Exception:                                    # noqa: BLE001
        pass
    _REGISTER_CACHE = {"long_words": frozenset(long_words),
                       "max_sentence_words": max_sentence}
    return _REGISTER_CACHE


def reset_register_cache() -> None:
    global _REGISTER_CACHE
    _REGISTER_CACHE = None


# A DRAFT THAT NEVER NAMES ITS OWN SUBJECT. "It's the only reason AI agents
# can even touch Python code" shipped as a Snapshield post (2026-07-29) with
# Snapshield nowhere in it — names_nothing passed because "Python" is a
# name. The check is anchored to the TOPIC: the draft opens on a bare
# pronoun AND no word of the topic's own subject appears anywhere in it.
_PRONOUN_OPEN_RE = re.compile(
    r"^\s*(?:it'?s|it\s|this\s|that'?s|they'?re)\b", re.IGNORECASE)


_DIRECTION_TOPIC_RE = re.compile(r'topic "([^"]{3,120})"')
_DIRECTION_GROUND_RE = re.compile(r"Ground it in ([^.]{3,120})")


def _topic_from_direction(direction: str) -> str:
    """The subject a direction is about, not the instruction wrapping it.

    Every consumer that wants "what is this post about" was slicing the raw
    direction and getting the fixed template instead. The topic is written in
    the direction twice, both times quoted or led — take whichever is there.
    """
    body = direction or ""
    match = _DIRECTION_TOPIC_RE.search(body) or _DIRECTION_GROUND_RE.search(body)
    return match.group(1).strip() if match else ""


_TOPIC_STOPWORDS = frozenset("""
the a an and or but for with from to of in on at by is are was were be been
now today new news launches launch launched ships shipped adds added gets
got makes made says said just still more most its it this that these those
what why how when who your you their our we they can could will would
""".split())


# NO SECOND NAMING GATE HERE. A "carries a name or a number" check was written
# on 2026-07-30 for the two arena defects and then deleted: `names_nothing`
# below already fires on both of them, and measuring it against his archive
# gave the same 2.9% (34 of 35 of his posts carry a name or a digit; the one
# that does not is a throwaway). The gate was never blind — it was
# unrepairable, having been left out of `_CLARITY_ISSUES`. Two detectors for
# one fault would have double-flagged the same sentence and fixed nothing.
def dangles_its_subject(text: str, topic: str) -> bool:
    """The draft opens on a pronoun and never names what it is about.

    COMPARED AGAINST THE TOPIC'S OWN WORDS, not against `subjects()`. The
    subject extractor collapses a topic to its ORGANISATION — "Anthropic
    launches Cowork" becomes {"anthropic"} — so a draft naming Cowork, the
    actual product the post is about, shared nothing with it and was flagged
    for never naming its subject. The question this gate asks is whether a
    stranger could tell what the post concerns, and the product name answers
    that as well as the company name does.
    """
    if not _PRONOUN_OPEN_RE.match((text or "").strip()):
        return False
    from .subject import subjects
    topic_words = {word for word in re.findall(r"[a-z0-9][a-z0-9.\-]{2,}",
                                               (topic or "").lower())
                   if word not in _TOPIC_STOPWORDS}
    topic_words |= {key for key in subjects(topic or "")}
    if not topic_words:
        return False
    body_words = set(re.findall(r"[a-z0-9][a-z0-9.\-]*", (text or "").lower()))
    return not (topic_words & body_words)


def explains_the_joke(text: str) -> bool:
    """A shitpost that lands its line and then explains it.

    SHAPE ONLY, NO VOCABULARY. There is no word list here and there must never
    be one: a list of "explainer" phrases is how `off_register` learned to eat
    "fatal flaw" and "let it rip", which are his. Sentence COUNT cannot have
    that failure mode — it does not know what the words are.

    Measured on his 31 real shitposts: 26 are exactly one sentence, and this
    fires on ONE — the "big patatos" bit, four sentences and a sustained joke.
    THAT COST IS ACCEPTED AND STATED: it is one of his best posts, and it still
    SHIPS, with the note attached. The gate is a note, never a reject.
    """
    return len(split_sentences(text or "")) > 3


def overwrites_the_bit(text: str) -> bool:
    """A shitpost that turned into an essay.

    His longest real shitpost is 30 words. Forty is chosen as a bar he has
    never crossed on this register rather than as a target to write toward —
    0 of 31 fire, so it can only ever catch something he has not done.
    """
    return len((text or "").split()) > 40


# A CLAUSE dash, in any spelling the model uses: em, en, or the ASCII
# " -- " (found untouched by the first audit, 2026-07-29). A dash glued to
# digits on both sides is a RANGE ("40–60") — data, not a mannerism — and
# stays out of both the count and the rewrite.
_CLAUSE_DASH_RE = re.compile(r"\s*(?<![0-9])[—–](?![0-9])\s*|\s+--\s+")


# SHOUTING IS NOT HIS EMPHASIS HABIT — IT IS HIS ACRONYMS.
#
# Measured over all 41 archived texts (2026-07-29): 14 contain an ALL-CAPS
# token, and every single token is an initialism or a product name — AI, API,
# GPT, EAP, IP — with exactly one ordinary word shouted in the whole archive.
# `account_model.allcaps_emphasis_rate` counts those acronyms as emphasis,
# reads 0.34, and tells the writer "you MAY put one word in ALL CAPS for
# emphasis, like they do". It does not. Live result, the draft he objected
# to: "claude opus 5 cost efficiency is HALF PRICE for FABLE performance" —
# three shouted words in eleven, and FABLE is a model NAME whose real casing
# is right there in the evidence.
#
# So the fix is not another instruction. A shouted token is restored to the
# casing the evidence uses, or lowercased when the evidence never mentions
# it; real initialisms are left alone.
# PURE LETTERS, THREE OR MORE. A token carrying a digit is a version or a
# model name, never emphasis — the first version of this lowercased "Kimi K3"
# to "Kimi k3" in seven of his own posts. Two-letter tokens are initialisms
# by construction ("AI", "IP"), so the floor is three.
_SHOUT_RE = re.compile(r"\b[A-Z]{3,}\b")
_REAL_INITIALISMS = frozenset("""
AI API GPU GPUS CPU RAM VRAM LLM LLMS CLI SDK IDE MCP RSS JSON HTTP HTTPS URL
IP EAP OCR MOE RL QA SOTA TPU ASIC NPU SSD USB PDF CSV XML YAML SQL NDA CEO
CTO GPT US UK EU UI UX API'S OS PR CI CD ML NLP RAG KYC SWE ARC AGI FSD MMLU
IMO IMHO TLDR FYI OK WTF LOL
""".split())


def normalise_shouting(text: str, evidence: str = "") -> str:
    """Restore shouted words to the casing the evidence uses."""
    body = text or ""
    if not body:
        return text
    source = evidence or ""

    def _fix(match: re.Match) -> str:
        token = match.group(0)
        if token in _REAL_INITIALISMS or len(token) <= 1:
            return token
        # The evidence is the authority on a product's real casing.
        for candidate in re.findall(rf"\b{re.escape(token)}\b", source,
                                    re.IGNORECASE):
            if candidate != token:
                return candidate
        return token.capitalize() if match.start() == 0 else token.lower()

    return _SHOUT_RE.sub(_fix, body)


def scrub_extra_dashes(text: str, *, keep_first: bool = True) -> str:
    """More than one clause dash is never his — keep the first, rewrite the
    rest into a sentence break (his own alternative per the style directive).

    `keep_first=False` is the GENERATION rule, stated by him directly on
    2026-07-30 after a drafted post carried one: "I never use the em dashes,
    so why do we have em dashes." The archive shows 3 of 43 posts with a
    single em dash, which is why the default here still tolerates one — his
    OWN text (enhancement mode) is never routed through this call at all —
    but a model draft gets none. Number ranges ("40–60") and hyphenated
    words are protected by the regex either way.
    """
    body = text or ""
    matches = _CLAUSE_DASH_RE.findall(body)
    if not matches or (keep_first and len(matches) <= 1):
        return text
    out = body
    skip = 1 if keep_first else 0
    for match in list(_CLAUSE_DASH_RE.finditer(out))[skip:][::-1]:
        tail = out[match.end():]
        tail = tail[:1].upper() + tail[1:] if tail else tail
        out = out[:match.start()] + ". " + tail
    return out


# THE FIELDS THAT CARRY THE PRODUCT'S OWN VOICE TO HIM.
#
# THIS IS AN ALLOW-LIST ON PURPOSE, and the reason is his own text. Both assist
# doors run an `enhance_composer` mode (main.py:4272, main.py:4505) whose
# payload carries HIS writing, improved — and `scrub_extra_dashes`'s contract
# is that his own text is never routed through it at all. A deny-list would
# have to stay complete forever to honour that; an allow-list cannot reach a
# field nobody named, so a new draft-bearing key is silently SAFE rather than
# silently scrubbed.
_PRODUCT_PROSE_FIELDS = frozenset({
    "headline", "rationale", "reasons", "note", "notes", "answer", "advice",
    "error", "stats_line", "downside", "alternatives", "reply_note",
    "temperature_line", "message", "summary", "reason",
})


def scrub_product_prose(value: Any, *, field: str | None = None) -> Any:
    """Strip clause dashes from the product's own prose at an exit surface.

    His instruction was "kill them everywhere", and the 114 source strings stay
    as authored — this is the boundary that cleans them, applied as the LAST
    transform before a payload leaves.

    Walks dicts and lists so the card's headline, rationale and reasons[] are
    covered by one call. `keep_first=False` because a single dash is still not
    his in the product's voice.

    NOT a dash deleter: `scrub_extra_dashes` rewrites a clause dash into a
    sentence break and capitalises what follows, and number ranges ("25-150s")
    are protected by its regex. The nine bare-dash JOINER fragments in this
    codebase degrade rather than shatter here ("5 quote-tweets. 3 this week")
    and want a source repair instead; they are listed in OUTPUT-SURFACE-MAP.md.
    """
    if isinstance(value, dict):
        return {key: scrub_product_prose(item, field=key) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_product_prose(item, field=field) for item in value]
    if isinstance(value, str) and field in _PRODUCT_PROSE_FIELDS:
        return scrub_extra_dashes(value, keep_first=False)
    return value


def tidy_whitespace(text: str) -> str:
    """The last word on spacing: what the reader sees carries no scaffolding.

    Measured 2026-07-30 on a live batch straight after a deploy: drafts
    reached the composer with doubled blank lines between blocks and
    trailing spaces at line ends — the model emits ragged newlines, the
    shaper adds its own "\\n\\n" joins, and nothing owned the final state.
    His archive has NO post with two consecutive blank lines (0 of 43), so
    collapsing runs of three-plus newlines to one blank line rewrites
    nothing of his; trailing spaces are invisible either way. Enhancement
    mode is exempt like every scrubber — his spaces are his.
    """
    lines = [line.rstrip() for line in (text or "").split("\n")]
    body = "\n".join(lines)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _off_register(text: str, evidence: str = "") -> bool:
    """One dash, a run-on sentence, or formal vocabulary this account's own
    archive never uses. All thresholds measured per account."""
    body = text or ""
    # A dash is off-register only for a writer who does not use them (V3).
    if _CLAUSE_DASH_RE.search(body) and not calibration.keeps_dashes():
        return True
    register = _account_register()
    ceiling = register["max_sentence_words"]
    for sentence in split_sentences(body):
        if len(sentence.split()) > ceiling:
            return True
    allowed = set(re.findall(r"[a-zA-Z']+", (evidence or "").lower()))
    their_words = register["long_words"]
    foreign = [w for w in re.findall(r"[a-zA-Z']+", body.lower())
               if len(w) >= 12 and w not in their_words and w not in allowed]
    return len(foreign) >= 2


# THE MODEL ANSWERING ITSELF. "ARC-AGI 3 result nobody saw coming?\n\nYeah,
# that's the one." shipped on 2026-07-27 — a question immediately answered by
# its own author is a conversation with nobody, and it reads as garbled logic.
# Calibrated the same evening: 0/35 fixture posts and 0/36 archive posts ever
# follow a question with an affirmation. Both halves die together.
_SELF_ANSWER_RE = re.compile(
    r"\?\s+(?:yeah|yep|yes|exactly|right|sure)\b[^.!?]{0,60}[.!?]",
    re.IGNORECASE)

# "X ISN'T ABOUT A. IT'S ABOUT B." The single most persistent shape in the
# generated posts, and it survived the rewrite of the drafting rules — it came
# back twice in ten drafts ("AI isn't about being smarter. It's about being
# trusted", "Model pricing isn't about cost. It's about who gets to use the
# best tools"). It reads like a thesis and contains no claim: both halves are
# abstractions, nothing is checkable, and the reader carries nothing away. He
# has never written one in 34 posts.
#
# The halves may be split by a full stop OR a comma — "It's not about who wins,
# it's about who gets used" is the same construction and slipped a version that
# only allowed sentence breaks.
# THE SECOND HALF DOES NOT HAVE TO SAY "about". This required the reveal to
# repeat the anchor — "it's not about X, it's about Y" — and the live escape
# swapped one word: "it's not about plugins making agents smarter, it's just a
# registry for users to write their own skills". Same template, same empty
# setup clause, and it shipped as an arena defect on 2026-07-30. The reveal
# word is one of a small closed set of pivots; anything else after the comma
# is a real second clause, not a formula. Checked against all 35 archive
# posts: 0 matches either way, so widening costs nothing.
_ABOUT_TEMPLATE_RE = re.compile(
    r"\b(?:is|are|it'?s|was)\s*n[o']?t\s+about\b[^.!?\n]{0,60}[.!?,]\s*"
    r"(?:it'?s|they'?re|that'?s)\s+"
    r"(?:about|just|simply|merely|really|actually|only)\b", re.IGNORECASE)

_HEDGE_WORDS = ("leverage", "authority", "ecosystem", "strategically", "utilize",
                "optimize", "synergy", "landscape", "positioning", "amplify",
                "engagement-wise", "in the niche", "thought leadership")
_CLAUSE_MARKERS = ("because", "rather than", "instead of", "which", "while",
                   "whereas", "in order to", "so that", "given that")
# A post read on a phone, once. Past ~30 words a sentence stops being scannable.
_MAX_SENTENCE_WORDS = 30
_MAX_MEAN_SENTENCE_WORDS = 24
_MAX_CLAUSE_MARKERS = 3


def _readability_issues(text: str) -> list[str]:
    """Catch drafts a tired person cannot parse in one read.

    The gates measured safety, length and topical connection, but nothing
    measured whether the sentence could be understood — so output like
    "posting this risks wasting your only slot today because it invites generic
    chat rather than building authority on AI models or pricing leaks which are
    currently trending in the niche" passed every check. One 34-word sentence,
    three subordinate clauses, two abstractions.
    """
    issues: list[str] = []
    sentences = [s.strip() for s in re.split(r"[.!?]+(?:\s|$)", text) if s.strip()]
    if not sentences:
        return issues
    counts = [len(re.findall(r"[\w'-]+", s)) for s in sentences]
    if any(count > _MAX_SENTENCE_WORDS for count in counts):
        issues.append("sentence_too_long")
    if len(counts) > 1 and (sum(counts) / len(counts)) > _MAX_MEAN_SENTENCE_WORDS:
        issues.append("dense_phrasing")
    lowered = text.lower()
    for sentence in sentences:
        marks = sum(1 for marker in _CLAUSE_MARKERS if marker in sentence.lower())
        if marks >= _MAX_CLAUSE_MARKERS:
            issues.append("tangled_clauses")
            break
    if sum(1 for word in _HEDGE_WORDS if word in lowered) >= 2:
        issues.append("abstract_jargon")
    # Cut off mid-thought: a draft that ends on a conjunction/preposition or
    # with no terminal punctuation reads as broken ("...and the wait is" / "but
    # it's not"). The user's rule: it MUST read cleanly and make sense.
    # A FULL STOP MEANS THE SENTENCE FINISHED. The old condition was
    # `not ends_clean OR last_word in _DANGLING_TAIL`, and that second clause
    # fires whatever the punctuation says — so every sentence ending on an
    # ordinary preposition was called truncated:
    #
    #   "If you're paying for speed, Opus 5 is the one to switch to."
    #   "That's a lot to take in."      "Nobody knows what it's for."
    #
    # All four test sentences of that shape were wrongly flagged, and the
    # writer was told "the last sentence looks unfinished" about a finished
    # one. Trusting terminal punctuation fixes all four AND catches MORE real
    # truncation (4 of 4, up from 3), because a trailing comma now counts —
    # "...It's about what you actually build with. If you're using it," is the
    # live case this gate was written for. His archive is unchanged at 2/35,
    # and those two really are cut off by the capture.
    stripped = text.strip()
    if stripped:
        last_word = re.sub(r"[^\w']", "", stripped.split()[-1]).lower()
        if stripped[-1] not in ".!?\"')":
            if not last_word or last_word in _DANGLING_TAIL or stripped[-1] == ",":
                issues.append("cut_off")
    # TRUNCATION IS NOT ALWAYS AT THE END. This only ever looked at the tail of
    # the whole draft, so a paragraph that stopped mid-word was invisible as
    # long as a later paragraph finished cleanly. Live:
    #
    #     "LLM-spend is a l
    #
    #      I build spend-pacing systems for ad auctions, ..."
    #
    # The first block stops on a stray letter and the second ends in a full
    # stop, so nothing objected and it reached the composer. A lone letter that
    # is not "a" or "I" cannot end an English clause; 0 of his 35 published
    # posts contain one.
    if stripped and "cut_off" not in issues:
        for block in re.split(r"\n\s*\n", stripped):
            for sentence in split_sentences(block):
                match = _STUB_END_RE.search(sentence.rstrip())
                if match and match.group(1).lower() not in _REAL_SHORT_WORDS:
                    issues.append("cut_off")
                    break
            if "cut_off" in issues:
                break
    return issues


# A ONE OR TWO LETTER STUB at the end of a sentence is the model stopping
# mid-word. One letter was not enough — measured live on 2026-07-27 the same
# fault arrived twice, once with one letter and once with two:
#
#     "LLM-spend is a l"
#     "CLI agents now work in any terminal. ... No more waiting for full CL."
#
# Two letters is also where the false positives live, because this beat ends
# sentences on real abbreviations constantly ("It's the way we're building
# AI."), so they are listed rather than guessed at. 0 of his 35 published posts
# match, and "AI", "$155" and "That's the move." all survive.
_STUB_END_RE = re.compile(r"(?:^|\s)([A-Za-z]{1,2})[.!?]?\s*$")
_REAL_SHORT_WORDS = frozenset("""
ai ml ui ux os pr ci cd db gb tb mb kb hz ok no go so it is us up on in to do
be we me my id vs pc tv 5g 4k a i x k m b
""".split())


# Words a complete post never ends on — a draft ending here was truncated
# mid-thought (token ceiling) or left a clause hanging.
_DANGLING_TAIL = frozenset({
    "and", "but", "so", "or", "the", "a", "an", "to", "of", "for", "with",
    "at", "in", "on", "is", "are", "was", "were", "be", "because", "that",
    "which", "while", "than", "as", "if", "when", "it's", "its", "their",
    # "Half the price? That's not" reached the composer as a finished reply on
    # 2026-07-26. Nothing objected: it ends on no punctuation, so the
    # ends-clean test passed, and "not" was not on this list.
    #
    # DELIBERATELY CONSERVATIVE. Checked against the last word of all 36 posts
    # in his archive, which end on "you", "do", "have", "this", "lol" and
    # "insane" among others — so the obvious-looking additions ("no", "more",
    # "even", "from", "like", "over", "about") are all left out: every one of
    # them ends a real English sentence ("the answer is no", "where are you
    # from", "it's over"). Only words that cannot are here.
    "not", "just", "very", "really", "into", "without", "within",
    "between", "toward", "towards", "upon", "whether",
})


_LEADING_MENTIONS = re.compile(r"^\s*((?:@[A-Za-z0-9_]+[,\s]*)+)")


_DASH_CONJUNCTIONS = {"but", "and", "so", "or", "yet", "nor", "because", "though",
                      "although", "while", "which", "that"}


def _capitalize_after_stops(text: str) -> str:
    """Capitalize the first letter after a sentence stop we just introduced."""
    return re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)


def _strip_emdashes(text: str) -> str:
    """Rewrite em-dashes the way this writer actually punctuates: a comma when a
    conjunction follows, otherwise a full stop and a fresh sentence. The em-dash
    (and its rarer spaced-hyphen cousin) is the single loudest AI-writing tell,
    and a 9B reaches for it constantly no matter what the prompt says."""
    def choose(m: "re.Match") -> str:
        tail = text[m.end():]
        word = re.match(r"([A-Za-z']+)", tail)
        return ", " if word and word.group(1).lower() in _DASH_CONJUNCTIONS else ". "
    out = re.sub(r"\s*[—–]\s*", choose, text)
    out = re.sub(r"\s+-\s+(?=[a-z])", choose, out)  # " - " used as a dash
    return _capitalize_after_stops(out)


def _strip_semicolons(text: str) -> str:
    """A semicolon joins two independent clauses; this writer never uses one, so
    make them two sentences instead."""
    return _capitalize_after_stops(re.sub(r"\s*;\s*", ". ", text))


def _measured_separator(style: dict) -> str:
    """The gap this writer actually puts between blocks.

    A missing measurement means an older or partial account model, so keep the
    safe readable default; only a measured low rate opts a writer into compact
    single newlines.
    """
    return "\n\n" if style.get("blank_line_rate", 0.5) >= 0.5 else "\n"


def _group_into_blocks(sentences: list[str], target_blocks: int) -> list[str]:
    """Distribute sentences into ~target_blocks groups, preserving order.

    The over-spacing bug was one sentence per block: a 5-sentence post came out
    as 5 blank-separated lines because the layout put EVERY sentence on its own
    block. The writer's measured `median_blocks` is ~3, i.e. they group roughly
    two thoughts per block — not one. This packs the sentences into that many
    groups as evenly as possible so the draft matches how they actually space.
    """
    target = max(1, min(target_blocks, len(sentences)))
    if target >= len(sentences):
        return list(sentences)
    # Even split: distribute the remainder across the first groups.
    base, extra = divmod(len(sentences), target)
    blocks, i = [], 0
    for g in range(target):
        take = base + (1 if g < extra else 0)
        blocks.append(" ".join(sentences[i:i + take]).strip())
        i += take
    return [b for b in blocks if b]


def _fit_blocks(sentences: list[str], target: int, separator: str) -> str:
    """Lay sentences out in `target` blocks, inside X's 280 characters.

    THE OLD FIT DESTROYED THE SHAPE IT HAD JUST BUILT: over the limit it ran
    `re.sub(r"\\n\\s*\\n", "\\n", result)` and deleted EVERY blank line, so a
    four-block draft one character too long shipped as one wall — and the
    shape recorded for the anti-repeat memory was then a shape the reader
    never saw. Blank lines cost one character each, so removing them saves
    almost nothing anyway; the length is in the words. Dropping trailing
    sentences is the policy this file already uses for the word bound, and it
    keeps the silhouette intact.
    """
    kept = list(sentences)
    while kept:
        blocks = max(1, min(target, len(kept)))
        candidate = separator.join(_group_into_blocks(kept, blocks))
        # `fit_to_limit`, never a raw slice: a hard cut lands mid-word and
        # silently, where this codebase's rule is that a truncation is visible
        # (the ellipsis) and falls on a word boundary. A raw [:280] here broke
        # two draft-cleaning tests that exist for exactly that reason.
        if len(candidate) <= 280 or len(kept) == 1:
            return fit_to_limit(candidate)
        kept.pop()
    return ""


def _reshape_to_style(text: str, style: dict,
                      known_block_counts: set[int] | None = None,
                      target_blocks: int | None = None) -> str:
    """Lay a draft out in this writer's real shape: the number of blocks they
    actually use, separated the way they actually separate them.

    Segmentation is NOT left to the 9B (it over-segments — one sentence per line,
    every line blank-separated). The thoughts are the sentences; the block COUNT
    and the separator are the writer's measured habits (`median_blocks`,
    `blank_line_rate`). Same words, same order — only the grouping and gaps change.

    `known_block_counts` — THE MEDIAN IS NOT THE ONLY SHAPE HE WRITES, and
    this function was enforcing it as though it were. Measured 2026-07-29
    over 663 generated drafts: 71.3% came out at exactly two blocks against
    27.5% of his archive, and his three-, four- and five-block posts (37.5%
    of everything he has published) appeared in 0.3% of drafts. Reproduced
    directly: a four-block draft went in and came out two blocks. Every
    varied draft the writer produced was being flattened here, AFTER the
    gates had passed it. When the caller passes the account's real block
    counts, a draft already wearing one of them is left exactly as written —
    only a foreign layout (the model's one-sentence-per-line over-
    segmentation, or a wall from an account that never writes walls) is
    regrouped. Callers that pass nothing keep the old behaviour, so the
    reply surface is untouched.
    """
    # LAYOUT IS ENFORCED, NOT REQUESTED — measured 2026-07-29 over 32 drafts
    # carrying a sampled shape: opener and closer instructions were obeyed
    # 96.9% of the time, block count 43.8%, and all thirteen requests for
    # three or more blocks came back as one or two. The model follows
    # rhetoric and ignores layout, so when the request named a layout it is
    # applied here with the same grouping machinery that used to impose the
    # median. Capped at the sentence count: this function never splits a
    # sentence, so four blocks out of two sentences is two blocks, and the
    # shape sampler's word target keeps the sentence supply honest.
    if target_blocks and style.get("multiline_rate", 0) >= 0.4:
        flat_target = re.sub(r"\s*\n\s*", " ", (text or "").strip())
        target_sentences = split_sentences(flat_target)
        if len(target_sentences) >= 2:
            effective = max(1, min(int(target_blocks), len(target_sentences)))
            if effective == 1:
                return fit_to_limit(flat_target)
            separator = "\n\n" if style.get("blank_line_rate", 0) >= 0.5 else "\n"
            return _fit_blocks(target_sentences, effective, separator)
    if known_block_counts:
        current = len([b for b in re.split(r"\n\s*\n", (text or "").strip())
                       if b.strip()])
        # `current >= 2` IS THE WHOLE GUARD. Without it, a draft the model
        # wrote as one paragraph has current == 1, and 1 is the account's most
        # common block count (14 of 40 posts), so the guard fired on every
        # single-paragraph draft and returned it unshaped — disabling layout
        # entirely on the enhance path, the decide path, sources.py, the
        # quality-gate tool, and the composer whenever no shape was sampled.
        # A one-paragraph draft is not "already his shape", it is the wall the
        # shaper exists to lay out. Only a draft that ALREADY has multiple
        # blocks can be wearing a multi-block silhouette worth protecting.
        if current >= 2 and current in known_block_counts \
                and len(split_sentences(text)) >= 2:
            return text
    if style.get("multiline_rate", 0) < 0.4:
        # A single-block writer (this is the reply path by design): collapse any
        # line breaks the model invented into one compact paragraph.
        collapsed = re.sub(r"\s*\n\s*", " ", text.strip())
        return collapsed if collapsed else text

    # Sentences, ignoring wherever the model happened to put newlines.
    flat = re.sub(r"\s*\n\s*", " ", text.strip())
    sentences = split_sentences(flat)
    # Keep a very short trailing fragment ("lol", "for real") attached rather
    # than orphaned.
    merged: list[str] = []
    for sentence in sentences:
        if merged and len(sentence.split()) <= 2 and not sentence.endswith(("?", "!")):
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    if len(merged) < 2:
        return text                       # single thought — nothing to lay out

    # The target count and the separator go together: a writer who leaves blank
    # lines between blocks is grouped to `median_blocks` and joined with "\n\n";
    # a writer who only uses single line breaks is grouped to `median_lines` and
    # joined with "\n". Either way it's the writer's measured count, never
    # one-per-sentence (the over-spacing bug).
    if style.get("blank_line_rate", 0) >= 0.5:
        target = int(style.get("median_blocks") or 3) or 3
        separator = "\n\n"
    else:
        target = int(style.get("median_lines") or 2) or 2
        separator = "\n"
    # Never let layout push the draft past X's limit — see `_fit_blocks` for
    # why the old flatten-every-blank-line fallback was worse than dropping a
    # trailing sentence.
    return _fit_blocks(merged, target, separator)


# Percentages, dollar figures, and multipliers — the shapes a fabricated
# statistic takes. Deliberately NOT matching bare version numbers ("Opus 4.8",
# "Fable 5", "GPT-5.6"), which are names, not claims.
# THE CENTS USED TO FALL OFF, AND THE CLAIM WAS THEN UNBACKED BY CONSTRUCTION.
# The currency branch read `\$\s?\d[\d,]*` — digits and commas, no decimal
# point — so "$0.90" matched as "$0", the comparison looked for "0" among the
# corpus figures {"12.40", "0.90", ...}, missed, and the draft was blocked as
# an invented figure. Measured live 2026-07-27 on a post reading "Serving cost
# per million tokens, 2024 to 2026: $12.40 down to $0.90": every reply quoting
# the post's own headline number was killed, and the case returned no draft at
# all in both rounds. Quoting their published figure is the most on-point thing
# a reply can do. An actually invented "$99.99" still resolves to 99.99, is
# still absent from the corpus, and is still caught.
_FIGURE_RE = re.compile(
    r"\d{1,3}(?:\.\d+)?\s?%|\$\s?\d[\d,]*(?:\.\d+)?|(?<![\d.,])\d{1,3}(?:\.\d+)?\s?[x×]\b")

# A precise decimal statistic ("88.8", "78.9") is the signature of an invented
# benchmark number. A version ("Opus 4.8", "TerminalBench 2.1") is preceded by a
# capitalised product word — that is a name, not a claim, so skip it.
_PRECISE_STAT_RE = re.compile(r"(?<![A-Za-z][ -])\b\d{1,3}\.\d\b")

# Number+unit claims ("16GB", "128k context", "40ms") and month-day dates
# ("July 30"). Bare years and model names ("K3", "70B" as a size tag inside a
# name) stay out of scope — units and dates are where fabrications hurt.
_UNIT_STAT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:GB|TB|MB|GHz|MHz|ms|fps|watts?|W)\b")
_DATE_CLAIM_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b")

# Benchmark / eval names: CamelCase-with-a-capital-tail ("TerminalBench",
# "SWE-bench") or all-caps evals ("MMLU", "GPQA", "HumanEval"), optionally with
# a version ("TerminalBench 2.1"). These get fabricated constantly ("leads on
# TerminalBench 2.1 with 88.8%") and the numbers-only check never saw them.
_BENCHMARK_RE = re.compile(
    r"\b(?:[A-Z][a-z]+(?:[A-Z][a-z]+)+|[A-Z]{3,})(?:[- ]?(?:bench|Bench|Eval|eval))?"
    r"(?:\s?\d+(?:\.\d+)?)?\b")
_BENCHMARK_HINT = re.compile(r"bench|eval|mmlu|gpqa|humaneval|swe-?bench|arc|gsm", re.I)

# The three shapes this account's fabrications actually take, none of which any
# pattern above matched. All three were generated live and shipped clean:
#
#   "Claude just dropped a 50K context window update"   invented capacity
#   "launching just 6 weeks after GPT-4.5"              invented interval
#   "Opus 5 scores 88.8 on the coding benchmark"        invented score
#
# A MODEL VERSION IS NOT A STATISTIC. "Opus 4.5", "GPT-5.6" and "Grok 4.5" are
# names, they appear in nearly every post, and they are almost never in the
# evidence corpus — flagging them would block everything he writes. So each
# pattern below excludes a number that follows a product name.
_MODEL_NAME = (r"(?:opus|sonnet|haiku|claude|fable|gpt|chatgpt|grok|gemini|"
               r"llama|mistral|kimi|qwen|deepseek|codex|o[1-9])")

# A capacity claim: a number with a K/M scale sitting next to context/token talk.
_CAPACITY_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?[KkMm]\b(?=[^.!?\n]{0,28}\b(?:context|tokens?|window)\b)"
    r"|\b(?:context|window)\b[^.!?\n]{0,16}?\b\d+(?:\.\d+)?\s?[KkMm]\b")

# A NUMBER FUSED TO A UNIT: "5-hour limits", "20x usage", "128k window",
# "3-day trial". The gate caught percentages, dollar figures and bare stats,
# but a hyphenated compound is one token to every one of those patterns — so a
# quote of "Claude Code now runs 2x faster" came back asserting "Claude Code's
# 5-hour limits doubled", with nothing in the post about five hours.
_UNIT_COMPOUND_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s?-?\s?"
    r"(?:hour|hours|hr|day|days|week|weeks|month|months|year|years|"
    r"minute|minutes|min|second|seconds|sec|gb|tb|mb|k|x)\b",
    re.IGNORECASE)

# An interval claim: "6 weeks after", "three days early", "two months late".
_INTERVAL_RE = re.compile(
    r"\b\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b")

# A bare precise score: "88.8 on the coding benchmark". The optional leading
# model name is CAPTURED rather than excluded by look-behind, which cannot take
# an alternation of differing widths; a match carrying a name is a version.
# A model family followed by a version. Used to CHECK the version against the
# evidence, never to reject versions as such — "Opus 5" is a name and must
# pass when it is real.
_MODEL_VERSION_RE = re.compile(
    r"\b(?P<family>opus|sonnet|haiku|claude|fable|gpt|chatgpt|grok|gemini|"
    r"llama|mistral|kimi|qwen|deepseek|codex)[\s\-]?(?P<ver>\d+(?:\.\d+)?)\b",
    re.IGNORECASE)


_BARE_STAT_RE = re.compile(rf"(?:(?P<name>{_MODEL_NAME})[\s-]*)?"
                           rf"\b(?P<num>\d{{1,3}}\.\d)\b", re.I)


def _expand_suffix(number: str, suffix: str) -> str:
    """"1.4" + "k" -> "1400"; integral results drop the decimal point."""
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix.lower()]
    value = float(number.replace(",", "")) * multiplier
    return str(int(value)) if value == int(value) else str(value)


def _starts_mid_number(text: str, start: int) -> bool:
    """True when a figure match begins inside a larger comma-grouped number.

    The multiples pattern reads "2,053x" and matches "053x" — a figure that
    does not exist. The old fragment-keeping corpus hid this by ALSO storing
    "053", so the phantom matched its own shadow; with whole-number matching
    the phantom flags his own post against itself. Skip any match whose first
    digit is preceded by a digit, or by a comma that follows a digit.
    """
    if start <= 0:
        return False
    before = text[start - 1]
    if before.isdigit():
        return True
    return before == "," and start >= 2 and text[start - 2].isdigit()


def _figure_supported(span: str, corpus_nums: set[str]) -> bool:
    """Is the first figure in this span present in the corpus, in ANY spelling?

    A draft figure earns support only as a WHOLE number: "$1k" is checked as
    1000 (and as "1" never — that is the fragment-vouching leak), "750k" as
    750000, "$1,400" as 1400. The corpus set carries the same canonical forms,
    so "750k" matches "750,000" and vice versa while "$1k" cannot match
    "$1,400".
    """
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([kKmMbB]\b)?", span or "")
    if not match:
        return True                     # no figure, nothing to support
    forms = {match.group(1).replace(",", "")}
    if match.group(2):
        forms.add(_expand_suffix(match.group(1), match.group(2)))
    return any(form in corpus_nums for form in forms)


def _unsupported_claims(text: str, corpus: str) -> list[str]:
    """Specific, checkable claims the model invented — present in the draft but
    nowhere in the evidence. Two shapes, both classic hallucinations:

    1. Figures: "bolsters security by 40%", "88.8%", "$20", "2x".
    2. Named benchmarks/evals: "TerminalBench 2.1", "SWE-bench", "MMLU" — with or
       without a precise stat. The user watched a post invent "GPT-5.6 Sol leads
       on TerminalBench 2.1 with 88.8%" out of thin air; the numbers-only check
       never looked at the benchmark name.
    """
    corpus_l = (corpus or "").lower()
    # THOUSANDS SEPARATORS BROKE THIS BOTH WAYS. The corpus was scanned with
    # `\d+(?:\.\d+)?`, which reads "$1,400" as the two numbers 1 and 400, while
    # the draft side strips every non-digit and produces "1400" — so a figure
    # quoted VERBATIM from the source could never match, and
    # `unsupported_claim` is a hard reject.
    #
    # Measured live on 2026-07-27, quoting @levelsio's "I audited my OpenAI and
    # Anthropic spend ... $1,400 last month": BOTH candidates were thrown away
    # for `unsupported_claim:$1,400`, and the request returned no draft at all.
    # The one number in the post was the one number the gate refused.
    #
    # FULL TOKENS ONLY, BOTH SIDES CANONICAL. The first repair of this set kept
    # the fragment forms too ("$1,400" contributing "1" and "400" as separate
    # numbers), and fragments VOUCH for figures they never stated: the live
    # quote "You spent $1k on AI" passed because fragment "1" matched, when the
    # source said $1,400 — a 29% misquote shipped as supported. "The bill was
    # 400 dollars" passed the same way. So the corpus keeps whole numbers
    # (commas stripped) plus k/m/b expansions, and each draft figure is checked
    # in its own canonical forms — "$1k" is 1000, "750k" is 750000, "$1.4k" is
    # 1400 — via `_figure_supported`. Equivalent spellings still match in both
    # directions; fragments no longer match anything.
    corpus_nums = {m.replace(",", "")
                   for m in re.findall(r"\d[\d,]*(?:\.\d+)?", corpus or "")}
    for whole, suffix in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*([kKmMbB])\b",
                                    corpus or ""):
        corpus_nums.add(_expand_suffix(whole, suffix))
    out: list[str] = []

    for match in _FIGURE_RE.finditer(text):
        if _starts_mid_number(text, match.start()):
            continue
        # One character of lookahead: the figure pattern's own span stops at
        # the digits, so "$1.4k" arrives as "$1.4" and the suffix that makes
        # it 1400 is invisible without it.
        if not _figure_supported(text[match.start():match.end() + 1], corpus_nums):
            out.append(match.group(0))

    # Number-fused-to-unit compounds: "5-hour limits", "128k window", "3-day".
    for match in _UNIT_COMPOUND_RE.finditer(text):
        if _starts_mid_number(text, match.start()):
            continue
        if not _figure_supported(match.group(0), corpus_nums):
            out.append(match.group(0).strip())

    # Precise one-decimal stats not in the evidence — invented benchmark numbers.
    for match in _PRECISE_STAT_RE.finditer(text):
        if _starts_mid_number(text, match.start()):
            continue
        if not _figure_supported(match.group(0), corpus_nums):
            out.append(match.group(0))

    # Number+unit claims — the "16GB is the minimum for 2026" class that slipped
    # the older shapes (seen live, registry F67). A unit makes it checkable.
    for match in _UNIT_STAT_RE.finditer(text):
        if _starts_mid_number(text, match.start()):
            continue
        if not _figure_supported(match.group(0), corpus_nums):
            out.append(match.group(0))

    # Capacity and interval claims — two of the three shapes seen live that
    # every pattern above walked past.
    for pattern in (_CAPACITY_RE, _INTERVAL_RE):
        for match in pattern.finditer(text):
            if _starts_mid_number(text, match.start()):
                continue
            if not _figure_supported(match.group(0), corpus_nums):
                out.append(match.group(0).strip())

    # The third: a bare precise score. A match that carried a model name is a
    # version ("Opus 4.5"), which is a name and not a measurement.
    for match in _BARE_STAT_RE.finditer(text):
        if match.group("name"):
            continue
        if not _figure_supported(match.group("num"), corpus_nums):
            out.append(match.group("num"))

    # A MODEL VERSION THAT DOES NOT EXIST. Versions were exempted above because
    # they are names, not statistics — but a name can still be invented, and
    # removing the entity names from the style examples made this worse rather
    # than better: with nothing to copy, the model started making versions up
    # ("Grok 2.0", "Grok-1", "Grok-2.5" in one run, when the real one is 4.5).
    # A wrong version number is the same class of error as naming the wrong
    # lab: instantly visible to the only readers who matter.
    # With no ground truth at all (a fresh install, empty archive, radar not
    # yet run) there is nothing to check against, and flagging every version
    # would block true statements on day one. Degrade to silent.
    try:
        attested = known_model_versions()
    except Exception:
        attested = set()
    # The reference frame must actually exist: either something real attests to
    # versions, or the evidence itself talks about versions (in which case a
    # draft naming a DIFFERENT one is checkable against it). Neither → skip.
    if attested or _MODEL_VERSION_RE.search(corpus_l):
        for match in _MODEL_VERSION_RE.finditer(text):
            family, number = match.group("family").lower(), match.group("ver")
            if number in corpus_nums:
                continue
            # Present in the evidence under any spacing/punctuation?
            if re.search(rf"\b{re.escape(family)}[\s\-]?{re.escape(number)}\b",
                         corpus_l, re.IGNORECASE):
                continue
            # Attested anywhere real: his archive, the radar, the release feed.
            if family + " " + number in attested:
                continue
            # Judge a family only where there IS a reference for it. Two ways:
            #
            #   the evidence names the SAME family at a DIFFERENT number, which
            #   is the exact failure seen live (handed Grok 4.5, wrote Grok 2.0);
            #   or something real attests to other versions of this family and
            #   this one is not among them.
            #
            # Otherwise stay quiet. Comparing models across labs from knowledge
            # is this account's whole beat — "Sonnet 5 matches Opus 4.8" must
            # not need a citation for Opus just because the evidence was about
            # Sonnet.
            contradicts = bool(re.search(rf"\b{re.escape(family)}[\s\-]?\d",
                                         corpus_l, re.IGNORECASE))
            family_known = any(pair.startswith(family + " ") for pair in attested)
            if contradicts or family_known:
                out.append(match.group(0).strip())

    # Month-day date claims ("July 30") — a specific date the evidence never
    # stated is a fabricated schedule, the single most embarrassing shape for
    # an account that posts release predictions.
    for match in _DATE_CLAIM_RE.finditer(text):
        month = match.group(0)[:3].lower()
        day_numbers = re.findall(r"\d{1,2}", match.group(0))
        if not (month in corpus_l and day_numbers and day_numbers[0] in corpus_nums):
            out.append(match.group(0))

    # Named benchmarks the evidence never mentions.
    for match in _BENCHMARK_RE.finditer(text):
        name = match.group(0).strip()
        if not _BENCHMARK_HINT.search(name):
            continue
        # The bare name (without the trailing version) must appear in evidence.
        base = re.sub(r"\s?\d+(?:\.\d+)?$", "", name).lower()
        if base and base not in corpus_l:
            out.append(name)

    # De-dup, preserve order.
    seen, unique = set(), []
    for item in out:
        if item.lower() not in seen:
            seen.add(item.lower())
            unique.append(item)
    return unique


# Back-compat alias — several call sites and tests use the old name.
_unsupported_figures = _unsupported_claims


def _match_account_style(text: str, brief: dict, action: str | None = None,
                         direction: str = "") -> str:
    """Deterministic style clean-up the prompt can't reliably enforce on a 9B.

    Strips the habits a small model gets wrong most, each gated on the account's
    OWN measured rate: hashtags and emoji it appends unbidden, and the two
    loudest AI tells — em-dashes and semicolons. Length/shape/voice stay with the
    prompt; these are the ones a 9B will not stop doing no matter how it's asked.
    """
    if not text:
        return text
    # PLACEHOLDER HANDLES ARE NEVER REAL, profile or no profile. This used to
    # sit below the style lookup, which returns early when the account has no
    # measured style - so "@mention Opus 5 is out..." sailed through enhance.
    # Longest alternatives first, and a word boundary, or "@username" matches
    # the "user" branch and leaves the word "name" stranded in the draft.
    text = re.sub(r"(?:^|(?<=\s))@(?:username|mention|handle|user|name)\b[,:]?\s*",
                  "", text, flags=re.IGNORECASE).strip()
    from .account_model import style_for_action
    style = style_for_action((brief or {}).get("account_model"), action)
    if not style:
        return text
    if style.get("hashtag_rate", 1.0) < 0.15:
        # Remove hashtag tokens; collapse the whitespace they leave behind.
        text = re.sub(r"\s*#[A-Za-z0-9_]+", "", text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if style.get("emoji_rate", 1.0) < 0.06:
        from .account_model import _EMOJI_RE
        text = _EMOJI_RE.sub("", text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if style.get("emdash_rate", 1.0) < 0.12 and ("—" in text or "–" in text or " - " in text):
        text = _strip_emdashes(text)
    if style.get("semicolon_rate", 1.0) < 0.05 and ";" in text:
        text = _strip_semicolons(text)
    # PLACEHOLDER HANDLES ARE NEVER REAL. The model copies instruction tokens
    # into drafts — "@mention Local models save money…" shipped a literal
    # placeholder as the first word of a post. No such account exists, and a
    # standalone post should carry no mention at all.
    text = re.sub(r"(?:^|(?<=\s))@(?:mention|handle|user|username|name)\b[,:]?\s*",
                  "", text, flags=re.IGNORECASE).strip()

    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    # LENGTH IS ENFORCED, not requested (registry G77). The Tibo reply came out
    # 27 essay words against a ~18-word reply style because the bound lived only
    # in the prompt. Over-length drafts drop trailing sentences — the lead
    # carries the point in this account's writing — down to the surface's p75.
    # Never cuts mid-sentence, never below the first sentence.
    bound = int(style.get("words_p75") or 0)
    # THE BOUND HAS TO KNOW THE SHAPE. words_p75 is a GLOBAL percentile, and a
    # sampled four-block silhouette is measured at 47 words against a global
    # p75 of 46 — so the amputation below ran first and stripped the very
    # sentences the layout needed, capping every long shape before it existed.
    # When the request named a layout, the bound becomes that shape's own
    # measured length (plus the same slack), so short shapes stay short and
    # long shapes are allowed to be long.
    if action == "post" and direction:
        try:
            from .shape import account_words_by_blocks, asked_blocks
            asked = asked_blocks(direction)
            shape_words = account_words_by_blocks().get(asked) if asked else None
            if shape_words:
                bound = max(bound, int(shape_words))
        except Exception:                                # noqa: BLE001
            pass
    if bound and len(re.findall(r"\S+", text)) > bound + 4:
        sentences = split_sentences(text.strip())
        while len(sentences) > 1 and len(re.findall(r"\S+", " ".join(sentences))) > bound:
            sentences.pop()
        text = " ".join(sentences).strip()
    # Lay a one-block draft out in the writer's real shape (one thought per line,
    # blank line between) — done AFTER the punctuation fixes so em-dash splits
    # become their own lines. This is the formatting the user asked for.
    #
    # POSTS KEEP A SHAPE THE ACCOUNT ACTUALLY USES. Passing the measured block
    # counts stops this call flattening his three-, four- and five-block posts
    # to the median two (see `_reshape_to_style`). Posts only: the reply
    # surface is a single block by measurement and its behaviour is unchanged.
    known: set[int] | None = None
    target: int | None = None
    if action == "post":
        try:
            from .shape import account_block_counts, asked_blocks
            known = account_block_counts()
            # The layout this request asked for, read out of the direction
            # itself so every composing path gets enforcement without new
            # plumbing. None when no shape was sampled (cold archive).
            target = asked_blocks(direction)
        except Exception:                                # noqa: BLE001
            known, target = None, None
    text = _reshape_to_style(text, style, known_block_counts=known,
                             target_blocks=target)
    return text


# A first-person possession: "my agent", "my repo", "my bill".
_BORROWED_POSSESSIVE_RE = re.compile(r"\b(my)\s+([a-z][a-z'-]{2,20})\b", re.I)


def _reclaim_borrowed_possessive(text: str, parent_text: str, *,
                                 parent_author: str = "",
                                 own_handle: str = "") -> str:
    """A reply that echoes the parent's "my X" is claiming the author's thing.

    Under "my agent opened 14 pull requests overnight", the writer replied "so now
    my agent renames 14 variables overnight" — a true sentence about Ben, and a
    false one about the account posting it. Both live arena drafts for that case
    did it, and nothing caught either: `_BORROWED_LIFE_RE` is a closed noun list
    (project, repo, startup, company, bill…) and "agent" is not in it, which in a
    product about agents is the one noun certain to recur.

    ADDING THE NOUN TO THAT GATE WAS THE WRONG FIX. `_invented_experience`
    rejects the candidate outright, and a rejected draft never enters `best`, so
    `_retry_note` cannot mention it — the writer would be retried blind, produce
    the same phrase, and four attempts later the reply would be silence. That
    trades a fixable sentence for nothing, which is the worse outcome.

    So this repairs instead. "my" becomes "your", which on a REPLY is not a
    compromise: the scrub chain already notes that a reply is a person answering a
    person, "where 'you' is the person they are talking to and entirely correct".
    The joke, the point and the register all survive; only the ownership moves
    back to whoever claimed it.

    Deliberately narrow. It fires only on a noun the PARENT itself claims in the
    first person, so it cannot touch a possession the account really has — his
    seven "my <noun>" posts are all predictions ("My Claude Opus 5 prediction"),
    which are his voice, and they are only reachable if a parent says "my claude".
    Measured on his archive: 0 of his replies carrying a parent are rewritten.
    Re-measured 2026-08-06 for the quote surface: 0 false rewrites across 735
    pairs (his 35 posts x 21 real captured parents), and only 2 of those 21
    parents arm the rule at all — so the observed zero is a real result rather
    than an unarmed one. The residual is named and inherited, not new: against a
    parent that claims the SAME noun he claims, all three of his possessive posts
    do rewrite ("My Claude Opus 5 prediction" -> "Your ..."). That collision
    shipped with the reply version under the same standard.

    HIS OWN POST IS NEVER BORROWED FROM. When the parent is the account's own —
    a self-reply, or a quote of his own post — every rewrite here is false by
    construction: he is the one who claimed the thing. Worse, `reply_plan`'s
    `continue` brief tells the writer to continue his own thread "in first
    person ... never address the author as 'you'", so on exactly those threads
    the brief asks for the first person this function was turning into "your".
    Two correct components, one wrong output.

    THE GUARD REQUIRES BOTH NAMES, NOT JUST EQUALITY. An empty-string default on
    each would make `parent_author == own_handle` true whenever the caller knows
    neither, silently disabling the repair everywhere — the same shape as the
    trailing default that once made a model pin never-falsy and switched off two
    subsystems. So it engages only when both are actually known.
    """
    if not text or not parent_text:
        return text
    if parent_author and own_handle and parent_author == own_handle:
        return text
    theirs = {match.group(2).lower()
              for match in _BORROWED_POSSESSIVE_RE.finditer(parent_text)}
    if not theirs:
        return text

    def swap(match: re.Match[str]) -> str:
        if match.group(2).lower() not in theirs:
            return match.group(0)
        return ("Your" if match.group(1)[0].isupper() else "your") + " " + match.group(2)

    return _BORROWED_POSSESSIVE_RE.sub(swap, text)


def _sanitize_reply_mentions(text: str, author_handle: str | None) -> str:
    """A reply may only address the post's AUTHOR, never an invented handle.

    Replying to @claudeai's post, the model sometimes opened with "@elonmusk,"
    or "@a_sample_reader" — fabricating that it was talking to a specific other
    person. That is both the wrong recipient and an invented interaction. On X
    the leading @mention run IS the addressing line, so any leading handle that
    is not the author is dropped; X prepends the real recipient itself, so a
    reply needs no leading mention at all.
    """
    if not text:
        return text
    match = _LEADING_MENTIONS.match(text)
    if not match:
        return text
    author = (author_handle or "").lstrip("@").lower()
    handles = re.findall(r"@([A-Za-z0-9_]+)", match.group(1))
    if author and all(handle.lower() == author for handle in handles):
        return text                       # already correct: addresses the author
    rest = text[match.end():].lstrip()
    prefix = f"@{author} " if author and any(h.lower() == author for h in handles) else ""
    return (prefix + rest).strip() or text


def _shows_context_connection(lowered: str, context_text: str) -> bool:
    """Is this reply engaged with the post, rather than about something else?

    Originally this demanded a literal 5+ letter word from the source's first
    twelve — which measures echo, not engagement. Answering "what positive
    impact has an LLM had on your life?" with "the code snippets that work on
    first try" shares no vocabulary and is a perfect reply, while "great
    positive impact!" passes on overlap alone. Every candidate for a real reply
    was rejected this way, and the user got a refusal instead of a draft.

    Connection is now satisfied by shared vocabulary OR by the reply carrying
    something concrete of its own — a specific is what makes a reply belong in
    a thread. Only a draft with neither is treated as unconnected.
    """
    source = context_text.lower()
    # Shared vocabulary, on stems so inflections still count.
    stems = {word[:5] for word in re.findall(r"[a-z]{4,}", source)} - _CONNECTION_STOP
    if any(stem in lowered for stem in stems):
        return True
    # Or a concrete particular of its own: a number, a quoted phrase, a named
    # thing, or a first-hand claim. Generic filler has none of these.
    if re.search(r"\d", lowered):
        return True
    if re.search(r"[\"'“‘][^\"'”’]{3,}[\"'”’]", lowered):
        return True
    if re.search(r"\b(i|my|we|our)\b", lowered) and len(re.findall(r"[a-z']+", lowered)) >= 10:
        return True
    return False


_CONNECTION_STOP = {"what", "that", "this", "with", "your", "have", "here",
                    "been", "from", "they", "them", "were", "will", "just",
                    "abou", "thin", "some", "more", "most", "very", "real"}


# A contiguous run this long, shared with something he already posted, is a
# quotation of himself rather than a coincidence of topic. Six words is short
# enough to catch a lifted opening and long enough that shared phrasing about
# the same subject ("half the price of Opus") does not trip it.
_COPY_RUN_WORDS = 6
# Overall vocabulary overlap. Measured on the real case: the copied draft
# scored 0.60 against its source post, while the next-closest archive post
# scored 0.19 — so the boundary is wide and 0.45 sits safely between them.
_COPY_OVERLAP = 0.45
# Borrowed six-word phrases needed before a draft counts as a rewrite of past
# writing. Measured on this account: real rewrites share 3-5, genuinely new
# writing about the same launch shares 0-1.
_COPY_MIN_RUNS = 2


_OWN_WRITING_CACHE: tuple[float, list[str]] | None = None
# Short on purpose: a draft is recorded the moment it is generated, so two
# clicks a minute apart must see each other. The query is a union over a
# few hundred short rows — re-running it is cheaper than a repeat.
_OWN_WRITING_TTL_S = 15.0


_KNOWN_VERSIONS_TTL_S = 300.0
_KNOWN_VERSIONS_CACHE: tuple[float, set[str]] | None = None


def known_model_versions() -> set[str]:
    """Versions that exist somewhere real, as "family number" pairs.

    Deliberately NOT a hardcoded table. Versions move every few weeks; a list
    baked into the source is wrong within a month and then confidently blocks
    true statements, which is worse than the problem it solves. So the truth
    comes from places that update themselves:

      - what the account has actually PUBLISHED (he does not post about models
        that do not exist),
      - what the radar has seen trending on X,
      - what the release radar found on the web.

    A version outside all three, and outside the evidence handed to the model,
    was invented between the prompt and the draft.
    """
    global _KNOWN_VERSIONS_CACHE
    now = time.monotonic()
    if _KNOWN_VERSIONS_CACHE and now - _KNOWN_VERSIONS_CACHE[0] < _KNOWN_VERSIONS_TTL_S:
        return _KNOWN_VERSIONS_CACHE[1]
    sources: list[str] = []
    try:
        sources.extend(str(row["text"]) for row in performance_records() if row.get("text"))
    except Exception:
        pass
    try:
        from .radar import recent_post_topics
        sources.extend(recent_post_topics())
    except Exception:
        pass
    try:
        from .storage import connect
        with connect() as db:
            rows = db.execute("SELECT topic, summary FROM radar_items "
                              "ORDER BY id DESC LIMIT 400").fetchall()
        for row in rows:
            sources.extend(str(row[key] or "") for key in ("topic", "summary"))
    except Exception:
        pass
    found: set[str] = set()
    for text in sources:
        for match in _MODEL_VERSION_RE.finditer(text or ""):
            found.add(match.group("family").lower() + " " + match.group("ver"))
    _KNOWN_VERSIONS_CACHE = (now, found)
    return found


# A first-person claim to have OBSERVED something specific. The model has no
# hands: it has never opened Slack, watched a booking fail, or run a benchmark.
# Live, a draft about a Salesforce agent read "I saw it book one with no
# attendees" — invented whole, and invisible to the claim gate because it
# contains no number to check.
#
# This is partly self-inflicted: reader_value REWARDS firsthand signal, so with
# nothing real to report the model manufactures some. An incentive to sound
# experienced is an incentive to lie unless the lie is blocked here.
_FIRSTHAND_EVENT_RE = re.compile(
    # Contractions matter: "the best model I've used" shipped live because this
    # expected "I used". Same claim, one apostrophe, invisible to the gate.
    r"\b(?:I|we)(?:'ve|'d|\s+have|\s+had)?\s+(?:just\s+|already\s+|ever\s+|been\s+)?"
    # "I asked Claude to build a parity harness" shipped live. Every verb here
    # is something only a person with hands can have done; the model has asked
    # nothing of anything.
    r"(?:saw|seen|watched|tried|tested|ran|run|used|using|installed|switched to|"
    r"benchmarked|measured|timed|hit|caught|asked|built|wrote|checked|compared|"
    r"deployed|shipped|spent|paid|bought|clicked|opened)\b", re.IGNORECASE)


# A CLAIM TO HAVE USED SOMETHING DOES NOT NEED THE WORD "I".
#
# The pattern above wants a first-person verb, and these three shipped without
# one (live, reply path, 2026-07-26):
#
#   "Opus 5's full-stack app builds still feel like a beta."   released that
#                                                              morning
#   "I'm on a CPU and it's already faster."                    no such machine
#   "Fable's $200 plan is the only one that feels fair."       no such plan
#
# The first and third assert a lived judgement — you cannot know a thing feels
# like a beta without having used it — and the second is a state rather than a
# past-tense verb, so "I'm on" slipped between "I used" and "I'm using".
#
# Both were checked against all 36 posts in his archive before being added:
# ZERO of his own posts match either. That is the bar for touching this gate,
# because a rule that also rejects his real writing costs more than it saves.
_FIRSTHAND_STATE_RE = re.compile(
    r"\b(?:i'?m|we'?re|i am|we are)\s+(?:on|running|using|testing)\b", re.I)
# A MACHINE THE ACCOUNT DOES NOT HAVE. Asked "what rig are you running it on?"
# the reply path answered "Running on a 2016 laptop" and "Running on a custom
# rig" — a subjectless claim about his hardware, so neither the first-person
# verb pattern nor the state pattern could see it. It cannot know his setup.
#
# "Running the 30B locally on one card is the whole trick" is deliberately NOT
# matched: that is a claim about the thing, not about his box. Zero of his 35
# archive texts match.
_OWN_RIG_RE = re.compile(
    r"^\s*[\"“]?\s*(?:running|runs|running it)\s+on\s+(?:a|an|my|the)\b"
    r"|\bmy (?:rig|setup|laptop|machine|box|card|gpu|build|server)\b", re.I)
_LIVED_JUDGEMENT_RE = re.compile(
    r"\b(?:still\s+)?(?:feels?|felt|seems?)\s+(?:like\s+)?(?:a\s+|an\s+)?"
    r"(?:beta|clunky|slow|slower|fast|faster|fair|rough|solid|janky|"
    r"unfinished|broken)\b"
    r"|\b(?:in practice|day[- ]to[- ]day|in my experience|"
    r"after a (?:week|month|day|few days) (?:of|with|on))\b", re.I)

# THE AUTHOR'S LIFE, BORROWED. When the topic is someone's blog post or Show HN
# writeup, the draft adopts their first person as his. Live on 2026-07-27:
#
#   "Why My Open-Source Project Hasn't Done Better"
#       -> "My open-source project has 400 stars, but it's not the LLMs that
#           are holding it back."
#   "C++ Vs Rust: Which is better for writing AI/ML code with LLMs"
#       -> "Rust's 50% lower token use is impressive, but I still can't get
#           past the type system tripping me up."
#
# These are worse than a weak post. They are false statements about his own
# work and habits, in his voice, and they are grammatical, on-topic and
# perfectly fluent — so nothing downstream can catch them. The topic filter
# (`event_subject`) now drops the essays that invite this, and this is the
# second line: the shape itself is refused wherever the topic came from.
#
# THE BAR THIS GATE IS HELD TO, and the reason the patterns are narrow: zero of
# his 35 published posts may match. Measured — 0/35, while catching all six
# live failures and blocking none of seven real first-person posts of his
# ("I'd take Fable 5 over every GPT model", "My prediction: July 30"). Opinion
# and prediction in the first person are his voice; a claim to OWN or to have
# LIVED WITH the subject is not.
_BORROWED_LIFE_RE = re.compile(
    # A possession the account cannot be assumed to have.
    r"\bmy\s+(?:open-?source\s+)?(?:project|repo|repository|codebase|"
    r"code\s?base|startup|company|team|cluster|server|bill|invoice|"
    r"subscription|side[-\s]?project|saas|product|app)\b"
    # AN OCCUPATION LIFTED FROM THE SUBMISSION AUTHOR. A Show HN body opens
    # "I build spend-pacing systems for ad auctions, and my side-project agent
    # workloads still surprised me with API bills I couldn't explain" — and
    # that arrived, verbatim in the first person, as his post. 0 of his 35
    # published posts match this shape.
    r"|\bi\s+(?:build|built|run|maintain|ship|work\s+on|consult)\s+"
    r"(?:\w+[-\s]?){0,3}(?:systems?|tools?|apps?|products?|agents?|"
    r"pipelines?|infrastructure|software)\b"
    # An ongoing personal struggle with the thing.
    r"|\bi\s+(?:still\s+)?(?:can'?t|cannot)\s+(?:get\s+past|get\s+over|"
    r"stand|deal\s+with|make\s+it)\b"
    r"|\bi\s+keep\s+(?:running\s+into|hitting|getting)\b"
    r"|\bi'?ve\s+been\s+(?:running|using|fighting|testing|paying)\b"
    # TIME HE HAS NOT SPENT. "CLI agents now work in any terminal. I've spent
    # years copying context between chat and shell." — a claim about years of
    # his life, lifted from whoever wrote the submission. 0 of his 35 posts.
    r"|\bi'?(?:ve)?\s*(?:have\s+)?spent\s+(?:years|months|weeks|days|hours|"
    r"the\s+(?:last|past)\s+\w+)\b"
    r"|\bi'?ve\s+(?:been\s+)?(?:waiting|wishing|hoping|struggling)\b"
    # A migration he did not make.
    r"|\bi\s+(?:switched|migrated|moved|ported|rewrote|cancelled|canceled)\s+"
    r"(?:my|our|the\s+whole|everything)\b"
    # A struggle borrowed from the article author, third confirmed appearance
    # ("the type system still trips me up", "I still tripped over its type
    # system", "C++ let me build fast"). 0/35 on his archive.
    r"|\btrips?\s+me\s+up\b|\btripped\s+(?:me\s+up|over)\b"
    r"|\blet\s+me\s+(?:build|ship|write|test)\b"
    # WORK HE IS NOT DOING RIGHT NOW. "I am fixing a mess left by an LLM tool
    # chain running in production right now" shipped 2026-07-28 — a
    # present-progressive claim of ongoing hands-on work, invented whole. The
    # verb list is WORK verbs only: stance verbs he actually uses ("I'm
    # watching to see", "I'm not buying it") stay out. 0/71 on his writing.
    r"|\bi\s*(?:'m|\s+am)\s+(?:fixing|debugging|refactoring|migrating|"
    r"rewriting|patching|untangling|cleaning\s+up|babysitting)\b", re.I)


def _invented_experience(text: str, corpus: str) -> str | None:
    """A fabricated personal anecdote, unsupported by anything he was given."""
    match = (_FIRSTHAND_EVENT_RE.search(text or "")
             or _FIRSTHAND_STATE_RE.search(text or "")
             or _OWN_RIG_RE.search(text or "")
             or _BORROWED_LIFE_RE.search(text or "")
             or _LIVED_JUDGEMENT_RE.search(text or ""))
    if not match:
        return None
    claim = match.group(0)
    # If the evidence or his own archive already describes this experience, it
    # is his to tell. Only inventions are blocked.
    # NO ESCAPE HATCH. Two looser versions of this rule both failed:
    #
    #   "allow it if the evidence contains a first-person phrase" — a Hacker
    #   News thread is full of strangers saying "I tried it", which licensed
    #   the model to write "I tried it. It still stutters on code."
    #
    #   "allow it if HIS archive contains one" — his archive is full of his own
    #   first-person writing, which licensed everything.
    #
    # Both miss the point. The model has not used the thing. His past
    # experience of one product cannot vouch for a claim about another. This
    # gate never runs in enhancement mode, where the words are his, so a real
    # anecdote he types himself is untouched.
    return claim.strip()


def _parrots_the_headline(text: str, corpus: str) -> bool:
    """Opens by repeating the headline it was handed.

    Six live drafts in a row began "Ask HN: Which is the least sloppy...",
    "Why my open-source project hasn't done better", "Salesforce rolls out...".
    A reader who saw the headline gets nothing from the headline again — which
    is exactly the "delivers nothing, no reason to read" complaint.
    """
    # The HEADLINE, not the whole prompt. Comparing against the full direction
    # blob — topic, angle, why, evidence, rules — matched any draft that used
    # ordinary words like "model" or "building", and rejected every candidate:
    # three of six live generations came back with no draft at all.
    headline = _headline_of(corpus)
    if not headline:
        return False
    # ANYWHERE, not just the opening. One live draft made its point and then
    # appended the source headline verbatim as a closing line — "Ask HN: Which
    # is the least sloppy and claudeism free model you have used?" — which an
    # opening-only check could not see.
    headline_words_all = _words_only(headline)
    if len(headline_words_all) >= 5:
        run = _longest_run(_words_only(text), headline_words_all)
        if run >= max(5, int(len(headline_words_all) * 0.7)):
            return True
    opening = _words_only(text)[:8]
    headline_words = _words_only(headline)
    if len(opening) < 6 or len(headline_words) < 4:
        return False
    # Order matters too: a repeat is the same words in the same sequence, not
    # an overlapping vocabulary.
    shared = sum(1 for word in opening if word in set(headline_words))
    in_order = _longest_run(opening, headline_words)
    return shared >= 6 and in_order >= 4


def _headline_of(corpus: str) -> str:
    """The first sentence of the evidence — what a reader would already have seen."""
    marker = _EVIDENCE_MARKER
    body = corpus.split(marker, 1)[1] if marker in corpus else ""
    if not body:
        match = re.search(r'topic\s+"([^"]{4,120})"', corpus)
        body = match.group(1) if match else ""
    if not body:
        # Called with the evidence itself rather than a built prompt.
        body = corpus or ""
    body = body.strip()
    if not body:
        return ""
    cut = re.search(r"[.!?](?:\s|$)", body)
    return body[:cut.end()] if cut else body[:160]


def _longest_run(words: list[str], reference: list[str]) -> int:
    """Longest consecutive stretch of `words` appearing in order in `reference`."""
    best = 0
    for start in range(len(words)):
        for i, ref_word in enumerate(reference):
            if ref_word != words[start]:
                continue
            length = 0
            while (start + length < len(words) and i + length < len(reference)
                   and words[start + length] == reference[i + length]):
                length += 1
            best = max(best, length)
    return best


def _words_only(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2]


def _injected_entities(text: str, corpus: str) -> list[str]:
    """Names the draft brought in that the evidence never mentioned.

    Live, a post about Midjourney's image release read "you're stuck with a
    model that can't even match Fable's image quality", and a post about
    distributed training read "Fable's API-only model shows the cost". Fable is
    a text model that neither article mentions. Both sentences are false, and
    both are invisible to a gate that checks numbers — the error is the NAME.

    This is the archive bias in its last hiding place. It no longer picks the
    topic; it reaches into an unrelated post as the comparison. A comparison the
    evidence cannot support is a claim he would have to defend, so it does not
    ship. Anything the evidence DOES mention stays: comparing labs on the record
    is the account's whole beat.
    """
    from .subject import _SUBJECT_ALIASES
    corpus_l = (corpus or "").lower()
    found: list[str] = []
    # NOT DOING THIS. Flagging any capitalised or version-suffixed name absent
    # from the evidence caught "NotebookLM's folders" — and also killed
    # "Midjourney v8.2 focuses on aesthetics" on a post grounded in an article
    # about Midjourney v8.2, because the evidence spelled it "Midjourney
    # Releasing v8.2" and an exact substring test cannot see that those are the
    # same thing. Live, this rejected 7 of 30 candidates and generation
    # returned nothing at all.
    #
    # Matching product names needs normalisation, not a wider net. Until then
    # the alias check below stands, which is narrow but never wrong.
    for alias in _SUBJECT_ALIASES:
        if len(alias) < 4:
            continue
        if not re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
            continue
        if re.search(rf"\b{re.escape(alias)}\b", corpus_l, re.IGNORECASE):
            continue
        found.append(alias)
    return found


# Empty motivational filler. Every one of these was read off a live draft and
# carries no information: delete the sentence and the post loses nothing.
_FILLER_RE = re.compile(
    r"\b(?:ahead of the curve|the next big thing|build now|don't wait|"
    r"time will tell|only time will tell|the future is here|"
    r"we'?re all just trying to keep up|no one'?s really winning|"
    r"that'?s not just growth|it is what it is|at the end of the day|"
    r"food for thought|let that sink in|the possibilities are)\b"
    # THE SAME TEMPLATE WITH A DIFFERENT TAIL. "if you're building with it,
    # you're already ahead of the curve" is listed above as a fixed phrase, so
    # the model kept the frame and swapped the ending — three times in one
    # afternoon on 2026-07-27:
    #
    #   "...you're already using Anthropic, OpenAI, or local models."
    #   "...you're already using models that outperform Cursor's default setup."
    #   "...you're stuck with the same old problems."
    #
    # Each presumes something about a reader nobody has met. Matching the FRAME
    # instead of the ending catches all four variants, including the original,
    # and still fires on 0 of his 35 posts — he does not address the reader at
    # all. This is a gate widening to cover a template the model routed around,
    # not a new rule.
    r"|\bif\s+you'?re\s+\w+ing\s+(?:with\s+)?(?:it|this|them)\s*,?\s*"
    r"you'?re\s+(?:already\s+|still\s+)?\w+",
    re.IGNORECASE)

# HYPE THAT NEEDS CORROBORATION BEFORE IT COUNTS AS FILLER.
#
# "game changer" sat in `_FILLER_RE` above until 2026-08-07, when he published
# "Claude Code letting sessions talk to each other is an absolute game changer."
# and the scrubber DELETED that whole first line. The extension captured the
# post ten seconds after he wrote it and the archive test caught the deletion
# minutes later — his own writing, eaten by a gate meant to catch the model's.
#
# THIS IS NOT A PHRASE REMOVAL. Deleting "game changer" from the list leaves
# the next hype noun to be found by him, on a live post, instead of by us —
# the exact failure recorded when "fatal flaw" and "let it rip" were blocked as
# human tragedy. That was resolved by SPLITTING the gate into unambiguous
# versus needs-corroboration, and this is the same split.
#
# The distinction that survives calibration: the other eleven entries are empty
# in EVERY context ("at the end of the day", "food for thought") and fire on 0
# of 95 archive posts and 0 of 35 fixture posts. This class is empty only when
# it evaluates NOTHING. "This is a game changer." names no subject and carries
# no information; his sentence names Claude Code and a specific capability, and
# the evaluation IS the post. Same words, opposite content.
_HYPE_NEEDS_SUBJECT_RE = re.compile(r"\b(?:game changer|game-changer)\b",
                                    re.IGNORECASE)


def _is_uncorroborated_hype(sentence: str) -> bool:
    """Hype with nothing concrete under it — filler. Hype ABOUT a named thing
    is his register and must survive.

    Corroboration is `named_things`, the reader the quote and reply briefs
    already use, rather than a second word list: a new hype noun added above
    inherits this test, and a new product name inherits the exemption. A list
    of "allowed subjects" would have to be maintained against a moving industry
    and would fail on exactly the launch he wants to post about.
    """
    if not _HYPE_NEEDS_SUBJECT_RE.search(sentence):
        return False
    try:
        from .reply_plan import named_things
    except Exception:                                        # noqa: BLE001
        # Never let an import failure turn this into a silent delete: with no
        # corroborator available the sentence is KEPT, because the cost of
        # keeping the model's filler is a flagged draft and the cost of
        # dropping his is a rewritten post.
        return False
    return not named_things(sentence)


_BACK_REFERENCE = re.compile(
    r"^\s*(?:it|that|this|they|those|these|he|she|which)\b", re.IGNORECASE)


def _loses_the_thread(text: str, topic: str, evidence: str) -> bool:
    """True when the post stops being about anything.

    Read off a live draft, in full:

        "What if seamlessness is just another form of burnout? Byung-Chul Han
         says positivity kills resistance. A person in distress opens a
         chatbot. It's not helping. We're all just trying to keep up. No one's
         really winning."

    Six sentences, six subjects, nothing a reader could repeat to anyone. Each
    sentence is grammatical; the post is not about anything. That is the
    "delivers nothing, technical slop" complaint exactly, and no readability
    score sees it because every sentence reads fine on its own.

    A post holds together when its sentences stay on ONE subject. A sentence
    counts as connected if it shares a real word with the topic, the evidence,
    or the opening line — or if it opens with a back-reference right after a
    sentence that was itself connected, which is how normal writing chains.
    """
    sentences = split_sentences(text or "")
    if len(sentences) < 3:
        return False                    # too short to wander
    anchor = set(_words_only(topic)) | set(_words_only(evidence))
    anchor |= set(_words_only(sentences[0]))
    anchor -= _GRAMMAR_WORDS
    if not anchor:
        return False                    # nothing to measure against
    connected = 0
    previous_ok = False
    for index, sentence in enumerate(sentences):
        words = set(_words_only(sentence)) - _GRAMMAR_WORDS
        ok = bool(words & anchor)
        if not ok and index > 0 and previous_ok and _BACK_REFERENCE.match(sentence):
            ok = True                   # "It's not helping." after a real line
        connected += 1 if ok else 0
        previous_ok = ok
    # CALIBRATED AGAINST HIS OWN WRITING, not against intuition. At a 0.5
    # threshold this flagged 7 of his 35 published posts — 20% — including
    # "Honestly, Kimi K3 might be the reason Opus 5 gets delayed. Anthropic
    # can't release..." which is perfectly coherent: it introduces a second
    # name in the second sentence, which is how people actually write. Every
    # other gate in this file sits at 0-6% on that same corpus. A third of the
    # way down from there is the honest bar.
    return (connected / len(sentences)) < 0.34


# Words that connect nothing: two posts sharing only these share no subject.
_GRAMMAR_WORDS = {
    "the", "and", "but", "for", "with", "that", "this", "you", "your", "not",
    "are", "was", "were", "have", "has", "had", "its", "it's", "they", "them",
    "their", "what", "when", "where", "who", "why", "how", "all", "just",
    "like", "get", "got", "now", "one", "out", "about", "from", "into", "than",
    "then", "there", "here", "some", "any", "can", "will", "would", "should",
    "could", "more", "most", "even", "still", "already", "yet", "own", "make",
    "makes", "made", "does", "doing", "done", "being", "been", "much", "many",
    "very", "really", "thing", "things", "stuff", "people", "everyone",
}


def offending_spans(text: str) -> list[str]:
    """The exact phrases that tripped the register gates, for a rewrite note.

    The revision pass used to be told only "a stock AI construction slipped in",
    which leaves the model guessing which words to change — and it guessed
    wrong often enough that flagged drafts shipped with the construction
    intact.
    """
    lowered = re.sub(r"\s+", " ", fold_punctuation(text).lower()).strip()
    found: list[str] = []
    for tell in _AI_TELLS:
        if tell in lowered:
            found.append(tell)
    for pattern in (_NOT_JUST_RE, _ABOUT_TEMPLATE_RE, _THATS_NOT_RE, _FILLER_RE):
        match = pattern.search(lowered)
        if match:
            found.append(match.group(0).strip())
    seen: set[str] = set()
    unique: list[str] = []
    for span in found:
        if span not in seen:
            seen.add(span)
            unique.append(span)
    return unique


# A sentence that exists only to set up a formula. "That's not a trend." says
# nothing on its own — it is scaffolding for the line after it.
_SETUP_SENTENCE_RE = re.compile(
    r"^(?:that'?s|this is|it'?s)\s+not\s+(?:just\s+)?(?:an?|the)?\s*"
    r"[a-z]+(?:\s+[a-z]+)?[.!?]?$", re.IGNORECASE)
# "The Cost of Seamlessness isn't just a buzzword." Same empty setup, but it
# opens with the subject rather than "That's", so the rule above walked past it.
# THE ARTICLE IS THE TELL. His own archive closes with the bare-noun form —
# "That filter isn't just safety. It's insurance." — and with the article
# optional this rule deleted that post on 2026-07-27 (caught by the
# byte-identical archive test the same evening). The template his drafts
# produce says "isn't just A buzzword / A trend / THE point"; his voice drops
# the article. Bare nouns pass, article forms die.
_ISNT_JUST_SENTENCE_RE = re.compile(
    r"^[^.!?]{0,60}\b(?:is|are|was)\s*n[o']?t\s+just\s+(?:an?|the)\s+"
    r"[a-z]+(?:\s+[a-z]+)?[.!?]?$", re.IGNORECASE)

# THE SAME MACHINE WITH A NOUN SUBJECT. "That's not a feature." was caught;
# "Opus 5's sloppiness isn't a bug." was not, and it is the identical empty
# setup — the next line ("It's a feature.") carries whatever point exists. The
# subject can be anything, so the pattern must not require "That's".
_NOUN_SETUP_RE = re.compile(
    r"^[^.!?]{0,44}\b(?:isn'?t|is not|'?s not|aren'?t|are not|was not|wasn'?t)\s+"
    r"(?:just\s+)?(?:an?|the)\s+[a-z]+(?:\s+[a-z]+)?[.!?]?$", re.IGNORECASE)

_ABOUT_SETUP_RE = re.compile(
    r"^(?:it'?s|that'?s|this)\s*(?:is\s*)?n[o']?t\s+about\s+[^.!?]{0,40}[.!?]?$",
    re.IGNORECASE)


_COMMA_TEMPLATE_RE = re.compile(
    # CURLY APOSTROPHES. Every other check in this function runs against
    # fold_punctuation(raw); this one substitutes into the live text, so it sees
    # what the model actually emitted — and the model emits U+2019. A pattern
    # written with a straight quote matched none of it. This is the fourth time
    # curly punctuation has silently disabled a gate in this file.
    r"(?:(?<=^)|(?<=[.!?]\s)|(?<=\n))"
    r"(?:that['’]?s|that is|this is|it['’]?s|it is)\s+not\s+"
    r"(?:just\s+)?(?:an?|the)?\s*"
    # ANY joiner, not just a comma. The model wrote "That's not a feature - it's
    # a firewall." as ONE sentence, so no sentence-level rule could see it — and
    # then _match_account_style turned the dash into a line break, resurrecting
    # the template as two visible sentences AFTER the scrubber had run. That is
    # how the formula kept reaching the composer with every gate passing.
    r"[a-z]+(?:\s+[a-z]+)?\s*[,;:—–-]\s*"
    # THE FINAL FULL STOP IS OPTIONAL, and requiring it was the whole bug.
    # This account's house style omits it — half his archive posts end without
    # one — so the construction survived intact whenever the model matched his
    # punctuation:
    #   "half the price? that's not a price, that's a discount for the whole
    #    world"                                     <- no period, not removed
    #   "half the price? that's not a price, that's a discount for the whole
    #    world."                                    <- one period, removed
    # Same sentence, opposite outcome.
    r"(?P<payoff>(?:it['’]?s|it is|that['’]?s|that is|they['’]?re|"
    r"they are)\s+[^.!?]+(?:[.!?]|$))",
    re.IGNORECASE)


# THE SAME COMMA TEMPLATE WITH A NOUN SUBJECT. The pattern above requires
# "That's" / "This is" / "It's", so the moment the model puts a real noun in
# front it walks straight through:
#
#   "Moe's not a card, it's a switch."          (live, reply to his own thread)
#   "Opus 5's sloppiness isn't a bug, it's a feature."
#   "The pricing page isn't a benchmark, it's a receipt."
#
# This is the fifth variant of one construction to need covering — sentence
# form, comma form, contraction form, "you're" form, and now noun subject. They
# are all the same machine: a setup that says nothing, joined to the half that
# does. Keep the payoff, drop the setup.
#
# Bounded subject (26 chars) so it cannot swallow a real clause, and it must
# carry an explicit copula or possessive — "Serving a 30B is now cheaper than
# the API, it's a real shift" is a genuine sentence and stays. Zero matches
# against all 35 posts in his archive.
_COMMA_TEMPLATE_NOUN_RE = re.compile(
    r"(?:(?<=^)|(?<=[.!?]\s)|(?<=\n))"
    r"[A-Z][^.!?,;\n]{0,26}?(?:['’]s|\s+is|\s+are|\s+was|\s+were)\s*n(?:['’]t|ot)\s+"
    # The article is OPTIONAL. "Sparse activation isn't just efficient, it's
    # how these models actually work" carries no "a"/"the" and walked through
    # a version of this that required one.
    r"(?:just\s+)?(?:an?|the)?\s*[a-z]+(?:\s+[a-z]+)?\s*[,;:—–-]\s*"
    r"(?P<payoff>(?:it['’]?s|it is|that['’]?s|that is|they['’]?re|"
    r"they are)\s+[^.!?]+(?:[.!?]|$))",
    re.IGNORECASE)


# A trailing comma, or a word that cannot be the last word of a sentence.
_DANGLING_END_RE = re.compile(
    r"(?:,\s*$|\b(?:and|but|or|so|if|when|while|because|that|which|the|a|an|"
    r"to|of|for|with|from|into|about|than|then|is|are|was|were|be|been|as|at|"
    r"on|in|by|it|its|this|these|those|you|your|we|our|they|their)\s*$)",
    re.IGNORECASE)


def drop_trailing_fragment(text: str) -> str:
    """Remove a final clause the model never finished.

    Generation is bounded by num_predict, so a draft can simply stop: "...It's
    about what you actually build with. If you're using it," — a real post,
    plus a dangling half-sentence that makes the whole thing read as broken.
    Cutting back to the last complete sentence loses nothing the reader had.
    """
    stripped = (text or "").rstrip()
    if not stripped:
        return text
    # HIS POSTS OFTEN END WITHOUT A FULL STOP — "this is the first time the
    # Jarvis comparison feels legit" is finished, and casual. Treating a
    # missing period as truncation rewrote 10 of his 34 published posts. The
    # real tell is stopping MID-CLAUSE: a trailing comma, or a dangling
    # function word that cannot end an English sentence.
    if not _DANGLING_END_RE.search(stripped):
        return text
    sentences = split_sentences(stripped)
    if len(sentences) < 2:
        return text          # nothing complete to fall back to; leave it
    while sentences and _DANGLING_END_RE.search(sentences[-1].rstrip()):
        sentences.pop()
    if not sentences:
        return text
    repaired = " ".join(sentences).strip()
    # Keep his paragraph shape: find where the kept text ends in the original.
    cut = stripped.find(sentences[-1])
    if cut >= 0:
        repaired = stripped[:cut + len(sentences[-1])].rstrip()
    return repaired or text


_POINT_IN_DIRECTION_RE = re.compile(
    r'THE POINT OF THIS POST, decided already:\s*"([^"]{8,200})"')


def _planned_point(direction: str) -> str:
    """The single claim this draft was supposed to argue, if one was chosen."""
    match = _POINT_IN_DIRECTION_RE.search(direction or "")
    return match.group(1).strip() if match else ""


# A sentence that carries NO information: no name, no number, no new idea —
# just a reaction to whatever came before it. Read off live output:
#
#   "That's a big deal."  "It affects the whole system."
#   "The outcome matters a lot."  "It's a choice."  "The risk is real."
#   "That's why it's timely."  "This is what it means for people who build."
#
# One draft was eight sentences long and six of them were these. That is the
# "I can't tell what the post is trying to say" complaint in its purest form:
# the post says nothing, at length. No prompt rule removes them because the
# model does not experience them as empty — they feel like emphasis.
_EMPTY_SENTENCE_RE = re.compile(
    r"^(?:that|this|it|the\s+\w+)(?:'?s|\s+is|\s+are|\s+was)?\s+"
    r"(?:a\s+|an\s+|the\s+)?"
    r"(?:big\s+deal|real|huge|massive|important|significant|clear|"
    r"a\s+problem|a\s+choice|a\s+trap|a\s+shift|why|what\s+it\s+means|"
    r"matters(?:\s+a\s+lot)?|the\s+point|the\s+question|timely|"
    r"not\s+nothing|everything|the\s+whole\s+system)\b[^.!?]{0,24}[.!?]?$",
    re.IGNORECASE)


# Words that name nothing. A sentence built only from these is a reaction to
# the post rather than a part of it.
_ABSTRACT_NOUNS = {
    "system", "systems", "thing", "things", "stuff", "people", "everything",
    "everyone", "anyone", "world", "future", "game", "space", "level", "side",
    "point", "question", "problem", "problems", "issue", "issues", "matter",
    "reality", "truth", "story", "picture", "shift", "change", "trend",
    "impact", "effect", "result", "outcome", "process", "whole", "real",
    "clear", "obvious", "simple", "hard", "easy", "better", "worse", "affects",
    "means", "matters", "actually", "really", "already", "still", "just",
}


def _specifics(text: str) -> set[str]:
    """The words that actually refer to something: names, numbers, real nouns."""
    bare = fold_punctuation(text or "")
    out = {w.lower() for w in re.findall(r"\b[A-Za-z][A-Za-z0-9.\-]{3,}", bare)}
    out |= {w for w in re.findall(r"\d+[\w%.]*", bare)}
    return {w for w in out if w not in _GRAMMAR_WORDS}


def _carries_nothing(sentence: str, already: set[str]) -> bool:
    """True when a sentence adds no NEW specific thing to what came before.

    The word-count version of this test was too kind. "It affects the whole
    system." has four content words and says nothing at all once the previous
    sentence already said a bad actor stops the process. Novelty is the honest
    measure: a sentence that introduces no name, no number and no new noun is
    reacting to the post rather than adding to it.
    """
    bare = fold_punctuation(sentence).strip()
    if not bare:
        return True
    # HIS "Why?" IS A DEVICE, NOT FILLER. He sets up a prediction and then
    # answers it — "Prediction: Grok 4.5 launches July 19. Why? ..." — and the
    # first version of this deleted that from two of his own published posts.
    # A very short question is doing work no word count can see.
    if bare.endswith("?") and len(bare.split()) <= 3:
        return False
    if _EMPTY_SENTENCE_RE.match(bare):
        return True
    # ABSTRACTIONS ARE NOT SPECIFICS. "It affects the whole system." introduces
    # three words this post has not used, and refers to nothing. A sentence
    # whose only new words are of this kind is reacting, not adding.
    fresh = _specifics(bare) - already
    if fresh and not (fresh - _ABSTRACT_NOUNS):
        return len(bare.split()) <= 12
    fresh = fresh - _ABSTRACT_NOUNS
    if fresh:
        return False
    # No new specifics. Long sentences can still carry an argument; short ones
    # at this point are "That's a big deal" wearing more words.
    return len(bare.split()) <= 12


# HE DOES NOT TALK TO THE READER. Measured across all 35 published posts: ZERO
# make a claim about the reader's situation, and exactly one contains the word
# "you" at all — a reply to @grok. His posts observe and predict; they do not
# instruct.
#
# The model closes on one constantly, and it is where the post stops being
# checkable and starts being an advert. Live on 2026-07-27, every one of these
# was the final block of an otherwise-grounded draft:
#
#   "World-model-optimizer cuts costs in half."   | "You're saving thousands on
#                                                    model training."
#   "Spur solver's Z3 magic solves ... bugs."     | "You're fighting a battle
#                                                    that's already lost."
#   "Rust's 50% lower token use is impressive."   | "Either way, we're all stuck
#                                                    with more code than we need."
#
# The first block is grounded in the evidence; the second is invented, about
# someone the writer has never met. It is deleted for the same reason
# `strip_formulas` deletes stock constructions rather than asking the model not
# to write them: three rounds of prompt rules did not stop those either.
#
# Only the LAST block, and never the only block — a post that is nothing but a
# reader claim has a different problem, and the quality gates should see it
# whole rather than be handed a scrubbed fragment.
# ANY second person, not a list of contractions. The narrow version missed
# "You don't need a cloud tool to trust it." and "It's just a better version of
# you, asking for help." — both live, both the empty closing line of an
# otherwise-grounded draft. Enumerating verb forms was always going to lose
# that race.
#
# Safe to widen because `names_nothing` is required alongside it everywhere it
# is used: measured, the wide form fires on 0 of his 35 published posts, the
# same as the narrow one, while "You're paying 4x what Goose charges" still
# ships because it carries a figure. He uses the word "you" in exactly one
# archived post, and that one is a reply to @grok.
_READER_CLAIM_RE = re.compile(r"\byou\b|\byou'?(?:re|ll|ve|d)\b|\byour\b"
                              r"|\bwe'?re all\b|\bwe all\b", re.IGNORECASE)

# A sentence that puts a question mark AT the reader. See talks_to_the_reader
# in `_draft_quality_issues`. Calibration found the one exception in his
# archive: "@grok … i think Anthropic is copying someone don't you?" — when
# the draft @-addresses a NAMED account, the "you" has a referent and the
# question is conversation, not buttonholing. No @-mention, no referent.
_READER_QUESTION_RE = re.compile(
    r"(?:^|[.!?\n]\s*)[^.!?\n]*\b(?:you|you're|your)\b[^.!?\n]*\?",
    re.IGNORECASE)


def _questions_the_reader(text: str) -> bool:
    if "@" in (text or ""):
        return False
    return bool(_READER_QUESTION_RE.search(text))


def asks_without_saying(text: str) -> bool:
    """Every sentence is a question, so the draft asserts nothing.

    THE DOMINANT QUOTE FAILURE. Measured 2026-07-27, five quotes of five
    different tweets came back as five rhetorical questions:

        "What's the catch with Goose if it's really beating Claude?"
        "What's the real cost of open weights when the models are already
         outpacing closed ones?"
        "Does Cowork mean Claude finally gets to work in your files without
         babysitting a chatbot?"
        "What's the point of running two models side by side if they don't
         actually disagree?"
        "What if the real problem isn't the switch statement but the models
         themselves?"

    One template, five times — repetitive across the batch on its own, and
    worse than that: a quote's entire job is the line the reader would not have
    got from the post they are already looking at, and a question supplies
    nothing. `what_works.py` measures him opening with a question in 0% of his
    posts, top or bottom.

    Calibrated at 1/35 against his archive, and that one is "@grok i think
    Anthropic is copying someone don't you?" — an @-mention, not a post written
    from scratch. His real question posts ("Why is Anthropic so quiet about
    Opus 5?") always land a claim afterwards, so they are untouched.
    """
    sentences = [s.strip() for s in split_sentences(text or "") if s.strip()]
    if not sentences:
        return False
    return all(s.rstrip().rstrip("\"')").endswith("?") for s in sentences)


def _cut_empty_closer(text: str, is_empty) -> str:
    """Drop a trailing block, or failing that a trailing sentence, that `is_empty`.

    BLOCK FIRST, THEN SENTENCE, and the sentence half is the one that matters.
    These scrubbers originally looked only at a trailing BLOCK — and
    `_match_account_style` is what CREATES blocks, so a draft the model wrote as
    one paragraph was invisible to them on the way in and had a trailing block
    by the time the reader saw it. Measured live on 2026-07-27, read out of
    `generation_candidates` while both scrubbers were deployed and their own
    unit tests were green:

        "Two coding agents can chat now. It's a new way to build apps.
         \\n\\n
         You're not using it yet."

    Working on the last sentence as well makes the result independent of
    whether the shaper split anything, which is the only version that actually
    holds. Never cuts the only sentence: a draft that is nothing BUT an empty
    closer has a different problem, and the quality gates should see it whole.
    """
    body = (text or "").rstrip()
    if not body:
        return text
    blocks = [b for b in re.split(r"\n\s*\n", body) if b.strip()]
    if len(blocks) >= 2 and is_empty(blocks[-1]):
        cut = body.rfind(blocks[-1])
        if cut > 0:
            return body[:cut].rstrip()
    sentences = [s.strip() for s in split_sentences(body) if s.strip()]
    if len(sentences) >= 2 and is_empty(sentences[-1]):
        cut = body.rfind(sentences[-1])
        if cut > 0:
            return body[:cut].rstrip()
    return text


_NAME_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")


def repair_name_fragments(text: str, corpus: str) -> str:
    """A capitalized token that is a MID-WORD fragment of an evidence name is
    that name, mangled — put the whole name back.

    "NoBg makes training it easy." shipped on 2026-07-27 beside evidence that
    only ever says FeyNoBg; the entity gate vouches for substrings, so the
    tear was invisible. The repair is deterministic and narrow:

      * a token that appears as a whole word in the evidence is left alone;
      * a token that is a PREFIX of an evidence name ("Opus" beside "Opus5")
        is left alone — truncating at the front is how names are shortened
        on purpose;
      * a token found mid-word inside exactly ONE evidence name is replaced
        with that name. Two possible hosts = ambiguity = hands off.
    """
    if not (text or "").strip() or not (corpus or "").strip():
        return text
    corpus_words = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", corpus))
    # A token that is a dot/hyphen-delimited SEGMENT of a corpus word is that
    # word being referenced normally — "GPT" beside "GPT-5.6" is fine. Without
    # this, his own archive post came back as "ChatGPT-5.6": the bare "GPT"
    # matched inside "ChatGPT" and the rewrite tore every version name after.
    segments = {seg.lower() for w in corpus_words
                for seg in re.split(r"[.\-]", w) if seg}
    lowered = {w.lower().strip(".-") for w in corpus_words}
    out = text
    for token in set(_NAME_TOKEN_RE.findall(text)):
        low = token.lower()
        if low in lowered or low in segments:
            continue
        hosts = {w for w in corpus_words
                 if len(w) > len(token) and token in w
                 and not w.lower().startswith(low)}
        if len(hosts) == 1:
            out = re.sub(rf"\b{re.escape(token)}\b", hosts.pop(), out)
    return out


# The verbs that make a short line a sentence rather than a noun pile. Used
# only to spare real closing questions from the fragment cut.
_HAS_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|has|have|had|do|does|did|can|could|will|"
    r"would|should|won'?t|isn'?t|aren'?t|doesn'?t|didn'?t|gets?|goes?|went|"
    r"means?|says?|said|beats?|runs?|ships?|makes?|takes?|needs?|wants?|"
    r"works?|comes?|looks?|shows?|know|think|see|matters?|\w+ing|\w+ed)\b",
    re.IGNORECASE)


def repair_stub_tail(text: str) -> str:
    """Cut a final sentence that stops on a one-or-two-letter stub.

    "That tree prints straight to stdout by a lo" shipped TWICE — the stub
    check flags it as `cut_off`, but a cut_off near miss still ships with a
    note, and a note does not un-break the sentence. `drop_trailing_fragment`
    cannot help: "lo" is not a dangling function word, it is half of one.
    Cutting the broken sentence loses nothing the reader ever had, and never
    cuts the only sentence — a draft that is entirely one broken line has
    nothing worth shipping and the gates keep treating it as such.
    """
    body = (text or "").rstrip()
    if not body:
        return text
    sentences = [s.strip() for s in split_sentences(body) if s.strip()]
    while len(sentences) >= 2:
        last = sentences[-1].rstrip()
        match = _STUB_END_RE.search(last)
        stubbed = bool(match) and match.group(1).lower() not in _REAL_SHORT_WORDS
        # A VERBLESS QUESTION FRAGMENT IS A SHRUG, NOT A SENTENCE.
        # "Frontier-Bench SOTA 43.3%?" trailed a live draft on 2026-07-28 —
        # a noun pile with a question mark, asking the reader to finish the
        # thought. Calibrated the same morning: 0/71 of his posts end on one
        # (his closing questions have verbs: "why isn't the code?").
        fragment_q = (last.endswith("?") and len(last) <= 64
                      and not _HAS_VERB_RE.search(last))
        if not stubbed and not fragment_q:
            break
        cut = body.rfind(sentences[-1])
        if cut <= 0:
            break
        body = body[:cut].rstrip()
        sentences.pop()
    return body or text


def _is_empty_reader_claim(chunk: str) -> bool:
    # It has to be a flourish, not the substance: a closer carrying a figure or
    # a named product is doing real work even in the second person.
    return bool(_READER_CLAIM_RE.search(fold_punctuation(chunk))) and names_nothing(chunk)


def drop_reader_address(text: str) -> str:
    """Remove a trailing block or sentence that only tells the reader about themselves."""
    return _cut_empty_closer(text, _is_empty_reader_claim)


# A VERDICT ABOUT THE CATEGORY, NOT ABOUT THE THING. The closing flourish is
# the commonest residual post defect, and my first attempt to gate it failed
# calibration badly: "a short last block that names nothing" fires on 11-23% of
# his own posts, where it is the BEST part of the writing —
#
#     "bro is not saving the multiverse, he's vibe coding it"
#     "The silence is making me think they're cooking."
#     "Just my prediction."
#
# So structure is the wrong axis. What separates his closer from the model's is
# not length, it is what the sentence is ABOUT. His are personal, specific to
# the moment, often a joke. The model's are a verdict on the whole category:
#
#     "It's a new way to build apps."
#     "That's a big shift from models that take weeks."
#     "It's the way we're building AI."
#     "That's a leaky faucet."
#
# Every one is "It's/That's a/the <abstraction>", and it is unfalsifiable — it
# could close a post about literally anything. Measured: catches 6 of 6 live
# examples, fires on 0 of 6 of his real closers, and 0 of his 35 published
# posts. `names_nothing` still required, so a closer carrying a figure or a
# product survives.
# "ABOUT" BELONGS IN THE LIST. The payoff half of the "X isn't about A, it's
# about B" template is the same empty verdict wearing a different preposition,
# and it is the single most persistent shape in this model's output — three
# separate measured runs on 2026-07-27 ended on one:
#
#     "It's about who gets to control the flow."
#     "It's about fixing the cracks in generative models."
#     "It's about control."
#
# `strip_formulas` does not delete the construction (verified) and the ai_tell
# gate only flags it, so it shipped. Adding the preposition takes the catch
# rate from 2 of 5 to 5 of 5 with no change on his archive: still 0 of 35, and
# still none of his own closers.
_CATEGORY_VERDICT_RE = re.compile(
    r"^\s*(?:it'?s|that'?s|this is)\s+(?:a|an|the|about|all about)\s+"
    r"[^.!?\n]{2,70}[.!]?\s*$",
    re.IGNORECASE)

# NARRATING THE SOURCE INSTEAD OF REACTING TO IT. Two shapes, one fault: the
# draft tells the reader about the article it was written from, and the reader
# has not seen that article. Both measured live on 2026-07-27:
#
#   "Rust's 50% lower token use is a win, but C++ still gets the job done.
#    The first experiment was to pit C++ against Rust in real code."
#   "CLI agents now work in any terminal. ... Until today's HN post showed
#    it's possible."
#
# This is `talks_about_the_post`, which the reply surface already treats as a
# real fault, arriving on the post surface — so it is an extension of an
# established rule rather than a new axis. 0 of his 35 published posts match,
# and "The first model to ship this wins" / "The paper trail on Opus 5 is thin"
# both survive.
_DESCRIBES_SOURCE_RE = re.compile(
    r"\bthe\s+(?:first|second|third|next|final)\s+(?:experiment|test|section|"
    r"part|step|example)\s+(?:was|is|shows?|covers?)\b"
    r"|\bthe\s+(?:article|post|paper|piece|writeup|thread|author)\s+"
    r"(?:says?|shows?|argues?|claims?|notes?|explains?|covers?|found)\b"
    r"|\baccording\s+to\s+(?:the\s+)?(?:article|post|paper|piece|writeup|"
    r"hn|hacker\s+news|reddit)\b"
    r"|\bin\s+(?:the|this)\s+(?:article|post|writeup)\b"
    r"|\b(?:today'?s|this|a|the)\s+(?:hn|hacker\s+news|show\s+hn|ask\s+hn|"
    r"reddit|arxiv)\s+(?:post|thread|submission|comment)\b"
    # POSSESSIVE AND SUBJECT FORMS. The first version wanted "the HN post" and
    # the model wrote "HN's first post?" and "Hacker News is the only place
    # this matters." — both live, both the same fault. Catch rate 2/4 -> 4/4,
    # still 0 of his 35 posts, and "Hacker News ranked it number one this
    # morning" (a fact ABOUT the world, not a citation) still ships.
    r"|\b(?:hn|hacker\s+news|reddit|arxiv)'?s\s+(?:first\s+|top\s+|latest\s+)?"
    r"(?:post|thread|comment|submission)\b"
    r"|\b(?:hn|hacker\s+news)\s+is\s+(?:the\s+)?(?:only|best|where)\b",
    re.IGNORECASE)

# "X is a big deal" — the exact hype sentence the prompt bans ("never write a
# sentence whose only point is that something is big"), and it shipped twice
# on 2026-07-28 as essentially the whole draft ("Ami's graph memory is a big
# deal."). Calibrated: 0/71 across the fixture and the full archive — the
# phrase is simply not his. Ships flagged, and the reviser is told the fix.
_BIG_DEAL_RE = re.compile(r"\bis a big deal\b", re.IGNORECASE)

# See `sweeping_claim` in `_draft_quality_issues`.
_SWEEPING_CLAIM_RE = re.compile(
    r"\b(?:no\s?one|nobody)(?:'s|s)?\s+(?:really\s+)?"
    r"(?:knows?|understands?|has\s+figured|figured|gets?|can)\b"
    r"|\b(?:everyone|everybody)\s+(?:will|is going to|'ll|has|knows)\b"
    r"|\b(?:win|wins|winning|lose|loses)\s+the\s+(?:ai\s+)?(?:race|war|game)\b",
    re.IGNORECASE)


def _is_empty_category_verdict(chunk: str) -> bool:
    return bool(_CATEGORY_VERDICT_RE.match(fold_punctuation(chunk))) and names_nothing(chunk)


def drop_category_verdict(text: str) -> str:
    """Remove a trailing block or sentence that is only a verdict on the category."""
    return _cut_empty_closer(text, _is_empty_category_verdict)


def tighten_to_the_point(text: str) -> str:
    """Delete sentences that add nothing, keep the ones that say something.

    Measured on his own posts: one sentence lifts engagement 11x and the
    two-to-three sentence shape is a drag. Filler is what fills that middle,
    and no prompt rule removes it because the model does not experience those
    sentences as empty — they feel like emphasis.

    Never cuts below two sentences and never touches a short post, so a real
    two-line take is safe.
    """
    if not text:
        return text
    spans = []
    cursor = 0
    for sentence in split_sentences(text):
        found = text.find(sentence, cursor)
        if found < 0:
            continue
        spans.append((found, found + len(sentence), sentence))
        cursor = found + len(sentence)
    if len(spans) < 3:
        return text
    seen: set[str] = set()
    drop: list[tuple[int, int]] = []
    kept = 0
    for begin, finish, raw in spans:
        if _carries_nothing(raw, seen):
            drop.append((begin, finish))
            continue
        seen |= _specifics(raw)
        kept += 1
    if not drop or kept < 2:
        return text
    out = text
    for begin, finish in reversed(drop):
        out = out[:begin] + out[finish:]
    out = re.sub(r"[ 	]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out or text


# THE CHECKER'S VOICE, NEVER THE WRITER'S. The clarity guidance describes what
# a weak draft is missing ("no concrete number, no named thing, no stake ...
# add one"). Handed that text, a small model sometimes writes it back AS the
# post — live, drafts ended:
#
#   "No numbers, no names, no stake — no reason to stop."
#   "There's no number, no name, no reason to care. Add one — a real figure,
#    what it means, or what you really think."
#
# A substring check on the guidance missed the second one because the model
# paraphrased it. The shape is what gives it away: a post never instructs its
# own reader to add a figure, and never narrates that it lacks a reason to be
# read. This is the last line before that reaches his feed.
_INSTRUCTION_LEAK_RE = re.compile(
    r"(?:\bno reason to (?:stop|care|read)\b"
    r"|\bnobody scrolling\b|\bno one scrolling\b"
    r"|\badd (?:one|a real|a concrete|a specific)\b"
    r"|\b(?:no|there'?s no) (?:number|numbers)[^.!?]{0,30}\b(?:no|nor) "
    r"(?:name|names|stake|position)\b"
    r"|\bwhat you (?:really |actually )?think\.?\s*$"
    # WRITING ADVICE, IN ANY WORDING. Two earlier versions of this gate matched
    # the guidance text, then its shape, and the model simply paraphrased again:
    # "People passing by won't care unless they have a reason to stop. Give them
    # a number, a name, or something that affects them. Don't add hype. Add real
    # meaning." Nothing lexical keeps up. What stays constant is the FUNCTION —
    # a post never instructs its own reader to add a number, a name or a stake,
    # because a post is not a critique of itself.
    r"|\b(?:give|add|include|use)\s+(?:them\s+|it\s+)?(?:a\s+|one\s+|real\s+)?"
    r"(?:number|numbers|name|names|figure|stake|specifics?|meaning)\b"
    r"|\b(?:don'?t|do not|never)\s+add\s+(?:hype|fluff|filler)\b"
    r"|\b(?:passing by|scrolling (?:by|past))\b[^.!?]{0,40}\b(?:won'?t|will not)\s+care\b"
    # A POST NEVER REVIEWS ITSELF. "Without specific numbers, it's hard to
    # grasp the real impact. A clear figure would show exactly how much Rust is
    # improving." — that is the checker talking about the draft, inside the
    # draft. The fourth distinct paraphrase of the same guidance to reach live
    # output; the guidance text itself has now been removed from the prompt, so
    # this is the backstop rather than the fix.
    r"|\bwithout (?:specific |concrete |real )?(?:numbers?|figures?|data)\b"
    r"|\ba (?:clear|specific|concrete) (?:figure|number|example) would\b"
    r"|\bhard to (?:grasp|gauge|see|judge) the (?:real )?(?:impact|scale|point)\b"
    r"|\bdon'?t just say\b|\bshow what it changes\b"
    r")",
    re.IGNORECASE)


# A POST NEVER TALKS ABOUT ITSELF AS A DRAFT. The clarity-revision guidance is
# prose, and the model copies prose. Measured live on 2026-07-27 — this reached
# the composer as a finished post:
#
#     "Nunchaku 4-bit Diffusers require a Blackwell GPU.
#      Use INT4 if you don't have one.
#      The draft cuts off mid-sentence. Finish it properly."
#
# The last line is the instruction, echoed. `llm.py` already records that
# three gates were written for this shape and each was defeated by the next
# rewording, which is why the second half of the check below does not try to
# describe the prose at all — it compares against the ACTUAL guidance strings,
# read out of the guidance tables, so a reworded instruction is still caught
# and a newly added one is covered the day it is written.
_META_ABOUT_DRAFT_RE = re.compile(
    r"\b(?:the|this|your)\s+draft\b"
    r"|\bthe\s+(?:final|last|first|opening)\s+sentence\b"
    r"|\b(?:rewrite|reword|revise)\s+(?:it|this|the)\b"
    r"|\bdo\s+not\s+(?:describe|mention|use|write|add)\b"
    r"|\busing\s+only\s+(?:the\s+)?facts\b", re.IGNORECASE)

_GUIDANCE_SHINGLES: frozenset[str] | None = None
_SHINGLE_WORDS = 6


def _shingles(text: str, size: int = _SHINGLE_WORDS) -> set[str]:
    words = re.findall(r"[a-z0-9']+", fold_punctuation(text or "").lower())
    return {" ".join(words[i:i + size]) for i in range(max(0, len(words) - size + 1))}


def _guidance_shingles() -> frozenset[str]:
    """Every distinctive phrase the revision guidance can put in front of the model."""
    global _GUIDANCE_SHINGLES
    if _GUIDANCE_SHINGLES is not None:
        return _GUIDANCE_SHINGLES
    collected: set[str] = set()
    try:
        from .llm import ModelClient
        for attr in ("CLARITY_GUIDANCE", "REPLY_CLARITY_GUIDANCE"):
            table = getattr(ModelClient, attr, None)
            if isinstance(table, dict):
                for value in table.values():
                    collected |= _shingles(str(value))
    except Exception:                                    # noqa: BLE001
        pass
    _GUIDANCE_SHINGLES = frozenset(collected)
    return _GUIDANCE_SHINGLES


def leaks_instructions(text: str, *, talks_about_drafts: bool = False) -> bool:
    """True when the draft is quoting the machinery instead of writing a post.

    `talks_about_drafts` is for the one place where naming a draft or a sentence is the
    subject rather than a leak: a chat answer to somebody who ASKED about writing. "A POST
    NEVER TALKS ABOUT ITSELF AS A DRAFT" is true of posts; it is false of advice. The two
    checks that compare against the machinery itself still run either way.
    """
    body = fold_punctuation(text or "")
    if _INSTRUCTION_LEAK_RE.search(body):
        return True
    if not talks_about_drafts and _META_ABOUT_DRAFT_RE.search(body):
        return True
    guidance = _guidance_shingles()
    return bool(guidance and (_shingles(text) & guidance))


def cut_to_one_point(text: str) -> str:
    """Keep the sentence that carries the claim. Drop the rest.

    This is the fix for "I can't tell what the post is trying to say", and it
    is deliberately mechanical, because three rounds of asking the model to be
    clear did not work and were never going to. A post that is one sentence
    cannot wander onto a second subject, cannot pad, and cannot leave a reader
    wondering which part was the point.

    His own numbers say the same thing from the other side: one sentence lifts
    engagement 11x against the rest of his posts, and the two-to-three sentence
    shape is a 0.39 drag. The shape that reads clearly is also the shape that
    performs.

    A very short opener keeps its follow-up, so "GPT-5.6 dropped today." is not
    left standing alone as the entire post — that is the barbell his archive
    actually shows, not a blanket one-line rule.
    """
    sentences = [x for x in split_sentences(text or "") if x.strip()]
    if len(sentences) < 2:
        return text
    kept = [sentences[0]]
    if len(kept[0].split()) < 9 and len(sentences) > 1:
        kept.append(sentences[1])
    out = " ".join(kept).strip()
    return out if len(out.split()) >= 5 else text


_DRAFTED_THIS_SESSION: list[str] = []


def remember_draft(text: str) -> None:
    """Record a draft the moment it is produced, not when the DB catches up.

    Two live posts came back one word apart — "the real fight's over who
    controls the data" and "who owns the data" — because the repeat gate reads
    a corpus cached for 15 seconds and both were generated inside that window.
    A draft has to be visible to the next draft immediately.
    """
    body = (text or "").strip()
    if body:
        _DRAFTED_THIS_SESSION.append(body)
        # A SHORT WINDOW ON PURPOSE. At 40 entries this killed generation
        # outright: after a run of testing, every new draft echoed something in
        # the backlog and 15 of 40 candidates died on copied_voice_example, so
        # the composer got nothing at all. The job here is to stop back-to-back
        # repeats — his published archive, checked separately, is what guards
        # against repeating himself over time.
        del _DRAFTED_THIS_SESSION[:-5]


# Roundup and calendar titles. "February 2026's AI model releases" is an
# article ABOUT a month, not an event — grounding a post on it produces
# "February 2026's AI model releases prove competition is driving innovation",
# which reads like a report on a calendar.
_ROUNDUP_TITLE_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{4}\b"
    r"|\b(?:this|last|next)\s+(?:week|month|year)\b"
    r"|\b(?:top|best|every|all)\s+\d+\b"
    r"|\bround-?up\b|\bnews today\b|\bwhat'?s new in\b"
    # NEWSLETTER DIGESTS. A daily-digest issue lists a dozen unrelated
    # stories, so a post grounded on one is a post about a newsletter:
    # "[AINews] Fearing RSI: OpenAI, Anthropic, GDM, Meta cosign letter" and
    # "Import AI 465: Open vs closed gaps; Kimi K3; Demis' policy plan" both
    # scored NOTABLE on 2026-07-29 while probing new sources, because they
    # name every lab at once. The shape is the tell: a bracketed newsletter
    # tag, or a masthead followed by an issue number.
    r"|\[\s*ai\s*news\s*\]|\bimport ai \d+\b|\bissue #?\d+\b"
    r"|^\s*\w[\w ]{0,20}\s#\d+\b",
    re.IGNORECASE)


def is_roundup_topic(topic: str) -> bool:
    return bool(_ROUNDUP_TITLE_RE.search(str(topic or "")))


def names_nothing(text: str) -> bool:
    """True when the post never says WHAT it is about.

    Live, after the one-point cut landed, three of ten read like this:

        "The project's approach to reducing token consumption through
         deterministic execution is undervalued in the AI community."
        "AI models still struggle to balance clarity and creativity."
        "The current AI models still fail to deliver clean, coherent writing."

    Each is one clear sentence — the cut worked — and each is about nothing in
    particular. "The project", "AI models", "the AI community": a reader cannot
    tell which project, which models, or act on any of it. A post has to name a
    thing: a product, a company, a version, or a number.
    """
    body = fold_punctuation(text or "")
    if not body.strip():
        return True
    if re.search(r"\d", body):
        return False                       # a figure is a specific
    # A proper noun anywhere other than the opening word of a sentence.
    for sentence in split_sentences(body):
        words = sentence.split()
        for word in words[1:]:
            bare = word.strip(".,!?:;'\"()")
            if len(bare) > 2 and bare[0].isupper() and bare.lower() not in _GRAMMAR_WORDS:
                return False
    # Or a first word that is itself a known subject ("Midjourney ships...").
    from .subject import subjects
    return not subjects(body)


# THREE LONG CONTENT WORDS IN A ROW with no small word between them. English
# uses short function words as joints; a run without any is a compressed noun
# phrase — "coding loop dominance" — grammatical and unsayable. My first
# attempt required specific noun suffixes and missed its own motivating
# example, because "loop" and "cost" have none.
_STACKED_NOUNS_RE = re.compile(r"\b[a-z]{5,}\s+[a-z]{4,}\s+[a-z]{5,}\b",
                               re.IGNORECASE)


def _has_noun_pileup(text: str) -> bool:
    """True when three long words run together with no joint between them."""
    for match in _STACKED_NOUNS_RE.finditer(fold_punctuation(text or "")):
        words = match.group(0).split()
        if any(w.lower() in _GRAMMAR_WORDS for w in words):
            continue            # a joint means it is a real clause
        if any(w.lower().endswith(("ed", "es", "ing")) and len(w) > 6
               for w in words):
            continue            # a verb means it is a real clause
        return True
    return False


def strip_formulas(text: str, *, min_survivors: int = 2) -> str:
    """Delete the stock constructions instead of asking the model not to write them.

    Three rounds of prompt rules did not stop these. The model produced a
    formula in EVERY candidate, so every candidate was flagged, and the
    near-miss path — which exists so a quality failure is not answered with
    silence — shipped one anyway. The reader saw the formula while the checker
    was technically working.

    They are all deletable. "That's not a trend. It's a model that scales
    faster than anything." loses nothing when the first sentence goes; the
    second was carrying the whole point. "If you're building with it, you're
    already ahead of the curve." carries nothing at all.

    Sentences are removed IN PLACE, so his line breaks survive. The first
    version rejoined on spaces and rewrote 15 of his 34 published posts into
    one block — the sentences were fine, the paragraphs were the casualty.

    Never cuts below two sentences or 15 words; if too little would survive the
    text is returned untouched and the gates deal with it.
    """
    if not text:
        return text
    # THE CONTEXT-FREE SUBSTITUTIONS RUN FIRST, and that ordering is the whole
    # point. Both used to sit below the span-count guard, so on any draft of
    # fewer than three sentences they never ran at all — which is most replies
    # and a good number of posts. `_COMMA_TEMPLATE_RE` in particular exists for
    # a construction that is ONE sentence ("That's not a guess, it's in the
    # fresh data."), so the guard was skipping precisely the drafts it was
    # written for. Measured: it matched all three of its own documented
    # examples and changed none of them.
    #
    # A bare capitalised "Name" mid-sentence is never something he wrote — it
    # is the model echoing the instruction "Name only ones in the evidence"
    # with no real name to put there ("MiniMax's 2.7 trill Name").
    text = re.sub(r"(?<=[a-z0-9])\s+Name\b", "", text)
    # Keep the half that says something, drop the setup. Both subject forms:
    # "That's not a price, it's a limit" and "Moe's not a card, it's a switch".
    for pattern in (_COMMA_TEMPLATE_RE, _COMMA_TEMPLATE_NOUN_RE):
        text = pattern.sub(lambda m: m.group("payoff")[0].upper()
                           + m.group("payoff")[1:], text)
    spans = [(m.start(), m.end(), m.group(0))
             for m in re.finditer(r"[^.!?\n]+[.!?]*", text) if m.group(0).strip()]
    if len(spans) < 3:
        return text
    # THE TEMPLATE CROSSES THE SENTENCE BOUNDARY, so per-sentence checks can
    # never see it whole: "…but it's not just about speed." ends the sentence
    # and "It's about who gets to control the flow." opens the next, and
    # neither half alone matches the register gates that catch the pair.
    # Measured surviving this scrubber untouched and shipping (flagged, still
    # shipped) three times on 2026-07-27. So the construction is located on
    # the WHOLE text — where `_NOT_JUST_RE`/`_ABOUT_TEMPLATE_RE` already sit
    # at 0/35 on his archive — and every sentence its span overlaps is dropped.
    # Newlines count as spaces here — length-preserving, so the spans still
    # align with `text`. The shaper's fourth strike: it split the construction
    # across a BLANK LINE ("…not just writing code anymore.\n\nThey're
    # debugging it, live.") and the sentence-shaped character class in the
    # register gates cannot cross \n, so the whole-text scan went blind the
    # moment the formatter touched the draft. Measured shipping exactly once
    # before this line existed.
    folded_all = fold_punctuation(text).replace("\n", " ")
    # The +40 window past the match matters: "isn't just cost. It's about who
    # uses it" carries its "about" AFTER the continuation pronoun the regex
    # stops at. _ABOUT_TEMPLATE_RE needs no check — "about" is its anchor.
    construction_spans = [
        m.span() for m in _NOT_JUST_RE.finditer(folded_all)
        if _not_just_is_template(folded_all[m.start():m.end() + 40])]
    construction_spans += [m.span() for m in _ABOUT_TEMPLATE_RE.finditer(folded_all)]
    construction_spans += [m.span() for m in _SELF_ANSWER_RE.finditer(folded_all)]
    drop: list[tuple[int, int]] = []
    survivors = 0
    for begin, finish, raw in spans:
        bare = fold_punctuation(raw).strip()
        lowered = bare.lower()
        if any(begin < c_end and finish > c_start
               for c_start, c_end in construction_spans):
            drop.append((begin, finish))
            continue
        if (_SETUP_SENTENCE_RE.match(bare) or _ABOUT_SETUP_RE.match(bare)
                or _ISNT_JUST_SENTENCE_RE.match(bare)
                or _NOUN_SETUP_RE.match(bare)
                # THE ANCHORED PATTERNS MISS THE EMBEDDED FORM. The four
                # `.match` checks above want the construction to OPEN the
                # sentence, and the live one arrives mid-clause: "The new
                # feature lets agents work together, but it's not just about
                # speed. It's about who gets to control the flow." — measured
                # surviving strip_formulas untouched, then shipping flagged as
                # an ai_tell three separate times on 2026-07-27. The register
                # gates (`_NOT_JUST_RE`, `_ABOUT_TEMPLATE_RE`) already SEE it
                # anywhere in a sentence and sit at 0/35 on his archive; this
                # makes the scrubber delete what they detect, which is this
                # module's founding rule.
                or (_NOT_JUST_RE.search(bare) and _not_just_is_template(bare))
                or _ABOUT_TEMPLATE_RE.search(bare)
                or _FILLER_RE.search(bare)
                # Hype that evaluates nothing. Split out of `_FILLER_RE` after
                # it deleted his own first line; see `_is_uncorroborated_hype`.
                or _is_uncorroborated_hype(bare)
                or _MIRRORED_YOU_RE.search(bare)
                or any(tell in lowered for tell in _SENTENCE_KILLERS)):
            drop.append((begin, finish))
        else:
            survivors += 1
    # A bare capitalised "Name" mid-sentence is never something he wrote. It is
    # the model echoing the instruction "Name only ones in the evidence" when it
    # has no real name to put there, and it shipped twice: "MiniMax's 2.7 trill
    # Name", "Moonshot AI launched Name". The prompt wording is fixed; this is
    # the backstop for when it recurs under a different phrasing. Runs whether
    # or not any sentence is being dropped — it was originally placed after the
    # early return below and therefore almost never ran.
    # DROP THE PAYOFF WITH ITS SETUP. "Opus 5's sloppiness isn't a bug." then
    # "It's a feature." — cutting only the first leaves a fragment that then
    # got its "It's" stripped as a dangling opener and shipped as "A feature."
    # Neither half carries information alone; they are one construction.
    if drop:
        dropped_ends = {finish for _, finish in drop}
        for index, (begin, finish, raw) in enumerate(spans):
            if (begin, finish) in drop:
                continue
            previous = spans[index - 1] if index else None
            if not previous or previous[1] not in dropped_ends:
                continue
            bare = fold_punctuation(raw).strip()
            if _BACK_REFERENCE.match(bare) and len(bare.split()) <= 5:
                drop.append((begin, finish))
                survivors -= 1
        drop.sort()

    # A REPLY IS ALLOWED TO BECOME ONE SENTENCE (min_survivors=1).
    #
    # "Three users bought it in the first hour? That's not a feature. It's a
    # validation." shipped under a post from somebody celebrating their first
    # paying customers. The surgery worked perfectly — both halves of the
    # construction were correctly marked for removal — and then this guard
    # vetoed the whole cut, because only one sentence would have been left.
    #
    # Two sentences is a sensible floor for a post. A reply's measured shape on
    # this account is ONE block of about 14 words, so on that surface the guard
    # fires on nearly every draft that needs it, and the formula ships intact.
    effective_min = min_survivors
    if construction_spans and survivors >= 1:
        # THE SAME VETO, ONE FLOOR UP. When the cut is a register CONSTRUCTION
        # — both halves marked, neither carrying anything — holding out for
        # two survivors re-ships the formula whole. That is exactly how
        # "That's not a feature. It's a validation." went out on the reply
        # surface before min_survivors=1 landed there, and how "not just
        # writing code anymore. / They're debugging it, live." went out on the
        # post surface tonight. One real surviving sentence is this account's
        # best-measured shape (4.45x engagement), not a failure state.
        effective_min = 1
    if not drop or survivors < effective_min:
        return text
    out = text
    for begin, finish in reversed(drop):
        out = out[:begin] + out[finish:]
    # Tidy what the removal left behind, without touching paragraph breaks.
    out = re.sub(r"[ 	]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    out = re.sub(r"^[ 	]+", "", out, flags=re.MULTILINE)
    # A floor of 15 words vetoed the surgery on exactly the drafts that needed
    # it: cutting the formula from a three-sentence post leaves two real
    # sentences and about twelve words, so the original shipped with the
    # formula intact. Two complete sentences is not a fragment — his own
    # archive has posts shorter than this.
    # A SETUP LEFT WITHOUT ITS PAYOFF. Cutting a formula can strand the line
    # that introduced it: one live post ended "...devs are racing to keep up.
    # The result?" with the answer removed. A trailing stub question is not an
    # ending, it is a dropped sentence.
    out = re.sub(r"(?:^|(?<=[.!?]))\s*[A-Z][^.!?]{0,40}\?\s*$", "", out).strip()
    if len(out.split()) < 6:
        return text
    # A survivor that now opens with a back-reference reads as a fragment:
    # "It's a model that scales faster", with its setup gone. Lift it.
    out = re.sub(r"^(?:it'?s|that'?s)\s+", "", out, count=1, flags=re.IGNORECASE)
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


# Phrases that condemn the WHOLE sentence containing them, not just themselves.
# Kept apart from _AI_TELLS, most of which appear mid-sentence in lines that are
# otherwise worth keeping.
# The mirrored second-person formula, in every wording. A literal list could
# not keep up: "if you're building with it, you're ahead of the curve" was
# banned, so the next run produced "if you're coding, you're paying for a tool",
# "if you're building, you're already past the demo phase", "if you're building
# with it, you're not getting a fresh start". Same empty machine, new nouns.
#
# The tell is the MIRROR — the clause repeats "you're" and lands on a state
# rather than a fact. "If you're making tools, this changes the math" does not
# mirror and survives, because it actually says something.
_MIRRORED_YOU_RE = re.compile(
    # "If you build with it, you're not just using a tool" escaped the version
    # that required the contraction in BOTH clauses. The mirror is what makes
    # it empty, and the mirror survives any conjugation of the first half.
    r"\bif you(?:'?re|\s+are)?\s+\w+[^.!?]{0,48}\byou'?re\b", re.IGNORECASE)

_SENTENCE_KILLERS = (
    "if you're building with it", "if youre building with it",
    "you're stuck with", "youre stuck with", "no real progress",
    "just more of the same", "ahead of the curve", "the next big thing",
    "getting left behind", "build now", "don't wait", "stay tuned",
    "the real fight is over", "changes everything",
)


def _own_writing() -> list[str]:
    """EVERYTHING this account has written OR Starling has drafted.

    The gate used to compare a draft against the five examples handed to the
    model — while the archive holds 33 posts. A draft rewriting any of the
    other 28 was invisible to it, which is precisely how "Why is Anthropic so
    quiet about Opus 5" came back as a new idea. What matters is whether HE
    already published this, not whether the prompt happened to include it.
    """
    global _OWN_WRITING_CACHE
    now = time.monotonic()
    if _OWN_WRITING_CACHE and now - _OWN_WRITING_CACHE[0] < _OWN_WRITING_TTL_S:
        # Session drafts are appended OUTSIDE the cache. Adding them to the
        # cached list meant they were only ever seen on a cache miss, so a
        # 15-second window produced exact duplicate posts — 1 and 6, 2 and 7,
        # 3 and 8 were identical in one live batch of ten.
        return [*_OWN_WRITING_CACHE[1], *_DRAFTED_THIS_SESSION]
    texts: list[str] = []
    try:
        texts.extend(str(row["text"]) for row in performance_records() if row.get("text"))
    except Exception:
        pass
    try:
        texts.extend(str(text) for text in learning_examples() if text)
    except Exception:
        pass
    # And everything Starling itself has already drafted. The repeat he caught
    # was never posted and never archived — it existed only here.
    try:
        texts.extend(previously_drafted_texts())
    except Exception:
        pass
    _OWN_WRITING_CACHE = (now, texts)
    return [*texts, *_DRAFTED_THIS_SESSION]


def _echo_is_the_writers_own(lowered: str, request, examples: list[str]) -> bool:
    """An ENHANCE rewrites the customer's own text. When that text is itself one of their past
    posts or an earlier Starling draft (they pressed Create, then Enhance), the rewrite echoes it
    because the customer's words do: not a copy. The rewrite is still checked against everything
    the customer's own draft does not already echo."""
    original = str(getattr(request, "original_draft", "") or "").strip().lower()
    if not getattr(request, "enhancement_mode", False) or not original:
        return False
    corpus = [*examples, *_own_writing()]
    others = [text for text in corpus if not _echoes_own_writing(original, [], corpus=[text])]
    if len(others) == len(corpus):
        return False
    return not _echoes_own_writing(lowered, [], corpus=others)


def _echoes_own_writing(lowered: str, examples: list[str],
                        corpus: list[str] | None = None) -> bool:
    """Is this draft a rewrite of something the account already posted?

    `corpus` overrides the live body of writing. Measurement tools need it: a
    draft is recorded the moment it is generated, so a judge that re-reads the
    corpus afterwards finds the draft matching itself.
    """
    draft_words = re.findall(r"[a-z0-9']+", lowered)
    if len(draft_words) < 6:
        return False
    # Compare against the WHOLE body of his own writing, with the prompt's
    # examples included (they may carry strategy exemplars the archive does not).
    examples = [*examples, *(_own_writing() if corpus is None else corpus)]
    draft_runs = {" ".join(draft_words[i:i + _COPY_RUN_WORDS])
                  for i in range(len(draft_words) - _COPY_RUN_WORDS + 1)}
    draft_tokens = set(re.findall(r"[a-z0-9]{3,}", lowered))
    for example in examples:
        example_lower = example.lower()
        example_words = re.findall(r"[a-z0-9']+", example_lower)
        shared: set[str] = set()
        if len(example_words) >= _COPY_RUN_WORDS:
            runs = {" ".join(example_words[i:i + _COPY_RUN_WORDS])
                    for i in range(len(example_words) - _COPY_RUN_WORDS + 1)}
            shared = draft_runs & runs
        overlap = 0.0
        tokens = set(re.findall(r"[a-z0-9]{3,}", example_lower))
        if tokens and draft_tokens:
            overlap = len(draft_tokens & tokens) / len(draft_tokens | tokens)
        # ONE shared phrase is the subject's own wording, not a copy: "opus 5 at
        # half the price" is how the launch was announced, and a draft that
        # borrows that fact while going somewhere new ("...changes the
        # build-vs-buy maths for small teams") is new writing. Measured against
        # this account: genuine rewrites of past drafts share 3-5 phrases,
        # genuinely new writing on the same beat shares 0-1. Overlap alone still
        # catches a full reword that borrows no phrase at all.
        if len(shared) >= _COPY_MIN_RUNS or overlap >= _COPY_OVERLAP:
            return True
    return False


_RECENT_REPLIES_CACHE: tuple[float, list[str]] | None = None
_RECENT_REPLIES_TTL_S = 15.0


def _recent_replies() -> list[str]:
    """Replies already drafted, cached briefly.

    `_draft_quality_issues` runs per candidate and again on every revision, so
    an uncached query here is four to six round trips per generation for a list
    that cannot meaningfully change inside one. Same TTL and same reasoning as
    `_own_writing`.
    """
    global _RECENT_REPLIES_CACHE
    now = time.monotonic()
    if _RECENT_REPLIES_CACHE and now - _RECENT_REPLIES_CACHE[0] < _RECENT_REPLIES_TTL_S:
        return _RECENT_REPLIES_CACHE[1]
    try:
        texts = previously_drafted_replies()
    except Exception:                                       # noqa: BLE001
        texts = []
    _RECENT_REPLIES_CACHE = (now, texts)
    return texts


_PUBLISHED_NAMES_CACHE: tuple[float, set[str]] | None = None
_PUBLISHED_NAMES_TTL_S = 300.0


def _published_names() -> set[str]:
    """Every name HE has published — his own beat, for the own-thread name gate.

    DO NOT BUILD THIS ON `_own_writing()`. That returns everything he wrote OR
    STARLING DRAFTED, and the draft half carries 126 names he has never once
    used — `ocr4`, `dharmaocr`, `animsequence`, `soundcues`, `qwen3-1.7b`,
    mostly arena residue. Feeding those to a name gate is the gate learning to
    accept its own hallucinations, permanently.

    `performance_records()` is his posts and already drops `deleted_at` rows:
    a post he disowned should not keep vouching for its names, and his deletion
    declarations only started landing once the `dleted` typo reached
    `mark_post_deleted`.

    Cached because `_reply_failures` runs per candidate and again on every
    revision — 3.2ms uncached is ~19ms per draft on the hot path. Same TTL and
    same reasoning as `_own_writing` and `_recent_replies`.
    """
    global _PUBLISHED_NAMES_CACHE
    now = time.monotonic()
    if (_PUBLISHED_NAMES_CACHE
            and now - _PUBLISHED_NAMES_CACHE[0] < _PUBLISHED_NAMES_TTL_S):
        return _PUBLISHED_NAMES_CACHE[1]
    names: set[str] = set()
    try:
        from .reply_plan import named_things
        from .storage import performance_records
        for row in performance_records():
            text = str(row.get("text") or "").strip()
            if text:
                names |= named_things(text)
    except Exception:                                       # noqa: BLE001
        names = set()          # fallback KEEPS the gate at its old strictness
    _PUBLISHED_NAMES_CACHE = (now, names)
    return names


# THE READER-VALUE FLOOR IS PER-SURFACE, because 0.25 was calibrated on posts
# and does not survive contact with a reply.
#
# Measured 2026-07-26 on this account, scoring everything as a reply:
#
#     his own short writing (23 texts, 4-30 words)  median 0.42, min 0.08
#     the BASELINE reply drafts (the bad ones)      median 0.29
#     the current reply drafts                      median 0.22
#
# The slop scores HIGHER than the drafts, which is the whole story: on a
# ten-word reply the signal terms (a stake, a position, a mechanism, a figure)
# have nowhere to accumulate, so the score mostly measures LENGTH. At 0.25 it
# rejected five of his own twenty-three short texts — including
# "this is the first time the 'Jarvis' comparison feels legit" (0.15) and the
# reply he singled out as the good one, "Claude Code is slowly turning into an
# entire dev team inside the terminal" (0.22).
#
# 0.15 keeps 22 of his 23. It is a weak discriminator on this surface and it is
# not pretending otherwise — the real work on a reply is done by the gates that
# ARE calibrated for it (leaked_into_reply, copies_the_post, bounced_question,
# analyst_voice, restates_the_source). What this floor still catches is the
# genuinely contentless one-liner.
#
# The post floor is untouched: there it separates cleanly (slop 0.00-0.24, his
# writing 0.26-0.83) and it is the other lane's calibration.
_REPLY_PAYOFF_FLOOR = 0.15
# THE POST FLOOR WAS SET FROM AN ASSERTION, NOT A MEASUREMENT. The comment
# above claims it "separates cleanly (slop 0.00-0.24, his writing 0.26-0.83)".
# Measured against the 35-post fixture on 2026-07-27 that is not true — the
# share of HIS OWN posts below each candidate floor is:
#
#     0.15   4/35  11%        0.22   8/35  23%
#     0.20   4/35  11%        0.25   8/35  23%   <- the value in use
#
# Four of his posts sit at exactly 0.210, and they include the two this
# codebase elsewhere names as his good ones: "this is the first time the
# 'Jarvis' comparison feels legit" and "Claude Code is slowly turning into an
# entire dev team inside the terminal". A floor that flags those is measuring
# the wrong thing, and 23% is over the 20% a revise trigger is allowed anyway.
#
# 0.20 sits in the flat part of the distribution — everything between 0.15 and
# 0.20 is identical, so this is a natural break rather than a number chosen to
# hit a target. It still catches the genuinely contentless one-liner.
_POST_PAYOFF_FLOOR = 0.20


def _payoff_floor(action: str | None) -> float:
    return _REPLY_PAYOFF_FLOOR if action == "reply" else _POST_PAYOFF_FLOOR


def _reply_ranking_key(candidate, objective: str) -> tuple[bool, float, float]:
    """Order reply candidates by whether they give the reader anything, first.

    GrowthScore judges shape, and a fluent restatement of the post has perfect
    shape: it fits the format, it aligns, it has a hook. So it cannot see the
    one fault that matters most on this surface. Measured over a 22-reply run,
    drafts under the payoff floor averaged 67.2 and drafts over it averaged
    68.0 — the scorer is, for this purpose, blind.

    Payoff therefore leads and GrowthScore breaks the tie inside each tier,
    which leaves the existing order alone except when the candidates actually
    differ on the floor — the case where the old key picked the empty one.
    """
    from .reader_value import reader_value
    return (not _weak_reply(candidate.text),
            candidate.growth_score if candidate.growth_score is not None else -1.0,
            _candidate_rank(candidate, objective))


def _evidence_of(request: DraftRequest) -> str:
    """The web facts this draft was given, wherever they travel.

    W2 (2026-09-21) moved the facts out of the 1,200-character `direction` into
    their own field (`DraftRequest.facts`), because the reply plan alone filled
    the direction and pushed them out. Older producers still carry them inside
    the direction after `_EVIDENCE_MARKER`, so that stays the fallback. None on
    `facts` means "this request predates the field", never "no facts".
    """
    facts = getattr(request, "facts", None)
    if facts is not None and str(facts).strip():
        return str(facts).strip()
    direction = str(getattr(request, "direction", "") or "")
    return direction.split(_EVIDENCE_MARKER, 1)[1].strip() if _EVIDENCE_MARKER in direction else ""


def _direction_with_facts(request: DraftRequest) -> str:
    """The direction as the gates used to read it: with the facts after the marker (W2)."""
    direction = str(getattr(request, "direction", "") or "")
    facts = str(getattr(request, "facts", "") or "").strip()
    if facts and _EVIDENCE_MARKER not in direction:
        return f"{direction} {_EVIDENCE_MARKER} {facts}".strip()
    return direction


def _conversation_text(request: DraftRequest) -> str:
    """Everything the reader can already see: the post, its parent, any quote."""
    return " ".join(part for part in (
        request.context.text or "",
        getattr(request.context, "parent_text", "") or "",
        getattr(request.context, "quote_text", "") or "") if part).strip()


def _reply_failures(text: str, request: DraftRequest) -> list[str]:
    """The ways a REPLY fails that a post cannot, so nothing else checks them.

    All four were live on 2026-07-26 with every existing gate green:

      leaked_into_reply  "I can't wait to see how Fable 5 handles a trip plan
                         with 8 friends" under a post about a weekend trip.
                         `_injected_entities` runs on every draft and cannot
                         catch this, because the corpus it is given includes
                         the voice examples — his own archive — so every lab
                         name in his 33 posts is permanently allowed. Measured:
                             _injected_entities(draft, parent)            -> ['fable']
                             _injected_entities(draft, parent + examples) -> []
                         Here the allowed set is the conversation plus verified
                         evidence, and nothing else.

      bounced_question   "What rig are you running it on?" -> "You're running
                         it on a GPU?". He was asked; the reply asks back.

      strawman_opener    "So you're saying Qwen's not the bottleneck?" to a post
                         whose own second clause answers that. Six of twenty-six
                         drafts opened this way; it is a tic, not a style.

      repeated_reply     "What's the catch?" drafted four separate times.
                         `_echoes_own_writing` wants two shared six-word runs
                         and a twelve-word reply contains seven runs in total,
                         so it never fires on this surface.

    The room is recomputed here rather than threaded through `DraftRequest`.
    It is pure regex over text the request already carries, so recomputing costs
    nothing and avoids widening the schema, the browser contract and every test
    that builds a request.
    """
    from .reply_plan import (bounces_the_question, copies_the_post,
                             engages_a_specific, has_no_position, intruders,
                             opens_with_a_tic, read_the_room, repeats_a_reply,
                             says_it_back, sounds_like_an_analyst,
                             talks_about_the_post)

    direction = str(getattr(request, "direction", "") or "")
    # The evidence really did verify these names, so they are not intrusions.
    evidence = _evidence_of(request)
    try:
        room = read_the_room(request.context, evidence=evidence)
    except Exception:                                       # noqa: BLE001
        return []

    issues: list[str] = []
    # ON HIS OWN THREAD, A NEW NAME IS THE POINT — the continue brief asks for
    # "the NEXT thought: the follow-up, the detail you left out", and `intruders`
    # rejects any name the parent paragraph lacks. Measured on 120 continuation
    # pairs from his own archive: the gate fires on 50% of his own writing, and
    # 0/3 on his published stranger-replies. "Now let's see how OpenAI responds
    # with ChatGPT" was rejected for naming OpenAI.
    #
    # WIDENED, NOT SKIPPED. Skipping leaves the surface with NO name gate at all
    # — `apply_support_check` excludes replies by design (a reply's evidence is
    # the conversation), so nothing else looks at names here. Widening to his
    # published vocabulary took the false-positive rate 50% -> 0% while still
    # blocking every planted outside-beat name (Coca-Cola, Goldman Sachs,
    # Peloton). Threshold is "anything he has published", once: it strictly
    # dominated appears-in-2+ (0% vs 2.5% false positives, same catches).
    #
    # Filtering the RETURNED names rather than widening `room.allowed`: the gate
    # already hands back exactly which names it objected to, so the own-thread
    # rule is one set difference and the Room stays untouched for every other
    # check that reads it.
    if room.move == "continue":
        if [n for n in intruders(text, room) if n not in _published_names()]:
            issues.append("leaked_into_reply")
    elif intruders(text, room):
        issues.append("leaked_into_reply")
    if copies_the_post(text, room):
        issues.append("copies_the_post")
    if bounces_the_question(text, room):
        issues.append("bounced_question")
    if opens_with_a_tic(text):
        issues.append("strawman_opener")
    if has_no_position(text):
        issues.append("no_position")
    if sounds_like_an_analyst(text):
        issues.append("analyst_voice")
    if talks_about_the_post(text):
        issues.append("talks_about_the_post")
    # THEIR POST REARRANGED. Raised as `restates_the_source` on purpose rather
    # than as a flag of its own: the sentence-level check above already owns
    # that name, and it already agrees with all four registries — the revise
    # set, the model's guidance and the note on his card. A second flag for the
    # same fault would double-flag one sentence and repair neither (the
    # `names_nothing` lesson). This only widens what raises it, to the fused
    # and short-fragment echoes that measure cannot see. Reply-only: the shared
    # gate below still serves posts and quotes unchanged.
    if "restates_the_source" not in issues and says_it_back(text, room):
        issues.append("restates_the_source")
    # DID IT READ THE POST. His complaint about the Tibo reply — "no attention
    # to detail like when tibo said codexy" — and measured across a full run,
    # eight of twenty-seven replies engaged nothing the post actually put on
    # the table: under "$12.40 down to $0.90" the reply was "a major shift in
    # compute efficiency".
    #
    # A GATE, not just a note, and that is the point of putting it here. Two
    # candidates are generated per reply; a fault the gate can see is a fault
    # the selection can route around, so this turns "one of the two engaged the
    # post" from luck into the one that ships.
    if not engages_a_specific(text, room):
        issues.append("ignores_the_post")
    prior = _recent_replies()
    # The draft being judged is recorded before this runs, so it matches
    # itself. Compare against everything EXCEPT an exact copy of this text.
    # DROP ONE COPY, NOT EVERY COPY. This draft is recorded before the check
    # runs, so its own row has to come out or it matches itself. Removing every
    # equal entry took the previous press's draft out too — so an EXACT
    # duplicate became the one thing this gate could not see, while everything
    # merely similar was caught. Measured live 2026-07-27: two of fourteen
    # posts returned a byte-identical reply on the second press, both shipped,
    # both flagged as repeats by the offline scorer that has no such filter.
    prior = _drop_own_record(prior, text)
    # The post goes in so that saying its name is not mistaken for repeating
    # himself — see `repeats_a_reply`. Every reply to the Opus 5 announcement
    # says "Opus 5"; that is the subject, not a rut.
    if prior and repeats_a_reply(text, prior, source=_conversation_text(request)):
        issues.append("repeated_reply")
    return issues


def _draft_quality_issues(text: str, request: DraftRequest, examples: list[str]) -> list[str]:
    """Reject weak model output before it is presented as the recommended move."""
    # Fold curly punctuation for MATCHING only — `text` itself is untouched, so
    # his typography survives while the gates below stop being blind to it.
    lowered = re.sub(r"\s+", " ", fold_punctuation(text).lower()).strip()
    words = re.findall(r"[a-z0-9']+", lowered)
    issues: list[str] = []
    # A LOWER FLOOR ON THE SHITPOST REGISTER, NOT NO FLOOR. His shortest real
    # shitposts are 3 tokens ("is this true?") and 4 ("Ik like this one",
    # "No ai imagine tho"), so a 5-token floor rejects his own writing on the
    # register whose median is ten words. Three is still a floor, and
    # `subject_absent` covers "three words about nothing" from the other side.
    #
    # NOTE THE TOKENISER, because the design's table and this gate disagree:
    # `words` here is `re.findall(r"[a-z0-9']+")`, so "@grok roast Grok 4.5"
    # is FIVE tokens (the decimal splits) and never fired. Counting with
    # `.split()` makes it four and looks like a fourth rescued post.
    _floor = 3 if getattr(request, "post_register", None) == "shitpost" else 5
    if len(words) < _floor:
        issues.append("too_short")
    generic = ("so true", "this", "agree", "exactly", "well said", "facts", "100%")
    if lowered.rstrip(".! ") in generic or (len(words) < 12 and any(lowered.startswith(item) for item in generic)):
        issues.append("generic_agreement")
    issues.extend(_readability_issues(text))
    if _EXPLAINER_SCAFFOLD.search(text):
        issues.append("explainer_scaffolding")
    if _contradicts_itself(text):
        issues.append("contradicts_itself")
    if _FILLER_RE.search(lowered):
        issues.append("empty_filler")
    # A POST THAT IS ABOUT NOTHING IN PARTICULAR. "AI models still struggle to
    # balance clarity and creativity" is one clean sentence about no product,
    # no version and no number, and a reader cannot act on it. Measured at 6%
    # against his 35 published posts, so it is safe to fire — but it is a
    # QUALITY bar, not an integrity one, which is why it lives here and not in
    # the reject-outright block in `_assemble_candidates`. A named draft still
    # outranks it; it only ever ships when nothing better came back, and then
    # with the fault named. Posts only: a quote or reply sits under the thing
    # it is about, so the reader already has the subject on screen.
    # NOT ON THE SHITPOST REGISTER. Measured on his 65 hand-read posts, this
    # fires on 11 of his 31 real shitposts (35%) — "bro i thought you in jail",
    # "is this true?", "Ik like this one". A register whose median is ten words
    # and whose job is a one-line aside names a product about a third of the
    # time, and the gate was calibrated at "6% against his 35 published posts"
    # on a corpus that structurally excluded the tail where that register lives.
    #
    # THE REPLACEMENT IS ALREADY WIRED AND STAYS UNCONDITIONAL: `dangles_its_subject`
    # asks the sharper question — does the draft share any word with its
    # assigned topic — which a from-scratch post can be held to and a caption
    # on an image cannot.
    if (request.action == "post" and names_nothing(text)
            and getattr(request, "post_register", None) != "shitpost"):
        issues.append("names_nothing")
    # DELIVERED FAR UNDER THE PLANNED SIZE. The retry door already buys more
    # shots (size_was_delivered); this is for the survivor when supply dies —
    # measured 2026-07-31, sixth batch over one story: every longer phrasing
    # sat in the repeat corpus, retries were killed as duplicates, and a
    # 7-word draft shipped against a 24-word plan with nothing on the card.
    # Fires only when the plan asked >=16 words and the draft delivered under
    # half; a planned short post is his voice and never flagged.
    if request.action == "post":
        try:
            from .shape import asked_words
            wanted = asked_words(str(getattr(request, "direction", "") or ""))
            if wanted and wanted >= 16 and len(text.split()) < wanted * 0.5:
                issues.append("under_planned_size")
        except Exception:                                # noqa: BLE001
            pass
    # A POST OR QUOTE THAT ONLY ASKS. Replies are exempt: asking the person you
    # are answering a real question is a conversation, not an evasion.
    #
    # THE SHITPOST REGISTER IS EXEMPT FOR THE SAME STATED REASON. It fires on 4
    # of his 31 real shitposts — "is this true?", "How are u posting this then
    # what type of magic do the tates have" — and a one-line aside asking about
    # the WORLD is the same shape the reply exemption already allows. What it
    # may not do is buttonhole a stranger, and that is a different gate:
    # `talks_to_the_reader` stays ON for shitposts (0 of 31, its calibration
    # reproduces exactly on the subset) and its `_questions_the_reader` branch
    # still catches a question put AT the reader.
    if (request.action in ("post", "quote") and asks_without_saying(text)
            and getattr(request, "post_register", None) != "shitpost"):
        issues.append("asks_without_saying")
    # THE TWO SHITPOST-REGISTER SHAPE CHECKS. Only this register has them,
    # because only this register has a shape to violate — a fact post SHOULD
    # run to four sentences and forty words. Both are notes, not rejects: they
    # route through `_WEAKNESS_NOTES` and the draft ships with its fault named.
    if getattr(request, "post_register", None) == "shitpost":
        if explains_the_joke(text):
            issues.append("explains_the_joke")
        if overwrites_the_bit(text):
            issues.append("overwrites_the_bit")
    # A DRAFT THAT IS ONLY A CLAIM ABOUT THE READER. `drop_reader_address`
    # deletes a trailing block, which cannot help when the reader claim IS the
    # whole draft — live, quoting @levelsio's spend audit: "You're spending
    # like a king but still can't tell what half the money did." One sentence,
    # about a stranger, carrying no name and no figure. He addresses the reader
    # in 0 of 35 published posts, so this is measured at 0% against his
    # writing; requiring `names_nothing` too keeps a closer that is doing real
    # work ("You're paying 4x what Goose charges") shippable. Replies exempt:
    # there, "you" is the person being answered.
    if (request.action in ("post", "quote")
            and ((_READER_CLAIM_RE.search(fold_punctuation(text))
                  and names_nothing(text))
                 # A QUESTION AT THE READER is flagged even when it names
                 # things. "You're paying 4x what Goose charges" is a closer
                 # doing real work; "You think AI coding agents are helping?"
                 # is a stranger being buttonholed — five of nine drafts
                 # opened that way on 2026-07-28 once a question hook title
                 # reached the post prompt. His one archived reader-question
                 # is @-addressed ("@grok … don't you?") — a referent, so
                 # exempt.
                 or _questions_the_reader(fold_punctuation(text)))):
        issues.append("talks_to_the_reader")
    # A CLAIM ABOUT EVERYONE, BACKED BY NOTHING. Third-time offender across
    # three measured runs, and always the sentence that turns a grounded post
    # into an opinion column:
    #
    #   "Rust's 50% lower token use is real. But no one knows how to use it."
    #   "If xAI keeps this speed, they'll win the AI race before people notice."
    #
    # Nothing in the evidence supports a claim about what no one knows.
    # Calibrated at 1/35 on his archive (a recap post), well inside the 20%
    # a revise trigger is allowed.
    if _SWEEPING_CLAIM_RE.search(fold_punctuation(text)):
        issues.append("sweeping_claim")
    # Calibrated 0/71 on his writing — see _BIG_DEAL_RE.
    if _BIG_DEAL_RE.search(fold_punctuation(text)):
        issues.append("empty_big_deal")
    # THE PROMPT BANS HEDGING ON EVERY SURFACE; ONLY REPLIES CHECKED IT.
    # BASE_DRAFT_RULES is shared by post, reply and quote and says "Never end
    # on a hedge" and "Do not argue both sides. Pick one." The verifier for
    # exactly those two rules — `reply_plan.has_no_position`, calibrated to
    # zero matches across his 36 archived posts — was wired to the reply
    # surface alone. So "maybe it ships this quarter, maybe it doesn't —
    # either way, worth watching" is banned twice in the prompt, caught on
    # the surface nobody sees cold, and shipped unflagged on the two he
    # publishes to strangers.
    if request.action in ("post", "quote"):
        try:
            from .reply_plan import has_no_position
            if has_no_position(text):
                issues.append("no_position")
        except Exception:                                # noqa: BLE001
            pass
    # THE REGISTER RULES, ENFORCED. The style directive already bans dashes
    # and formal vocabulary and the model slips it — measured 2026-07-28,
    # drafts carried dashes at ten times his archive rate. One dash / a 30+
    # word sentence / stacked long words he never uses = one revise pass with
    # the rule named. FP on his archive: dash 3/39, the rest 0/39 — inside
    # the 20% a revise trigger is allowed.
    if request.action in ("post", "quote") and _off_register(
            text, _direction_with_facts(request)):
        issues.append("off_register")
    # The topic's own subject must appear when the draft opens on a pronoun.
    #
    # `direction[:200]` WAS ALWAYS BOILERPLATE. The post direction opens with
    # a fixed 207-character template ("Write one original standalone post for
    # this account today, in the writer's voice: …"), so the first 200
    # characters never reached the topic — the gate compared every draft
    # against instruction text, found no shared subject, and flagged roughly
    # one post in five with "this never names what it's about", spending a
    # revise call that could not possibly succeed and shipping a false reason.
    # The topic is quoted inside the direction; take that.
    if request.action == "post" and dangles_its_subject(
            text, _topic_from_direction(str(getattr(request, "direction", "") or ""))):
        issues.append("dangling_subject")
    # NO SECOND GATE HERE. `names_nothing` above already fires on both of the
    # 2026-07-30 arena defects — it was never blind, it was unrepairable. A
    # near-duplicate check would have double-flagged the same sentence on the
    # card and changed nothing. The fix is the revise path, not another gate.
    # The reader never saw the article. Posts and quotes only — a reply may
    # legitimately refer to a link the person it answers actually posted.
    if (request.action in ("post", "quote")
            and _DESCRIBES_SOURCE_RE.search(fold_punctuation(text))):
        issues.append("describes_the_source")
    # A NAME THE MODEL GARBLED. Live 2026-07-30: evidence said Goose, the
    # draft said "Goats is free for the same work today" — a false statement
    # about a product, and both existing nets are blind to it by design
    # (`_injected_entities` is alias-only, `_unsupported_claims` watches
    # figures). The check requires the mangled TWIN in the handed material,
    # which is what keeps it surgical: 1 flag in 1,499 historical candidates,
    # and it was this bug. See name_check.py for the calibration.
    if request.action in ("post", "quote"):
        try:
            from .name_check import mangled_names
            handed = " ".join((_direction_with_facts(request),
                               _conversation_text(request),
                               " ".join(examples or [])))
            if mangled_names(text, handed):
                issues.append("mangled_name")
        except Exception:                                # noqa: BLE001
            pass
    # SAYING THE HEADLINE BACK. Moved here from the reject-outright block,
    # where it was taking whole generations to zero: a reader who saw the
    # headline gets nothing from the headline again, but that is a weak post,
    # not a dishonest one, and it must not be answered with an empty composer.
    if _parrots_the_headline(text, _direction_with_facts(request)):
        issues.append("parrots_headline")
    # THREE ABSTRACT NOUNS IN A ROW. "GPT-5.6 Sol's cost efficiency and Codex
    # coding loop dominance" is grammatical and unreadable — nobody says
    # "coding loop dominance" out loud. A pileup like that is the model
    # compressing several facts into one phrase instead of picking one.
    # NOT WIRED. A "three long words with no joint" heuristic for noun pileups
    # flagged 15 of his 35 published posts and most good drafts — "Midjourney
    # v8.2 prioritizes aesthetics over stability" trips it. English simply puts
    # long words next to each other all the time. Detecting an unsayable phrase
    # needs to know what the words ARE, not how long they are, and a gate that
    # rejects 43% of his own writing is worse than the fault it was chasing.
    # _has_noun_pileup stays for reference; it does not gate anything.
    # Wandering is a REVISE trigger, not a kill. Calibrated against his own 34
    # published posts, the check flags 4 of them — those are real posts that
    # introduce a second name in the second sentence, and rejecting that shape
    # outright would reject him. One rewrite, then it ships or it doesn't.
    # ON AN ENHANCE THE SUBJECT IS HIS DRAFT, not the instruction text.
    #
    # `direction` is the right anchor for a CREATED post: it carries the topic and
    # the grounded evidence, which is what the sentences should stay near. On an
    # enhance it carries "sharpen the hook, tighten slack phrases, fix typos..."
    # — boilerplate that anchors nothing — so the only real anchor left was the
    # rewrite's own first sentence.
    #
    # Measured on a coherent three-sentence rewrite: sentence 2 and sentence 3
    # shared no exact word with sentence 1 ("prices" is not "pricing"), giving
    # 1/3 connected against a 0.34 bar, and the card said it "drifts away from
    # the post it answers partway through". His own draft is the honest anchor,
    # and using it neither loosens the threshold nor touches the calibration —
    # it just measures drift against the subject instead of against a to-do list.
    # W2: the plan and the facts left the direction for fields of their own, and
    # both are part of what the draft should stay on; a reply also stays on the
    # conversation it answers.
    thread_anchor = " ".join(part for part in (
        _direction_with_facts(request),
        str(getattr(request, "writing_brief", "") or ""),
        _conversation_text(request) if request.action in ("reply", "quote") else "") if part)
    if getattr(request, "enhancement_mode", False):
        thread_anchor = str(getattr(request, "original_draft", "") or "") or thread_anchor
    if _loses_the_thread(text, str(getattr(request, "topic", "") or ""), thread_anchor):
        issues.append("loses_the_thread")
    # Naming the wrong lab is the one error this audience always catches, and
    # it cannot be reworded into correctness — the draft is simply wrong. Two
    # of the 153 drafts on this machine carry it. `text`, not `lowered`: the
    # patterns are case-insensitive and want the original word boundaries.
    if entity_truth.misattributions(text) or entity_truth.internal_rivalry(text):
        issues.append("misattributed_model")
    if (request.action in ("reply", "quote") and request.context.text
            and not _shows_context_connection(lowered, request.context.text)):
        # It need not repeat the source's wording, but it must show topical connection.
        issues.append("missing_context_connection")
    # ...and the matching CEILING. The check above is a similarity floor, which
    # a paraphrase of the source clears perfectly — so the only enforced
    # constraint used to reward restating the post. A quote whose sentences are
    # the source's sentences again spends the account's distribution telling
    # the reader what they just read. Meaning, not wording, so it rejects
    # outright rather than going through the clarity-revise pass.
    # The SOURCE is the whole visible unit — a post that quotes another post
    # shows the reader both layers, so restating the QUOTED layer is just as
    # much a restatement (the quote-of-a-quote blindness in the registry, A6).
    # ...AND THE PARENT. The reader of a reply sees the post above it too, so
    # restating THAT is the same offence. It was not checked, and on his own
    # thread that means the draft could hand his own words back to the person
    # answering them — live, under "Running the 30B locally on one card, the
    # trick is the MoE routing, not the VRAM", the reply came back "It's a
    # single card with a MoE routing trick."
    #
    # `_source_mirroring` takes the MAX containment per draft sentence, so a
    # longer source can only raise the score — this is a strict addition, not a
    # dilution. Measured on that case: 0.21 -> 0.50 (blocked), while two
    # genuinely different replies to the same post stayed at 0.10 and 0.25.
    full_source = _conversation_text(request)
    # A FROM-SCRATCH POST HAS A SOURCE TOO — the web evidence it was grounded
    # in. Nothing ever checked it, because `context.text` is empty for a post,
    # so the whole restatement gate sat idle on exactly the surface that needed
    # it most. Measured against the live grounding: generated drafts restated
    # the evidence at 0.53-0.76 similarity while his own posts sit at
    # 0.00-0.08. He REACTS to news; the drafts REPEAT it, which is precisely
    # why a reader who saw the headline learns nothing from the post.
    if request.action == "post" and not full_source:
        full_source = _evidence_of(request)
    # AN ENHANCE HAS NO SOURCE TO RESTATE.
    #
    # `main` builds a synthetic context for a post enhance whose text IS his own
    # draft (main.py, the `else` beside `if creating:`), so `_conversation_text`
    # hands this gate his own words — and a rewrite of his words mirrors them by
    # construction. Measured on the real draft/rewrite pair: `restates_the_source`
    # fired every time, and the card told him "this leans on saying the post back.
    # Swap the opening for your own take before posting" — advice with no referent,
    # because there is no other post. It also set `needs_review`, so a clean
    # enhance always arrived looking suspect.
    #
    # A REPLY or QUOTE enhance is deliberately untouched: there the context really
    # is somebody else's post, and leaning on it is the fault it always was.
    if getattr(request, "enhancement_mode", False) and full_source:
        flat = lambda value: re.sub(r"\s+", " ", str(value or "")).strip().lower()
        own = flat(getattr(request, "original_draft", ""))
        if own and flat(full_source) == own:
            full_source = ""
    if (request.action in ("reply", "quote", "post")) and full_source:
        # PADDING HIDES AN ECHO. Whole-text similarity is diluted by whatever
        # follows: a draft that opened "Claude Opus 5 is here - our most capable
        # model yet, half the price of Opus 4.5." and then padded with invented
        # bullets scored 0.24 and sailed through, even though its FIRST LINE was
        # the source almost word for word. The opening line is what the reader
        # meets, so it is judged on its own as well as in the whole.
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        opens_with_echo = (len(first_line.split()) >= 6
                           and _source_mirroring(first_line, full_source) >= 0.6)
        if opens_with_echo or _source_mirroring(text, full_source) >= 0.5:
            issues.append("restates_the_source")
    if any(re.search(rf"\b{re.escape(phrase)}\b", lowered) for phrase in ("follow me", "repost if", "comment yes", "like and repost")):
        issues.append("engagement_bait")
    # THE READER'S QUESTION: why read this instead of scrolling? Nothing else
    # in the pipeline asks it — a draft could be perfectly in-voice, perfectly
    # formatted, and give a stranger no reason to stop. Treated as a clarity
    # issue so the revise pass gets one chance to add the missing substance.
    from .reader_value import reader_value
    payoff = reader_value(text, request.action)
    if payoff["score"] < _payoff_floor(request.action):
        issues.append("no_reader_payoff")

    # His personal banned register — a clarity issue, so the revise-once pass
    # rewords the sentence instead of killing an otherwise-sound draft.
    # _NOT_JUST_RE catches the CONSTRUCTION "not just X, it's Y" whatever the
    # contraction ("Apple's not just fast, it's got..." — seen live, G84).
    if (any(tell in lowered for tell in _AI_TELLS)
            or _NOT_JUST_RE.search(lowered)
            or _ABOUT_TEMPLATE_RE.search(lowered)
            or _THATS_NOT_RE.search(lowered)):
        issues.append("ai_tell")
    # Says nothing, loudly. Both conditions are required on purpose: this
    # account writes real short punchy posts carrying only one anchor, and they
    # must still pass. It is the COMBINATION — asserting significance while
    # naming nothing checkable — that has never once appeared in its archive.
    if empty_hype_hits(text) and len(_anchors(text)) <= 1:
        issues.append("says_nothing")
    # REWRITING HIS OWN OLD POST. This used to require the WHOLE past post to
    # appear verbatim inside the draft, which essentially never happens — so
    # the gate passed a draft that opened "Why is Anthropic so quiet about
    # Opus 5? It'll be significantly better than Fable and way cheaper", seven
    # words lifted straight from his July post with the tail reworded. Real
    # copying is partial, so measure it two ways that partial copying cannot
    # slip past: a long shared word-run, or heavy overall overlap.
    if _echoes_own_writing(lowered, examples) and not _echo_is_the_writers_own(lowered, request, examples):
        issues.append("copied_voice_example")
    # THE REPLY-ONLY FAULTS, folded in here rather than gated separately on
    # purpose. A first version of this rejected them outright and produced the
    # one outcome he has ruled out — "Nothing usable came back from the model
    # this time" — because all three candidates bounced the same question and
    # every one was thrown away. Routed through `quality_issues` they get what
    # every other quality miss gets: one revision pass, and failing that they
    # ship with the weakness named, which he can fix in five seconds. Only the
    # leak is an integrity failure (see _INTEGRITY_ISSUES) because naming a lab
    # the conversation never mentioned is a claim, not a wording problem.
    if request.action == "reply" and not getattr(request, "enhancement_mode", False):
        # DEDUPED, because the reply lane and the shared gate above can now
        # raise the same `restates_the_source` about the same sentence — the
        # sentence-level measure and the pooled one overlap by design. A
        # repeated issue is not a crash, it is the same fault counted twice in
        # the near-miss ordering and read out twice on his card.
        issues.extend(issue for issue in _reply_failures(text, request)
                      if issue not in issues)
    elif (request.action == "quote"
          and not getattr(request, "enhancement_mode", False)):
        # ONE CHECK FROM `_reply_failures`, NOT THE FUNCTION.
        #
        # WHY NOT JUST ADD "quote" TO THE ACTION TEST ABOVE — the question the
        # next reader will ask. `_reply_failures` raises EIGHT issues, and the
        # gate is on the container, so widening it enables all eight. Measured
        # on 22 real captured posts (2026-08-07): `intruders`, which raises
        # `leaked_into_reply`, fires on 4 of 22 quote drafts and THREE ARE
        # FALSE — "Iron Man" answering a post that said "Tony Stark", an
        # author's own surname, and "LotR" for a source that wrote "Lord of the
        # Rings". It would reject the best draft in that run. The other six are
        # simply unmeasured on this surface, and `bounced_question` cannot
        # apply: a quote has nobody to bounce a question back to.
        #
        # `copies_the_post` is the exception, and on a quote it matters MORE
        # than on a reply: the post being quoted is rendered directly
        # underneath, so a verbatim copy puts the duplicate above the original.
        # Live on 2026-08-07, quoting "i see your moore's law and i raise you
        # 20x", the draft came back as that sentence character-for-character —
        # the restatement gate named it and it shipped anyway, because nothing
        # on this surface treats copying as an integrity failure.
        #
        # Measured: 1 of 22, and zero false positives on the other 21.
        # It is in `_INTEGRITY_ISSUES`, so this REJECTS rather than annotating,
        # and `_REJECTION_REASONS` already carries the sentence for it.
        from .reply_plan import copies_the_post, read_the_room
        direction = str(getattr(request, "direction", "") or "")
        # Same evidence split the reply lane uses, so `allowed` is built the
        # way production builds it and not the way a harness would.
        #
        # THE ONE INPUT THAT WOULD BREAK THIS: `allowed` includes `evidence`,
        # so evidence containing the DRAFT's own words turns a 1-in-22 gate
        # into 11-in-22 (measured on the same 22 by feeding each draft in as
        # its own evidence). It is unreachable, and that was checked rather
        # than assumed, on both paths a draft could get back in:
        #
        #   `_EVIDENCE_MARKER` is READ-ONLY everywhere it appears — :5385,
        #   :5677 and :5798 are all `direction.split(...)`, extraction and
        #   never assembly. And the only mutation of `direction` in this
        #   module is a scaffold-tail TRUNCATION (`direction = direction[:cut]`)
        #   which shortens and never appends. So no retry path reintroduces a
        #   previous candidate either — the first-pass argument alone would not
        #   have covered that.
        #
        # The 1-in-22 is therefore a property of the gate, not of the
        # configuration it was measured in. Anything that ever writes generated
        # text back into the direction makes this reject half of everything.
        evidence = _evidence_of(request)
        try:
            room = read_the_room(request.context, evidence=evidence)
        except Exception:                                   # noqa: BLE001
            room = None
        if room is not None and copies_the_post(text, room):
            issues.append("copies_the_post")
    return issues


def _voice_score(text: str, examples: list[str]) -> float | None:
    if not examples:
        return None
    text_tokens = set(re.findall(r"[\w']+|[^\w\s]", text.lower()))
    example_tokens = set(re.findall(r"[\w']+|[^\w\s]", " ".join(examples).lower()))
    overlap = len(text_tokens & example_tokens) / max(1, len(text_tokens))
    return round(min(0.98, 0.28 + overlap * 0.62), 3)


def _novelty_score(text: str, examples: list[str]) -> float:
    if not examples:
        return 0.75
    words = set(re.findall(r"\w+", text.lower()))
    similarities = []
    for example in examples:
        other = set(re.findall(r"\w+", example.lower()))
        similarities.append(len(words & other) / max(1, len(words | other)))
    return round(max(0.05, 1.0 - max(similarities, default=0.0)), 3)


def _fatigue_score(text: str, recent_angles: list[str]) -> float:
    if not recent_angles:
        return 0.0
    words = set(re.findall(r"\w+", text.lower()))
    scores = []
    for angle in recent_angles:
        other = set(re.findall(r"\w+", angle.lower()))
        scores.append(len(words & other) / max(1, len(words | other)))
    return round(max(scores, default=0.0), 3)


def _strategic_role(action: str, objective: str) -> str:
    if action == "reply":
        return "add useful context to an existing conversation"
    if action == "quote":
        return "frame a distinct point of view for the account's audience"
    return f"build {objective} through an original, substantive post"


def _grok_allowed(context: PostContext, historic_rate: float) -> bool:
    text = context.text.lower()
    asks_analysis = any(token in text for token in ("why", "how", "explain", "fact check", "source", "data", "true"))
    assessment = assess_context(context)
    return asks_analysis and not assessment.blocked and assessment.research_allowed and not assessment.local_only and (historic_rate >= 0.01 or "@grok" in text)


def _record_safety(category: str, severity: str, blocked: bool, reasons: list[str], context: dict) -> None:
    # Pure intelligence helpers remain usable before FastAPI startup creates the durable schema.
    try:
        save_safety_event(category, severity, blocked, reasons, context)
    except sqlite3.OperationalError:
        pass
