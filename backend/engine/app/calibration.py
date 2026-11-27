"""Per-account calibration of the quality gates and scrubbers (V3, 2026-09-21).

WHY. Every quality gate in `intelligence._draft_quality_issues` was calibrated as
"fires on at most N of HIS 35 posts" — one author's writing. Run over other
customers' OWN published posts as if the model had written them, the same gates
flagged 17 of 25 posts of a terse writer (no_reader_payoff 14, names_nothing 12)
and 12 of 20 of a long-form one (loses_the_thread 6). A gate that fires on a
writer's own voice is not measuring quality for that writer; it is measuring
distance from somebody else, and every firing buys a retry, a rewrite toward the
other person's register, or a "Heads up" on a draft that sounds exactly like them.

WHAT THIS DOES. It runs the same gates over this account's own published posts,
once per corpus version, and any gate that fires on more than NOTE_ONLY_RATE of
them becomes a NOTE ONLY for this account: it is recorded on the draft's
rationale, but it no longer triggers a retry, a rewrite or a near-miss flag.
With fewer than MIN_OWN_POSTS on record the global calibration stands, because
a handful of posts is noise, not a habit.

WHAT IT NEVER TOUCHES. Integrity gates stay global for every account, whatever
its own posts look like: engagement bait, copying (its own past posts or the post
being answered), naming what the conversation never mentioned, getting a lab
wrong, and every hard gate in `_assemble_candidates` (invented figures, invented
experience, leaked instructions, unvouched links, injected names). Those are
about honesty, not voice.

The scrubbers get the same treatment through the account's measured habits:
clause dashes are not removed from a writer whose own posts use them, a leading
@handle is kept for a writer who addresses people, and a quote is not cut to one
point when this writer's quotes are longer than one sentence.
"""
from __future__ import annotations

import threading
import time
from typing import Any

MIN_OWN_POSTS = 10
NOTE_ONLY_RATE = 0.15
# Per surface when a surface has this many own posts; otherwise all surfaces pooled.
MIN_SURFACE_POSTS = 5
MAX_POSTS = 150

# Habit thresholds for the scrubbers (the style block's own bands).
EMDASH_HABIT_RATE = 0.12
MENTION_HABIT_RATE = 0.3
QUOTE_SENTENCES_HABIT = 1.0

# Gates a writer's own posts can say nothing about, because they judge a draft
# against the conversation, the evidence or the writer's own history rather than
# against how it is written. They keep the global calibration.
_CONTEXT_RELATIVE = frozenset({
    "ignores_the_post", "missing_context_connection", "restates_the_source",
    "bounced_question", "repeated_reply", "under_planned_size", "dangling_subject",
    "parrots_headline", "describes_the_source", "mangled_name", "talks_about_the_post",
})

_CACHE: dict[str, Any] = {"key": None, "value": None, "at": 0.0}
_LOCK = threading.Lock()
_TTL_S = 900.0
_COMPUTING = threading.local()


def _integrity() -> frozenset[str]:
    from .intelligence import _INTEGRITY_ISSUES
    return frozenset(_INTEGRITY_ISSUES) | {"engagement_bait", "misattributed_model",
                                           "copied_voice_example", "copies_the_post",
                                           "leaked_into_reply"}


def _surface(post_type: Any) -> str:
    kind = str(post_type or "post").lower()
    return "reply" if kind.startswith("repl") else ("quote" if kind.startswith("quot") else "post")


def _request_for(row: dict, surface: str):
    from .schemas import DraftRequest, PostContext
    if surface == "post":
        context = PostContext(author="You", text="", visibility="public")
    else:
        parent = str(row.get("parent_text") or "").strip()
        context = PostContext(author=str(row.get("parent_author") or "Someone"),
                              text=parent or "(the post they answered is not on record)",
                              visibility="public")
    return DraftRequest(action=surface, context=context, direction="", research_mode="off",
                        media_policy="no_media", grok_permission=False)


def _measure(rows: list[dict]) -> dict:
    """Run the quality gates over the account's own posts. Pure: no model, no network."""
    from .intelligence import _draft_quality_issues
    fired: dict[str, dict[str, int]] = {}
    counts: dict[str, int] = {}
    integrity = _integrity()
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        surface = _surface(row.get("post_type"))
        try:
            issues = set(_draft_quality_issues(text, _request_for(row, surface), []))
        except Exception:                                   # noqa: BLE001
            continue
        counts[surface] = counts.get(surface, 0) + 1
        for issue in issues - integrity - _CONTEXT_RELATIVE:
            fired.setdefault(issue, {}).setdefault(surface, 0)
            fired[issue][surface] += 1
    total = sum(counts.values())
    rates: dict[str, dict[str, float]] = {}
    for issue, by_surface in fired.items():
        pooled = sum(by_surface.values()) / total if total else 0.0
        rates[issue] = {"all": round(pooled, 3)}
        # Every surface with enough posts gets its own rate, zero included: a
        # gate that never fires on this writer's replies is calibrated for
        # replies, whatever it does on their posts.
        for surface, count in counts.items():
            if count >= MIN_SURFACE_POSTS:
                rates[issue][surface] = round(by_surface.get(surface, 0) / count, 3)
    return {"sample": total, "by_surface": counts, "rates": rates}


def _habits(rows: list[dict]) -> dict:
    from .account_model import infer_account_model
    from .text_normalizer import split_sentences
    style: dict = {}
    try:
        style = ((infer_account_model() or {}).get("voice_profile") or {}).get("style") or {}
    except Exception:                                       # noqa: BLE001
        style = {}
    quotes = [str(row.get("text") or "") for row in rows if _surface(row.get("post_type")) == "quote"]
    quote_sentences = (sum(len(split_sentences(text)) for text in quotes) / len(quotes)) if quotes else 0.0
    return {"emdash_rate": float(style.get("emdash_rate") or 0.0),
            "mention_rate": float(style.get("mention_rate") or 0.0),
            "quote_sentences": round(quote_sentences, 2), "quotes": len(quotes)}


def _own_rows() -> list[dict]:
    from .storage import records
    rows = [row for row in records()
            if not row.get("deleted_at") and row.get("text")
            and str(row.get("visibility") or "public") != "protected"]
    return rows[-MAX_POSTS:]


def calibration() -> dict:
    """This account's calibration, cached per corpus version (never raises)."""
    if getattr(_COMPUTING, "active", False):
        return {"sample": 0, "rates": {}, "habits": {}, "calibrated": False}
    try:
        from . import storage
        key = (storage.corpus_version(), str(storage.DB_PATH))
    except Exception:                                       # noqa: BLE001
        key = None
    now = time.monotonic()
    with _LOCK:
        if _CACHE["key"] == key and _CACHE["value"] is not None and now - _CACHE["at"] < _TTL_S:
            return _CACHE["value"]
    _COMPUTING.active = True
    try:
        rows = _own_rows()
        measured = _measure(rows) if len(rows) >= MIN_OWN_POSTS else {"sample": len(rows), "rates": {}}
        value = {**measured, "habits": _habits(rows), "calibrated": len(rows) >= MIN_OWN_POSTS}
    except Exception:                                       # noqa: BLE001
        value = {"sample": 0, "rates": {}, "habits": {}, "calibrated": False}
    finally:
        _COMPUTING.active = False
    with _LOCK:
        _CACHE.update(key=key, value=value, at=time.monotonic())
    return value


def reset() -> None:
    """Test hook."""
    with _LOCK:
        _CACHE.update(key=None, value=None, at=0.0)


def issue_rate(issue: str, action: str | None) -> float | None:
    """How often this gate fires on the account's own posts (None when uncalibrated)."""
    value = calibration()
    if not value.get("calibrated"):
        return None
    rates = (value.get("rates") or {}).get(issue)
    if not rates:
        return 0.0
    surface = _surface(action)
    return float(rates.get(surface, rates.get("all", 0.0)))


def note_only(issue: str, action: str | None) -> bool:
    """True when this gate fires on more than NOTE_ONLY_RATE of the writer's own posts."""
    if issue in _integrity() or issue in _CONTEXT_RELATIVE:
        return False
    rate = issue_rate(issue, action)
    return rate is not None and rate > NOTE_ONLY_RATE


def split_issues(issues: list[str], action: str | None) -> tuple[list[str], list[str]]:
    """(issues that still block or buy a retry, issues that are only notes for this account)."""
    blocking, notes = [], []
    for issue in issues:
        (notes if note_only(issue, action) else blocking).append(issue)
    return blocking, notes


def habit(name: str) -> float:
    return float((calibration().get("habits") or {}).get(name) or 0.0)


def keeps_dashes() -> bool:
    return habit("emdash_rate") >= EMDASH_HABIT_RATE


